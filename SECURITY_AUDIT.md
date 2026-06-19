# Security audit — Prompt Your Own Adventure

Audit performed before public release of the Cloud Run deployment.
Findings are listed by severity, with concrete fixes shipped or
mitigations recommended.

## Threat model

The deployment is public (`--allow-unauthenticated` on Cloud Run) so a
class grader can play without GCP login. That means:

- Anyone who knows the URL can hit every endpoint.
- Every call that hits an LLM (`/api/turn`, `/api/scenario`,
  `/api/auto-action`, `/api/suggestions`, `/api/companion/ask`,
  `/api/climax`) burns tokens billed to the operator's OpenAI /
  Anthropic accounts.
- All players share one global `world_state.json` (single Cloud Run
  instance via `--max-instances 1`).
- API keys live only in Cloud Run env vars; they are never returned
  by any endpoint or baked into the image.

## Findings

### 🔴 CRITICAL — fixed

**C1. `/api/mcp-call` allowed anonymous invocation of any MCP tool.**
The Inspector pane's manual tool-invoker let anyone call
`initialize_game` (wipes the current game), `mutate_world_state` with
extreme values (instantly kills the player), or `add_enemy` (corrupts
the active game for other concurrent visitors).
**Fix:** added an `INSPECTOR_PUBLIC_TOOLS = {"get_world_state"}`
allowlist on the public endpoint. Mutating tools are still callable by
the orchestrator's own code path; only the public manual-invoker is
gated. Full tool schemas remain discoverable via `/api/mcp-inspect`
so the protocol surface is still observable.

### 🟠 HIGH — fixed

**H1. No length cap on user-controlled text fields.**
A single 1 MB action submitted to `/api/turn` would balloon into a
multi-megabyte prompt feeding the Rule Enforcer + Narrator, burning
dollars in tokens per request. Same risk for the companion's
`question` and `history` fields.
**Fix:** Pydantic `Field(max_length=...)` on every user-controlled
text input:
- `TurnRequest.action`: 500 chars
- `CompanionAskRequest.question`: 500 chars
- `CompanionMessage.content`: 2000 chars
- `CompanionAskRequest.history`: capped at 30 entries
- `SwapRequest.model`: 80 chars
- `McpCallRequest.tool`: 80 chars

**H2. No global request-body limit.**
Independent of Pydantic, the HTTP server happily accepted multi-MB
JSON bodies before any per-field validation ran.
**Fix:** middleware rejects requests with `Content-Length > 16 KB`
returning HTTP 413. Comfortably above every legitimate body shape.

### 🟡 MEDIUM — mitigation recommended (operator action)

**M1. No per-IP rate limiting.** A single client could hit
`/api/turn` in a tight loop. Mitigations (not implemented; pick one):
- Set a **hard spending cap** on the OpenAI and Anthropic accounts
  (Settings → Limits → Hard limit, e.g., $10 / month each). Bounds
  worst-case cost regardless of request volume.
- Add `slowapi` or similar IP-based rate limiting if/when the URL is
  ever broadly shared.

**M2. Single global state.** Concurrent visitors interfere with each
other (one's mutation appears in another's dashboard). Not a security
issue per se, but is a multi-tenancy bug. Mitigation: cookie-keyed
session state with per-session world files. Out of scope for a class
demo, documented as a known limitation.

### 🟢 LOW — acknowledged, not fixed

**L1. Prompt injection.** Players can try to break out of the game
frame by sending action text that contains instructions to the LLM.
**Blast radius:** limited. The Rule Enforcer outputs structured JSON
that the orchestrator parses; injection can't escape the schema. The
Narrator and the companion output free text that the player sees —
worst case is embarrassing or off-brand narration. No system access,
no key exfiltration, no state corruption.

**L2. Companion history is client-supplied.** A malicious client
could send a fabricated conversation `history` to the companion to
manipulate its response. Blast radius is the same as L1.

**L3. Error responses include Python exception class names.**
e.g. `{"error": "ValueError: ..."}`. Minor information disclosure;
nothing security-relevant exposed (no stack traces, no paths).

## Verified safe

- ✅ API keys are in `.env` (gitignored) and Cloud Run env vars only;
  never in source, build context, or HTTP responses.
- ✅ `.dockerignore` excludes `.env` from the image layer.
- ✅ Single MCP server process serializes file access via
  `threading.Lock`; no race conditions on `world_state.json`.
- ✅ Cloud Run `--max-instances 1` prevents distributed state
  corruption.
- ✅ No file-path traversal vectors (no user input ever becomes a
  filesystem path).
- ✅ No SQL or NoSQL injection vectors (no DB).
- ✅ CORS not explicitly configured; FastAPI default is same-origin,
  which is correct for our case (frontend and API on same host).

## Operator checklist before sharing the URL publicly

1. ☐ Set a **hard monthly spending cap** on the OpenAI account
   (Settings → Limits → Hard limit).
2. ☐ Set a **monthly spending alert** on the Anthropic Console
   account.
3. ☐ Only share the URL with people who should have it (graders,
   reviewers). Don't post to social.
4. ☐ Monitor Cloud Run request volume in the GCP console occasionally.
   If you see sustained unexpected traffic, take the service down
   (`gcloud run services delete rpg-engine --region us-central1`).
