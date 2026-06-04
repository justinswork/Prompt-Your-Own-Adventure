// Prompt Your Own Adventure — vanilla frontend.
// Talks to /api/* on the FastAPI orchestrator. No build step.

const $ = (id) => document.getElementById(id);

let currentScenario = null;     // generated scenario awaiting accept
let currentNarrator = null;     // active narrator model
let turnHistory = [];           // [{turn, action, prose, fromAuto}]
let viewIndex = -1;             // index into turnHistory currently shown
let companionHistory = [];      // [{role: "user"|"assistant", content}]
let companionAutoDismissed = false;  // one-shot: dismiss after first action

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

function getSelectedDifficulty() {
  const checked = document.querySelector(
    'input[name="difficulty"]:checked'
  );
  return checked ? checked.value : null;
}

async function rerollScenario() {
  const difficulty = getSelectedDifficulty();
  if (!difficulty) {
    // Nothing chosen yet — leave the setup screen idle.
    $("scenario-card").classList.add("hidden");
    $("btn-accept").disabled = true;
    return;
  }
  // Lock the difficulty radios during the in-flight roll so the user
  // can't change difficulty mid-fetch and get a scenario that doesn't
  // match the new selection.
  const radios = document.querySelectorAll('input[name="difficulty"]');
  radios.forEach((r) => { r.disabled = true; });
  setBusy("setup-loading", ["btn-reroll", "btn-accept"], true);
  $("scenario-card").classList.add("hidden");
  try {
    currentScenario = await jpost("/api/scenario", { difficulty });
    const rolledDifficulty = currentScenario.difficulty || difficulty;
    const diffTag = $("s-difficulty");
    diffTag.textContent = rolledDifficulty;
    diffTag.className = "difficulty-tag diff-" + rolledDifficulty;
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

    const eul = $("s-enemies");
    eul.innerHTML = "";
    const enemies = Array.isArray(currentScenario.starting_enemies)
      ? currentScenario.starting_enemies
      : [];
    if (enemies.length === 0) {
      const li = document.createElement("li");
      li.className = "hint";
      li.textContent = "(none — quiet start)";
      eul.appendChild(li);
    } else {
      for (const e of enemies) {
        const li = document.createElement("li");
        li.innerHTML = `<strong>${escapeHtml(e.name)}</strong> ` +
          `<span class="hint">[threat: ${escapeHtml(e.threat || "medium")}, hp ${e.hp}]</span><br>` +
          `<span class="hint">${escapeHtml(e.description || "")}</span>`;
        eul.appendChild(li);
      }
    }

    $("scenario-card").classList.remove("hidden");
    $("btn-accept").disabled = false;
  } catch (e) {
    alert("Scenario gen failed: " + e.message);
  } finally {
    setBusy("setup-loading", ["btn-reroll", "btn-accept"], false);
    radios.forEach((r) => { r.disabled = false; });
  }
}

async function acceptScenario() {
  if (!currentScenario) return;
  setBusy("setup-loading", ["btn-reroll", "btn-accept"], true);
  try {
    const { state } = await jpost("/api/accept", currentScenario);
    renderState(state);
    resetNarrationHistory();
    $("telemetry-log").innerHTML = "";
    show("game-screen");
    resetCompanionChat(state.companion);
    setCompanionPaneState("expanded");
    companionAutoDismissed = false;
    $("action-input").focus();
  } catch (e) {
    alert("Accept failed: " + e.message);
  } finally {
    setBusy("setup-loading", ["btn-reroll", "btn-accept"], false);
  }
}

// ---------- Companion ----------------------------------------------------

function setCompanionPaneState(state) {
  // state: "expanded" | "collapsed" | "none"
  document.body.classList.remove("companion-expanded", "companion-collapsed");
  if (state === "expanded") document.body.classList.add("companion-expanded");
  else if (state === "collapsed") document.body.classList.add("companion-collapsed");
}

function renderCompanionMeta(companion) {
  if (!companion) return;
  $("companion-avatar").textContent = companion.avatar || "🧭";
  $("companion-name").textContent = companion.name || "Companion";
  $("companion-persona").textContent = companion.persona || "";
  $("companion-rail-avatar").textContent = companion.avatar || "🧭";
}

function appendCompanionMessage(role, content, opts = {}) {
  const list = $("companion-messages");
  const bubble = document.createElement("div");
  bubble.className = "msg appear " + (role === "user" ? "msg-user" : "msg-companion");
  if (opts.thinking) bubble.classList.add("thinking");
  bubble.textContent = content;
  list.appendChild(bubble);
  list.scrollTop = list.scrollHeight;
  return bubble;
}

function resetCompanionChat(companion) {
  companionHistory = [];
  $("companion-messages").innerHTML = "";
  renderCompanionMeta(companion);
  // Seed the chat with the companion's greeting.
  if (companion && companion.greeting) {
    appendCompanionMessage("assistant", companion.greeting);
    companionHistory.push({ role: "assistant", content: companion.greeting });
  }
}

async function askCompanion(question) {
  const q = (question || "").trim();
  if (!q) return;
  // Show user bubble immediately + a thinking placeholder.
  appendCompanionMessage("user", q);
  companionHistory.push({ role: "user", content: q });
  const thinkingEl = appendCompanionMessage("assistant", "…", { thinking: true });
  setCompanionBusy(true);

  try {
    const { reply, companion } = await jpost("/api/companion/ask", {
      question: q,
      history: companionHistory.slice(0, -1),  // exclude the just-added user msg (server adds it)
    });
    // Replace the thinking bubble with the real reply.
    thinkingEl.classList.remove("thinking");
    thinkingEl.textContent = reply;
    companionHistory.push({ role: "assistant", content: reply });
    // Refresh meta in case difficulty/persona drifted (defensive).
    if (companion) renderCompanionMeta(companion);
  } catch (e) {
    thinkingEl.classList.remove("thinking");
    thinkingEl.textContent = "(I can't hear you right now: " + e.message + ")";
  } finally {
    setCompanionBusy(false);
    $("companion-input").value = "";
    $("companion-input").focus();
  }
}

function setCompanionBusy(busy) {
  $("btn-companion-ask").disabled = busy;
  $("companion-input").disabled = busy;
  document.querySelectorAll(".quick-prompt").forEach((b) => { b.disabled = busy; });
}

// ---------- Game screen ------------------------------------------------------

function hpColorClass(hp, maxHp) {
  const pct = (hp / maxHp) * 100;
  if (pct >= 60) return "hp-full";
  if (pct >= 25) return "hp-mid";
  return "hp-low";
}

function renderState(state) {
  const hp = state.player_status.health;
  const hpMax = 100;
  const hpEl = $("hp");
  hpEl.textContent = hp;
  hpEl.className = hpColorClass(hp, hpMax);
  const hpPct = Math.max(0, Math.min(100, (hp / hpMax) * 100));
  $("hp-bar-fill").style.width = hpPct + "%";
  $("turn").textContent = state.player_status.turn_count;
  $("location").textContent = state.current_location;
  $("genre").textContent = state.genre;
  $("objective").textContent = state.objective;
  const invEl = $("inventory");
  invEl.innerHTML = "";
  if (state.inventory.length === 0) {
    const li = document.createElement("li");
    li.className = "inv-empty";
    li.textContent = "(empty)";
    invEl.appendChild(li);
  } else {
    for (const item of state.inventory) {
      const li = document.createElement("li");
      li.textContent = item;
      invEl.appendChild(li);
    }
  }

  const difficulty = state.difficulty || "normal";
  const badge = $("difficulty-badge");
  badge.textContent = difficulty;
  badge.className = "difficulty-tag diff-" + difficulty;

  renderEnemies(state.enemies || [], state.current_location);
}

function renderEnemies(enemies, currentLocation) {
  const container = $("enemies-list");
  container.innerHTML = "";
  if (enemies.length === 0) {
    // CSS :empty::after handles the visual empty state
    return;
  }
  for (const e of enemies) {
    const pct = Math.max(0, Math.min(100, (e.hp / e.max_hp) * 100));
    const here = e.location === currentLocation;
    const row = document.createElement("div");
    row.className = "enemy-row" + (here ? " here" : "");
    const threat = e.threat || "medium";
    const locText = here
      ? `📍 here · ${escapeHtml(e.location)}`
      : `📍 ${escapeHtml(e.location)}`;
    row.innerHTML = `
      <div class="enemy-line">
        <span class="enemy-name">${escapeHtml(e.name)}</span>
        <span class="enemy-threat threat-${escapeHtml(threat)}" title="Threat level">
          Threat: ${escapeHtml(threat)}
        </span>
        <span class="enemy-hp ${hpColorClass(e.hp, e.max_hp)}" title="Current / max HP">
          <strong>${e.hp}</strong><span class="hp-max">/${e.max_hp}</span>
        </span>
      </div>
      <div class="enemy-sub" title="Enemy location">${locText}</div>
      <div class="enemy-bar"><div class="enemy-bar-fill" style="width:${pct}%"></div></div>`;
    container.appendChild(row);
  }
}

function appendNarration(turn, action, prose, fromAuto) {
  turnHistory.push({ turn, action, prose, fromAuto: !!fromAuto });
  viewIndex = turnHistory.length - 1;
  renderCurrentTurn();
}

function renderCurrentTurn() {
  const display = $("narration-display");
  const counter = $("narration-counter");
  const prev = $("btn-prev-turn");
  const next = $("btn-next-turn");

  if (turnHistory.length === 0) {
    display.innerHTML = `<div class="narration-empty hint">Submit an action below to begin.</div>`;
    counter.textContent = "no turns yet";
    prev.disabled = true;
    next.disabled = true;
    return;
  }

  const entry = turnHistory[viewIndex];
  const autoBadge = entry.fromAuto ? ` <span class="auto-badge">auto</span>` : "";
  display.innerHTML = `
    <div class="turn-entry appear">
      <div class="turn-tag">Turn ${entry.turn}${autoBadge}</div>
      <div class="action">› ${escapeHtml(entry.action)}</div>
      <div class="prose">${escapeHtml(entry.prose)}</div>
    </div>`;
  display.scrollTop = 0;

  counter.textContent = `Turn ${entry.turn} of ${turnHistory.length}`;
  prev.disabled = viewIndex <= 0;
  next.disabled = viewIndex >= turnHistory.length - 1;
}

function navigateTurn(delta) {
  const next = viewIndex + delta;
  if (next < 0 || next >= turnHistory.length) return;
  viewIndex = next;
  renderCurrentTurn();
}

function resetNarrationHistory() {
  turnHistory = [];
  viewIndex = -1;
  renderCurrentTurn();
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
    case "agent": {
      const iter = String(event.iteration).padStart(1, "0");
      const tag = event.final
        ? `[AGENT iter${iter} ✓]`
        : `[AGENT iter${iter}  ]`;
      const callSummary = (event.tool_calls && event.tool_calls.length)
        ? event.tool_calls
            .map((tc) => `${tc.name}(${JSON.stringify(tc.args)})`)
            .join(", ")
        : (event.final ? "(no tool call — agent done)" : "(no tool call)");
      html = `<span class="tag">${escapeHtml(tag)}</span><span class="meta">${escapeHtml(callSummary)}</span>`;
      if (event.text) {
        html += `<span class="payload">“${escapeHtml(event.text)}”</span>`;
      }
      break;
    }
    case "mcp_client": {
      const srcTag = event.source && event.source !== "orchestrator"
        ? ` <em class="src">[${escapeHtml(event.source)}]</em>` : "";
      html = `<span class="tag">[MCP CLIENT ]</span><span class="meta">→ ${escapeHtml(event.tool)}${srcTag}</span><span class="payload">${escapeHtml(JSON.stringify(event.payload))}</span>`;
      break;
    }
    case "mcp_server": {
      html = `<span class="tag">[MCP SERVER ]</span><span class="meta">← ${escapeHtml(event.status)}  ${escapeHtml(event.summary || "")}</span>`;
      break;
    }
    case "fallback": {
      html = `<span class="tag">[FALLBACK   ]</span><span class="meta">${escapeHtml(event.note || "")}</span>`;
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
  turnBlock.scrollIntoView({ block: "nearest", behavior: "smooth" });
  for (const e of events) {
    const eventEl = renderTelemetryEvent(e);
    turnBlock.appendChild(eventEl);
    eventEl.scrollIntoView({ block: "nearest", behavior: "smooth" });
    await new Promise((r) => setTimeout(r, 220));
  }
}

async function submitAction(action, opts = {}) {
  setBusy("turn-loading", ["btn-submit", "btn-auto"], true);
  let ended = false;
  try {
    const resp = await jpost("/api/turn", { action });
    const turn = resp.state.player_status.turn_count;

    await streamTelemetry(resp.telemetry, turn);
    renderState(resp.state);
    appendNarration(turn, action, resp.narration, opts.fromAuto);

    ended = resp.ended;
    if (ended) {
      await endGame(resp.end_reason, resp.victory);
    }
    // Auto-dismiss the companion pane to its rail after the player's
    // first action — they've started playing, they don't need the
    // onboarding chat in their face anymore. They can summon it again
    // anytime via the rail.
    if (!companionAutoDismissed) {
      companionAutoDismissed = true;
      setCompanionPaneState("collapsed");
    }
  } catch (e) {
    alert("Turn failed: " + e.message);
  } finally {
    setBusy("turn-loading", ["btn-submit", "btn-auto"], false);
    $("action-input").value = "";
    if (!ended) $("action-input").focus();
    // keep inspector counters & recent-calls list current
    refreshInspector().catch(() => {});
  }
  return ended;
}

async function autoAction(opts = {}) {
  if ($("btn-submit").disabled && !opts.force) return false;
  setBusy("turn-loading", ["btn-submit", "btn-auto"], true);
  let action;
  try {
    const r = await jpost("/api/auto-action");
    action = r.action;
    pushAutoActionEvent(action, r.model);
    $("action-input").value = action;
  } catch (e) {
    alert("Auto-action failed: " + e.message);
    setBusy("turn-loading", ["btn-submit", "btn-auto"], false);
    return false;
  }
  // brief reveal so the user sees what was chosen before it submits
  await new Promise((r) => setTimeout(r, 350));
  setBusy("turn-loading", ["btn-submit", "btn-auto"], false);
  const ended = await submitAction(action, { fromAuto: true });

  if (!ended && $("cb-autoplay").checked) {
    await new Promise((r) => setTimeout(r, 700));
    if ($("cb-autoplay").checked) await autoAction();
  }
  return ended;
}

function pushAutoActionEvent(action, model) {
  const container = $("telemetry-log");
  const block = document.createElement("div");
  block.className = "telemetry-turn appear";
  block.innerHTML = `
    <div class="event router">
      <span class="tag">[MODEL ROUTER]</span>
      <span class="meta">auto-action ← ${escapeHtml(model)}  "${escapeHtml(action)}"</span>
    </div>`;
  container.appendChild(block);
  block.scrollIntoView({ block: "nearest", behavior: "smooth" });
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
  block.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

// ---------- MCP side pane ----------------------------------------------------

function toggleMcpPane(forceCollapse) {
  const rail = $("btn-mcp-expand");
  const collapseNow = forceCollapse !== undefined
    ? forceCollapse
    : !document.body.classList.contains("mcp-collapsed");
  document.body.classList.toggle("mcp-collapsed", collapseNow);
  rail.classList.toggle("hidden", !collapseNow);
}

async function refreshInspector() {
  let data;
  try {
    data = await jget("/api/mcp-inspect");
  } catch (e) {
    alert("MCP inspect failed: " + e.message);
    return;
  }

  $("insp-server-name").textContent = data.server_name + " · " + data.transport;
  $("insp-server-url").textContent = data.server_url;
  $("insp-uptime").textContent = data.uptime_s;
  $("insp-call-count").textContent = data.call_count;
  $("insp-tools-count").textContent = `(${data.tools.length})`;
  $("insp-recent-count").textContent = `(${data.recent_calls.length})`;

  // Tools
  const toolsEl = $("insp-tools");
  toolsEl.innerHTML = "";
  const sel = $("insp-tool-select");
  const previousSelection = sel.value;
  sel.innerHTML = "";
  for (const t of data.tools) {
    const card = document.createElement("div");
    card.className = "tool-card";
    card.innerHTML = `
      <div class="tool-name">${escapeHtml(t.name)}()</div>
      <div class="tool-desc">${escapeHtml(t.description || "(no description)")}</div>
      <details>
        <summary>inputSchema</summary>
        <pre>${escapeHtml(JSON.stringify(t.inputSchema, null, 2))}</pre>
      </details>`;
    toolsEl.appendChild(card);

    const opt = document.createElement("option");
    opt.value = t.name;
    opt.textContent = t.name;
    sel.appendChild(opt);
  }
  if (previousSelection) sel.value = previousSelection;

  // Recent calls (newest first)
  const recentEl = $("insp-recent");
  recentEl.innerHTML = "";
  if (data.recent_calls.length === 0) {
    recentEl.innerHTML = `<div class="hint">No calls yet. Play a turn or use the manual invoker.</div>`;
  } else {
    for (const c of [...data.recent_calls].reverse()) {
      const row = document.createElement("div");
      row.className = "call-row";
      const d = new Date(c.ts * 1000);
      const hh = String(d.getHours()).padStart(2, "0");
      const mm = String(d.getMinutes()).padStart(2, "0");
      const ss = String(d.getSeconds()).padStart(2, "0");
      const msCls = !c.ok ? "err" : c.elapsed_ms > 200 ? "slow" : "";
      const srcTag = c.source && c.source !== "orchestrator"
        ? `<span class="src-tag">${escapeHtml(c.source)}</span>` : "";
      const preview = c.ok
        ? escapeHtml(JSON.stringify(c.args))
        : escapeHtml(c.error || "(error)");
      row.innerHTML = `
        <span class="time">${hh}:${mm}:${ss}</span>
        <span class="name">${escapeHtml(c.tool)}${srcTag}</span>
        <span class="ms ${msCls}">${c.elapsed_ms}ms</span>
        <span class="preview">${preview}</span>`;
      recentEl.appendChild(row);
    }
  }
}

async function invokeManually() {
  const tool = $("insp-tool-select").value;
  let args;
  try {
    const raw = $("insp-args").value.trim() || "{}";
    args = JSON.parse(raw);
  } catch (e) {
    const out = $("insp-result");
    out.classList.add("err");
    out.textContent = "Invalid JSON args: " + e.message;
    return;
  }
  const out = $("insp-result");
  out.classList.remove("err");
  out.textContent = "(calling " + tool + "...)";
  try {
    const r = await jpost("/api/mcp-call", { tool, args });
    if (r.ok) {
      out.textContent = JSON.stringify(r.result, null, 2);
    } else {
      out.classList.add("err");
      out.textContent = r.error;
    }
  } catch (e) {
    out.classList.add("err");
    out.textContent = e.message;
  }
  // refresh the recent-calls list to show the call we just made
  await refreshInspector();
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
  $("btn-auto").addEventListener("click", () => autoAction());
  $("cb-autoplay").addEventListener("change", () => {
    if ($("cb-autoplay").checked && !$("btn-submit").disabled) autoAction();
  });
  $("btn-restart").addEventListener("click", () => {
    currentScenario = null;
    $("btn-accept").disabled = true;
    $("btn-reroll").disabled = true;
    // Clear any prior difficulty selection so the user has to pick again.
    document.querySelectorAll('input[name="difficulty"]').forEach((r) => {
      r.checked = false;
    });
    $("scenario-card").classList.add("hidden");
    // Clear companion + reset narration for the next playthrough.
    setCompanionPaneState("none");
    companionHistory = [];
    companionAutoDismissed = false;
    $("companion-messages").innerHTML = "";
    resetNarrationHistory();
    show("setup-screen");
  });

  // Picking a difficulty (initially or changing) rolls a fresh scenario
  // calibrated to that difficulty.
  document.querySelectorAll('input[name="difficulty"]').forEach((r) => {
    r.addEventListener("change", () => {
      currentScenario = null;
      $("btn-reroll").disabled = false;
      rerollScenario();
    });
  });

  // Narration pagination
  $("btn-prev-turn").addEventListener("click", () => navigateTurn(-1));
  $("btn-next-turn").addEventListener("click", () => navigateTurn(1));

  // Companion chat
  $("companion-form").addEventListener("submit", (e) => {
    e.preventDefault();
    askCompanion($("companion-input").value);
  });
  document.querySelectorAll(".quick-prompt").forEach((btn) => {
    btn.addEventListener("click", () => askCompanion(btn.dataset.prompt));
  });
  $("btn-companion-collapse").addEventListener("click",
    () => setCompanionPaneState("collapsed"));
  $("btn-companion-expand").addEventListener("click",
    () => setCompanionPaneState("expanded"));

  // MCP side pane
  $("btn-mcp-collapse").addEventListener("click", () => toggleMcpPane(true));
  $("btn-mcp-expand").addEventListener("click", () => toggleMcpPane(false));
  $("insp-refresh").addEventListener("click", refreshInspector);
  $("insp-call").addEventListener("click", invokeManually);
  $("insp-tool-select").addEventListener("change", () => {
    $("insp-args").value = "{}";
    $("insp-result").textContent = "";
    $("insp-result").classList.remove("err");
  });

  // initial inspector load so server info + tool list show up before any turn
  refreshInspector().catch((e) => console.warn("initial inspector load failed:", e));

  // Start with the MCP pane collapsed — game takes full width by default.
  toggleMcpPane(true);

  show("setup-screen");
  // No initial reroll — wait for the user to pick a difficulty.
}

init();
