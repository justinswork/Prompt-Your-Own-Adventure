"""
FastAPI orchestrator for the RPG engine.

Three-tier architecture:
    browser  ←→  this FastAPI app  ←→  MCP server (server.py)

The MCP ClientSession is opened once at startup via a FastAPI lifespan
context manager and held open for the entire app process. Each player
turn calls the LLM router + mutate_world_state over that persistent
session, then returns a structured payload the frontend renders.

Run:  uvicorn app:app --reload
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

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
    generate_action,
    generate_scenario,
    narrate,
    narrate_climax,
    rule_enforcer,
)


# ---------- MCP tool wrappers --------------------------------------------------

def _unwrap_tool_result(result: Any) -> dict:
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
                    continue
    raise RuntimeError("Could not parse MCP tool result")


async def mcp_get_world_state(session: ClientSession) -> dict:
    result = await session.call_tool("get_world_state", {})
    return _unwrap_tool_result(result)


async def mcp_initialize_game(session: ClientSession, scenario: dict) -> dict:
    args = {
        "genre": scenario["genre"],
        "location": scenario["location"],
        "objective": scenario["objective"],
        "starting_items": scenario["starting_items"],
    }
    result = await session.call_tool("initialize_game", args)
    return _unwrap_tool_result(result)


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
    session: ClientSession, mutation: dict
) -> tuple[dict, dict]:
    args = _build_mutate_args(mutation)
    result = await session.call_tool("mutate_world_state", args)
    return _unwrap_tool_result(result), args


# ---------- App lifecycle ------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    server_script = str(Path(__file__).parent / "server.py")
    server_params = StdioServerParameters(
        command=sys.executable,
        args=[server_script],
        env=os.environ.copy(),
    )
    stack = AsyncExitStack()
    read, write = await stack.enter_async_context(stdio_client(server_params))
    session = await stack.enter_async_context(ClientSession(read, write))
    await session.initialize()

    tools_resp = await session.list_tools()
    tool_names = [t.name for t in tools_resp.tools]

    app.state.mcp = session
    app.state.narrator = DEFAULT_NARRATOR
    app.state.tools = tool_names

    print(f"[startup] MCP handshake complete. Tools: {tool_names}", file=sys.stderr)
    try:
        yield
    finally:
        await stack.aclose()


app = FastAPI(title="Prompt Your Own Adventure", lifespan=lifespan)

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------- Schemas ------------------------------------------------------------

class Scenario(BaseModel):
    genre: str
    location: str
    objective: str
    starting_items: list[str]


class TurnRequest(BaseModel):
    action: str


class SwapRequest(BaseModel):
    model: str


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
    state = await mcp_get_world_state(request.app.state.mcp)
    return {"state": state, "narrator": request.app.state.narrator}


@app.post("/api/scenario")
async def gen_scenario():
    try:
        return await asyncio.to_thread(generate_scenario)
    except Exception as e:
        raise HTTPException(500, f"scenario generation failed: {e}")


@app.post("/api/accept")
async def accept_scenario(scenario: Scenario, request: Request):
    state = await mcp_initialize_game(request.app.state.mcp, scenario.model_dump())
    return {"state": state}


@app.post("/api/swap")
async def swap_narrator(req: SwapRequest, request: Request):
    request.app.state.narrator = req.model
    return {"narrator": request.app.state.narrator}


@app.post("/api/turn")
async def play_turn(req: TurnRequest, request: Request):
    mcp: ClientSession = request.app.state.mcp
    narrator: str = request.app.state.narrator
    telemetry: list[dict] = []

    state_before = await mcp_get_world_state(mcp)

    telemetry.append({
        "stage": "router",
        "model": RULE_ENFORCER_MODEL,
        "note": "Player intent requires structural mutation → routing to Rule Enforcer.",
    })

    try:
        mutation = await asyncio.to_thread(rule_enforcer, state_before, req.action)
    except Exception as e:
        raise HTTPException(500, f"rule enforcer failed: {e}")

    telemetry.append({
        "stage": "router",
        "model": RULE_ENFORCER_MODEL,
        "intent": mutation.get("intent_class", "unknown"),
        "reason": mutation.get("reason", ""),
    })

    mutate_args = _build_mutate_args(mutation)
    telemetry.append({
        "stage": "mcp_client",
        "tool": "mutate_world_state",
        "payload": {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "mutate_world_state", "arguments": mutate_args},
        },
    })

    new_state, _ = await mcp_mutate_world_state(mcp, mutation)

    telemetry.append({
        "stage": "mcp_server",
        "status": "ACK ok",
        "summary": (
            f"committed Δhp={mutate_args['health_change']:+d} "
            f"+items={mutate_args['add_items']} "
            f"-items={mutate_args['remove_items']} "
            f"objective_item={new_state['has_objective_item']}"
        ),
    })

    telemetry.append({
        "stage": "router",
        "model": narrator,
        "note": "Routing mutated state → Narrator (frontier) for prose.",
    })

    try:
        prose = await asyncio.to_thread(
            narrate, narrator, new_state, req.action, mutation,
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
        "mutation": mutation,
        "ended": ended,
        "victory": victory,
        "end_reason": end_reason,
    }


@app.post("/api/auto-action")
async def auto_action(request: Request):
    mcp: ClientSession = request.app.state.mcp
    state = await mcp_get_world_state(mcp)
    try:
        action = await asyncio.to_thread(generate_action, state)
    except Exception as e:
        raise HTTPException(500, f"auto-action generation failed: {e}")
    return {"action": action, "model": ROUTER_MODEL}


@app.post("/api/climax")
async def climax(request: Request):
    mcp: ClientSession = request.app.state.mcp
    narrator: str = request.app.state.narrator
    state = await mcp_get_world_state(mcp)
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
