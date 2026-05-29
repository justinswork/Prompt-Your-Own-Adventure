// Prompt Your Own Adventure — vanilla frontend.
// Talks to /api/* on the FastAPI orchestrator. No build step.

const $ = (id) => document.getElementById(id);

let currentScenario = null;     // generated scenario awaiting accept
let currentNarrator = null;     // active narrator model

// ---------- API helpers ------------------------------------------------------

async function jpost(path, body) {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body !== undefined ? JSON.stringify(body) : "{}",
  });
  if (!r.ok) {
    const text = await r.text().catch(() => "");
    throw new Error(`${path} → ${r.status} ${text}`);
  }
  return r.json();
}

async function jget(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${path} → ${r.status}`);
  return r.json();
}

// ---------- UI helpers -------------------------------------------------------

function show(screenId) {
  for (const s of document.querySelectorAll(".screen")) s.classList.add("hidden");
  $(screenId).classList.remove("hidden");
}

function setBusy(loaderId, btnIds, busy) {
  $(loaderId).classList.toggle("hidden", !busy);
  for (const id of btnIds) $(id).disabled = busy;
}

function escapeHtml(s) {
  return String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

// ---------- Setup screen -----------------------------------------------------

async function rerollScenario() {
  setBusy("setup-loading", ["btn-reroll", "btn-accept"], true);
  $("scenario-card").classList.add("hidden");
  try {
    currentScenario = await jpost("/api/scenario");
    $("s-genre").textContent = currentScenario.genre;
    $("s-location").textContent = currentScenario.location;
    $("s-objective").textContent = currentScenario.objective;
    const ul = $("s-items");
    ul.innerHTML = "";
    for (const item of currentScenario.starting_items) {
      const li = document.createElement("li");
      li.textContent = item;
      ul.appendChild(li);
    }
    $("scenario-card").classList.remove("hidden");
    $("btn-accept").disabled = false;
  } catch (e) {
    alert("Scenario gen failed: " + e.message);
  } finally {
    setBusy("setup-loading", ["btn-reroll", "btn-accept"], false);
  }
}

async function acceptScenario() {
  if (!currentScenario) return;
  setBusy("setup-loading", ["btn-reroll", "btn-accept"], true);
  try {
    const { state } = await jpost("/api/accept", currentScenario);
    renderState(state);
    $("narration-log").innerHTML = "";
    $("telemetry-log").innerHTML = "";
    show("game-screen");
    $("action-input").focus();
  } catch (e) {
    alert("Accept failed: " + e.message);
  } finally {
    setBusy("setup-loading", ["btn-reroll", "btn-accept"], false);
  }
}

// ---------- Game screen ------------------------------------------------------

function renderState(state) {
  $("hp").textContent = state.player_status.health;
  $("turn").textContent = state.player_status.turn_count;
  $("location").textContent = state.current_location;
  $("genre").textContent = state.genre;
  $("objective").textContent = state.objective;
  $("inventory").textContent = state.inventory.length
    ? state.inventory.join(", ")
    : "(empty)";
}

function appendNarration(turn, action, prose) {
  const div = document.createElement("div");
  div.className = "turn-entry appear";
  div.innerHTML = `
    <div class="turn-tag">Turn ${turn}</div>
    <div class="action">› ${escapeHtml(action)}</div>
    <div class="prose">${escapeHtml(prose)}</div>
  `;
  const log = $("narration-log");
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}

function renderTelemetryEvent(event) {
  const div = document.createElement("div");
  div.className = "event " + event.stage + " appear";
  let html = "";
  switch (event.stage) {
    case "router": {
      const meta = event.intent
        ? `intent=${event.intent}  ${event.reason || ""}`
        : `→ ${event.model}  ${event.note || ""}`;
      html = `<span class="tag">[MODEL ROUTER]</span><span class="meta">model=${escapeHtml(event.model)}  ${escapeHtml(meta)}</span>`;
      break;
    }
    case "mcp_client": {
      html = `<span class="tag">[MCP CLIENT ]</span><span class="meta">→ ${escapeHtml(event.tool)}</span><span class="payload">${escapeHtml(JSON.stringify(event.payload))}</span>`;
      break;
    }
    case "mcp_server": {
      html = `<span class="tag">[MCP SERVER ]</span><span class="meta">← ${escapeHtml(event.status)}  ${escapeHtml(event.summary || "")}</span>`;
      break;
    }
    default:
      html = `<span class="tag">[${escapeHtml(event.stage)}]</span><span class="meta">${escapeHtml(JSON.stringify(event))}</span>`;
  }
  div.innerHTML = html;
  return div;
}

async function streamTelemetry(events, turnNumber) {
  const container = $("telemetry-log");
  const turnBlock = document.createElement("div");
  turnBlock.className = "telemetry-turn appear";
  turnBlock.innerHTML = `<div class="turn-header">⚙ TELEMETRY :: TURN ${String(turnNumber).padStart(2, "0")}</div>`;
  container.appendChild(turnBlock);
  for (const e of events) {
    turnBlock.appendChild(renderTelemetryEvent(e));
    container.scrollTop = container.scrollHeight;
    await new Promise((r) => setTimeout(r, 220));
  }
}

async function submitAction(action) {
  setBusy("turn-loading", ["btn-submit"], true);
  try {
    const resp = await jpost("/api/turn", { action });
    const turn = resp.state.player_status.turn_count;

    await streamTelemetry(resp.telemetry, turn);
    renderState(resp.state);
    appendNarration(turn, action, resp.narration);

    if (resp.ended) {
      await endGame(resp.end_reason, resp.victory);
    }
  } catch (e) {
    alert("Turn failed: " + e.message);
  } finally {
    setBusy("turn-loading", ["btn-submit"], false);
    $("action-input").value = "";
    $("action-input").focus();
  }
}

async function swapNarrator(model) {
  try {
    const r = await jpost("/api/swap", { model });
    currentNarrator = r.narrator;
    pushRouterSwapEvent(model);
  } catch (e) {
    alert("Swap failed: " + e.message);
  }
}

function pushRouterSwapEvent(model) {
  const container = $("telemetry-log");
  const block = document.createElement("div");
  block.className = "telemetry-turn appear";
  block.innerHTML = `
    <div class="event router">
      <span class="tag">[MODEL ROUTER]</span>
      <span class="meta">narrator swapped → ${escapeHtml(model)}</span>
    </div>`;
  container.appendChild(block);
  container.scrollTop = container.scrollHeight;
}

// ---------- Climax screen ----------------------------------------------------

async function endGame(reason, _victory) {
  try {
    const r = await jpost("/api/climax");
    const headline = $("end-headline");
    if (r.victory) {
      headline.textContent = "✦ VICTORY ✦";
      headline.className = "victory";
    } else {
      headline.textContent = "✦ GAME OVER ✦";
      headline.className = "defeat";
    }
    $("end-narration").textContent = r.narration;
    $("end-stats").textContent =
      `final hp=${r.state.player_status.health}  ` +
      `turns=${r.state.player_status.turn_count}  ` +
      `objective_item=${r.state.has_objective_item}  ` +
      `inventory=[${r.state.inventory.join(", ")}]  ` +
      `reason=${reason}  narrator=${r.narrator_model}`;
    show("end-screen");
  } catch (e) {
    alert("Climax narration failed: " + e.message);
  }
}

// ---------- Bootstrap --------------------------------------------------------

async function init() {
  try {
    const health = await jget("/api/health");
    currentNarrator = health.narrator;
    const sel = $("narrator-select");
    sel.innerHTML = "";
    for (const m of health.narrator_choices) {
      const opt = document.createElement("option");
      opt.value = m;
      opt.textContent = m;
      if (m === currentNarrator) opt.selected = true;
      sel.appendChild(opt);
    }
    sel.addEventListener("change", () => swapNarrator(sel.value));
  } catch (e) {
    console.error("init /api/health failed:", e);
  }

  $("btn-reroll").addEventListener("click", rerollScenario);
  $("btn-accept").addEventListener("click", acceptScenario);
  $("action-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const a = $("action-input").value.trim();
    if (a) submitAction(a);
  });
  $("btn-restart").addEventListener("click", async () => {
    currentScenario = null;
    $("btn-accept").disabled = true;
    show("setup-screen");
    rerollScenario();
  });

  show("setup-screen");
  rerollScenario();
}

init();
