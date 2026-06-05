# Prompt Your Own Adventure

> **Status:** ✅ Complete. 2-week Gen AI class project, both weeks shipped. Live at https://rpg-engine-887701342599.us-central1.run.app/

A model-routed random text RPG built around a real **agentic workflow** over the **Model Context Protocol (MCP)**. Three decoupled processes talk over actual wire protocols:

```
  browser ──HTTP──▶  FastAPI orchestrator  ──MCP / SSE──▶  RPG_Engine
   :8000                  app.py (:8000)                  server.py (:8001)
```

Per turn, the orchestrator routes between two LLMs:

- **Rule Enforcer agent** (`gpt-4o-mini`) — runs an OpenAI function-calling tool-use loop, autonomously invoking MCP tools (`get_world_state`, `mutate_world_state`) until it has resolved the player's action
- **Narrator** (`claude-sonnet-4-6` by default, hot-swappable) — frontier model that turns the mutated state into vivid in-genre prose

The **MCP server has zero LLM logic** — it's a strict rules / state engine exposing seven tools and one resource:

**State / player tools:**
- `get_world_state()`
- `initialize_game(genre, location, objective, starting_items, difficulty, starting_enemies, companion, max_turns)`
- `mutate_world_state(health_change, add_items, remove_items, current_location, has_objective_item)`

**Combat / world tools:**
- `add_enemy(name, hp, location, threat, description)`
- `damage_enemy(enemy_id, damage)`
- `remove_enemy(enemy_id)`

**Memory tool + Resource:**
- `record_turn(turn, action, summary, narration_excerpt)` — orchestrator-only tool that appends a turn record to the history.
- `world://turn-history` — the project's first **MCP Resource**. Exposes the per-turn history as JSON so the Narrator (or any external MCP client) can fetch it for tonal continuity and narrative callbacks. Resources are MCP's "readable data sources" primitive; this expands our protocol surface beyond Tools alone.

Only `mutate_world_state` advances the turn counter, so the agent can freely chain multiple combat tool calls within a single turn.

## How this satisfies the rubric

The assignment called for *"an agentic workflow using tools, MCP, and model routing"*. Each requirement is implemented as follows:

| Requirement | How it's met | Where to look |
| --- | --- | --- |
| **Agentic workflow** | Each turn spawns a Rule Enforcer agent that runs an autonomous tool-use loop. The LLM — not the orchestrator — decides which MCP tools to call, with what arguments, and when to stop. Up to 5 iterations per turn; the loop exits when the model returns without a `tool_call`. | `rule_enforcer_agent()` in [`llm.py`](llm.py); turn driver in [`app.py`](app.py) `/api/turn` |
| **Tools (LLM tool-use)** | The MCP server's `inputSchema` for each tool is translated into OpenAI's `tools=` function-calling format and passed to the model on every iteration. The model emits real `tool_call` messages with structured arguments; the orchestrator executes them and feeds results back as `role: "tool"` messages. | `_mcp_to_openai_tools()` in [`app.py`](app.py); messages loop in `rule_enforcer_agent()` |
| **MCP** | All state lives on a separate FastMCP server process. Connection uses **SSE transport on `http://127.0.0.1:8001/sse`** (a real network endpoint, inspectable with `curl`). The orchestrator opens one persistent `ClientSession` at startup. Tool schemas are discovered via `tools/list` at handshake. Any MCP-aware client (Claude Desktop, IDE plugins, a CLI) could connect to the same server without code changes. | [`server.py`](server.py), `lifespan()` in [`app.py`](app.py), MCP Inspector pane in the UI |
| **Model routing** | Three distinct models, dispatched per concern: `gpt-4o-mini` for scenario generation, action generation, and the Rule Enforcer agent; a frontier model (default `claude-sonnet-4-6`) for the Narrator. The narrator can be hot-swapped mid-game via the in-UI dropdown (Claude Sonnet 4.6 / Opus 4.7 / Haiku 4.5 / GPT-4o / GPT-4o-mini / GPT-4.1). The `narrate()` function dispatches by model family to the right SDK at runtime. | `narrate()` and `NARRATOR_CHOICES` in [`llm.py`](llm.py); `/swap` endpoint and dropdown in `app.js` |

Open the **🔌 MCP pane** (right side of the page) and play any turn — you'll see the agent's per-iteration reasoning, each `tool_call` it emits, the matching `[MCP CLIENT]` / `[MCP SERVER]` JSON-RPC payloads going over the wire, and finally the narrator being routed in. The whole rubric is observable live.

## Eval cases

Concrete reference outputs for each LLM surface — what "good" looks like for scenario generation, the Rule Enforcer agent's tool-use loop, the companion's in-character Q&A, and the narrator's pill-marker placement — live in **[`EVALS.md`](EVALS.md)**. Each case has an input, a representative good output, and an annotation explaining the success criteria.

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

- **Generated AI companion** — Every adventure spawns a unique NPC guide: themed avatar, name, persona, and a calibrated reliability score (1–10, shown as a colored chip) tied to difficulty. They greet you on a dedicated intro screen with an in-character welcome that names the setting, restates the objective in their voice, and acknowledges how trustworthy they'll be. From the game screen, click the rail on the left edge to summon them and ask anything about your current world, inventory items, threats, or what to try next. They answer in-character via a per-question LLM call grounded in actual state. Their voice shifts dramatically by difficulty: warm and direct on easy, theatrical and outright unreliable on nightmare.
- **🔌 MCP Inspector** (collapsible right-side pane) — live snapshot of the MCP connection: server URL, transport, discovered tool schemas (pulled from `tools/list`), recent call history with roundtrip times, and a manual tool-invoker that lets you call any tool with arbitrary JSON args. Proves the protocol is real and observable.
- **`/swap` narrator dropdown** — hot-swap the Narrator LLM mid-game (Claude Sonnet 4.6 → GPT-4o → Claude Opus 4.7 → Haiku 4.5 → …) without restarting anything. Model routing as a player-facing control.
- **Per-turn event pills inline with narration** — the Narrator embeds `[[PILL N]]` markers in its prose at the moment each event happens; the frontend parses them and renders compact colored chips (`🗡️ Attacked Wraith · 9 dmg`, `💔 Took damage · 7`, `🎯 Objective acquired!`) right after the sentence that describes that beat. The LLM and the UI cooperate to make mechanics visible without breaking the story.
- **Long-term narrative memory via MCP Resource** — each turn's outcomes are recorded back to the MCP server. The Narrator on subsequent turns pulls the history via `world://turn-history` (the project's first MCP **Resource**, not another Tool) and weaves natural callbacks to earlier events.
- **🎲 Choose for me / Auto-play** — let the router model pick player actions, optionally chained on a loop, so you can watch the whole pipeline (agent tool-use → mutation → narration → companion availability) play out hands-off.
- **Live telemetry pane** — every turn streams `[MODEL ROUTER]` / `[AGENT iter N]` / `[MCP CLIENT]` / `[MCP SERVER]` events with raw JSON-RPC payloads, color-coded by stage.

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

## Deploy to Cloud Run

Both processes run inside one container (`run.py` spawns the MCP server on internal port 8001, then uvicorn on `$PORT`). Cloud Run gives you a public `https://*.run.app` URL — no domain purchase required.

**Prerequisites:** [`gcloud` CLI](https://cloud.google.com/sdk/docs/install) installed and logged in (`gcloud auth login`); an existing GCP project; billing enabled (Cloud Run free tier easily covers a class demo).

**One-time project setup:**

```bash
gcloud config set project YOUR_PROJECT_ID
gcloud services enable run.googleapis.com cloudbuild.googleapis.com
```

**Deploy** (from the project root — builds the image via Cloud Build, no local Docker required):

```bash
gcloud run deploy rpg-engine \
  --source . \
  --region us-central1 \
  --max-instances 1 \
  --memory 512Mi \
  --cpu 1 \
  --timeout 300 \
  --allow-unauthenticated \
  --set-env-vars "OPENAI_API_KEY=sk-...,ANTHROPIC_API_KEY=sk-ant-..."
```

The command prints the public URL when it finishes (something like `https://rpg-engine-abc123-uc.a.run.app`). Open it in a browser and play.

**Things worth knowing:**

- `--max-instances 1` keeps the in-memory MCP session and the JSON state file coherent. Don't increase it without first switching state to Firestore — two instances would race on `world_state.json`.
- `world_state.json` lives on the container's ephemeral disk. Cold starts wipe it; the server re-seeds from `world_state.template.json` on first read. For class-demo gameplay (~10 turns, short sessions) this is fine.
- API keys go via `--set-env-vars`, never baked into the image. For a more polished setup, switch to [Secret Manager](https://cloud.google.com/run/docs/configuring/secrets).
- LLM API costs are paid out-of-band on your OpenAI/Anthropic accounts; Cloud Run itself stays in the free tier for typical usage.
- To redeploy after a code change: re-run the same `gcloud run deploy` command. Cloud Build will rebuild and roll out.

## Combat + difficulty

The world now carries an `enemies` array and a `difficulty` setting (easy / normal / hard / nightmare), chosen on the setup screen. The scenario generator seeds the scene with appropriate creatures; the Rule Enforcer agent has three new MCP tools (`add_enemy`, `damage_enemy`, `remove_enemy`) it can chain together within a single turn to resolve combat. Concretely:

- The agent inspects which enemies are at the player's current location, decides damage, calls `damage_enemy(id, dmg)` for each one the player attacks (auto-removed if hp ≤ 0), applies enemy retaliation to the player via `mutate_world_state(health_change=...)`, and may call `add_enemy` to spawn new threats when difficulty warrants.
- Only `mutate_world_state` increments the turn counter, so the agent's combat chain can run to any reasonable depth within one turn.
- The agent's system prompt is dynamically calibrated by difficulty — `nightmare` tells it enemies hit for 15-35 and to spawn ambushes most turns; `easy` keeps damage 3-10 and enemy spawns rare.

This was the highest-leverage week-2 change because it forces the agent to make multiple coordinated tool calls per turn instead of always exactly one — which is where the rubric word *agentic* actually starts to do work.

## What was built

Both weeks of the project are shipped:

**Week 1** — three-tier architecture (browser → FastAPI orchestrator → FastMCP server), agentic Rule Enforcer with OpenAI function-calling tool-use loop, four-way model routing (gpt-4o-mini for fast structured work, swappable frontier model for narration), Cloud Run deployment, MCP Inspector pane, eval cases.

**Week 2** — persistent enemies with combat, four difficulty levels with per-difficulty turn budgets and reliability profiles, a generated guide companion with chat (`gpt-4o-mini` grounded in current state), an intro-screen take/solo decision flow, animated end screen with stat-card grid, per-turn event pills interwoven with narration, paginated narration with per-turn state snapshots and a read-only review mode, and long-term narrative memory via an MCP Resource (`world://turn-history`) that the narrator pulls before each turn to weave callbacks.

## Possible extensions (out of scope for this project)

These are the directions a hypothetical "week 3" could go:

- **A Director agent (multi-agent system).** A second agent that runs *between* player turns and decides what the world does — spawn an enemy, ratchet up tension, drop a hint about the objective, shift weather. Two LLM agents both driving MCP tools is the canonical multi-agent pattern.
- **MCP Prompts.** We use two of MCP's three primitives (Tools + Resources). Exposing the narrator persona / system instructions as a **Prompt** would round out the protocol surface fully.
- **NPCs with their own agents.** A `dialogue_with(npc_name)` tool that hands the conversation to a per-NPC agent. Each NPC has its own state (mood, knowledge, inventory) in `world_state.json`. Multi-agent storytelling on top of the existing infrastructure.
