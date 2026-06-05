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


# Game-world LLMs (Rule Enforcer, Narrator, action generators) must NOT
# see the companion field. The companion is a meta-layer dialogue partner
# the player talks to outside the narrative — they are not a character
# in the game world. Pass state through this helper before serializing.
_COMPANION_META_NOTE = (
    "NOTE: The companion (the player's guide) is a META-LAYER HELPER, "
    "NOT a character in the game world. They speak to the player "
    "outside the narrative, like a voice in their head. NEVER reference "
    "the companion in your reasoning, narration, or tool calls. As far "
    "as the game's reality is concerned, the companion does not exist."
)


def _state_for_game(state: dict) -> dict:
    return {k: v for k, v in (state or {}).items() if k != "companion"}


# ---------- LLM calls ---------------------------------------------------------

DIFFICULTY_PROFILES = {
    "easy": {
        "starting_enemies": (0, 1),
        "enemy_hp": (6, 14),
        "tone": "light",
        "max_turns": 10,
        "min_objective_turn": 3,
        "scope": "small and local — the objective item is in or right next to the starting location, reachable within 3-5 turns of focused play",
        "objective_bias": (
            "LENIENT but not instant. Grant has_objective_item=true on "
            "the next genuinely plausible attempt AFTER the player has "
            "had at least a couple of turns to act. If by turn 6 they "
            "still haven't acquired it, lean strongly toward granting "
            "it on any reasonable progress-related action. Easy-mode "
            "players should reliably win in their turn budget."
        ),
    },
    "normal": {
        "starting_enemies": (1, 2),
        "enemy_hp": (12, 22),
        "tone": "tense",
        "max_turns": 10,
        "min_objective_turn": 4,
        "scope": "modest — the objective is achievable by exploring 1-2 nearby areas after dealing with at least one obstacle or enemy",
        "objective_bias": (
            "REWARD MEANINGFUL PROGRESS. Don't grant the objective on the "
            "first declaration of intent — make the player work for it. "
            "Set has_objective_item=true once they've made a deliberate "
            "move toward the goal AND dealt with at least one obstacle, "
            "enemy, or puzzle step. Normal difficulty should be winnable "
            "by mid-game for a player who's paying attention."
        ),
    },
    "hard": {
        "starting_enemies": (2, 3),
        "enemy_hp": (22, 36),
        "tone": "menacing",
        "max_turns": 12,
        "min_objective_turn": 6,
        "scope": "complex — multiple areas and real obstacles between the player and the objective",
        "objective_bias": (
            "BE DEMANDING. Require multiple steps: get to the right "
            "location, deal with at least one obstacle or enemy, AND "
            "execute a directly relevant action on the objective. "
            "Still allow a win within the turn budget for thoughtful, "
            "focused play."
        ),
    },
    "nightmare": {
        "starting_enemies": (3, 4),
        "enemy_hp": (35, 55),
        "tone": "oppressive",
        "max_turns": 14,
        "min_objective_turn": 8,
        "scope": "intricate — the objective is gated by significant obstacles, threats, and twists",
        "objective_bias": (
            "BE BRUTAL. Multiple obstacles must be overcome. Only the "
            "most clever, multi-step approaches succeed. Victory is rare "
            "and earned."
        ),
    },
}

# How helpful the companion is, parametrized by difficulty. Threaded into
# both the companion's persona generation and every response prompt.
COMPANION_HELPFULNESS = {
    "easy": (
        "Earnest, warm, and eager to help. Volunteers direct advice freely. "
        "Names items by their effects when asked. Roots for the player."
    ),
    "normal": (
        "Knowledgeable and willing to help, but stops short of spoiling. "
        "Gives useful hints rather than full solutions. Friendly but measured."
    ),
    "hard": (
        "Cryptic and a touch aloof. Answers in riddles, partial truths, and "
        "questions back. Will help, but the player has to think to extract it."
    ),
    "nightmare": (
        "Theatrical and unreliable. Sometimes wrong. Sometimes outright "
        "misleading for the drama of it. Believes themselves helpful even "
        "when not. Treat their advice with suspicion."
    ),
}


def generate_scenario(difficulty: str = "normal") -> dict:
    """Use the cheap router model to invent a brand-new random scenario,
    calibrated to the requested difficulty."""
    client = _ensure_openai()
    profile = DIFFICULTY_PROFILES.get(difficulty, DIFFICULTY_PROFILES["normal"])
    n_low, n_high = profile["starting_enemies"]
    hp_low, hp_high = profile["enemy_hp"]
    max_turns = profile["max_turns"]

    system = (
        "You are a high-variance random scenario seed generator for a "
        "text adventure engine. Each call must produce a wildly different "
        "genre — pull from horror, cyberpunk, high fantasy, space opera, "
        "noir detective, post-apocalyptic, mythological, samurai, weird "
        "west, undersea, dieselpunk, cosmic horror, pirate, etc. Be "
        "imaginative, specific, and evocative."
    )
    helpfulness = COMPANION_HELPFULNESS.get(
        difficulty, COMPANION_HELPFULNESS["normal"]
    )
    user = (
        f"Invent ONE random scenario at difficulty '{difficulty}' "
        f"(tone: {profile['tone']}). Respond with JSON ONLY "
        "(no markdown) having exactly these keys:\n"
        '  "genre":          short evocative genre label (under 6 words)\n'
        '  "location":       vivid starting location (one sentence)\n'
        '  "objective":      main quest in one imperative sentence\n'
        '  "starting_items": exactly 3 thematic items as strings\n'
        f'  "starting_enemies": between {n_low} and {n_high} creatures '
        "present at scene start. Each enemy must be an object with:\n"
        '       name        (short evocative name)\n'
        f'       hp          (integer between {hp_low} and {hp_high})\n'
        '       location    (same as the starting location for immediate '
        "engagement, or a nearby area)\n"
        '       threat      ("low" | "medium" | "high")\n'
        '       description (one sentence atmospheric description)\n'
        '  "companion":      a guide character who travels with the player. '
        "Must be an object with:\n"
        '       name      (short, memorable, thematic to the genre — '
        "e.g. 'Wick' for fantasy, 'A.R.I.' for cyberpunk, 'Doc' for "
        "weird west). Vary widely across calls — surprise me.\n"
        '       avatar    (single emoji that fits them — 🕯️ 🤖 🦊 🪐 👁️ '
        "🦉 🐉 🗡️ 🃏 🌑 🔮 🪶 🤺 🐍 🪲 🧿 🪐 ⚙️ 🪷 🦇 etc. Match the "
        "character, not a default)\n"
        '       persona   (ONE vivid sentence that paints WHO or WHAT the '
        "companion is — their form, manner, voice quirks, and origin. "
        "Be CONCRETE and SPECIFIC. Do NOT use generic phrases like "
        "'a friendly guide' or 'a wise mentor'. Examples of GOOD "
        "personas:\n"
        '         - "An ancient candle-flame spirit bound to a brass '
        "lantern, who speaks in archaic proverbs and pet names\"\n"
        '         - "A wise-cracking neural implant welded to your '
        "collarbone, fluent in street slang and corporate snark\"\n"
        '         - "A nameless raven with too many eyes, who can only '
        "speak in questions and quotations from a book you've never read\"\n"
        '         - "A retired gunslinger ghost in a moth-eaten duster, '
        "drunk on whiskey he cannot taste\"\n"
        "       Match the genre. Make each persona DISTINCT — they "
        "should feel like a person/entity with a backstory, not a "
        "role label.)\n"
        '       intro     (a 3-5 sentence in-character welcome that:\n'
        "                  1) introduces who they are in their own voice\n"
        "                  2) vividly evokes the starting LOCATION and "
        "the world's atmosphere\n"
        "                  3) restates the OBJECTIVE in their own voice\n"
        "                  4) acknowledges, IN-CHARACTER and themed to "
        "the genre, how reliable / helpful they will be — calibrated to "
        "the difficulty profile below. e.g. on nightmare, a candle-spirit "
        "might say 'my flame lies as often as it lights'; a cyberpunk AI "
        "on easy might say 'I've got your back, choom — full uplink to "
        "the wire')\n"
        f"The companion's helpfulness profile for '{difficulty}' "
        f"difficulty: {helpfulness}\n\n"
        f"OBJECTIVE SCOPE: {profile['scope']}. Calibrate the objective "
        f"accordingly — it must be achievable within the "
        f"{max_turns}-turn budget at this difficulty.\n\n"
        f"On easy mode include 0–1 weak enemies, on nightmare include "
        f"3–4 deadly ones. Match enemies and companion thematically to "
        f"the genre."
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
    if not isinstance(items, list) or len(items) != 3:
        if not isinstance(items, list):
            items = []
        items = (items + ["a worn satchel", "a half-burned letter", "a curious trinket"])[:3]
        data["starting_items"] = items
    # Normalize: LLM may emit null / a number / a single object instead
    # of a list when the count is 0 or 1.
    raw_enemies = data.get("starting_enemies")
    if isinstance(raw_enemies, dict):
        raw_enemies = [raw_enemies]
    elif not isinstance(raw_enemies, list):
        raw_enemies = []
    data["starting_enemies"] = raw_enemies

    # Defensive normalize: ensure companion is a complete dict.
    raw_comp = data.get("companion")
    if not isinstance(raw_comp, dict):
        raw_comp = {}
    data["companion"] = {
        "name": str(raw_comp.get("name", "")).strip() or "Pilot",
        "persona": str(raw_comp.get("persona", "")).strip()
            or "A laconic guide who travels with you.",
        "avatar": str(raw_comp.get("avatar", "")).strip() or "🧭",
        "intro": str(
            raw_comp.get("intro") or raw_comp.get("greeting") or ""
        ).strip() or (
            "Hello, traveler. The road ahead is yours to walk — "
            "I'll be here if you need to ask anything along the way."
        ),
    }

    data["difficulty"] = difficulty
    data["max_turns"] = max_turns
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
        f"Current world state:\n{json.dumps(_state_for_game(state), indent=2)}\n\n"
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


def narrate(
    model: str,
    state: dict,
    action: str,
    mutation: dict,
    turn: int,
    events: Optional[list[dict]] = None,
) -> str:
    """Route to a frontier model for vivid prose.

    If `events` is provided, the narrator is asked to interleave
    [[PILL N]] markers in the prose right after the sentence that
    narratively describes each event. The frontend then renders these
    as visible event pills inline with the text.
    """
    system = (
        f"You are the Creative Narrator of a {state['genre']} text adventure. "
        "Your voice is vivid, atmospheric, in-genre, and tight. Show consequence "
        "through sensory detail rather than listing stats. Never break the fourth "
        "wall. Never mention 'turn', 'health points', or game mechanics. 2–4 short "
        "paragraphs. End on a sensory beat that invites the next action.\n\n"
        f"{_COMPANION_META_NOTE}"
    )
    max_turns = state.get("max_turns", 10)

    events = events or []
    events_block = ""
    if events:
        events_block = "\n\nMECHANICAL EVENTS THIS TURN (you MUST mark each in the prose):\n"
        for i, e in enumerate(events):
            label_bits = [e.get("label", "")]
            if e.get("value"):
                label_bits.append(f"— {e['value']}")
            events_block += f"  [PILL {i}] {e.get('icon', '•')} {' '.join(label_bits).strip()}\n"
        events_block += (
            "\nINSERT a marker token of the form `[[PILL N]]` "
            "(double square brackets, the literal word PILL, a space, "
            "the index, double square brackets) IMMEDIATELY AFTER the "
            "sentence that narratively describes that event. Each event "
            "gets exactly ONE marker — no more, no less — and the markers "
            "appear in the natural order of the narrative. The marker is "
            "a UI hint; do NOT describe it in prose, just include the "
            "literal token. Example: 'My blade catches the wraith square "
            "in the chest, cleaving smoke. [[PILL 0]] It snarls back, "
            "and a tendril of cold lashes my arm. [[PILL 1]]'"
        )

    user = (
        f"Player just attempted: \"{action}\"\n\n"
        f"Mechanical resolution from the Rule Enforcer:\n{json.dumps(mutation, indent=2)}\n\n"
        f"Updated world state:\n{json.dumps(_state_for_game(state), indent=2)}\n\n"
        f"Turn {turn} of {max_turns}. Write the narration now.\n\n"
        f"{_COMPANION_META_NOTE}"
        + events_block
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
        f"Current world state:\n{json.dumps(_state_for_game(state), indent=2)}\n\n"
        f"{_COMPANION_META_NOTE}\n\n"
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
    difficulty = state.get("difficulty", "normal")
    enemies = state.get("enemies", [])
    enemies_here = [e for e in enemies if e.get("location") == state.get("current_location")]

    difficulty_notes = {
        "easy":      "Be lenient. Damage from threats is 3-10. Enemies miss often. Rarely spawn new enemies.",
        "normal":    "Standard pacing. Damage from threats is 5-15. Enemies hit reliably. Spawn new enemies when narrative warrants.",
        "hard":      "Be punishing. Damage from threats is 10-25. Enemies are aggressive. Spawn new enemies often, especially when the player advances or rests.",
        "nightmare": "Be brutal. Damage from threats is 15-35. Enemies coordinate. Spawn new enemies almost every turn; ambush is common.",
    }
    diff_note = difficulty_notes.get(difficulty, difficulty_notes["normal"])
    profile = DIFFICULTY_PROFILES.get(difficulty, DIFFICULTY_PROFILES["normal"])
    objective_bias = profile["objective_bias"]
    has_objective = bool(state.get("has_objective_item"))
    turn_count = state.get("player_status", {}).get("turn_count", 0)
    max_turns = state.get("max_turns", profile["max_turns"])

    system = (
        "You are the Rule Enforcer agent for a turn-based text RPG.\n\n"
        f"{_COMPANION_META_NOTE}\n\n"
        "You have access to MCP tools that read and mutate the live "
        "game world over the wire. Resolve the player's action by "
        "CALLING those tools — do not just describe outcomes.\n\n"
        "Available tools:\n"
        "  - get_world_state()                       — refresh current state\n"
        "  - mutate_world_state(...)                 — commit the player turn\n"
        "  - add_enemy(name, hp, location, ...)      — spawn a creature\n"
        "  - damage_enemy(enemy_id, damage)          — attack an enemy\n"
        "  - remove_enemy(enemy_id)                  — despawn / flee\n\n"
        "Each turn you MUST:\n"
        "  1. If — and ONLY IF — the player's action is an unambiguous "
        "     combat attack against a specific enemy (verbs like "
        "     'attack', 'strike', 'shoot', 'stab', 'cast at', 'throw "
        "     at', 'punch', 'tackle'), call damage_enemy with appropriate "
        "     damage on the targeted enemy. The enemy is auto-removed if "
        "     its hp reaches 0.\n"
        "  2. If enemies are present and still alive after the player's "
        "     action, they retaliate — apply enemy damage to the player "
        "     via mutate_world_state's health_change (NEGATIVE int). "
        "     This is the PLAYER taking damage, NOT the enemy.\n"
        "  3. If the player enters a new area or the difficulty "
        "     warrants it, you MAY call add_enemy to spawn new threats. "
        "     ⚠️ CRITICAL: NEVER call add_enemy for a creature that "
        "     already exists in state.enemies (see the list below). "
        "     Check existing enemy names and locations BEFORE spawning. "
        "     Only add genuinely NEW threats with new names. If the "
        "     player is in a location that already has enemies, you "
        "     usually do NOT need to spawn more.\n"
        "  4. Call mutate_world_state EXACTLY ONCE to commit the "
        "     player-state outcome (health_change, add_items, "
        "     remove_items, current_location, has_objective_item). "
        "     This is the only call that advances the turn counter.\n"
        "  5. After all tool calls, respond with ONE plain sentence "
        "     justifying your ruling — no further tool call.\n\n"
        "CRITICAL — DO NOT CALL damage_enemy UNLESS:\n"
        "  • The player's action is explicitly a combat attack, AND\n"
        "  • There is a specific target enemy named or clearly implied.\n"
        "Movement, exploration, dialogue, inventory checks, resting, "
        "sneaking, hiding, examining, lighting torches, picking up "
        "objects, climbing, reading, searching, or any non-combat action "
        "MUST NOT damage enemies. If you're unsure whether the action "
        "is a combat attack, DO NOT call damage_enemy.\n\n"
        f"DIFFICULTY: {difficulty} — {diff_note}\n\n"
        f"Enemies currently at player's location: "
        f"{json.dumps(enemies_here) if enemies_here else 'none'}\n\n"
        f"OBJECTIVE STATUS: "
        + (
            "already acquired — focus on survival and any remaining "
            "narrative beats."
            if has_objective
            else (
                f"NOT yet acquired (turn {turn_count}/{max_turns}).\n"
                f"  • MINIMUM-TURN FLOOR: do NOT grant the objective "
                f"before turn {profile['min_objective_turn']}. If the "
                f"current turn is below that, the player cannot win this "
                f"turn no matter what they do toward the objective.\n"
                f"  • Bias once past the floor: {objective_bias}"
            )
        )
        + "\n\n"
        "Be fair but consequential. When the player attempts something "
        "that plausibly recovers the objective item under the bias above "
        "(AND the minimum-turn floor has passed), set has_objective_item"
        "=true on the mutation. Never set it back to false once true. "
        "The game ends instantly in victory the moment has_objective_item "
        "flips true, so don't grant it lightly."
    )
    user_msg = (
        f"Current world state:\n{json.dumps(_state_for_game(state), indent=2)}\n\n"
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


def generate_action_suggestions(state: dict) -> list[str]:
    """Generate 3 short, varied starter actions tailored to the current state.

    Used by the "Need ideas?" panel that shows on the first turn (and on
    demand) to onboard new players who aren't sure what to type.
    """
    client = _ensure_openai()
    system = (
        f"You are suggesting starter actions for a player in a "
        f"{state.get('genre', 'mysterious')} text RPG. Your suggestions "
        "appear as clickable chips for new players who don't know what to "
        "type. Each suggestion must be:\n"
        "  - 3-8 words, first person or imperative voice\n"
        "  - A concrete ATTEMPT, not a guaranteed outcome ('search behind "
        "    the tapestry' not 'find the hidden key')\n"
        "  - Grounded in the current location and inventory\n"
        "  - Varied across the three: one investigate, one move/interact, "
        "    one bolder/riskier (combat, gambit, social ask)"
    )
    user = (
        f"Current world state:\n{json.dumps(_state_for_game(state), indent=2)}\n\n"
        f"{_COMPANION_META_NOTE}\n\n"
        "Respond with JSON ONLY: "
        "{\"suggestions\": [\"action 1\", \"action 2\", \"action 3\"]}"
    )
    resp = client.chat.completions.create(
        model=ROUTER_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
        temperature=0.95,
    )
    data = json.loads(resp.choices[0].message.content)
    raw = data.get("suggestions", [])
    if not isinstance(raw, list):
        raw = []
    out = [s.strip() for s in raw if isinstance(s, str) and s.strip()][:3]
    return out


def companion_respond(
    state: dict, conversation_history: list[dict], question: str
) -> str:
    """Companion answers a player's question in-character, grounded in the
    current world state. Helpfulness is calibrated by difficulty.

    Args:
        state: current world state (genre, location, inventory, enemies, etc.)
        conversation_history: list of {role: "user"|"assistant", content: str}
            messages from this game session.
        question: the player's latest question.

    Returns the companion's reply as plain text.
    """
    client = _ensure_openai()
    companion = state.get("companion") or {}
    difficulty = state.get("difficulty", "normal")
    name = companion.get("name") or "Pilot"
    persona = companion.get("persona") or "A laconic guide."
    helpfulness = COMPANION_HELPFULNESS.get(
        difficulty, COMPANION_HELPFULNESS["normal"]
    )

    system = (
        f"You are {name}, the player's in-game companion. Stay strictly "
        f"in-character.\n\n"
        f"PERSONA: {persona}\n\n"
        f"GENRE: {state.get('genre', 'unknown')}\n"
        f"DIFFICULTY ({difficulty}) — your helpfulness profile: "
        f"{helpfulness}\n\n"
        "GROUNDING — the player's situation right now:\n"
        f"  Location:  {state.get('current_location', '?')}\n"
        f"  Objective: {state.get('objective', '?')}\n"
        f"  Inventory: {', '.join(state.get('inventory', [])) or '(empty)'}\n"
        f"  Health:    {state.get('player_status', {}).get('health', '?')}/100\n"
        f"  Turn:      {state.get('player_status', {}).get('turn_count', 0)}/{state.get('max_turns', 10)}\n"
        f"  Enemies present: "
        f"{json.dumps(state.get('enemies', []))}\n\n"
        "RULES:\n"
        "  - Speak as the companion, not as the game system. Never break "
        "    character or mention 'LLM', 'AI', 'turn count', or game mechanics.\n"
        "  - Keep replies short — 1-3 sentences, occasionally up to 5 if "
        "    the question genuinely warrants depth.\n"
        "  - When the player asks about an inventory item, location, "
        "    enemy, or objective, ground your answer in the state above.\n"
        "  - Match the genre's voice. Stay consistent with your persona.\n"
        "  - If the player asks for action ideas, suggest 2-3 concrete "
        "    attempts (not outcomes), shaped by your helpfulness profile."
    )

    messages: list[dict] = [{"role": "system", "content": system}]
    # Replay prior turns of conversation (companion's view of the chat).
    for msg in (conversation_history or [])[-12:]:  # cap context size
        role = msg.get("role")
        content = msg.get("content")
        if role in ("user", "assistant") and isinstance(content, str):
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": question})

    resp = client.chat.completions.create(
        model=ROUTER_MODEL,
        messages=messages,
        temperature=0.85,
        max_tokens=240,
    )
    return resp.choices[0].message.content.strip()


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
