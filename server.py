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
        return json.load(f)


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
        }
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


if __name__ == "__main__":
    mcp.run()
