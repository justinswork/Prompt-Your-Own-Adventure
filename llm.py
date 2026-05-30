"""
LLM router + model-routed game logic.

Pure functions — no I/O beyond the OpenAI / Anthropic SDK calls. The
FastAPI orchestrator (app.py) handles transport and MCP, this module
just turns prompts into structured outputs.

Three callable surfaces:
    generate_scenario()                       → random scenario dict
    rule_enforcer(state, action)              → structured mutation dict
    narrate(model, state, action, mutation,
            turn)                             → narrative prose string
"""

from __future__ import annotations

import json
import os

from openai import OpenAI
from anthropic import Anthropic


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

NARRATOR_CHOICES = [
    "claude-sonnet-4-6",
    "claude-opus-4-7",
    "claude-haiku-4-5",
    "gpt-4o",
    "gpt-4o-mini",
    "gpt-4.1",
]


_openai_client: OpenAI | None = OpenAI() if os.getenv("OPENAI_API_KEY") else None
_anthropic_client: Anthropic | None = Anthropic() if os.getenv("ANTHROPIC_API_KEY") else None


def _ensure_openai() -> OpenAI:
    if _openai_client is None:
        raise RuntimeError("OPENAI_API_KEY not set in environment.")
    return _openai_client


def _ensure_anthropic() -> Anthropic:
    if _anthropic_client is None:
        raise RuntimeError("ANTHROPIC_API_KEY not set in environment.")
    return _anthropic_client


# ---------- LLM calls ---------------------------------------------------------

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


def generate_action(state: dict) -> str:
    """Use the router model to pick a plausible player action for this state.

    Used by the "Choose for me" / auto-play feature so the system can be
    watched end-to-end without manual input.
    """
    client = _ensure_openai()
    system = (
        f"You are playing the protagonist of a {state['genre']} text RPG. "
        "Choose ONE concrete, in-character action to attempt this turn. "
        "Keep it to 1-2 short sentences, first person or imperative. Vary "
        "your approach across turns — investigate, move, interact, take "
        "risks, pursue the objective, react to threats. Describe what you "
        "ATTEMPT, never dictate outcomes (wrong: 'I find the key'; right: "
        "'I search behind the tapestry for the key'). Stay grounded in the "
        "current location and inventory."
    )
    user = (
        f"Current world state:\n{json.dumps(state, indent=2)}\n\n"
        "Respond with JSON ONLY: {\"action\": \"your action here\"}"
    )
    resp = client.chat.completions.create(
        model=ROUTER_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
        temperature=1.0,
    )
    data = json.loads(resp.choices[0].message.content)
    action = (data.get("action") or "").strip()
    if not action:
        action = "I take a moment to study my surroundings."
    if len(action) > 300:
        action = action[:300]
    return action


async def rule_enforcer_agent(
    state: dict,
    action: str,
    tool_schemas: list[dict],
    tool_caller,
    max_iterations: int = 5,
) -> dict:
    """The Rule Enforcer as an actual agent.

    Hands the MCP tool schemas to OpenAI's function-calling API and runs
    a tool-use loop. The model autonomously decides which MCP tools to
    call (e.g. get_world_state to recheck context, mutate_world_state to
    commit the outcome) and with what arguments. Loops until the model
    stops emitting tool_calls or max_iterations is hit.

    This is the canonical "agentic + tools + MCP" path: the LLM, not the
    orchestrator, drives the protocol.

    Args:
        state:        current world state (also passed in the user msg)
        action:       the player's submitted action
        tool_schemas: list of tools in OpenAI function-calling format
        tool_caller:  async callable (name, args) -> dict that actually
                      executes the tool over MCP. Supplied by the
                      orchestrator so this module stays MCP-agnostic.
        max_iterations: safety cap on the tool-use loop.

    Returns:
        dict with keys:
            iterations:   list of per-step records (text + tool_calls
                          + tool_results)
            final_text:   the model's final natural-language summary
            mutate_args:  the args passed to mutate_world_state (or {}
                          if the agent never called it — caller should
                          force a no-op fallback in that case)
            mutated:      bool, whether mutate_world_state was called
    """
    client = _ensure_openai()
    system = (
        "You are the Rule Enforcer agent for a turn-based text RPG.\n\n"
        "You have access to MCP tools that read and mutate the live "
        "game world over the wire. Resolve the player's action by "
        "CALLING those tools — do not just describe outcomes.\n\n"
        "Each turn you MUST:\n"
        "  1. Decide the mechanical outcome of the player's action.\n"
        "  2. Call mutate_world_state EXACTLY ONCE to commit the "
        "     outcome (health_change, add_items, remove_items, "
        "     current_location, has_objective_item).\n"
        "  3. After that tool call returns, respond with ONE plain "
        "     sentence justifying your ruling — no further tool call.\n\n"
        "You MAY call get_world_state first if you need to verify "
        "current state, but state is already given to you below. Be "
        "fair but consequential: damage from danger is typically 5-25, "
        "healing is rare. If the player's action plausibly recovers "
        "the objective item, set has_objective_item=true on the "
        "mutation. Never set it back to false."
    )
    user_msg = (
        f"Current world state:\n{json.dumps(state, indent=2)}\n\n"
        f'Player action: "{action}"\n\n'
        "Resolve this turn by invoking the appropriate MCP tool(s)."
    )

    messages: list[dict] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_msg},
    ]

    iterations: list[dict] = []
    mutate_args: dict = {}
    mutated = False

    for i in range(1, max_iterations + 1):
        resp = client.chat.completions.create(
            model=RULE_ENFORCER_MODEL,
            messages=messages,
            tools=tool_schemas,
            tool_choice="auto",
            temperature=0.4,
        )
        msg = resp.choices[0].message

        assistant_record: dict = {
            "role": "assistant",
            "content": msg.content or "",
        }
        if msg.tool_calls:
            assistant_record["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in msg.tool_calls
            ]
        messages.append(assistant_record)

        iter_record: dict = {
            "i": i,
            "text": msg.content or "",
            "tool_calls": [],
        }

        if not msg.tool_calls:
            iterations.append(iter_record)
            break

        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}

            try:
                result = await tool_caller(name, args)
                error: str | None = None
            except Exception as e:  # noqa: BLE001
                result = None
                error = f"{type(e).__name__}: {e}"

            iter_record["tool_calls"].append({
                "id": tc.id,
                "name": name,
                "args": args,
                "result": result,
                "error": error,
            })

            if name == "mutate_world_state":
                mutated = True
                mutate_args = args

            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(
                    result if error is None else {"error": error}
                ),
            })

        iterations.append(iter_record)

    final_text = ""
    for it in reversed(iterations):
        if it["text"]:
            final_text = it["text"]
            break

    return {
        "iterations": iterations,
        "final_text": final_text,
        "mutate_args": mutate_args,
        "mutated": mutated,
    }


def narrate_climax(model: str, state: dict, victory: bool, reason: str) -> str:
    """Generate the final scene at the end of the 10-turn run."""
    if victory:
        action = "Deliver the closing victory scene as the player completes the objective."
    elif reason == "health":
        action = "Deliver a thematic death scene as the player's wounds overtake them."
    else:
        action = (
            "Deliver a thematic failure scene as time runs out on the quest "
            "without the objective item recovered."
        )
    fake_mutation = {
        "health_change": 0,
        "add_items": [],
        "remove_items": [],
        "current_location": None,
        "has_objective_item": state.get("has_objective_item"),
        "intent_class": "climax",
        "reason": action,
    }
    return narrate(model, state, action, fake_mutation, state["player_status"]["turn_count"])
