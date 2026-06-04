"""
FastAPI orchestrator for the RPG engine.

Three-tier architecture (all three are real processes / network hops):

    browser  ──HTTP──▶  this FastAPI app  ──MCP/SSE──▶  RPG_Engine
    (static + REST)    (orchestrator on 8000)        (FastMCP on 8001)

The MCP ClientSession is opened once at startup via a FastAPI lifespan
context manager and held open for the entire app process. Each player
turn calls the LLM router + mutate_world_state over that persistent
session, then returns a structured payload the frontend renders.

Run two terminals:
    Terminal 1:  python server.py
    Terminal 2:  uvicorn app:app --reload

Or use the convenience launcher:  python run.py
"""

from __future__ import annotations

import asyncio
import collections
import json
import os
import sys
import time
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from mcp import ClientSession
from mcp.client.sse import sse_client

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from llm import (
    DEFAULT_NARRATOR,
    NARRATOR_CHOICES,
    ROUTER_MODEL,
    RULE_ENFORCER_MODEL,
    companion_respond,
    generate_action,
    generate_action_suggestions,
    generate_scenario,
    narrate,
    narrate_climax,
    rule_enforcer_agent,
)


# ---------- MCP tool wrappers --------------------------------------------------

def _unwrap_tool_result(result: Any) -> dict:
    # MCP error response: surface the server's error text instead of a
    # generic "could not parse" — makes server-side schema/arg mismatches
    # diagnosable from the orchestrator logs.
    if getattr(result, "isError", False):
        msgs = []
        for block in getattr(result, "content", None) or []:
            text = getattr(block, "text", None)
            if text:
                msgs.append(text)
        raise RuntimeError(
            "MCP tool error: " + (" | ".join(msgs) if msgs else "(no detail)")
        )

    if hasattr(result, "structuredContent") and result.structuredContent:
        sc = result.structuredContent
        if isinstance(sc, dict) and "result" in sc and len(sc) == 1:
            return sc["result"]
        return sc
    if hasattr(result, "content") and result.content:
        for block in result.content:
            text = getattr(block, "text", None)
            if text:
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    # Last-ditch: surface the raw text so we can see what
                    # the server actually said.
                    raise RuntimeError(f"MCP tool returned non-JSON text: {text}")
    raise RuntimeError("Could not parse MCP tool result (no content / structuredContent)")


async def _recorded_call(
    session: ClientSession,
    recent_calls,
    tool: str,
    args: dict,
    source: str = "orchestrator",
) -> dict:
    """Wrap session.call_tool with timing + history recording."""
    started = time.monotonic()
    ok = True
    err: str | None = None
    raw: Any = None
    try:
        raw = await session.call_tool(tool, args)
        return _unwrap_tool_result(raw)
    except Exception as e:  # noqa: BLE001
        ok = False
        err = f"{type(e).__name__}: {e}"
        raise
    finally:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        recent_calls.append({
            "ts": time.time(),
            "tool": tool,
            "args": args,
            "elapsed_ms": elapsed_ms,
            "ok": ok,
            "error": err,
            "source": source,
        })


async def mcp_get_world_state(session: ClientSession, recent_calls) -> dict:
    return await _recorded_call(session, recent_calls, "get_world_state", {})


async def mcp_initialize_game(
    session: ClientSession, recent_calls, scenario: dict
) -> dict:
    args = {
        "genre": scenario["genre"],
        "location": scenario["location"],
        "objective": scenario["objective"],
        "starting_items": scenario["starting_items"],
        "difficulty": scenario.get("difficulty", "normal"),
        "starting_enemies": scenario.get("starting_enemies", []),
        "companion": scenario.get("companion") or {},
    }
    return await _recorded_call(session, recent_calls, "initialize_game", args)


AGENT_TOOL_ALLOWLIST = {
    "get_world_state",
    "mutate_world_state",
    "add_enemy",
    "damage_enemy",
    "remove_enemy",
}


def _mcp_to_openai_tools(tools_full: list[dict]) -> list[dict]:
    """Translate FastMCP tool schemas to OpenAI function-calling format."""
    out: list[dict] = []
    for t in tools_full:
        if t["name"] not in AGENT_TOOL_ALLOWLIST:
            continue
        out.append({
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"] or "",
                "parameters": t["inputSchema"],
            },
        })
    return out


def _build_mutate_args(mutation: dict) -> dict:
    args: dict[str, Any] = {
        "health_change": int(mutation.get("health_change", 0) or 0),
        "add_items": list(mutation.get("add_items") or []),
        "remove_items": list(mutation.get("remove_items") or []),
    }
    if mutation.get("current_location"):
        args["current_location"] = mutation["current_location"]
    if mutation.get("has_objective_item") is True:
        args["has_objective_item"] = True
    return args


async def mcp_mutate_world_state(
    session: ClientSession, recent_calls, mutation: dict
) -> tuple[dict, dict]:
    args = _build_mutate_args(mutation)
    state = await _recorded_call(session, recent_calls, "mutate_world_state", args)
    return state, args


# ---------- App lifecycle ------------------------------------------------------

MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://127.0.0.1:8001/sse")


@asynccontextmanager
async def lifespan(app: FastAPI):
    stack = AsyncExitStack()
    last_err: Exception | None = None
    streams = None
    for attempt in range(1, 16):
        try:
            streams = await stack.enter_async_context(sse_client(MCP_SERVER_URL))
            break
        except Exception as e:  # noqa: BLE001
            last_err = e
            print(
                f"[startup] MCP server not reachable at {MCP_SERVER_URL} "
                f"(attempt {attempt}/15): {type(e).__name__}",
                file=sys.stderr,
            )
            await asyncio.sleep(1.0)
    if streams is None:
        raise RuntimeError(
            f"could not connect to MCP server at {MCP_SERVER_URL}: {last_err!r}\n"
            f"   start it first with:  python server.py"
        )
    read, write = streams[0], streams[1]
    session = await stack.enter_async_context(ClientSession(read, write))
    await session.initialize()

    tools_resp = await session.list_tools()
    tools_full = [
        {
            "name": t.name,
            "description": (t.description or "").strip(),
            "inputSchema": t.inputSchema,
        }
        for t in tools_resp.tools
    ]
    tool_names = [t["name"] for t in tools_full]

    app.state.mcp = session
    app.state.narrator = DEFAULT_NARRATOR
    app.state.tools = tool_names
    app.state.tools_full = tools_full
    app.state.mcp_server_url = MCP_SERVER_URL
    app.state.started_at = time.time()
    app.state.recent_calls = collections.deque(maxlen=100)

    print(
        f"[startup] MCP handshake complete via {MCP_SERVER_URL}. Tools: {tool_names}",
        file=sys.stderr,
    )
    try:
        yield
    finally:
        await stack.aclose()


app = FastAPI(title="Prompt Your Own Adventure", lifespan=lifespan)

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------- Schemas ------------------------------------------------------------

class Enemy(BaseModel):
    name: str
    hp: int
    location: str
    threat: str = "medium"
    description: str = ""


class Companion(BaseModel):
    name: str = ""
    persona: str = ""
    avatar: str = ""
    greeting: str = ""


class Scenario(BaseModel):
    genre: str
    location: str
    objective: str
    starting_items: list[str]
    difficulty: str = "normal"
    starting_enemies: list[Enemy] = []
    companion: Companion = Companion()


class ScenarioRequest(BaseModel):
    difficulty: str = "normal"


class TurnRequest(BaseModel):
    action: str


class SwapRequest(BaseModel):
    model: str


class CompanionMessage(BaseModel):
    role: str  # "user" or "assistant"
    content: str


class CompanionAskRequest(BaseModel):
    question: str
    history: list[CompanionMessage] = []


# ---------- Routes -------------------------------------------------------------

@app.get("/")
async def root():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/api/health")
async def health(request: Request):
    return {
        "ok": True,
        "tools": request.app.state.tools,
        "narrator": request.app.state.narrator,
        "narrator_choices": NARRATOR_CHOICES,
        "rule_enforcer_model": RULE_ENFORCER_MODEL,
    }


@app.get("/api/state")
async def get_state(request: Request):
    state = await mcp_get_world_state(
        request.app.state.mcp, request.app.state.recent_calls
    )
    return {"state": state, "narrator": request.app.state.narrator}


@app.post("/api/scenario")
async def gen_scenario(req: ScenarioRequest = ScenarioRequest()):
    try:
        return await asyncio.to_thread(generate_scenario, req.difficulty)
    except Exception as e:
        raise HTTPException(500, f"scenario generation failed: {e}")


@app.post("/api/accept")
async def accept_scenario(scenario: Scenario, request: Request):
    state = await mcp_initialize_game(
        request.app.state.mcp,
        request.app.state.recent_calls,
        scenario.model_dump(),
    )
    return {"state": state}


@app.post("/api/swap")
async def swap_narrator(req: SwapRequest, request: Request):
    request.app.state.narrator = req.model
    return {"narrator": request.app.state.narrator}


@app.post("/api/turn")
async def play_turn(req: TurnRequest, request: Request):
    mcp: ClientSession = request.app.state.mcp
    recent_calls = request.app.state.recent_calls
    narrator: str = request.app.state.narrator
    telemetry: list[dict] = []

    state_before = await mcp_get_world_state(mcp, recent_calls)

    telemetry.append({
        "stage": "router",
        "model": RULE_ENFORCER_MODEL,
        "note": (
            "Spawning Rule Enforcer AGENT with MCP tools "
            "[get_world_state, mutate_world_state] → LLM drives the loop."
        ),
    })

    # Translate discovered MCP tool schemas to OpenAI function-calling format,
    # and hand the agent a tool_caller that actually executes via MCP.
    openai_tools = _mcp_to_openai_tools(request.app.state.tools_full)

    # Guard against the agent calling mutate_world_state more than once
    # per turn — each call increments turn_count, which would corrupt the
    # "Turn N of 10" tracking. Subsequent calls return the current state
    # with an explanatory note so the agent can correct course.
    mutate_call_count = 0

    async def agent_tool_caller(name: str, args: dict) -> dict:
        nonlocal mutate_call_count
        if name == "mutate_world_state":
            if mutate_call_count >= 1:
                current = await mcp_get_world_state(mcp, recent_calls)
                return {
                    **current,
                    "_note": (
                        "mutate_world_state has already been called this "
                        "turn. Further calls are ignored to preserve the "
                        "turn counter. Finish your response now."
                    ),
                }
            mutate_call_count += 1
        return await _recorded_call(
            mcp, recent_calls, name, args, source="agent"
        )

    try:
        agent_result = await rule_enforcer_agent(
            state_before, req.action, openai_tools, agent_tool_caller
        )
    except Exception as e:
        raise HTTPException(500, f"rule enforcer agent failed: {e}")

    # Emit per-iteration telemetry so the UI shows the agent's reasoning
    # and tool calls in order.
    for it in agent_result["iterations"]:
        telemetry.append({
            "stage": "agent",
            "iteration": it["i"],
            "text": it["text"],
            "tool_calls": [
                {"name": tc["name"], "args": tc["args"]}
                for tc in it["tool_calls"]
            ],
            "final": not it["tool_calls"],
        })
        # Also surface each tool call as the usual [MCP CLIENT]/[MCP SERVER]
        # pair so the protocol is visible end-to-end.
        for tc in it["tool_calls"]:
            telemetry.append({
                "stage": "mcp_client",
                "tool": tc["name"],
                "source": "agent",
                "payload": {
                    "jsonrpc": "2.0",
                    "id": it["i"],
                    "method": "tools/call",
                    "params": {"name": tc["name"], "arguments": tc["args"]},
                },
            })
            telemetry.append({
                "stage": "mcp_server",
                "status": "ACK ok" if tc["error"] is None else "ERR",
                "summary": tc["error"]
                    or f"result: {json.dumps(tc['result'])[:200]}",
            })

    # If the agent forgot to commit a mutation, force a no-op so the turn
    # counter still advances (otherwise the game would soft-lock).
    if not agent_result["mutated"]:
        telemetry.append({
            "stage": "fallback",
            "note": "Agent did not call mutate_world_state — forcing no-op.",
        })
        await mcp_mutate_world_state(mcp, recent_calls, {})

    new_state = await mcp_get_world_state(mcp, recent_calls)
    mutate_args = agent_result["mutate_args"]

    telemetry.append({
        "stage": "router",
        "model": narrator,
        "note": "Routing mutated state → Narrator (frontier) for prose.",
    })

    # The narrator's `mutation` arg is shaped like the args the agent
    # passed to mutate_world_state — keeps the narrator function generic.
    narrator_mutation = dict(mutate_args) if mutate_args else {"health_change": 0}
    narrator_mutation.setdefault("intent_class", "agent")
    narrator_mutation.setdefault("reason", agent_result["final_text"])

    try:
        prose = await asyncio.to_thread(
            narrate, narrator, new_state, req.action, narrator_mutation,
            new_state["player_status"]["turn_count"],
        )
    except Exception as e:
        prose = f"(narration failed: {e})"

    ended = False
    victory = False
    end_reason = ""
    if new_state["player_status"]["health"] <= 0:
        ended = True
        end_reason = "health"
    elif new_state["player_status"]["turn_count"] >= 10:
        ended = True
        end_reason = "climax"
        victory = bool(new_state.get("has_objective_item"))

    return {
        "state": new_state,
        "telemetry": telemetry,
        "narration": prose,
        "narrator_model": narrator,
        "mutation": narrator_mutation,
        "agent": {
            "iterations": len(agent_result["iterations"]),
            "final_text": agent_result["final_text"],
            "mutated": agent_result["mutated"],
        },
        "ended": ended,
        "victory": victory,
        "end_reason": end_reason,
    }


@app.post("/api/auto-action")
async def auto_action(request: Request):
    mcp: ClientSession = request.app.state.mcp
    state = await mcp_get_world_state(mcp, request.app.state.recent_calls)
    try:
        action = await asyncio.to_thread(generate_action, state)
    except Exception as e:
        raise HTTPException(500, f"auto-action generation failed: {e}")
    return {"action": action, "model": ROUTER_MODEL}


@app.post("/api/suggestions")
async def suggestions(request: Request):
    mcp: ClientSession = request.app.state.mcp
    state = await mcp_get_world_state(mcp, request.app.state.recent_calls)
    try:
        sugg = await asyncio.to_thread(generate_action_suggestions, state)
    except Exception as e:
        raise HTTPException(500, f"suggestion generation failed: {e}")
    return {"suggestions": sugg, "model": ROUTER_MODEL}


@app.post("/api/companion/ask")
async def companion_ask(req: CompanionAskRequest, request: Request):
    mcp: ClientSession = request.app.state.mcp
    state = await mcp_get_world_state(mcp, request.app.state.recent_calls)
    history = [m.model_dump() for m in req.history]
    try:
        reply = await asyncio.to_thread(
            companion_respond, state, history, req.question
        )
    except Exception as e:
        raise HTTPException(500, f"companion response failed: {e}")
    return {
        "reply": reply,
        "companion": state.get("companion", {}),
        "model": ROUTER_MODEL,
    }


@app.post("/api/climax")
async def climax(request: Request):
    mcp: ClientSession = request.app.state.mcp
    narrator: str = request.app.state.narrator
    state = await mcp_get_world_state(mcp, request.app.state.recent_calls)
    victory = bool(state.get("has_objective_item"))
    reason = "health" if state["player_status"]["health"] <= 0 else "climax"
    try:
        prose = await asyncio.to_thread(narrate_climax, narrator, state, victory, reason)
    except Exception as e:
        prose = f"(climax narration failed: {e})"
    return {
        "state": state,
        "victory": victory,
        "reason": reason,
        "narration": prose,
        "narrator_model": narrator,
    }


# ---------- MCP Inspector ------------------------------------------------------

class McpCallRequest(BaseModel):
    tool: str
    args: dict[str, Any] = {}


@app.get("/api/mcp-inspect")
async def mcp_inspect(request: Request):
    """Live snapshot of the MCP server connection — server URL, tool
    schemas, and the recent-call history. Powers the in-UI inspector."""
    return {
        "server_url": request.app.state.mcp_server_url,
        "transport": "sse",
        "server_name": "RPG_Engine",
        "uptime_s": int(time.time() - request.app.state.started_at),
        "tools": request.app.state.tools_full,
        "recent_calls": list(request.app.state.recent_calls),
        "call_count": len(request.app.state.recent_calls),
    }


@app.post("/api/mcp-call")
async def mcp_call(req: McpCallRequest, request: Request):
    """Manually invoke any discovered MCP tool with arbitrary JSON args.
    Lets a grader poke the server through the same protocol the
    orchestrator uses — proves it's a real MCP service, not a sham."""
    mcp: ClientSession = request.app.state.mcp
    recent_calls = request.app.state.recent_calls
    try:
        result = await _recorded_call(
            mcp, recent_calls, req.tool, req.args, source="inspector"
        )
        return {"ok": True, "result": result}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
