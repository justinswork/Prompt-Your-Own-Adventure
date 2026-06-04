"""
Standalone MCP Server for the Universal Random RPG Engine.

This server exposes the world state database (world_state.json) as a set of
strict, deterministic tools. It contains ZERO LLM logic. It is purely a
physics / rule enforcement layer that the client orchestrator talks to over
the Model Context Protocol.

Run directly with:    python server.py
Inspect schema with:  fastmcp dev server.py
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Optional

from fastmcp import FastMCP

mcp = FastMCP("RPG_Engine")

STATE_FILE = Path(__file__).parent / "world_state.json"
TEMPLATE_FILE = Path(__file__).parent / "world_state.template.json"
_file_lock = threading.Lock()


def _read_state() -> dict:
    if not STATE_FILE.exists():
        with TEMPLATE_FILE.open("r", encoding="utf-8") as f:
            template = json.load(f)
        _write_state(template)
    with STATE_FILE.open("r", encoding="utf-8") as f:
        state = json.load(f)
    # Defensive backfill for older state files predating the
    # difficulty / enemies schema additions.
    state.setdefault("difficulty", "normal")
    state.setdefault("enemies", [])
    return state


def _next_enemy_id(state: dict) -> str:
    """Return the smallest 'eN' id not currently used by any enemy."""
    used = {e["id"] for e in state.get("enemies", [])}
    n = 1
    while f"e{n}" in used:
        n += 1
    return f"e{n}"


def _write_state(state: dict) -> None:
    with STATE_FILE.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


@mcp.tool()
def get_world_state() -> dict:
    """
    Read and return the complete current world state from world_state.json.

    Returns the full universe snapshot including genre, current_location,
    objective, player_status (health and turn_count), inventory, and the
    has_objective_item flag. This is the canonical source of truth — call
    this at the start of every turn before any narrative or mechanical
    reasoning so that decisions are grounded in the latest persisted state.
    """
    with _file_lock:
        return _read_state()


@mcp.tool()
def initialize_game(
    genre: str,
    location: str,
    objective: str,
    starting_items: list[str],
    difficulty: str = "normal",
    starting_enemies: Optional[list[dict]] = None,
) -> dict:
    """
    Overwrite world_state.json to begin a brand-new universe.

    Call this exactly once per playthrough, after the player accepts the
    generated scenario. It resets player_status to {health: 100,
    turn_count: 0}, clears the has_objective_item flag, and seeds the
    inventory with the provided starting_items.

    Args:
        genre: A short genre tag, e.g. "cyberpunk noir" or "high fantasy".
        location: The starting room / area description.
        objective: The single-sentence main quest the player must complete.
        starting_items: 3 thematic items the player begins with.
        difficulty: One of "easy" | "normal" | "hard" | "nightmare".
            Affects the Rule Enforcer agent's combat math and enemy spawn
            rate, plus the scenario generator's tone.
        starting_enemies: Optional list of enemies present at scenario
            start. Each entry is a dict with name/hp/location/threat/
            description. The server assigns each one a unique id.

    Returns the freshly committed world state dict.
    """
    with _file_lock:
        state = {
            "genre": genre,
            "current_location": location,
            "objective": objective,
            "player_status": {"health": 100, "turn_count": 0},
            "inventory": list(starting_items),
            "has_objective_item": False,
            "difficulty": difficulty,
            "enemies": [],
        }
        if starting_enemies:
            for raw in starting_enemies:
                hp = int(raw.get("hp", 10))
                state["enemies"].append({
                    "id": _next_enemy_id(state),
                    "name": str(raw.get("name", "Unknown")),
                    "hp": hp,
                    "max_hp": hp,
                    "location": str(raw.get("location", location)),
                    "threat": str(raw.get("threat", "medium")),
                    "description": str(raw.get("description", "")),
                })
        _write_state(state)
        return state


@mcp.tool()
def mutate_world_state(
    health_change: int = 0,
    add_items: Optional[list[str]] = None,
    remove_items: Optional[list[str]] = None,
    current_location: Optional[str] = None,
    has_objective_item: Optional[bool] = None,
) -> dict:
    """
    Safely apply a partial mutation to world_state.json and flush to disk.

    This is the ONLY way the world should change. Each call also auto-
    increments player_status.turn_count by 1, so call it exactly once per
    player turn — even when the mechanical outcome is "nothing happens"
    (pass all defaults in that case).

    Args:
        health_change: Signed integer applied to player_status.health.
            Negative = damage, positive = healing. 0 for neutral turns.
        add_items: Items to append to inventory. None or [] for no change.
        remove_items: Items to remove from inventory (silently ignored if
            not present). None or [] for no change.
        current_location: New location string if the player moved this
            turn. Pass None to leave the location unchanged.
        has_objective_item: Set to True the moment the player acquires the
            quest's key item. Pass None to leave the flag unchanged. Once
            True, do not flip back to False.

    Returns the full updated world state after the mutation.
    """
    with _file_lock:
        state = _read_state()

        state["player_status"]["health"] = (
            state["player_status"]["health"] + int(health_change)
        )
        state["player_status"]["turn_count"] = (
            state["player_status"]["turn_count"] + 1
        )

        if add_items:
            state["inventory"].extend(add_items)
        if remove_items:
            for item in remove_items:
                if item in state["inventory"]:
                    state["inventory"].remove(item)

        if current_location is not None:
            state["current_location"] = current_location
        if has_objective_item is not None:
            state["has_objective_item"] = bool(has_objective_item)

        _write_state(state)
        return state


@mcp.tool()
def add_enemy(
    name: str,
    hp: int,
    location: str,
    threat: str = "medium",
    description: str = "",
) -> dict:
    """
    Spawn a new enemy into the world.

    Use when the player enters a dangerous area, the agent decides a
    creature should ambush, or difficulty escalates and more threats
    appear. Does NOT increment turn_count (only mutate_world_state
    does that — call this BEFORE mutate_world_state in the same turn).

    Args:
        name: Short evocative name (e.g. "Shadow Wraith", "Iron Sentinel").
        hp: Starting hit points (also stored as max_hp). Calibrate to
            difficulty: easy 8-15, normal 15-25, hard 25-40, nightmare 40-60.
        location: Where the enemy is. Usually the player's current_location
            for immediate engagement, but can be elsewhere if pre-seeding.
        threat: "low" | "medium" | "high" — narrative tag that the agent
            uses to scale combat math and the narrator uses for tone.
        description: One-sentence atmospheric description rendered to the
            player.

    Returns the full updated world state, with the new enemy appended to
    state.enemies and a server-assigned id (e.g. "e3").
    """
    with _file_lock:
        state = _read_state()
        new_enemy = {
            "id": _next_enemy_id(state),
            "name": name,
            "hp": int(hp),
            "max_hp": int(hp),
            "location": location,
            "threat": threat,
            "description": description,
        }
        state.setdefault("enemies", []).append(new_enemy)
        _write_state(state)
        return state


@mcp.tool()
def damage_enemy(enemy_id: str, damage: int) -> dict:
    """
    Apply damage to an enemy by id. If hp drops to 0 or below, the
    enemy is removed from the world automatically.

    Use when the player attacks a specific enemy. Does NOT increment
    turn_count — call mutate_world_state separately to commit the
    full turn outcome (player's damage taken, items used, etc.).

    Args:
        enemy_id: The id (e.g. "e1") of the enemy to damage. Get this
            from get_world_state() or the return value of add_enemy.
        damage: Damage amount (positive int). Negative values heal,
            which is rare but allowed.

    Returns the full updated world state. If the enemy is killed by
    this call, it will be absent from state.enemies in the response.
    """
    with _file_lock:
        state = _read_state()
        enemies = state.get("enemies", [])
        for idx, enemy in enumerate(enemies):
            if enemy["id"] == enemy_id:
                enemy["hp"] = enemy["hp"] - int(damage)
                if enemy["hp"] <= 0:
                    enemies.pop(idx)
                break
        _write_state(state)
        return state


@mcp.tool()
def remove_enemy(enemy_id: str) -> dict:
    """
    Remove an enemy from the world without dealing damage.

    Use when the enemy flees, despawns naturally, wanders off, or is
    otherwise no longer present (but not killed in combat — for that,
    use damage_enemy until hp drops to 0). Does NOT increment
    turn_count.

    Args:
        enemy_id: The id (e.g. "e1") of the enemy to remove.

    Returns the full updated world state.
    """
    with _file_lock:
        state = _read_state()
        state["enemies"] = [
            e for e in state.get("enemies", []) if e["id"] != enemy_id
        ]
        _write_state(state)
        return state


if __name__ == "__main__":
    import sys

    HOST, PORT = "127.0.0.1", 8001
    print(
        f"[RPG_Engine] starting MCP SSE server on http://{HOST}:{PORT}/sse",
        file=sys.stderr,
    )
    mcp.run(transport="sse", host=HOST, port=PORT)
