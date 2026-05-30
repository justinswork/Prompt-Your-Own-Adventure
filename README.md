# Prompt Your Own Adventure

A model-routed random text RPG built around a real **agentic workflow** over the **Model Context Protocol (MCP)**. Three decoupled processes talk over actual wire protocols:

```
  browser ──HTTP──▶  FastAPI orchestrator  ──MCP / SSE──▶  RPG_Engine
   :8000                  app.py (:8000)                  server.py (:8001)
```

Per turn, the orchestrator routes between two LLMs:

- **Rule Enforcer agent** (`gpt-4o-mini`) — runs an OpenAI function-calling tool-use loop, autonomously invoking MCP tools (`get_world_state`, `mutate_world_state`) until it has resolved the player's action
- **Narrator** (`claude-sonnet-4-6` by default, hot-swappable) — frontier model that turns the mutated state into vivid in-genre prose

The **MCP server has zero LLM logic** — it's a strict rules / state engine exposing three tools:

- `get_world_state()`
- `initialize_game(genre, location, objective, starting_items)`
- `mutate_world_state(health_change, add_items, remove_items, current_location, has_objective_item)`

## How this satisfies the rubric

The assignment called for *"an agentic workflow using tools, MCP, and model routing"*. Each requirement is implemented as follows:

| Requirement | How it's met | Where to look |
| --- | --- | --- |
| **Agentic workflow** | Each turn spawns a Rule Enforcer agent that runs an autonomous tool-use loop. The LLM — not the orchestrator — decides which MCP tools to call, with what arguments, and when to stop. Up to 5 iterations per turn; the loop exits when the model returns without a `tool_call`. | `rule_enforcer_agent()` in [`llm.py`](llm.py); turn driver in [`app.py`](app.py) `/api/turn` |
| **Tools (LLM tool-use)** | The MCP server's `inputSchema` for each tool is translated into OpenAI's `tools=` function-calling format and passed to the model on every iteration. The model emits real `tool_call` messages with structured arguments; the orchestrator executes them and feeds results back as `role: "tool"` messages. | `_mcp_to_openai_tools()` in [`app.py`](app.py); messages loop in `rule_enforcer_agent()` |
| **MCP** | All state lives on a separate FastMCP server process. Connection uses **SSE transport on `http://127.0.0.1:8001/sse`** (a real network endpoint, inspectable with `curl`). The orchestrator opens one persistent `ClientSession` at startup. Tool schemas are discovered via `tools/list` at handshake. Any MCP-aware client (Claude Desktop, IDE plugins, a CLI) could connect to the same server without code changes. | [`server.py`](server.py), `lifespan()` in [`app.py`](app.py), MCP Inspector pane in the UI |
| **Model routing** | Three distinct models, dispatched per concern: `gpt-4o-mini` for scenario generation, action generation, and the Rule Enforcer agent; a frontier model (default `claude-sonnet-4-6`) for the Narrator. The narrator can be hot-swapped mid-game via the in-UI dropdown (Claude Sonnet 4.6 / Opus 4.7 / Haiku 4.5 / GPT-4o / GPT-4o-mini / GPT-4.1). The `narrate()` function dispatches by model family to the right SDK at runtime. | `narrate()` and `NARRATOR_CHOICES` in [`llm.py`](llm.py); `/swap` endpoint and dropdown in `app.js` |

Open the **🔌 MCP pane** (right side of the page) and play any turn — you'll see the agent's per-iteration reasoning, each `tool_call` it emits, the matching `[MCP CLIENT]` / `[MCP SERVER]` JSON-RPC payloads going over the wire, and finally the narrator being routed in. The whole rubric is observable live.

## Quickstart

```bash
pip install -r requirements.txt
cp .env.example .env       # then paste your OPENAI_API_KEY + ANTHROPIC_API_KEY
```

Then either:

**One-command launcher** (good for development):

```bash
python run.py
```

**Two terminals** (recommended for demos — you can see the MCP server's own log output):

```bash
# Terminal 1
python server.py

# Terminal 2
uvicorn app:app --reload
```

Either way, open <http://127.0.0.1:8000>.

## Wow-factor controls

- **🔌 MCP Inspector** (top-right of the page) — live snapshot of the MCP connection: server URL, transport, discovered tool schemas (pulled from `tools/list`), a manual tool-invoker that calls any tool with arbitrary JSON args, and a recent-call history with roundtrip times.
- **`/swap` narrator dropdown** — hot-swap the Narrator LLM mid-game (Claude Sonnet 4.6 → GPT-4o → Claude Opus 4.7 → …) without restarting anything. Demonstrates that the architecture really is modular.
- **🎲 Choose for me / Auto-play** — let the router model pick player actions so you can watch the whole pipeline run hands-off.
- **Telemetry pane** — every turn streams `[MODEL ROUTER]` / `[MCP CLIENT]` / `[MCP SERVER]` events with raw JSON-RPC payloads.

## When does this architecture actually make sense?

Honest answer: for a single-player, single-client web game like this one, **MCP is still overkill**. You could replace the whole `server.py` ↔ `app.py` MCP layer with a plain Python module import and lose no game functionality (and gain ~20 ms of latency back per turn).

What MCP genuinely buys you here:

- **A real tool-use surface for the LLM.** The agent loop in `rule_enforcer_agent()` is using MCP the way it was designed to be used: tool schemas are discovered over the protocol, handed to the model, and the model's `tool_call` outputs are executed back over the same protocol. Without MCP, you'd be hand-rolling a bespoke function-calling pipeline anyway.
- **Multi-client capability for free.** The server is a network service. Any MCP-aware client (Claude Desktop, IDE plugins, a Discord bot) could connect to the same server with no code changes. Whether or not we use that capability today, the architecture is ready for it.
- **A forcing function for clean boundaries.** Speaking strict JSON-RPC over a wire prevents the rules engine from quietly growing tendrils into the orchestrator (or vice versa) the way two co-located Python modules tend to.

The honest caveat: **there is still only one client.** The whole point of MCP is "many clients, one server, common protocol," and in the current single-deploy form factor the protocol's full value proposition isn't realized. For a *real* product version of this game, I'd probably drop MCP and use plain imports unless the multi-client story was an actual product requirement.

## Architecture summary

| Process | Port | Role |
| --- | --- | --- |
| `server.py` | 8001 (SSE) | FastMCP server — pure state / rules. No LLM. |
| `app.py` | 8000 (HTTP) | FastAPI orchestrator. Holds one persistent MCP session, runs the Rule Enforcer agent tool-use loop per turn, then routes to the Narrator. Serves the SPA. |
| Browser | — | Vanilla HTML/JS, no build step. Renders state, telemetry, inspector. |
