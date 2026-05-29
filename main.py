"""
Universal Random RPG Engine — Agentic Client.

Connects to the standalone MCP server (server.py) over stdio, discovers
the world-state tools, and runs a 10-turn terminal game loop with explicit
Model Routing between a fast Rule Enforcer LLM and a frontier Narrator LLM.

Required env vars:  OPENAI_API_KEY, ANTHROPIC_API_KEY
Run:                python main.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

try:
    from openai import OpenAI
except ImportError:
    print("Missing dependency: pip install openai", file=sys.stderr)
    raise

try:
    from anthropic import Anthropic
except ImportError:
    print("Missing dependency: pip install anthropic", file=sys.stderr)
    raise


# ---------- Terminal styling --------------------------------------------------

class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    CYAN = "\033[96m"
    YELLOW = "\033[93m"
    GREEN = "\033[92m"
    RED = "\033[91m"
    MAGENTA = "\033[95m"
    BLUE = "\033[94m"
    GRAY = "\033[90m"
    WHITE = "\033[97m"


def _enable_windows_ansi() -> None:
    if os.name == "nt":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
        except Exception:
            pass


# ---------- Model configuration ----------------------------------------------

ROUTER_MODEL = "gpt-4o-mini"
RULE_ENFORCER_MODEL = "gpt-4o-mini"
DEFAULT_NARRATOR = "claude-sonnet-4-6"

ANTHROPIC_MODELS = {
    "claude-sonnet-4-6",
    "claude-opus-4-7",
    "claude-haiku-4-5-20251001",
    "claude-haiku-4-5",
    "claude-sonnet-4-5",
    "claude-3-5-sonnet-latest",
    "claude-3-5-sonnet-20241022",
    "claude-3-5-haiku-latest",
}
OPENAI_MODELS = {
    "gpt-4o",
    "gpt-4o-mini",
    "gpt-4.1",
    "gpt-4.1-mini",
    "gpt-4-turbo",
}

openai_client = OpenAI() if os.getenv("OPENAI_API_KEY") else None
anthropic_client = Anthropic() if os.getenv("ANTHROPIC_API_KEY") else None


# ---------- Telemetry pretty-printer -----------------------------------------

def banner() -> None:
    print(f"{C.BOLD}{C.CYAN}")
    print("=" * 76)
    print("  UNIVERSAL RANDOM RPG ENGINE  ::  MCP DEMONSTRATION".center(76))
    print("       Model-Routed Storytelling  +  FastMCP Tool Calling".center(76))
    print("=" * 76)
    print(f"{C.RESET}")


def telemetry_open(turn: int) -> None:
    print(
        f"\n{C.YELLOW}{C.BOLD}--------- "
        f"⚙️  TELEMETRY LOGS  ::  TURN {turn:02d}  "
        f"---------{C.RESET}"
    )


def telemetry_close() -> None:
    print(f"{C.YELLOW}{C.BOLD}{'-' * 56}{C.RESET}\n")


def log_router(active_model: str, intent_class: str, reason: str) -> None:
    print(
        f"{C.MAGENTA}[MODEL ROUTER]{C.RESET}  active={C.BOLD}{active_model}{C.RESET}  "
        f"intent={C.BOLD}{intent_class}{C.RESET}\n"
        f"               {C.GRAY}{reason}{C.RESET}"
    )


def log_mcp_client(tool_name: str, params: dict) -> None:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": params},
    }
    print(f"{C.BLUE}[MCP CLIENT ]{C.RESET}  → server  tool={C.BOLD}{tool_name}{C.RESET}")
    payload_str = json.dumps(payload, separators=(",", ": "))
    print(f"               {C.GRAY}{payload_str}{C.RESET}")


def log_mcp_server(ok: bool, summary: str) -> None:
    status_color = C.GREEN if ok else C.RED
    status = "ACK ok" if ok else "ERR"
    print(f"{C.BLUE}[MCP SERVER ]{C.RESET}  ← {status_color}{status}{C.RESET}  {C.GRAY}{summary}{C.RESET}")


def log_narrator(model: str) -> None:
    print(
        f"{C.MAGENTA}[MODEL ROUTER]{C.RESET}  routing → narrator "
        f"model={C.BOLD}{model}{C.RESET} {C.GRAY}(frontier; high-fidelity prose){C.RESET}"
    )


# ---------- LLM helpers -------------------------------------------------------

def _strip_json_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def _ensure_openai() -> OpenAI:
    if openai_client is None:
        raise RuntimeError("OPENAI_API_KEY not set in environment.")
    return openai_client


def _ensure_anthropic() -> Anthropic:
    if anthropic_client is None:
        raise RuntimeError("ANTHROPIC_API_KEY not set in environment.")
    return anthropic_client


def generate_scenario() -> dict:
    """Use the cheap router model to invent a brand-new random scenario."""
    client = _ensure_openai()
    system = (
        "You are a high-variance random scenario seed generator for a "
        "text adventure engine. Each call must produce a wildly different "
        "genre — pull from horror, cyberpunk, high fantasy, space opera, "
        "noir detective, post-apocalyptic, mythological, samurai, weird "
        "west, undersea, dieselpunk, cosmic horror, pirate, etc. Be "
        "imaginative, specific, and evocative."
    )
    user = (
        "Invent ONE random scenario. Respond with JSON ONLY (no markdown) "
        "having exactly these keys:\n"
        '  "genre": short evocative genre label (under 6 words)\n'
        '  "location": vivid starting location (one sentence)\n'
        '  "objective": main quest in one imperative sentence\n'
        '  "starting_items": exactly 3 thematic items as strings\n'
    )
    resp = client.chat.completions.create(
        model=ROUTER_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
        temperature=1.1,
    )
    data = json.loads(resp.choices[0].message.content)
    items = data.get("starting_items", [])
    if len(items) != 3:
        items = (items + ["a worn satchel", "a half-burned letter", "a curious trinket"])[:3]
        data["starting_items"] = items
    return data


def rule_enforcer(state: dict, action: str) -> dict:
    """Use the fast model to mechanically resolve the player's action."""
    client = _ensure_openai()
    system = (
        "You are the Rule Enforcer for a turn-based text RPG. You evaluate "
        "the mechanical, physical outcome of a player's action against the "
        "given world state. You are NOT a storyteller — you produce only "
        "structured state-change data. Be fair but consequential: actions "
        "have weight. Damage from danger is typically 5–25. Healing is rare. "
        "If the player attempts something the world clearly enables them to "
        "find or acquire the objective item, set has_objective_item=true."
    )
    user = (
        f"Current world state:\n{json.dumps(state, indent=2)}\n\n"
        f'Player action: "{action}"\n\n'
        "Respond with JSON ONLY containing exactly these keys:\n"
        '  "health_change": signed int (negative=damage, positive=heal, 0=neutral)\n'
        '  "add_items": list[str] (items acquired this turn; [] if none)\n'
        '  "remove_items": list[str] (items consumed/lost; [] if none)\n'
        '  "current_location": string OR null (new location if moved, else null)\n'
        '  "has_objective_item": true OR null (true if just acquired; null otherwise — never false)\n'
        '  "intent_class": one short label like "movement", "combat", "interact", "inventory", "social", "stealth"\n'
        '  "reason": one-sentence mechanical justification\n'
    )
    resp = client.chat.completions.create(
        model=RULE_ENFORCER_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
        temperature=0.4,
    )
    return json.loads(resp.choices[0].message.content)


def narrate(model: str, state: dict, action: str, mutation: dict, turn: int) -> str:
    """Route to a frontier model for vivid prose."""
    system = (
        f"You are the Creative Narrator of a {state['genre']} text adventure. "
        "Your voice is vivid, atmospheric, in-genre, and tight. Show consequence "
        "through sensory detail rather than listing stats. Never break the fourth "
        "wall. Never mention 'turn', 'health points', or game mechanics. 2–4 short "
        "paragraphs. End on a sensory beat that invites the next action."
    )
    user = (
        f"Player just attempted: \"{action}\"\n\n"
        f"Mechanical resolution from the Rule Enforcer:\n{json.dumps(mutation, indent=2)}\n\n"
        f"Updated world state:\n{json.dumps(state, indent=2)}\n\n"
        f"Turn {turn} of 10. Write the narration now."
    )

    if model in ANTHROPIC_MODELS or model.startswith("claude"):
        client = _ensure_anthropic()
        resp = client.messages.create(
            model=model,
            max_tokens=600,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(
            block.text for block in resp.content if getattr(block, "type", "") == "text"
        ).strip()

    if model in OPENAI_MODELS or model.startswith("gpt"):
        client = _ensure_openai()
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.9,
        )
        return resp.choices[0].message.content.strip()

    raise ValueError(f"Unknown narrator model: {model!r}")


# ---------- MCP tool wrappers -------------------------------------------------

def _unwrap_tool_result(result: Any) -> dict:
    """Extract the JSON payload from an MCP CallToolResult."""
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
    log_mcp_client("get_world_state", {})
    result = await session.call_tool("get_world_state", {})
    state = _unwrap_tool_result(result)
    log_mcp_server(True, f"state read (turn={state['player_status']['turn_count']}, hp={state['player_status']['health']})")
    return state


async def mcp_initialize_game(session: ClientSession, scenario: dict) -> dict:
    args = {
        "genre": scenario["genre"],
        "location": scenario["location"],
        "objective": scenario["objective"],
        "starting_items": scenario["starting_items"],
    }
    log_mcp_client("initialize_game", args)
    result = await session.call_tool("initialize_game", args)
    state = _unwrap_tool_result(result)
    log_mcp_server(True, "world initialized; world_state.json flushed to disk")
    return state


async def mcp_mutate_world_state(session: ClientSession, mutation: dict) -> dict:
    args = {
        "health_change": int(mutation.get("health_change", 0) or 0),
        "add_items": list(mutation.get("add_items") or []),
        "remove_items": list(mutation.get("remove_items") or []),
    }
    if mutation.get("current_location"):
        args["current_location"] = mutation["current_location"]
    if mutation.get("has_objective_item") is True:
        args["has_objective_item"] = True

    log_mcp_client("mutate_world_state", args)
    result = await session.call_tool("mutate_world_state", args)
    state = _unwrap_tool_result(result)
    log_mcp_server(
        True,
        f"committed Δhp={args['health_change']:+d}, "
        f"+items={args['add_items']}, -items={args['remove_items']}, "
        f"loc={'yes' if 'current_location' in args else 'unchanged'}, "
        f"objective_item={state['has_objective_item']}",
    )
    return state


# ---------- Game phases -------------------------------------------------------

async def phase_setup(session: ClientSession) -> dict:
    """Phase 1: regenerate scenarios until the player accepts one."""
    print(f"{C.CYAN}{C.BOLD}Phase 1 — Scenario Generation Setup{C.RESET}\n")
    while True:
        print(f"{C.GRAY}[router → {ROUTER_MODEL}] rolling new scenario...{C.RESET}")
        try:
            scenario = generate_scenario()
        except Exception as e:
            print(f"{C.RED}Scenario generation failed: {e}{C.RESET}")
            sys.exit(1)

        print(f"\n{C.YELLOW}{'═' * 70}{C.RESET}")
        print(f"  {C.BOLD}Genre:{C.RESET}       {scenario['genre']}")
        print(f"  {C.BOLD}Location:{C.RESET}    {scenario['location']}")
        print(f"  {C.BOLD}Objective:{C.RESET}   {scenario['objective']}")
        print(f"  {C.BOLD}Starting Items:{C.RESET}")
        for it in scenario["starting_items"]:
            print(f"     · {it}")
        print(f"{C.YELLOW}{'═' * 70}{C.RESET}\n")

        choice = input(f"{C.BOLD}Accept and Play this setting? (y/n): {C.RESET}").strip().lower()
        if choice == "y":
            print(f"\n{C.GREEN}Sealing universe...{C.RESET}")
            state = await mcp_initialize_game(session, scenario)
            return state
        print(f"{C.GRAY}Rerolling...{C.RESET}\n")


def parse_swap(action: str) -> str | None:
    parts = action.strip().split(maxsplit=1)
    if len(parts) >= 1 and parts[0].lower() == "/swap":
        if len(parts) == 2 and parts[1].strip():
            return parts[1].strip()
    return None


async def phase_main_loop(session: ClientSession, state: dict) -> None:
    print(f"\n{C.CYAN}{C.BOLD}Phase 2 — Live 10-Turn Adventure{C.RESET}")
    print(
        f"{C.GRAY}Hidden cheat: type {C.BOLD}/swap <model_name>{C.RESET}{C.GRAY} "
        f"to retarget the Narrator LLM on the fly.{C.RESET}\n"
    )
    narrator_model = DEFAULT_NARRATOR
    print(
        f"{C.CYAN}You stand in:{C.RESET} {state['current_location']}\n"
        f"{C.CYAN}Your goal:{C.RESET}    {state['objective']}\n"
        f"{C.CYAN}Inventory:{C.RESET}    {', '.join(state['inventory']) or '(empty)'}\n"
    )

    turn = 1
    while turn <= 10:
        print(
            f"{C.BOLD}{C.WHITE}── Turn {turn}/10 "
            f"│ ❤ {state['player_status']['health']} "
            f"│ 📍 {state['current_location']}{C.RESET}"
        )
        raw_action = input(f"{C.BOLD}> {C.RESET}").strip()
        if not raw_action:
            print(f"{C.GRAY}(silence is also a choice — but pick a real action){C.RESET}\n")
            continue

        swap_target = parse_swap(raw_action)
        if swap_target is not None:
            old = narrator_model
            narrator_model = swap_target
            print(
                f"{C.MAGENTA}[MODEL ROUTER]{C.RESET}  narrator swapped  "
                f"{C.GRAY}{old}{C.RESET} → {C.BOLD}{narrator_model}{C.RESET}\n"
            )
            continue

        telemetry_open(turn)

        try:
            mutation = rule_enforcer(state, raw_action)
        except Exception as e:
            print(f"{C.RED}Rule Enforcer failed: {e}{C.RESET}")
            telemetry_close()
            continue

        log_router(
            RULE_ENFORCER_MODEL,
            mutation.get("intent_class", "unknown"),
            mutation.get("reason", "structural mutation required → routing to MCP."),
        )

        try:
            state = await mcp_mutate_world_state(session, mutation)
        except Exception as e:
            print(f"{C.RED}MCP mutate failed: {e}{C.RESET}")
            telemetry_close()
            continue

        log_narrator(narrator_model)
        telemetry_close()

        try:
            prose = narrate(narrator_model, state, raw_action, mutation, turn)
        except Exception as e:
            print(f"{C.RED}Narrator failed: {e}{C.RESET}\n")
            prose = f"(the world holds its breath as the moment slips by — {mutation.get('reason', '')})"

        print(f"{C.WHITE}{prose}{C.RESET}\n")

        if state["player_status"]["health"] <= 0:
            await ending(session, state, narrator_model, victory=False, reason="health")
            return

        turn += 1

    await ending(
        session,
        state,
        narrator_model,
        victory=bool(state.get("has_objective_item")),
        reason="climax",
    )


async def ending(
    session: ClientSession,
    state: dict,
    narrator_model: str,
    *,
    victory: bool,
    reason: str,
) -> None:
    print(f"\n{C.CYAN}{C.BOLD}── Climax ──{C.RESET}\n")
    if victory:
        prompt_action = "Deliver the closing victory scene as the player completes the objective."
    elif reason == "health":
        prompt_action = "Deliver a thematic death scene as the player's wounds overtake them."
    else:
        prompt_action = (
            "Deliver a thematic failure scene as time runs out on the player's quest "
            "without the objective item recovered."
        )

    fake_mutation = {
        "health_change": 0,
        "add_items": [],
        "remove_items": [],
        "current_location": None,
        "has_objective_item": state.get("has_objective_item"),
        "intent_class": "climax",
        "reason": prompt_action,
    }
    try:
        prose = narrate(narrator_model, state, prompt_action, fake_mutation, 10)
    except Exception as e:
        prose = f"(climax narration failed: {e})"

    headline = (
        f"{C.GREEN}{C.BOLD}*** VICTORY ***{C.RESET}"
        if victory
        else f"{C.RED}{C.BOLD}*** GAME OVER ***{C.RESET}"
    )
    print(headline)
    print(f"{C.WHITE}{prose}{C.RESET}\n")
    print(
        f"{C.GRAY}Final state:  hp={state['player_status']['health']}  "
        f"turns={state['player_status']['turn_count']}  "
        f"objective_item={state.get('has_objective_item')}  "
        f"inventory={state['inventory']}{C.RESET}"
    )


# ---------- Entrypoint --------------------------------------------------------

async def run() -> None:
    _enable_windows_ansi()
    banner()

    if openai_client is None:
        print(f"{C.RED}OPENAI_API_KEY not set — required for router + rule enforcer.{C.RESET}")
        sys.exit(2)
    if anthropic_client is None:
        print(
            f"{C.YELLOW}ANTHROPIC_API_KEY not set — default narrator unavailable. "
            f"You can still /swap to an OpenAI narrator at runtime.{C.RESET}"
        )

    server_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.py")
    server_params = StdioServerParameters(
        command=sys.executable,
        args=[server_script],
        env=os.environ.copy(),
    )

    async with AsyncExitStack() as stack:
        read, write = await stack.enter_async_context(stdio_client(server_params))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()

        tools = await session.list_tools()
        print(f"{C.GRAY}[MCP CLIENT ] handshake complete. Discovered tools:{C.RESET}")
        for t in tools.tools:
            print(f"{C.GRAY}              · {t.name}{C.RESET}")
        print()

        state = await phase_setup(session)
        await phase_main_loop(session, state)


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print(f"\n{C.GRAY}interrupted.{C.RESET}")


if __name__ == "__main__":
    main()
