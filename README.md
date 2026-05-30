# Prompt Your Own Adventure

A model-routed random text RPG built on top of the **Model Context Protocol (MCP)**. Three real, decoupled processes talking over actual wire protocols:

```
  browser ──HTTP──▶  FastAPI orchestrator  ──MCP / SSE──▶  RPG_Engine
   :8000                  app.py (:8000)                  server.py (:8001)
```

Two LLMs are routed per turn:

- **Rule Enforcer** (`gpt-4o-mini`) — fast structured JSON over the current world state, decides mechanical outcomes
- **Narrator** (`claude-sonnet-4-6` by default, hot-swappable) — frontier model that turns the mutation into vivid in-genre prose

The **MCP server has zero LLM logic** — it's a strict rules / state engine exposing three tools:

- `get_world_state()`
- `initialize_game(genre, location, objective, starting_items)`
- `mutate_world_state(health_change, add_items, remove_items, current_location, has_objective_item)`

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

Honest answer: for a single-player, single-client web game like this one, **MCP is overkill**. You could replace the whole `server.py` ↔ `app.py` MCP layer with a plain Python module import and lose zero functionality (and gain ~20 ms of latency back per turn).

Two related caveats worth being upfront about:

1. **The LLM doesn't directly invoke MCP tools.** In canonical MCP usage, the LLM client sees the tool schemas, decides which tool to call, emits a `tool_use` message, and the protocol handles execution. Here, the Rule Enforcer LLM just emits a JSON blob; *the orchestrator* parses that and calls the MCP tool. We're using MCP as a glorified RPC layer for the orchestrator, not as a tool-use surface for the LLM.
2. **There is only one client.** The whole point of MCP is "many clients, one server, common protocol." With only the FastAPI orchestrator talking to the server, the protocol's value proposition isn't being realized.

So why use MCP here at all?

- **As a learning vehicle for the protocol.** The three game tools (`get` / `init` / `mutate`) map cleanly to MCP primitives and make a good worked example of how a FastMCP server is structured and discovered.
- **As architectural future-proofing.** The server is now a network service. Any MCP-aware client (Claude Desktop, custom IDE extensions, a Discord bot, an Alexa skill) could connect to it without code changes. That's MCP's actual selling point, and it's there for free once the protocol is in place.
- **As a forcing function for clean boundaries.** Speaking strict JSON-RPC over a wire prevents the rules engine from quietly growing tendrils into the orchestrator (or vice versa) the way two co-located Python modules tend to.

For a *real* product version of this game, I'd probably drop MCP and use plain imports unless the multi-client story was an actual product requirement.

## Architecture summary

| Process | Port | Role |
| --- | --- | --- |
| `server.py` | 8001 (SSE) | FastMCP server — pure state / rules. No LLM. |
| `app.py` | 8000 (HTTP) | FastAPI orchestrator. Holds one persistent MCP session, routes between Rule Enforcer + Narrator. Serves the SPA. |
| Browser | — | Vanilla HTML/JS, no build step. Renders state, telemetry, inspector. |

## Project layout

```
server.py                  # FastMCP server (rules engine)
app.py                     # FastAPI orchestrator
llm.py                     # Rule Enforcer + Narrator + scenario/action gen
run.py                     # Convenience launcher (spawns both)
static/
  index.html
  style.css
  app.js
world_state.template.json  # blank schema
.env / .env.example        # API keys (.env is gitignored)
```
