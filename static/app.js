// Agent Portal frontend: agent list w/ polling for in-flight creates/deletes,
// a create-agent modal, and a chat view -- one page, no build step, no
// framework, matching every demo_web.html in this overall project.

const MODELS_BY_PLATFORM = {
  gemini: ["gemini-2.5-flash", "gemini-2.5-pro"],
  azure: ["gpt-4.1-mini"],
  aws: ["us.anthropic.claude-haiku-4-5-20251001-v1:0"],
};

let config = { platforms: {}, tools: {}, mcp_servers: {}, deployment_modes: {} };
let agents = [];
let pollTimer = null;
let ws = null;
// The latency card's click handler is re-bound fresh each openChat() call
// (it closes over that session's own latencyCardCollapsed/selectTurn), so
// the previous one has to be explicitly removed first -- traceBody itself
// persists across chat sessions (it's not recreated the way `ws` is), so
// without this, repeatedly opening chat (or Back -> Chat again) would
// stack up a new listener on every call instead of replacing it.
let traceBodyClickHandler = null;

const agentListEl = document.getElementById("agent-list");
const agentListView = document.getElementById("agent-list-view");
const chatView = document.getElementById("chat-view");
const chatBody = document.getElementById("chat-body");
const chatForm = document.getElementById("chat-form");
const chatInput = document.getElementById("chat-input");
const sendBtn = document.getElementById("send-btn");
const chatAgentName = document.getElementById("chat-agent-name");
const chatAgentPlatform = document.getElementById("chat-agent-platform");
const tracePane = document.getElementById("trace-pane");
const toggleTraceBtn = document.getElementById("toggle-trace-btn");
const turnsBody = document.getElementById("turns-body");
const traceBody = document.getElementById("trace-body");
const traceTitle = document.getElementById("trace-title");
const refreshTraceBtn = document.getElementById("refresh-trace-btn");

const modalOverlay = document.getElementById("modal-overlay");
const modalTitle = document.getElementById("modal-title");
const newAgentForm = document.getElementById("new-agent-form");
const formError = document.getElementById("form-error");
const platformSelect = document.getElementById("f-platform");
const platformNote = document.getElementById("f-platform-note");
const modelSelect = document.getElementById("f-model");
const deploymentModeField = document.getElementById("f-deployment-mode-field");
const deploymentModeSelect = document.getElementById("f-deployment-mode");
const deploymentModeNote = document.getElementById("f-deployment-mode-note");
const regionField = document.getElementById("f-region-field");
const regionSelect = document.getElementById("f-region");
const regionNote = document.getElementById("f-region-note");
const runtimeVersionField = document.getElementById("f-runtime-version-field");
const runtimeVersionSelect = document.getElementById("f-runtime-version");
const toolOptions = document.getElementById("tool-options");
const mcpOptions = document.getElementById("mcp-options");
const createBtn = document.getElementById("create-btn");
const cancelBtn = document.getElementById("cancel-btn");

// The same modal/form serves both "New Agent" and "Edit Agent" -- every
// field it has is exactly the set create_agent() accepts, so there's no
// separate edit-specific subset to maintain. null means "New Agent" mode;
// otherwise the agent being edited, used both to prefill the form and,
// on submit, to know a delete-then-recreate is needed rather than a plain
// create. See openEditModal() and the submit handler below.
let editingAgent = null;

async function loadConfig() {
  config = await (await fetch("/api/config")).json();
  platformSelect.innerHTML = "";
  for (const [id, p] of Object.entries(config.platforms)) {
    const opt = document.createElement("option");
    opt.value = id;
    // A platform can be unpickable for two different reasons -- not built yet,
    // or built but not configured in this checkout -- and only the backend
    // knows which (see server.py's _platform_status). An <option> has room for
    // neither sentence, so it gets a neutral suffix plus the real reason as a
    // tooltip, and unavailablePlatformNote() below spells them out in full.
    opt.textContent = p.available ? p.label : `${p.label} (unavailable)`;
    opt.disabled = !p.available;
    if (!p.available) opt.title = p.unavailable_reason || "";
    platformSelect.appendChild(opt);
  }
  const firstAvailable = Object.entries(config.platforms).find(([, p]) => p.available);
  if (firstAvailable) platformSelect.value = firstAvailable[0];
  updateModelOptions();
  renderToolOptions();
  updateDeploymentModeField();
  updateRuntimeVersionField();
  renderPlatformNote();

  mcpOptions.innerHTML = "";
  const mcpEntries = Object.entries(config.mcp_servers);
  if (!mcpEntries.length) {
    mcpOptions.innerHTML = `<div class="tool-desc">No MCP servers configured.</div>`;
  }
  for (const [id, s] of mcpEntries) {
    mcpOptions.appendChild(catalogOptionRow(id, s));
  }
}

// The unavailable platforms' reasons, listed under the Platform select. The
// select itself can only grey those options out, which answers "why can't I
// pick this" with nothing actionable -- and if *none* is configured (a fresh
// clone with an untouched .env) there'd otherwise be no explanation on screen
// at all for a form that can't be submitted.
function renderPlatformNote() {
  const blocked = Object.values(config.platforms).filter((p) => !p.available && p.unavailable_reason);
  platformNote.textContent = blocked.length
    ? `Unavailable: ${blocked.map((p) => `${p.label} — ${p.unavailable_reason}`).join(" ")}`
    : "";
}

// One checkbox row for a tool or an MCP server. An entry the backend reports as
// unavailable renders disabled with the fix shown in place of its description:
// the description sells a capability that isn't there, where the reason names
// the variable to go set. Built with createElement rather than an innerHTML
// template because these strings now include backend-supplied reasons.
function catalogOptionRow(id, entry) {
  const unavailable = entry.available === false;
  const row = document.createElement("label");
  row.className = unavailable ? "tool-option unavailable" : "tool-option";
  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.value = id;
  checkbox.disabled = unavailable;
  const label = document.createElement("span");
  label.className = "tool-label";
  label.textContent = unavailable ? `${entry.label} (not configured)` : entry.label;
  const description = document.createElement("span");
  description.className = "tool-desc";
  description.textContent = unavailable ? entry.unavailable_reason : entry.description;
  const text = document.createElement("div");
  text.append(label, description);
  row.append(checkbox, text);
  return row;
}

function updateModelOptions() {
  const models = MODELS_BY_PLATFORM[platformSelect.value] || [];
  modelSelect.innerHTML = models.map((m) => `<option value="${m}">${m}</option>`).join("");
}

// Rebuilt (not just hidden) on every platform change, so a tool restricted
// to a different platform can't stay checked-but-hidden and get submitted
// anyway -- see AVAILABLE_TOOLS' optional "platforms" allowlist in
// deployers/__init__.py.
function renderToolOptions() {
  const platform = platformSelect.value;
  toolOptions.innerHTML = "";
  for (const [id, t] of Object.entries(config.tools)) {
    if (t.platforms && !t.platforms.includes(platform)) continue;
    toolOptions.appendChild(catalogOptionRow(id, t));
  }
}

// How the same agent code gets packaged (AWS: a code zip run by AgentCore's
// managed runtime, or a container image built here and pushed to ECR) --
// offered only for a platform whose deployer actually has a choice to make,
// which the config says rather than this file assuming which platform that
// is. Rebuilt (not just hidden) per platform change for the same reason
// renderToolOptions is: a mode selected for one platform must not stay
// selected-but-hidden and get submitted from another.
function updateDeploymentModeField(selected) {
  const modes = config.platforms[platformSelect.value]?.deployment_modes || [];
  deploymentModeField.classList.toggle("hidden", !modes.length);
  deploymentModeSelect.innerHTML = modes
    .map((id) => `<option value="${id}">${config.deployment_modes[id]?.label || id}</option>`)
    .join("");
  if (selected && modes.includes(selected)) deploymentModeSelect.value = selected;
  updateDeploymentModeNote();
}

function updateDeploymentModeNote() {
  deploymentModeNote.textContent = config.deployment_modes[deploymentModeSelect.value]?.description || "";
}

deploymentModeSelect.addEventListener("change", updateDeploymentModeNote);

// Where the agent gets deployed, for a platform whose deployer offers a choice
// (AWS: any region the portal is configured for). Same shape and the same
// rebuild-per-platform reason as updateDeploymentModeField above: a region
// picked for one platform must not stay selected-but-hidden and get submitted
// from another. The first entry is the default, so a form that's never touched
// this field submits the region the portal would have used anyway.
function updateRegionField(selected) {
  const regions = config.platforms[platformSelect.value]?.regions || [];
  regionField.classList.toggle("hidden", !regions.length);
  regionSelect.innerHTML = regions.map((id) => `<option value="${id}">${id}</option>`).join("");
  if (selected && regions.includes(selected)) regionSelect.value = selected;
  updateRegionNote();
}

// Named for what it actually is -- a warning, on the one choice here whose
// consequences the form can't show: a region needs its own one-time setup
// (staging bucket, ECR repository for container mode, CloudWatch Transaction
// Search for traces), and the models offered above are US inference profiles.
function updateRegionNote() {
  const regions = config.platforms[platformSelect.value]?.regions || [];
  regionNote.textContent =
    regionSelect.value && regionSelect.value !== regions[0]
      ? `${regionSelect.value} needs its own one-time setup (staging bucket, and ECR if you pick a container deploy) and its own Transaction Search for traces -- see docs/aws.md's "Regions".`
      : "";
}

regionSelect.addEventListener("change", updateRegionNote);

// AgentCore Runtime's platform version is an AWS-only concept, so the field is
// shown only for that platform -- and reset to the V2 default whenever it's
// hidden, so a V1 left selected on a previous AWS create can't be submitted
// invisibly from a Gemini/Azure one (server.py ignores it for non-AWS
// platforms, but the form shouldn't be lying either way).
//
// Deliberately separate from the Deployment field above rather than folded
// into one control: the two are independent axes, and the pair is the point --
// a V1 container against a V2 code zip is the comparison worth running.
function updateRuntimeVersionField() {
  const isAws = platformSelect.value === "aws";
  runtimeVersionField.classList.toggle("hidden", !isAws);
  if (!isAws) runtimeVersionSelect.value = "V2";
}

platformSelect.addEventListener("change", () => {
  updateModelOptions();
  renderToolOptions();
  updateDeploymentModeField();
  updateRegionField();
  updateRuntimeVersionField();
});

// The badge/picker version of a deployment mode: short enough to sit next to
// a platform and model badge, where AVAILABLE_DEPLOYMENT_MODES' full label
// ("Code zip (managed runtime)") would crowd the card. Falls through to the
// raw id for a mode this frontend hasn't got a short name for, rather than
// showing nothing for an agent that does have one recorded.
const DEPLOYMENT_MODE_SHORT_LABELS = { code: "code zip", container: "container" };

function deploymentModeShortLabel(mode) {
  return DEPLOYMENT_MODE_SHORT_LABELS[mode] || mode;
}

// Where an agent actually lives, for the card badge and the load test's picker.
// Prefers the stored column, and falls back to the region field of an AWS ARN
// (arn:aws:bedrock-agentcore:us-west-2:...) so agents that predate that column
// -- and any imported by startup seeding, which doesn't record one -- still show
// where they are. "" when neither is available, which is every Gemini/Azure
// agent: their region isn't a per-agent property, so a badge would be noise on
// every card rather than information.
function agentRegion(a) {
  if (a.region) return a.region;
  const parts = (a.platform_resource_id || "").split(":");
  return parts.length > 3 && parts[0] === "arn" ? parts[3] : "";
}

function escapeHtml(s) {
  return s.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
}

function renderAgents() {
  if (!agents.length) {
    agentListEl.innerHTML = `<div class="empty-state">No agents yet -- click "+ New Agent" to deploy one.</div>`;
    return;
  }
  agentListEl.innerHTML = "";
  for (const a of agents) {
    const card = document.createElement("div");
    card.className = "card";
    const canChat = a.status === "active";
    // Editing (delete + recreate) needs the agent to be in a settled state --
    // racing it against an in-flight create or delete would either edit a
    // not-yet-real resource or fight the delete already in progress.
    const canEdit = a.status === "active" || a.status === "failed";
    const canDelete = a.status !== "deleting";
    card.innerHTML = `
      <div class="card-title">${escapeHtml(a.name)}</div>
      <div class="badges">
        <span class="badge platform-${a.platform}">${a.platform}</span>
        ${a.deployment_mode ? `<span class="badge">${escapeHtml(deploymentModeShortLabel(a.deployment_mode))}</span>` : ""}
        ${agentRegion(a) ? `<span class="badge">${escapeHtml(agentRegion(a))}</span>` : ""}
        <span class="badge">${escapeHtml(a.model)}</span>
        <span class="badge status-${a.status}">${a.status}</span>
      </div>
      <div class="card-desc">${escapeHtml(a.description || "(no description)")}</div>
      ${a.error_message ? `<div class="card-error">${escapeHtml(a.error_message)}</div>` : ""}
      <div class="card-actions">
        <button class="chat-btn" ${canChat ? "" : "disabled"}>Chat</button>
        <button class="secondary edit-btn" ${canEdit ? "" : "disabled"}>Edit</button>
        <button class="delete-btn" ${canDelete ? "" : "disabled"}>Delete</button>
      </div>
    `;
    card.querySelector(".chat-btn").onclick = () => openChat(a);
    card.querySelector(".edit-btn").onclick = () => openEditModal(a);
    card.querySelector(".delete-btn").onclick = () => deleteAgent(a);
    agentListEl.appendChild(card);
  }
}

async function refreshAgents() {
  agents = await (await fetch("/api/agents")).json();
  renderAgents();
  const inFlight = agents.some((a) => a.status === "creating" || a.status === "deleting");
  if (inFlight && !pollTimer) {
    pollTimer = setInterval(refreshAgents, 3000);
  } else if (!inFlight && pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

async function deleteAgent(agent) {
  if (!confirm(`Delete "${agent.name}"? This tears down the real hosted deployment.`)) return;
  await fetch(`/api/agents/${agent.id}`, { method: "DELETE" });
  refreshAgents();
}

// --- new/edit agent modal ---

function openCreateModal() {
  editingAgent = null;
  newAgentForm.reset();
  formError.style.display = "none";
  modalTitle.textContent = "New Agent";
  createBtn.textContent = "Create";
  updateModelOptions();
  renderToolOptions();
  updateDeploymentModeField();
  updateRegionField();
  updateRuntimeVersionField();
  modalOverlay.classList.remove("hidden");
}

function openEditModal(agent) {
  editingAgent = agent;
  newAgentForm.reset();
  formError.style.display = "none";
  modalTitle.textContent = `Edit "${agent.name}"`;
  createBtn.textContent = "Recreate";

  platformSelect.value = agent.platform;
  updateModelOptions();
  // Prefilled with what the agent was actually deployed as, when the portal
  // recorded it -- an agent imported by startup seeding (or created before
  // this was recorded) has no mode stored, and falls back to the platform's
  // default rather than a guess about someone else's deployment.
  updateDeploymentModeField(agent.deployment_mode);
  // Prefilled with where the agent actually is (its ARN, when no column was
  // recorded -- see agentRegion), so Recreate rebuilds it in the same region
  // instead of silently relocating it, which is exactly how a recreate once
  // deleted a us-west-2 agent and failed to recreate it in us-east-1. Changing
  // this select is now how you deliberately move an agent between regions.
  updateRegionField(agentRegion(agent));
  // The runtime version gets no such prefill, because unlike the deployment
  // mode it isn't recorded on the agent: get_agent_runtime is the only
  // authority on which version an existing runtime was created with, so
  // Recreate surfaces the visible V2 default instead of a guess.
  updateRuntimeVersionField();
  // The agent's current model may not be one of MODELS_BY_PLATFORM's
  // quick-pick options (e.g. one tried out ad hoc and never added there) --
  // add it so the select can actually show/keep it selected instead of
  // silently falling back to whichever option happens to be first.
  if (![...modelSelect.options].some((o) => o.value === agent.model)) {
    const opt = document.createElement("option");
    opt.value = agent.model;
    opt.textContent = agent.model;
    modelSelect.insertBefore(opt, modelSelect.firstChild);
  }
  modelSelect.value = agent.model;

  document.getElementById("f-name").value = agent.name;
  document.getElementById("f-description").value = agent.description || "";
  document.getElementById("f-agent-instructions").value = agent.agent_instructions || "";

  renderToolOptions();
  // `!cb.disabled &&` matters on an *edit*: an agent may have been created back
  // when a tool's endpoint was configured, and recreating it with that tool
  // still selected would just fail. Not silent -- the row is right there in the
  // form, greyed, saying which variable to set (see catalogOptionRow).
  for (const cb of toolOptions.querySelectorAll("input")) {
    cb.checked = !cb.disabled && agent.tools.includes(cb.value);
  }
  for (const cb of mcpOptions.querySelectorAll("input")) {
    cb.checked = !cb.disabled && agent.mcp_servers.includes(cb.value);
  }

  modalOverlay.classList.remove("hidden");
}

document.getElementById("new-agent-btn").onclick = openCreateModal;
cancelBtn.onclick = () => {
  modalOverlay.classList.add("hidden");
  editingAgent = null;
};

// Polls until the agent is actually gone (its DB row deleted) rather than
// assuming a fixed delay, since a real cloud delete's own completion time
// varies by platform. server.py's _run_undeploy only drops the row once
// the platform's own list_deployed() confirms the resource is gone (not
// just that undeploy()'s API call was accepted -- AWS's delete is
// documented as asynchronous, see docs/aws.md's "How AWS deploy works"), so
// this 404 is a real "safe to reuse the name" signal, not an optimistic
// one. Used by the Recreate flow below so the new create never races the
// old resource's name/slug still being torn down. Returns false (not an
// error) on timeout, e.g. because the delete itself failed server-side and
// left the agent active with an error_message -- the caller surfaces that
// as a message rather than silently proceeding to create a duplicate.
//
// The budget (7 min) deliberately outlasts server.py's own
// _wait_until_actually_gone (6 min): it must finish before the agent's DB
// row disappears, and a real AgentCore delete takes minutes.
async function waitForDeleted(agentId, { attempts = 210, intervalMs = 2000 } = {}) {
  for (let i = 0; i < attempts; i++) {
    const resp = await fetch(`/api/agents/${agentId}`);
    if (resp.status === 404) return true;
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
  return false;
}

// Runs the delete-then-create in the background after the modal has
// already closed, rather than blocking it on a real cloud teardown (which
// can take anywhere from seconds to tens of seconds -- see docs/aws.md's
// "How AWS deploy works" on DeleteAgentRuntime being asynchronous). The old
// agent's card shows "deleting" and then disappears through the normal
// refreshAgents() polling exactly like a plain Delete does; once the
// delete has actually completed, the new agent is created exactly like a
// plain New Agent submit, complete with its own "creating" card. The wait
// still matters even though it's non-blocking for the user: firing the
// create before the old resource is actually gone risks a real name/slug
// collision on the platform (see waitForDeleted's docstring).
async function recreateAgent(oldAgent, body) {
  try {
    await fetch(`/api/agents/${oldAgent.id}`, { method: "DELETE" });
    refreshAgents();
    const deleted = await waitForDeleted(oldAgent.id);
    if (!deleted) {
      alert(
        `"${oldAgent.name}" didn't finish deleting in time -- check its status in the list. ` +
          `The new agent was not created; you can retry Edit -> Recreate once it's gone.`
      );
      return;
    }
    const resp = await fetch("/api/agents", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!resp.ok) {
      const err = await resp.json();
      // The old agent is already gone at this point -- say so, rather than
      // a generic failure message that reads like nothing happened.
      alert(`Deleted "${oldAgent.name}", but recreating it failed: ${err.detail || "unknown error"}`);
      return;
    }
    refreshAgents();
  } catch (err) {
    alert(`Recreate failed: ${err.message}`);
  }
}

newAgentForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const tools = [...toolOptions.querySelectorAll("input:checked")].map((el) => el.value);
  const mcpServers = [...mcpOptions.querySelectorAll("input:checked")].map((el) => el.value);
  const body = {
    name: document.getElementById("f-name").value.trim(),
    platform: platformSelect.value,
    model: modelSelect.value,
    description: document.getElementById("f-description").value.trim(),
    agent_instructions: document.getElementById("f-agent-instructions").value.trim(),
    tools,
    mcp_servers: mcpServers,
    // null, not the select's leftover value, for a platform with no packaging
    // choice -- the field is hidden then, and the server rejects a mode its
    // platform doesn't support rather than quietly ignoring it.
    deployment_mode: deploymentModeField.classList.contains("hidden") ? null : deploymentModeSelect.value,
    // Same null-when-hidden rule and the same reason as deployment_mode above.
    region: regionField.classList.contains("hidden") ? null : regionSelect.value,
    runtime_version: platformSelect.value === "aws" ? runtimeVersionSelect.value : null,
  };

  if (editingAgent) {
    const oldAgent = editingAgent;
    const ok = confirm(
      `Delete "${oldAgent.name}" and recreate it with these settings? This tears down the real hosted deployment first.`
    );
    if (!ok) return;
    modalOverlay.classList.add("hidden");
    editingAgent = null;
    recreateAgent(oldAgent, body); // intentionally not awaited -- see its docstring
    return;
  }

  createBtn.disabled = true;
  try {
    const resp = await fetch("/api/agents", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!resp.ok) {
      const err = await resp.json();
      throw new Error(err.detail || "Failed to create agent");
    }
    modalOverlay.classList.add("hidden");
    refreshAgents();
  } catch (err) {
    formError.textContent = err.message;
    formError.style.display = "block";
  } finally {
    createBtn.disabled = false;
  }
});

// --- chat view ---

function addBubble(role, text) {
  const el = document.createElement("div");
  el.className = "bubble " + role;
  el.textContent = text;
  chatBody.appendChild(el);
  chatBody.scrollTop = chatBody.scrollHeight;
  return el;
}

// Render agent response text into a bubble. Markdown is parsed and rendered
// as HTML; responses that already start with an HTML tag are injected as-is
// (the agent formatted its own output); plain text renders identically since
// marked.parse leaves it unchanged.
function renderBubbleText(el, text) {
  if (text.trimStart().startsWith("<")) {
    el.innerHTML = text;
  } else {
    el.innerHTML = marked.parse(text);
  }
}

function addTypingBubble() {
  const el = document.createElement("div");
  el.className = "bubble agent typing";
  el.innerHTML = `<span class="dot"></span><span class="dot"></span><span class="dot"></span>`;
  chatBody.appendChild(el);
  chatBody.scrollTop = chatBody.scrollHeight;
  return el;
}

function renderTurnsList(turns, selectedTurn, onSelect) {
  turnsBody.innerHTML = "";
  for (const [idx, t] of Object.entries(turns)) {
    const el = document.createElement("div");
    el.className = "turn-item" + (Number(idx) === selectedTurn ? " selected" : "");
    const statusText = {
      streaming: "answering...", pending: "waiting on telemetry", ready: "trace ready",
      unavailable: "not ready", not_supported: "unavailable", error: "error",
    }[t.status] || "";
    const statusClass = t.status === "ready" ? "ready" : (t.status === "pending" ? "pending" : "");
    el.innerHTML = `<span class="turn-preview">${idx}. ${escapeHtml(t.text)}</span><span class="turn-status ${statusClass}">${statusText}</span>`;
    el.onclick = () => onSelect(Number(idx));
    turnsBody.appendChild(el);
  }
}

// Every deployer's get_trace() renders lines as plain "<Role>: <text>"
// strings (see deployers/__init__.py) -- parsed back out here purely for
// display so the shared backend shape stays simple, rather than pushing a
// {role, text} shape through the interface before Gemini/Azure need it too.
function parseTraceLine(line) {
  let m;
  if ((m = line.match(/^You: ([\s\S]*)$/))) return { role: "user", label: "You", text: m[1] };
  if ((m = line.match(/^Agent: -> call ([\s\S]*)$/))) return { role: "call", label: "Tool call", text: m[1] };
  if ((m = line.match(/^tool: ([\s\S]*)$/))) return { role: "result", label: "Tool result", text: m[1] };
  if ((m = line.match(/^Agent: ([\s\S]*)$/))) return { role: "agent", label: "Agent", text: m[1] };
  return { role: "", label: "", text: line };
}

function traceHtml(traceId, spans) {
  if (!spans || !spans.length) {
    return `<div class="trace-status">(no application-level spans found for this trace)</div>`;
  }
  let html = `<div class="trace-id">trace ${escapeHtml(traceId)}</div>`;
  for (const span of spans) {
    html += `<div class="span-card">`;
    html += `<span class="span-duration">${span.duration_ms != null ? span.duration_ms + "ms" : ""}</span>`;
    html += `<span class="span-name">${escapeHtml(span.name)}</span>`;
    for (const line of span.lines) {
      const p = parseTraceLine(line);
      html += `<div class="span-line span-line-${p.role}">`;
      if (p.label) html += `<span class="line-label">${p.label}</span>`;
      html += `<span class="line-text">${escapeHtml(p.text)}</span></div>`;
    }
    html += `</div>`;
  }
  return html;
}

// Collapses a turn's ordered tool-call list into "name" or "name x N" per
// distinct name, in first-seen order -- e.g. ["get_stock_price",
// "get_stock_price", "search_web"] -> ["get_stock_price x2", "search_web"].
// Repetition isn't proof of a wasted call (the same tool for two different
// tickers is legitimate), but showing the count instead of a flat list
// makes an actual duplicate-looking pattern easy to spot at a glance,
// which a flat comma list buries.
function groupToolCallCounts(names) {
  const counts = new Map();
  for (const name of names) counts.set(name, (counts.get(name) || 0) + 1);
  return [...counts.entries()].map(([name, n]) => (n > 1 ? `${name} x${n}` : name));
}

// The "why was this turn slow" story: where the turn's time actually went
// (total turn time, broken into pieces that sum to it),
// trajectory (the ordered tool-call sequence), and the LLM call itself
// (token counts/growth, a turn-average output rate, and -- AWS only for
// now, see deployers/aws.py's latest_retries -- retries as an honest,
// imprecise "something made this call slower than a clean round trip"
// throttling signal). Populated from answer_end and usage, both of which
// merge straight into the turns[] entry as they arrive -- see their
// handlers below -- so this renders whatever's landed so far rather than
// waiting for everything at once (usage in particular can trail answer_end
// by several seconds on Azure).
function latencyBreakdownHtml(t, collapsed) {
  if (t.elapsed_seconds === undefined) return ""; // nothing to show until answer_end has landed for this turn

  const secs = (ms) => `${(ms / 1000).toFixed(1)}s`;

  // Total first, then the pieces it splits into as indented sub-rows. The
  // total genuinely does contain all of them: server.py stamps the turn's
  // start clock before calling stream_chat, which waits out the session
  // warmup before sending anything -- so warmup time is turn time the user
  // sat through, not a separate figure alongside it. Presenting it as a
  // sum is the whole point; the previous layout listed the same numbers as
  // peers, which left no way to tell what added up to what.
  //
  // Deliberately excluded from this group: time to first activity. It
  // overlaps the pieces (it's measured from the same start clock and spans
  // the warmup plus part of the request) rather than being one of them, so
  // it sits further down with the other cross-cutting numbers instead of
  // inside a breakdown that's supposed to add up.
  // Same value the chat bubble's "full response" text shows, formatted
  // through secs() so a whole-second total reads "3.0s" rather than "3s"
  // and lines up with the one-decimal sub-rows underneath it.
  const totalMs = Math.round(t.elapsed_seconds * 1000);
  const rows = [["Total turn time", secs(totalMs)]];
  if (t.warmup_ms != null) {
    // min() rather than the raw values, so the sub-rows always add up to
    // the total shown above them. Both bounds are real cases, not defensive
    // noise: warmup_ms can exceed a total that was rounded to one decimal,
    // and on every turn after the first, awaiting the already-finished
    // warmup task returns instantly (warmup_ms ~= 0) while agent_init_ms
    // still carries the first turn's measurement -- that time was spent,
    // but not during this turn.
    const warmupMs = Math.min(t.warmup_ms, totalMs);
    //
    // "Waiting for" is load-bearing in this label, not filler: it is the
    // part of the cold start this turn actually sat through, which is
    // usually less than the full cold start. The warmup ping is fired when
    // the chat panel opens, so it runs while the user types -- by the time
    // Send is pressed only the remainder is left to wait on. Verified by
    // holding a fixed pre-send delay of 0/3/6s against one agent: this
    // number fell 8.1s -> 5.2s -> 2.1s while wall-clock turn time stayed
    // flat at ~8.15s. That makes it the honest figure for a breakdown of
    // where *this turn's* time went, but it is not the platform's cold
    // start, and calling it one is exactly why the load test's ~8.3s
    // looked like it contradicted the chat panel's ~5.7s. The load test
    // awaits immediately with no typing gap, so there it does measure the
    // full cold start.
    // Left as one piece here, with the platform-vs-agent split shown in the
    // full-cost group below instead. That split describes the whole session
    // start, and most of it usually happened before this turn began -- cutting
    // *this turn's* wait into those same two parts would attribute time to a
    // turn that didn't pay it.
    rows.push(["Waiting for session warmup", secs(warmupMs), true]);
    rows.push(["Request handling", secs(totalMs - warmupMs), true]);
  }

  // Second group, deliberately outside the total above: the warmup's own full
  // duration, timed from when the ping was fired at session-open rather than
  // from when this turn started awaiting it. It is not a component of turn
  // time -- most of it usually happened before the user pressed Send -- so
  // adding it to the first group would double-count and stop that group
  // summing to the total.
  //
  // Showing it is the honest version of the pre-warm trade-off. Pre-warming
  // doesn't make the cold start shorter, it moves it out of the user's way:
  // the full cost stays ~8s, and what the turn pays is only whatever was
  // still running when Send was pressed. Without this group the panel
  // reported the smaller number alone, which read as a contradiction against
  // the load test's ~8.3s -- the load test awaits immediately, so there the
  // two numbers coincide. Same quantity, now labeled on both screens.
  if (t.cold_start_ms != null) {
    const coldStartMs = t.cold_start_ms;
    // Clamped for the same reason as above: the turn can only have waited
    // through part of a warmup that started before it did.
    const waitedMs = t.warmup_ms != null ? Math.min(t.warmup_ms, coldStartMs) : 0;
    rows.push(["Session warmup, full cost (once)", secs(coldStartMs)]);
    // Two different cuts of that same total, so they deliberately don't stack:
    // this first pair is what the time went on, the pair after it is who ended
    // up waiting on it. Only AWS reports this one (see
    // deployers/__init__.py) -- elsewhere the total stays whole rather than
    // showing "n/a" against a split that platform doesn't measure.
    if (t.platform_startup_ms != null && t.agent_init_ms != null) {
      rows.push(["Platform startup (before agent code)", secs(Math.min(t.platform_startup_ms, coldStartMs)), true]);
      rows.push(["Agent initialization", secs(Math.min(t.agent_init_ms, coldStartMs)), true]);
    }
    // "Hidden by pre-warming" rather than "overlapped with your typing":
    // typing is what it overlaps on the first turn, but on later turns the
    // warmup finished during an earlier turn, and pre-warming is the reason
    // in both cases. The pair is the trade-off itself -- the full cost
    // doesn't shrink, it just lands where the user isn't waiting on it.
    rows.push(["Hidden by pre-warming", secs(coldStartMs - waitedMs), true]);
    rows.push(["Waited for in this turn", secs(waitedMs), true]);
    // Only when it's big enough to explain something. In an ordinary chat
    // session this is a couple of milliseconds of thread-pool scheduling, so a
    // row for it on every turn would be noise; it earns its place when this
    // process was itself the slow part, which is exactly the case where the
    // rest of this card would otherwise be read as the platform's fault.
    // Not a sub-row: the sub-rows above are cuts of the full cost, and this
    // came before that cost started (see deployers/__init__.py).
    if (t.client_queue_ms != null && t.client_queue_ms >= LOADTEST_CLIENT_QUEUE_WARN_MS) {
      rows.push(["Queued in the portal before the call", secs(t.client_queue_ms)]);
    }
  }

  const toolCalls = t.tool_calls || [];
  rows.push([
    `Tool calls (${toolCalls.length})`,
    toolCalls.length ? groupToolCallCounts(toolCalls).join(", ") : "none",
  ]);

  rows.push(["Time to first activity", t.ttfa_ms != null ? secs(t.ttfa_ms) : "--"]);

  if (t.input_tokens != null || t.output_tokens != null) {
    const deltaText = t.input_tokens_delta != null ? ` (${t.input_tokens_delta >= 0 ? "+" : ""}${t.input_tokens_delta} vs previous turn)` : "";
    const inputText = t.input_tokens != null ? `Input ${t.input_tokens.toLocaleString()}${deltaText}` : "Input --";
    const outputText = t.output_tokens != null ? `Output ${t.output_tokens.toLocaleString()}` : "Output --";
    rows.push(["Tokens", `${inputText} / ${outputText}`]);
    if (t.output_tokens != null && t.elapsed_seconds > 0) {
      rows.push(["Output rate (turn average)", `~${Math.round(t.output_tokens / t.elapsed_seconds)} tok/s`]);
    }
  }

  if (t.retries != null) {
    rows.push(["Retries", t.retries > 0 ? `${t.retries} detected (possible throttling)` : "none detected"]);
  }

  // The title itself is the toggle target (see the delegated click handler
  // in openChat) -- collapsing hides the rows but keeps a one-line summary
  // in the title so the headline number is still visible without expanding.
  const chevron = collapsed ? "▸" : "▾";
  const summary = collapsed ? `<span class="latency-summary">${t.elapsed_seconds}s total</span>` : "";
  const rowsHtml = collapsed
    ? ""
    : rows
        .map(
          ([label, value, sub]) =>
            // Indentation has to come from a class, not leading spaces in
            // the label -- HTML collapses those, which is why the previous
            // "  Platform handoff (of which)" rows rendered flush left and
            // read as peers of the total rather than parts of it.
            `<div class="latency-row${sub ? " latency-row-sub" : ""}"><span class="latency-label">${escapeHtml(label)}</span><span class="latency-value">${escapeHtml(String(value))}</span></div>`
        )
        .join("");
  return `<div class="latency-card">
    <div class="latency-card-title"><span class="latency-chevron">${chevron}</span>Latency breakdown${summary}</div>
    ${rowsHtml}
  </div>`;
}

function openChat(agent) {
  // Without this, clicking "Chat" more than once (or Back -> Chat again)
  // leaves the previous connection open -- a real bug found live: it
  // silently ran a second concurrent session against the same hosted
  // agent, and Cloud Logging showed activity from the stale session
  // interleaved with the new one, making the new one look hung.
  if (ws) { ws.onclose = null; ws.close(); ws = null; }

  chatBody.innerHTML = "";
  chatAgentName.textContent = agent.name;
  chatAgentPlatform.textContent = `-- ${agent.platform} / ${agent.model}`;
  agentListView.classList.add("hidden");
  chatView.classList.add("active");
  tracePane.classList.add("collapsed");
  toggleTraceBtn.textContent = "Show Details";
  turnsBody.innerHTML = "";
  traceBody.innerHTML = `<div class="trace-status">Send a message to see its trace here.</div>`;
  refreshTraceBtn.style.display = "none";

  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  ws = new WebSocket(`${protocol}//${location.host}/ws/agents/${agent.id}`);
  let currentBubble = null;
  let pendingBubble = null;
  let pendingUserText = "";
  // A tool_call/reasoning_delta that arrives *after* the bubble already has
  // real preamble text (e.g. the agent says "I'll check that for you." and
  // only then calls a tool) used to be silently dropped: the old handlers
  // only reacted while the bubble still had its "typing"/"activity" class,
  // which real text had already cleared. That left the bubble looking
  // finished/frozen for the whole tool round-trip, even though a real
  // activity signal was in fact coming through. This tracks a small,
  // separate status line under the bubble instead of depending on the
  // bubble's own class, so activity keeps showing regardless of whether
  // real text already started.
  let activityEl = null;
  // Whether the latency breakdown card's rows are hidden (title + one-line
  // summary only) -- session-scoped, not per-turn, so collapsing it once
  // sticks across every turn for this chat session rather than needing to
  // be redone each time. See latencyBreakdownHtml's collapsed param and
  // the delegated click handler below (traceBody's content is fully
  // replaced on every render, so a listener on the card itself wouldn't
  // survive -- delegating to the stable traceBody element does).
  let latencyCardCollapsed = false;
  // Token usage can arrive well after answer_end (Azure's lands on a
  // trailing event server-side -- see deployers/__init__.py's
  // latest_usage docstring), sent as its own message tagged by turn
  // rather than blocking answer_end for it. Keyed by turn so a usage
  // message that arrives after the *next* turn has already started still
  // finds the right bubble's stats line instead of the newest one.
  const statsByTurn = {};
  // turn -> {text, status: 'streaming'|'pending'|'ready'|'unavailable'|'not_supported'|'error', spans, traceId}
  const turns = {};
  let selectedTurn = null;

  function selectTurn(idx) {
    selectedTurn = idx;
    renderTurnsList(turns, selectedTurn, selectTurn);
    const t = turns[idx];
    if (!t) return;
    traceTitle.textContent = `Trace -- turn ${idx}`;
    // The latency breakdown is independent of (and usually ready well
    // before) the trace transcript below it -- it comes from answer_end/
    // usage, not the trace-indexing round trip -- so it's always prepended
    // regardless of which of the branches below the transcript itself is in.
    let bodyHtml;
    if (t.status === "ready") {
      refreshTraceBtn.style.display = "none";
      bodyHtml = traceHtml(t.traceId, t.spans);
    } else if (t.status === "streaming" || t.status === "pending") {
      refreshTraceBtn.style.display = "none";
      // The wait is named rather than left as a spinner-ish "...": on AWS
      // the gen_ai.* records take 60-90s to become queryable in CloudWatch
      // Logs Insights, which is long enough that an unexplained wait reads
      // as a broken panel. Retrying is automatic, so there's nothing to do
      // but wait, and saying so is the whole point.
      bodyHtml = `<div class="trace-status">Waiting for this turn's telemetry to become queryable -- on AWS that takes about a minute after the answer. Retrying automatically; the transcript appears here on its own.</div>`;
    } else if (t.status === "not_supported") {
      refreshTraceBtn.style.display = "none";
      bodyHtml = `<div class="trace-status">Trace viewing isn't implemented for this platform yet.</div>`;
    } else {
      refreshTraceBtn.style.display = "inline-block";
      bodyHtml = `<div class="trace-status">${escapeHtml(t.detail || "Telemetry for this turn still isn't queryable after a couple of minutes of automatic retries. Click Refresh to try again.")}</div>`;
    }
    traceBody.innerHTML = latencyBreakdownHtml(t, latencyCardCollapsed) + bodyHtml;
  }

  if (traceBodyClickHandler) traceBody.removeEventListener("click", traceBodyClickHandler);
  traceBodyClickHandler = (e) => {
    if (!e.target.closest(".latency-card-title")) return;
    latencyCardCollapsed = !latencyCardCollapsed;
    if (selectedTurn !== null) selectTurn(selectedTurn);
  };
  traceBody.addEventListener("click", traceBodyClickHandler);

  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    switch (msg.type) {
      case "answer_start":
        currentBubble = pendingBubble || addTypingBubble();
        currentBubble.dataset.turn = msg.turn;
        pendingBubble = null;
        activityEl = null;
        turns[msg.turn] = { text: pendingUserText, status: "streaming" };
        renderTurnsList(turns, selectedTurn, selectTurn);
        break;
      // A tool-call decision or a reasoning token is real evidence the
      // agent is working, well before any answer text exists on turns that
      // open with a tool call -- shown in place of the generic dots
      // instead of leaving them up until the first answer_delta, per the
      // TTFA pattern. But the agent can just as easily emit preamble text
      // *before* calling a tool ("I'll check that for you."), in which
      // case the bubble already has real content by the time this fires --
      // showActivity() below handles both: while the bubble is still just
      // dots, it takes over the bubble itself; once real text exists, it
      // adds a separate status line underneath instead of overwriting it.
      case "tool_call":
        showActivity(`Calling ${msg.name}...`);
        break;
      case "reasoning_delta":
        showActivity("Thinking...");
        break;
      case "answer_delta":
        clearActivity();
        if (currentBubble.classList.contains("typing") || currentBubble.classList.contains("activity")) {
          currentBubble.classList.remove("typing", "activity");
          currentBubble.dataset.rawText = "";
        }
        currentBubble.dataset.rawText = (currentBubble.dataset.rawText || "") + msg.text;
        renderBubbleText(currentBubble, currentBubble.dataset.rawText);
        chatBody.scrollTop = chatBody.scrollHeight;
        break;
      case "answer_end": {
        clearActivity();
        sendBtn.disabled = false;
        chatInput.disabled = false;
        chatInput.focus();
        const statsEl = document.createElement("div");
        statsEl.className = "bubble-stats";
        const latencyText = msg.ttfa_ms != null
          ? `First sign of life: ${(msg.ttfa_ms / 1000).toFixed(1)}s (full response: ${msg.elapsed_seconds}s)`
          : `Full response: ${msg.elapsed_seconds}s`;
        statsEl.textContent = latencyText;
        currentBubble.insertAdjacentElement("afterend", statsEl);
        statsByTurn[msg.turn] = { el: statsEl, latencyText };
        turns[msg.turn] = {
          ...turns[msg.turn],
          status: "pending",
          ttfa_ms: msg.ttfa_ms,
          elapsed_seconds: msg.elapsed_seconds,
          warmup_ms: msg.warmup_ms,
          platform_startup_ms: msg.platform_startup_ms,
          agent_init_ms: msg.agent_init_ms,
          cold_start_ms: msg.cold_start_ms,
          client_queue_ms: msg.client_queue_ms,
          tool_calls: msg.tool_calls,
          retries: msg.retries,
        };
        renderTurnsList(turns, selectedTurn, selectTurn);
        // Same auto-select-the-latest-turn convention the trace handlers
        // below already use -- the latency breakdown is available now,
        // well before the trace transcript is, so there's no reason to
        // wait for that to show it.
        if (selectedTurn === null || selectedTurn === msg.turn) selectTurn(msg.turn);
        break;
      }
      case "usage": {
        const stats = statsByTurn[msg.turn];
        if (stats) {
          stats.el.textContent = `${stats.latencyText}, ${msg.input_tokens} / ${msg.output_tokens} tokens`;
        }
        turns[msg.turn] = {
          ...turns[msg.turn],
          input_tokens: msg.input_tokens,
          output_tokens: msg.output_tokens,
          input_tokens_delta: msg.input_tokens_delta,
        };
        if (selectedTurn === msg.turn) selectTurn(msg.turn);
        break;
      }
      case "trace": {
        turns[msg.turn] = { ...turns[msg.turn], status: "ready", traceId: msg.trace_id, spans: msg.spans };
        renderTurnsList(turns, selectedTurn, selectTurn);
        selectTurn(msg.turn);
        break;
      }
      case "trace_unavailable": {
        // "indexing" means the server has another automatic attempt queued,
        // so it maps to the same waiting state as "pending" -- offering
        // Refresh there invites clicking that cannot help (the telemetry
        // simply isn't queryable yet). "not_ready" is the server's last
        // word, and only that gets the button.
        const status = { not_supported: "not_supported", error: "error", indexing: "pending" }[msg.reason] || "unavailable";
        turns[msg.turn] = { ...turns[msg.turn], status, detail: msg.detail };
        renderTurnsList(turns, selectedTurn, selectTurn);
        if (selectedTurn === null || selectedTurn === msg.turn) selectTurn(msg.turn);
        break;
      }
      case "error":
        clearActivity();
        if (currentBubble && (currentBubble.classList.contains("typing") || currentBubble.classList.contains("activity"))) {
          currentBubble.remove();
        }
        addBubble("agent", `(error: ${msg.detail})`);
        sendBtn.disabled = false;
        chatInput.disabled = false;
        break;
    }
  };

  function clearActivity() {
    if (activityEl) {
      activityEl.remove();
      activityEl = null;
    }
  }

  function showActivity(text) {
    if (currentBubble.classList.contains("typing") || currentBubble.classList.contains("activity")) {
      currentBubble.classList.remove("typing");
      currentBubble.classList.add("activity");
      currentBubble.textContent = text;
      return;
    }
    if (!activityEl) {
      activityEl = document.createElement("div");
      activityEl.className = "bubble agent activity";
      currentBubble.insertAdjacentElement("afterend", activityEl);
    }
    activityEl.textContent = text;
    chatBody.scrollTop = chatBody.scrollHeight;
  }
  ws.onclose = () => {
    if (chatView.classList.contains("active")) {
      addBubble("agent", "(disconnected)");
    }
  };

  chatForm.onsubmit = (e) => {
    e.preventDefault();
    const text = chatInput.value.trim();
    if (!text || ws.readyState !== WebSocket.OPEN) return;
    addBubble("user", text);
    pendingUserText = text;
    pendingBubble = addTypingBubble();
    sendBtn.disabled = true;
    chatInput.disabled = true;
    ws.send(JSON.stringify({ type: "user_message", text }));
    chatInput.value = "";
  };

  toggleTraceBtn.onclick = () => {
    const collapsed = tracePane.classList.toggle("collapsed");
    toggleTraceBtn.textContent = collapsed ? "Show Details" : "Hide Details";
  };

  refreshTraceBtn.onclick = () => {
    if (selectedTurn === null) return;
    // A closed socket used to make this button do nothing at all, with no
    // feedback -- indistinguishable from "the trace still isn't ready", and
    // the state you land in whenever the server restarts with the chat
    // panel left open. Traces are fetched over this same connection, so
    // there's nothing to retry until the chat is reopened; say that.
    if (ws.readyState !== WebSocket.OPEN) {
      turns[selectedTurn] = {
        ...turns[selectedTurn],
        status: "unavailable",
        detail: "This chat's connection has closed, so the trace can't be fetched. Go Back and open Chat again to reconnect.",
      };
      selectTurn(selectedTurn);
      return;
    }
    ws.send(JSON.stringify({ type: "get_trace", turn: selectedTurn }));
  };
}

document.getElementById("back-btn").onclick = () => {
  if (ws) { ws.onclose = null; ws.close(); ws = null; }
  chatView.classList.remove("active");
  agentListView.classList.remove("hidden");
  refreshAgents();
};

// --- load test view ---
// Drives server.py's /ws/loadtest, which itself drives perf/loadtest.py's real
// concurrent-session engine (the same code the standalone CLI uses) against
// this server -- so a test run from here exercises the identical
// client-facing WebSocket path a real browser's Chat view does, just N
// times concurrently. Config form stays visible and disabled (not hidden)
// while a test runs, so the parameters that produced a given result stay
// on screen next to it instead of disappearing and reappearing.

const loadtestBtn = document.getElementById("loadtest-btn");
const loadtestView = document.getElementById("loadtest-view");
const loadtestBackBtn = document.getElementById("loadtest-back-btn");
const loadtestConfigForm = document.getElementById("loadtest-config-form");
const loadtestCompareToggle = document.getElementById("lt-compare-toggle");
const loadtestAgentLabelEl = document.getElementById("lt-agent-label");
const loadtestAgentSelect = document.getElementById("lt-agent");
const loadtestAgentBField = document.getElementById("lt-agent-b-field");
const loadtestAgentBSelect = document.getElementById("lt-agent-b");
const loadtestModeSelect = document.getElementById("lt-mode");
const loadtestModeHint = document.getElementById("lt-mode-hint");
const loadtestMessageField = document.getElementById("lt-message-field");
const loadtestCapsHint = document.getElementById("lt-caps-hint");
const loadtestShapeSelect = document.getElementById("lt-shape");
const loadtestUsersField = document.getElementById("lt-users-field");
const loadtestUsersInput = document.getElementById("lt-users");
const loadtestIterationsField = document.getElementById("lt-iterations-field");
const loadtestIterationsInput = document.getElementById("lt-iterations");
const loadtestSessionsField = document.getElementById("lt-sessions-field");
const loadtestSessionsInput = document.getElementById("lt-sessions");
const loadtestMessageInput = document.getElementById("lt-message");
const loadtestErrorEl = document.getElementById("loadtest-error");
const loadtestStartBtn = document.getElementById("loadtest-start-btn");
const loadtestRunActions = document.getElementById("loadtest-run-actions");
const loadtestStopBtn = document.getElementById("loadtest-stop-btn");
const loadtestDownloadBtn = document.getElementById("loadtest-download-btn");
const loadtestOverallHeader = document.getElementById("loadtest-overall-header");
const loadtestProgressEl = document.getElementById("loadtest-progress");
const loadtestResultsEl = document.getElementById("loadtest-results");
const loadtestResultsPlaceholder = document.getElementById("loadtest-results-placeholder");
const loadtestFormFields = [
  loadtestCompareToggle, loadtestAgentSelect, loadtestAgentBSelect, loadtestModeSelect,
  loadtestShapeSelect, loadtestUsersInput, loadtestIterationsInput, loadtestSessionsInput,
  loadtestMessageInput,
];

let loadtestWs = null;
// The last run's records, kept only so Download JSON has something to
// serialize. Client-side on purpose: the server keeps no run history, and
// this is the same record shape perf/loadtest.py's --output writes, so a
// portal run and a CLI run stay interchangeable artifacts.
let loadtestLastRun = null;

// One place owns "a run is in flight" UI state: the form is disabled, Stop is
// offered, and Download (which needs a finished run's records) is not.
function setLoadtestFormEnabled(enabled) {
  loadtestStartBtn.disabled = !enabled;
  loadtestStartBtn.textContent = enabled ? "Start Test" : "Running...";
  for (const el of loadtestFormFields) el.disabled = !enabled;
  loadtestStopBtn.classList.toggle("hidden", enabled);
  loadtestStopBtn.disabled = enabled;
  loadtestStopBtn.textContent = "Stop";
}

// Session-start-only is naturally a "how many concurrent cold starts can
// this handle" check -- one session start per simulated user is the
// natural unit there, unlike chat mode's repeated-turns-per-user default.
// Reset on every mode switch (not just once) so it stays predictable
// rather than depending on whichever mode happened to be selected last.
const LOADTEST_DEFAULT_ITERATIONS = { chat: 3, warmup_only: 1 };

// Mirrors server.py's LOADTEST_MAX_TOTAL_SESSIONS(_WARMUP_ONLY): a
// platform-startup-only session makes no LLM call at all on AWS/Gemini, so a
// long run of them is cheap; the same count of full chat turns would be that
// many real LLM calls. The server clamps regardless -- these numbers are here
// so the form's own inputs and hints say the same thing it would enforce.
const LOADTEST_MAX_TOTAL_SESSIONS = { chat: 60, warmup_only: 500 };

// Mirrors server.py's LOADTEST_MAX_USERS so the hint and the input's own max
// quote the number the server would actually enforce.
const LOADTEST_MAX_USERS = 40;

// A loop run's default: long enough that percentiles mean something (and that
// drift over the run would show), short enough not to be a surprise
// half-hour commitment from a single click.
const LOADTEST_DEFAULT_LOOP_SESSIONS = 100;

// "Platform startup only" never sends a chat message, so neither the message
// field nor -- in burst shape -- the iteration count applies: a warmup-only
// "iteration" is a whole separate session (fresh WebSocket, fresh warmup), so
// there is no such thing as iterating *within* one. Fields are hidden rather
// than just disabled, since a field that's present-but-unusable for the
// current mode is more confusing than one that's simply not there.
//
// Loop shape replaces both concurrency fields with one Sessions count, because
// that's the whole idea: sessions one after another, no concurrency to
// configure. It goes to the server as users=1 with Sessions as iterations --
// the same engine, different numbers (see server.py's loadtest_ws).
function updateLoadtestModeUi() {
  const warmupOnly = loadtestModeSelect.value === "warmup_only";
  const loop = loadtestShapeSelect.value === "loop";
  const maxTotal = LOADTEST_MAX_TOTAL_SESSIONS[loadtestModeSelect.value];

  loadtestMessageField.classList.toggle("hidden", warmupOnly);
  loadtestUsersField.classList.toggle("hidden", loop);
  loadtestIterationsField.classList.toggle("hidden", loop || warmupOnly);
  loadtestSessionsField.classList.toggle("hidden", !loop);
  loadtestIterationsInput.value = LOADTEST_DEFAULT_ITERATIONS[loadtestModeSelect.value];
  loadtestUsersInput.max = LOADTEST_MAX_USERS;
  loadtestSessionsInput.max = maxTotal;
  loadtestSessionsInput.value = Math.min(LOADTEST_DEFAULT_LOOP_SESSIONS, maxTotal);

  if (loop) {
    loadtestCapsHint.textContent =
      `One session at a time, up to ${maxTotal} of them` +
      (warmupOnly
        ? " -- enough samples for percentiles that mean something, and for drift over the run to show. At a typical 2.5s session start, 500 sessions is about 20 minutes; on a slower agent, well over an hour. There's a Stop button, and results build up as the run goes."
        : " -- but each one is a real LLM call, so this mode stays capped much lower than platform-startup-only. There's a Stop button, and results build up as the run goes.");
  } else {
    loadtestCapsHint.textContent =
      `Capped at ${LOADTEST_MAX_USERS} concurrent sessions and ${maxTotal} total` +
      (warmupOnly
        ? " -- so a burst measures at most 20 session starts at once. Use the Loop shape for a bigger sample."
        : " (concurrent sessions x iterations); a high concurrent-sessions value clamps iterations down to fit.");
  }

  loadtestModeHint.textContent = warmupOnly
    ? "Measures session-start cost -- each session opens and waits for warmup to finish, then closes, without sending a chat message. " +
      "No LLM call at all for AWS/Gemini; Azure's own warmup mechanism includes one trivial real completion call under the hood (see docs/latency.md)."
    : "Each session sends one real chat message and waits for the full answer -- exercises the LLM call, not just session start.";
}
loadtestModeSelect.addEventListener("change", updateLoadtestModeUi);
loadtestShapeSelect.addEventListener("change", updateLoadtestModeUi);

// Comparing 2 agents reuses every field in the form as-is (same mode/
// concurrency/iterations/message applied to both) -- only the agent
// picker itself changes shape, from one dropdown to two.
function updateLoadtestCompareUi() {
  const compare = loadtestCompareToggle.checked;
  loadtestAgentBField.classList.toggle("hidden", !compare);
  loadtestAgentBSelect.required = compare;
  loadtestAgentLabelEl.textContent = compare ? "Agent A" : "Agent";
}
loadtestCompareToggle.addEventListener("change", updateLoadtestCompareUi);

function populateLoadtestAgentSelect(selectEl, activeAgents) {
  selectEl.innerHTML = "";
  if (!activeAgents.length) {
    const opt = document.createElement("option");
    opt.textContent = "No active agents -- create one first";
    opt.disabled = true;
    selectEl.appendChild(opt);
    return;
  }
  for (const a of activeAgents) {
    const opt = document.createElement("option");
    opt.value = a.id;
    // Deployment mode and region included when known: comparing a code-zip
    // agent against a container one, or the same agent in two regions, are both
    // normal reasons to be on this screen, and two agents whose names differ by
    // a word are easy to pick the wrong way round from the platform alone.
    const parts = [
      a.platform,
      a.deployment_mode ? deploymentModeShortLabel(a.deployment_mode) : null,
      agentRegion(a) || null,
    ];
    opt.textContent = `${a.name} (${parts.filter(Boolean).join(", ")})`;
    selectEl.appendChild(opt);
  }
}

function openLoadTestView() {
  if (loadtestWs) { loadtestWs.onclose = null; loadtestWs.close(); loadtestWs = null; }

  const activeAgents = agents.filter((a) => a.status === "active");
  populateLoadtestAgentSelect(loadtestAgentSelect, activeAgents);
  populateLoadtestAgentSelect(loadtestAgentBSelect, activeAgents);
  // Default Agent B to a *different* agent than Agent A when there's a
  // choice, so simply checking "Compare" and clicking Start Test works
  // immediately instead of hitting the "pick two different agents" error.
  if (activeAgents.length > 1) loadtestAgentBSelect.selectedIndex = 1;

  loadtestCompareToggle.checked = false;
  updateLoadtestCompareUi();
  loadtestModeSelect.value = "warmup_only";
  loadtestShapeSelect.value = "burst";
  updateLoadtestModeUi();
  setLoadtestFormEnabled(true);
  loadtestRunActions.classList.add("hidden");
  loadtestDownloadBtn.classList.add("hidden");
  loadtestErrorEl.style.display = "none";
  loadtestOverallHeader.classList.add("hidden");
  loadtestProgressEl.classList.add("hidden");
  loadtestProgressEl.innerHTML = "";
  loadtestResultsEl.classList.add("hidden");
  loadtestResultsEl.innerHTML = "";
  loadtestResultsPlaceholder.classList.remove("hidden");

  agentListView.classList.add("hidden");
  loadtestView.classList.add("active");
}

loadtestBtn.onclick = openLoadTestView;

loadtestBackBtn.onclick = () => {
  if (loadtestWs) { loadtestWs.onclose = null; loadtestWs.close(); loadtestWs = null; }
  loadtestView.classList.remove("active");
  agentListView.classList.remove("hidden");
  refreshAgents();
};

// The load test's own chronology: `users` simulated users run concurrently,
// each working through its iterations in sequence (see perf/loadtest.py's
// run_user), so iteration 0 of every user happens at roughly the same
// moment, then iteration 1, and so on. Sorting by (iteration, user) is the
// closest thing to a real time axis these records support.
//
// Emphatically *not* the order results arrive in, which is what every one of
// these charts used to plot. Concurrent sessions complete in ascending
// duration by definition -- the fastest one finishes first -- so completion
// order sorted each chart into a rising line no matter how the agent
// behaved, and a perfectly steady agent looked like one degrading over the
// run. The scatter exists to show spread; that artifact replaced the spread
// with a slope.
function loadtestRunOrder(results) {
  return [...results].sort((a, b) => a.iteration - b.iteration || a.user - b.user);
}

// The x-axis for a run-ordered scatter: a divider between iterations and the
// iteration number under each group. Run order groups into waves (all users'
// iteration 0, then all users' iteration 1...), and drawing those waves is
// what makes the axis self-explanatory -- without them, nothing on screen
// distinguishes run order from the completion order this replaced, which is
// exactly the misread the ordering is there to prevent.
//
// Group boundaries come from the points' own iteration values rather than
// arithmetic on the user count, because a chart may have dropped sessions
// (an errored session with no value to plot), which makes waves uneven.
//
// Past LOADTEST_MAX_ITERATION_AXIS_GROUPS groups the per-group treatment stops
// being an axis and becomes noise: a loop run is one session per iteration, so
// 500 sessions would draw 500 dividers and 500 numbers over a 550px chart.
// Above that, the run is long enough that where a session sits in the sequence
// is the useful reading, so a plain run-order axis replaces the groups.
const LOADTEST_MAX_ITERATION_AXIS_GROUPS = 12;

// The fallback axis for a long run: session number at the ends and the middle,
// no dividers. It answers the one question a 500-session chart is actually
// asked -- "how far into the run is this dot" -- which the per-iteration axis
// answers too, just illegibly once every dot is its own group.
function loadtestRunOrderAxisSvg(points, xFor, geom) {
  const { height } = geom;
  const n = points.length;
  return [0, Math.floor((n - 1) / 2), n - 1]
    .map((i, k) => {
      const anchor = k === 0 ? "start" : k === 2 ? "end" : "middle";
      return `<text x="${xFor(i).toFixed(1)}" y="${(height - 6).toFixed(1)}" class="chart-axis-label" text-anchor="${anchor}">${i + 1}</text>`;
    })
    .join("");
}

// Dots have to shrink as the run grows or the scatter stops being a scatter:
// 500 dots at r=4 across ~550px overlap into one solid band, and a band shows
// neither spread nor drift. Sized by sample count rather than by available
// width because the width barely varies -- the chart column is fixed-ish and
// the run length is what changes by 25x.
function loadtestDotRadius(n) {
  if (n <= 80) return 4;
  if (n <= 200) return 2.5;
  return 1.5;
}

// A rolling median through the scatter, drawn only once a run is long enough
// for drift to be a real question (and long enough that the dots alone can't
// answer it). Median rather than mean so one 30-second timeout doesn't drag
// the line up through a whole window; a window of n/20 keeps the line's own
// resolution proportional to the run.
const LOADTEST_MEDIAN_LINE_MIN_POINTS = 100;

function loadtestMedianLineSvg(points, xFor, yFor, extraClass) {
  const n = points.length;
  if (n < LOADTEST_MEDIAN_LINE_MIN_POINTS) return "";
  const window = Math.max(5, Math.round(n / 20));
  const half = Math.floor(window / 2);
  const coords = points.map((_, i) => {
    const slice = points
      .slice(Math.max(0, i - half), Math.min(n, i + half + 1))
      .map((p) => p.y)
      .sort((a, b) => a - b);
    const mid = Math.floor(slice.length / 2);
    const median = slice.length % 2 ? slice[mid] : (slice[mid - 1] + slice[mid]) / 2;
    return `${xFor(i).toFixed(1)},${yFor(median).toFixed(1)}`;
  });
  return `<polyline points="${coords.join(" ")}" class="chart-median-line${extraClass ? ` ${extraClass}` : ""}" />`;
}

function loadtestIterationAxisSvg(points, xFor, geom) {
  const { padding, plotH, height } = geom;
  if (points.some((p) => p.iteration == null)) return "";

  const groups = [];
  points.forEach((p, i) => {
    const last = groups[groups.length - 1];
    if (last && last.iteration === p.iteration) last.end = i;
    else groups.push({ iteration: p.iteration, start: i, end: i });
  });
  if (groups.length < 2) return "";
  if (groups.length > LOADTEST_MAX_ITERATION_AXIS_GROUPS) return loadtestRunOrderAxisSvg(points, xFor, geom);

  return groups
    .map((g, i) => {
      const label = `<text x="${((xFor(g.start) + xFor(g.end)) / 2).toFixed(1)}" y="${(height - 6).toFixed(1)}" class="chart-axis-label" text-anchor="middle">${g.iteration}</text>`;
      if (i === 0) return label;
      const x = ((xFor(groups[i - 1].end) + xFor(g.start)) / 2).toFixed(1);
      return `<line x1="${x}" y1="${padding.top}" x2="${x}" y2="${padding.top + plotH}" class="chart-gridline" />${label}`;
    })
    .join("");
}

// Whether the iteration axis above actually draws anything -- same
// single-group rule, asked separately so a title or note doesn't point at
// "iteration" groups that aren't on screen. Session-start-only runs are
// always one wave (no iterations knob in that mode at all), and a chat run
// with iterations set to 1 is too. So is a long loop run, where every session
// is its own iteration and the axis falls back to plain run order -- the
// titles have to stop saying "by iteration" when nothing on screen groups by
// it.
function loadtestHasIterationAxis(points) {
  if (points.some((p) => p.iteration == null)) return false;
  const groups = new Set(points.map((p) => p.iteration)).size;
  return groups > 1 && groups <= LOADTEST_MAX_ITERATION_AXIS_GROUPS;
}

// Renders a single-series scatter as a plain SVG string -- x = run order
// (see loadtestRunOrder), y = the metric. containerWidthPx is measured live
// (not a fixed constant) so the viewBox matches the actually-rendered pixel
// width; otherwise the browser's non-uniform scaling of a fluid-width/
// fixed-height SVG would stretch the circular dots into ellipses.
function loadtestScatterSvg(points, containerWidthPx) {
  const width = Math.max(240, containerWidthPx || 600);
  const height = 160;
  const padding = { top: 10, right: 10, bottom: 20, left: 46 };
  const plotW = width - padding.left - padding.right;
  const plotH = height - padding.top - padding.bottom;
  const n = points.length;
  const maxY = Math.max(1, ...points.map((p) => p.y));

  const xFor = (i) => padding.left + (n <= 1 ? plotW / 2 : (i / (n - 1)) * plotW);
  const yFor = (v) => padding.top + plotH - (v / maxY) * plotH;

  const grid = [0, 0.5, 1]
    .map((f) => {
      const y = padding.top + plotH * (1 - f);
      const label = Math.round(maxY * f).toLocaleString();
      return (
        `<line x1="${padding.left}" y1="${y.toFixed(1)}" x2="${width - padding.right}" y2="${y.toFixed(1)}" class="chart-gridline" />` +
        `<text x="${padding.left - 6}" y="${(y + 3).toFixed(1)}" class="chart-axis-label" text-anchor="end">${label}</text>`
      );
    })
    .join("");

  const r = loadtestDotRadius(n);
  const dots = points
    .map(
      (p, i) =>
        `<circle cx="${xFor(i).toFixed(1)}" cy="${yFor(p.y).toFixed(1)}" r="${r}" class="chart-dot ${p.status}"><title>${escapeHtml(p.title)}</title></circle>`
    )
    .join("");

  const iterAxis = loadtestIterationAxisSvg(points, xFor, { padding, plotH, height });
  const medianLine = loadtestMedianLineSvg(points, xFor, yFor);
  return `<svg viewBox="0 0 ${width} ${height}" class="chart-svg" style="height:${height}px">${grid}${iterAxis}${dots}${medianLine}</svg>`;
}

// One scatter marker. Shape is a real second encoding channel here, not
// decoration: the comparison view already spends color on agent identity
// (see style.css's note on --accent/--warn), so shape is what still
// distinguishes the two agents for anyone who can't separate blue from
// amber, and it's what keeps a printed or screenshotted chart readable.
// A diamond's radius is scaled up slightly so it reads as the same visual
// weight as a circle rather than smaller (2r^2 vs pi*r^2).
function loadtestMarkerSvg(shape, cx, cy, classes, title, radius) {
  const attrs = `class="chart-marker ${classes}"><title>${escapeHtml(title)}</title>`;
  const r = radius == null ? 4 : radius;
  if (shape === "diamond") {
    const dr = r * 1.25;
    const points = [`${cx},${cy - dr}`, `${cx + dr},${cy}`, `${cx},${cy + dr}`, `${cx - dr},${cy}`].join(" ");
    return `<polygon points="${points}" ${attrs}</polygon>`;
  }
  return `<circle cx="${cx}" cy="${cy}" r="${r}" ${attrs}</circle>`;
}

// Renders two series on one shared pair of axes as a plain SVG string.
// The shared y-axis is the entire point: two separate charts, each
// auto-scaled to its own max, make a 3x difference look like no
// difference at all, which is exactly the mistake this replaces.
//
// x is each series' *own* run order (see loadtestRunOrder), spread across
// the full width, so the two clouds line up as distributions rather than as
// paired observations -- agent A's 3rd session and agent B's 3rd aren't the
// same moment, and the agents don't even run at the same time (see
// server.py's loadtest_ws). Nothing in this chart invites reading one point
// against the point above it; the comparison it supports is cloud vs cloud,
// on the y-axis.
//
// The iteration dividers come from the longest series. Both agents run the
// same test config (one shared users/iterations, not per-side -- see
// server.py's loadtest_ws), so their waves line up by construction; taking
// the longest just means a side that lost sessions to errors doesn't shrink
// the axis for the side that didn't.
function loadtestOverlayScatterSvg(series, containerWidthPx) {
  const width = Math.max(240, containerWidthPx || 600);
  const height = 190;
  const padding = { top: 10, right: 10, bottom: 20, left: 46 };
  const plotW = width - padding.left - padding.right;
  const plotH = height - padding.top - padding.bottom;
  const maxY = Math.max(1, ...series.flatMap((s) => s.points.map((p) => p.y)));

  const yFor = (v) => padding.top + plotH - (v / maxY) * plotH;

  const grid = [0, 0.5, 1]
    .map((f) => {
      const y = padding.top + plotH * (1 - f);
      const label = Math.round(maxY * f).toLocaleString();
      return (
        `<line x1="${padding.left}" y1="${y.toFixed(1)}" x2="${width - padding.right}" y2="${y.toFixed(1)}" class="chart-gridline" />` +
        `<text x="${padding.left - 6}" y="${(y + 3).toFixed(1)}" class="chart-axis-label" text-anchor="end">${label}</text>`
      );
    })
    .join("");

  const xForN = (n) => (i) => padding.left + (n <= 1 ? plotW / 2 : (i / (n - 1)) * plotW);
  const longest = series.reduce((a, b) => (b.points.length > a.points.length ? b : a), series[0]);
  const iterAxis = loadtestIterationAxisSvg(longest.points, xForN(longest.points.length), { padding, plotH, height });

  // One radius for both sides, from the longer one: sizing each series to its
  // own count would make the side that lost sessions to errors draw visibly
  // bigger dots, which reads as a difference between the agents.
  const r = loadtestDotRadius(longest.points.length);
  const markers = series
    .map((s) => {
      const xFor = xForN(s.points.length);
      return (
        s.points
          .map((p, i) =>
            loadtestMarkerSvg(
              s.shape,
              xFor(i).toFixed(1),
              yFor(p.y).toFixed(1),
              `${s.colorClass}${p.status === "error" ? " errored" : ""}`,
              p.title,
              r
            )
          )
          .join("") + loadtestMedianLineSvg(s.points, xFor, yFor, s.colorClass)
      );
    })
    .join("");

  return `<svg viewBox="0 0 ${width} ${height}" class="chart-svg" style="height:${height}px">${grid}${iterAxis}${markers}</svg>`;
}

function loadtestStatTile(label, value, sub, isError, isWarn) {
  return `<div class="loadtest-stat-tile${isError ? " errors" : isWarn ? " warn" : ""}">
    <span class="stat-label">${escapeHtml(label)}</span>
    <span class="stat-value">${escapeHtml(value)}</span>
    ${sub ? `<span class="stat-sub">${escapeHtml(sub)}</span>` : ""}
  </div>`;
}

// Above this, a run's client queue time is called out rather than merely
// reported: sessions are then waiting on each other inside the portal process
// for long enough to be a visible share of a ~2s session start, so the run is
// partly measuring the portal instead of the platform. Mirrors
// perf/loadtest.py's CLIENT_QUEUE_WARN_MS deliberately -- the CLI's summary
// line and this view must not disagree about whether a run was client-bound.
const LOADTEST_CLIENT_QUEUE_WARN_MS = 250;

// One warning shared by the single-agent and comparison views, so a
// client-bound run can't be flagged on one screen and pass silently on the
// other. Judged on the worst summary on screen, because both agents in a
// comparison run share this one portal process: if it saturated, neither
// agent's numbers are clean. Returns "" when there's nothing to say, which is
// the normal case -- queue time is milliseconds on an unloaded machine.
function loadtestClientQueueCalloutHtml(summaries) {
  const stats = (summaries || []).map((s) => s && s.client_queue_ms).filter((q) => q != null && q.mean != null);
  if (!stats.length) return "";
  const worst = stats.reduce((a, b) => (b.p95 > a.p95 ? b : a));
  if (worst.p95 < LOADTEST_CLIENT_QUEUE_WARN_MS) return "";
  return `<div class="loadtest-callout warn"><b>This run was partly bound by the portal, not the platform.</b>
    Sessions waited ${loadtestFmtMs(worst.p50)} at p50 and ${loadtestFmtMs(worst.p95)} at p95 (worst ${loadtestFmtMs(worst.max)}) inside this process before their session-start call was even issued.
    That wait is this machine's, and it is <i>not</i> counted in any figure below -- but a machine busy enough to queue its own requests was also a poor place to measure from, so read the tails below as upper bounds.
    Re-run with fewer concurrent sessions, or from a less loaded machine, to measure the platform rather than the load generator.</div>`;
}

const loadtestFmtMs = (v) => (v == null ? "--" : v >= 1000 ? `${(v / 1000).toFixed(1)}s` : `${Math.round(v)}ms`);

const loadtestChartLegendHtml = `<div class="chart-legend">
  <span><span class="chart-legend-swatch ok"></span>Success</span>
  <span><span class="chart-legend-swatch error"></span>Error</span>
</div>`;

// Chat mode: two charts (TTFA + total turn time), since those are two
// genuinely different measures on the same ms unit -- never one dual-axis
// chart for both, per this app's own dataviz convention (see the trace
// panel's own latency breakdown for the same rule applied elsewhere).
function renderChatLoadtestResults(results, summary, containerWidth) {
  const statsHtml = `<div class="loadtest-stats">
    ${loadtestStatTile("Sessions", String(summary.total))}
    ${loadtestStatTile("Errors", `${summary.errors} / ${summary.total}`, null, summary.errors > 0)}
    ${loadtestStatTile("TTFA p50", loadtestFmtMs(summary.ttfa_ms.p50), `p95 ${loadtestFmtMs(summary.ttfa_ms.p95)}`)}
    ${loadtestStatTile("TTFA mean", loadtestFmtMs(summary.ttfa_ms.mean), `${loadtestFmtMs(summary.ttfa_ms.min)} to ${loadtestFmtMs(summary.ttfa_ms.max)}`)}
    ${loadtestStatTile("Total p50", loadtestFmtMs(summary.elapsed_ms.p50), `p95 ${loadtestFmtMs(summary.elapsed_ms.p95)}`)}
    ${loadtestStatTile("Total mean", loadtestFmtMs(summary.elapsed_ms.mean), `${loadtestFmtMs(summary.elapsed_ms.min)} to ${loadtestFmtMs(summary.elapsed_ms.max)}`)}
  </div>`;

  // ttfa_ms is null for a session that errored before any activity signal
  // ever arrived -- there's no honest y-value to plot for those, so they're
  // omitted from this chart specifically (not fabricated as 0) and called
  // out in a note instead. elapsed_ms is present either way (an error still
  // took some real amount of time before it was detected), so the total-
  // time chart below includes every session.
  const ordered = loadtestRunOrder(results);
  const ttfaPoints = ordered
    .filter((r) => r.ttfa_ms != null)
    .map((r) => ({
      y: r.ttfa_ms,
      iteration: r.iteration,
      status: r.error ? "error" : "ok",
      title: `user ${r.user} iter ${r.iteration}: TTFA ${(r.ttfa_ms / 1000).toFixed(1)}s`,
    }));
  const ttfaOmitted = results.length - ttfaPoints.length;

  const elapsedPoints = ordered.map((r) => ({
    y: r.elapsed_ms,
    iteration: r.iteration,
    status: r.error ? "error" : "ok",
    title: r.error
      ? `user ${r.user} iter ${r.iteration}: ERROR after ${(r.elapsed_ms / 1000).toFixed(1)}s -- ${r.error}`
      : `user ${r.user} iter ${r.iteration}: ${(r.elapsed_ms / 1000).toFixed(1)}s total`,
  }));

  const ttfaChart = `<div class="chart-card">
    <div class="chart-card-header"><span class="chart-card-title">Time to first activity (per session${loadtestHasIterationAxis(ttfaPoints) ? ", by iteration" : ""})</span>${loadtestChartLegendHtml}</div>
    ${loadtestScatterSvg(ttfaPoints, containerWidth)}
    ${ttfaOmitted > 0 ? `<div class="chart-note">${ttfaOmitted} errored session(s) omitted -- no TTFA signal was ever received.</div>` : ""}
  </div>`;

  const elapsedChart = `<div class="chart-card">
    <div class="chart-card-header"><span class="chart-card-title">Total turn time (per session${loadtestHasIterationAxis(elapsedPoints) ? ", by iteration" : ""})</span>${loadtestChartLegendHtml}</div>
    ${loadtestScatterSvg(elapsedPoints, containerWidth)}
  </div>`;

  return statsHtml + ttfaChart + elapsedChart;
}

// Which warmup number this view reports, and it is deliberately the full
// cold start rather than the wait a turn experienced. cold_start_ms is timed
// from when the warmup ping was fired; warmup_ms is timed from when someone
// started awaiting it. In the load test the two are nearly identical -- it
// awaits immediately, with no user typing in between -- but preferring
// cold_start_ms makes that an explicit guarantee instead of a coincidence of
// how the harness happens to be written, and makes this the same quantity
// the chat panel labels "Session warmup, full cost (once)". Reporting
// warmup_ms here while the chat panel showed its own smaller warmup_ms is
// what made the two screens look like they disagreed by ~2x.
//
// Falls back to warmup_ms for results recorded before cold_start_ms existed,
// or from a deployer that doesn't report it.
function loadtestWarmupStats(summary) {
  return summary.cold_start_ms && summary.cold_start_ms.mean != null
    ? summary.cold_start_ms
    : summary.warmup_ms;
}

function loadtestWarmupMs(r) {
  return r.cold_start_ms != null ? r.cold_start_ms : r.warmup_ms;
}

// Shared by the single-agent warmup chart and the comparison overlay that
// replaces both agents' copies of it -- the explanation of *which* warmup
// number this is has to survive into the comparison view, where it's the
// only warmup chart there is.
const LOADTEST_WARMUP_NOTE =
  'The warmup\'s full duration, start to finish. The chat panel reports this same number as "Session warmup, full cost (once)" -- there it also shows how much of it a real turn actually waited through, which is far less, because the portal fires the warmup as soon as the chat panel opens and it runs while you type. This harness sends its request immediately, so here nothing is hidden by a typing gap.';

// Null only if the session itself errored before "warmup_done" ever
// arrived (e.g. a dropped connection) -- omitted for the same reason chat
// mode omits a null ttfa_ms: no honest value to plot. Shared by the
// single-agent chart and the comparison overlay so both drop exactly the
// same sessions.
// valueOf/label default to the full warmup; the comparison view passes the
// runtime-startup-only figure instead when both agents report the split, so
// its scatter plots the same quantity its headline chart does.
// `value` rather than the more natural `valueOf`: `{}.valueOf` inherits
// Object.prototype's, so a destructuring default would never fire and the
// no-metric caller would end up calling that instead.
function loadtestWarmupPoints(results, namePrefix, { value = loadtestWarmupMs, label = "warmup" } = {}) {
  const prefix = namePrefix ? `${namePrefix} -- ` : "";
  return loadtestRunOrder(results)
    .filter((r) => value(r) != null)
    .map((r) => ({
      y: value(r),
      iteration: r.iteration,
      status: r.error ? "error" : "ok",
      title: `${prefix}user ${r.user} iter ${r.iteration}: ${label} ${(value(r) / 1000).toFixed(1)}s`,
    }));
}

// Everything the agent platform did before any of the agent's own code ran,
// with the agent's imports and per-session construction subtracted back out
// (deployers/aws.py computes it as cold_start_ms - agent_init_ms). AWS-only,
// so anything keyed off this has to fall back to the full warmup on platforms
// that don't report the split.
const LOADTEST_PLATFORM_STARTUP_METRIC = {
  value: (r) => r.platform_startup_ms,
  label: "platform startup",
  stats: (s) => s.platform_startup_ms,
  scatterTitle: "Platform startup",
};

// The comparison view leads with platform startup rather than the full warmup
// because the number under test is the platform's, and a full-warmup headline
// adds this agent's own imports and construction on top of it -- which moves
// when the agent's dependencies change, making "is this platform under 2s"
// unanswerable from the chart that's meant to answer it. The full warmup
// stays, one card down, since that total is what a user actually waits
// through.
const LOADTEST_PLATFORM_STARTUP_NOTE =
  "What the agent platform itself spent before a single line of the agent's own code ran -- the agent's imports and per-session setup are excluded, so this is the provider's latency on its own and is what compares like-for-like against another provider's. The full wait, both parts together, is the next chart down.";

// The percentile ladder the warmup chart reports. p99 is included knowing
// exactly what it's worth at this harness's sample sizes -- see
// compute_summary's own note in perf/loadtest.py, and the chart note below,
// which says so on screen rather than letting the label imply a real tail
// estimate.
const LOADTEST_WARMUP_PERCENTILES = ["p50", "p75", "p95", "p99"];

// Shown wherever that p99 bar is, so the label can't imply a tail estimate it
// isn't -- or, on a long run, so it doesn't disclaim one it now is. Which of
// the two it says depends on the run's own sample count, because that's what
// changed: a loop run of 500 sessions puts real observations either side of
// the 99th percentile, while a 20-session burst still can't. The boundary is
// where interpolation stops being between the two slowest points.
// See compute_summary's own note in perf/loadtest.py.
const LOADTEST_P99_SAMPLE_FLOOR = 100;

function loadtestP99Note(n) {
  return n >= LOADTEST_P99_SAMPLE_FLOOR
    ? `Over ${n} sessions p99 is a real tail estimate -- several sessions land above it, so it reflects how bad a slow start actually gets rather than just naming the slowest one.`
    : `At this run's sample size (${n} session${n === 1 ? "" : "s"}) p99 always lands between the two slowest sessions, so read it as "the worst one" rather than as a tail estimate. A loop run of 100+ sessions gives a real one.`;
}

// Session-start-only mode: no LLM call happened, so there's no TTFA/token/
// tool-call story to show at all -- only the cost of starting a session.
//
// The headline is platform startup, not the total: this mode exists to
// measure what the *platform* costs, and that's the number that can be held
// up against another provider's, because it doesn't move when this agent's
// dependencies or tool set change (see perf/loadtest.py's module docstring).
// So platform startup gets the first percentile chart and the first scatter,
// with the full session warmup -- what a user actually waits through --
// below it. On a platform that doesn't report the split, the full warmup
// leads by default and says so, rather than a total quietly standing in for
// a platform number.
//
// The percentile ladders were stat tiles reading "2.8s / 3.1s / 4.9s". Four
// numbers in a row is the one thing a bar chart is unambiguously better at
// than text -- the shape of the tail is the whole point, and tiles make p50
// and p99 look like equally-weighted trivia.
//
// comparison:true strips this down to what the two-agent view above it
// hasn't already said: the percentile bars and the per-session scatters all
// go, because that view charts both agents' percentiles side by side and
// overlays their sessions on one shared axis -- rendering each agent's own
// copy underneath is the same data a second time on a worse axis. What
// survives per agent is the session/error count and the startup split, minus
// the platform-startup half that view leads with.
function renderWarmupOnlyLoadtestResults(results, summary, containerWidth, { comparison = false } = {}) {
  const warmup = loadtestWarmupStats(summary);
  const platform = LOADTEST_PLATFORM_STARTUP_METRIC.stats(summary);
  // AWS only (see deployers/aws.py's latest_platform_startup_ms docstring) --
  // on Azure/Gemini nothing measures the handoff, so these are all-None and
  // every platform-startup card is skipped rather than showing a misleading
  // "n/a" for a split those runs can't report.
  const hasSplit = platform != null && platform.mean != null && summary.agent_init_ms.mean != null;
  // Sits next to Errors because it answers the same kind of question -- is
  // this run trustworthy -- rather than being a latency figure in its own
  // right. p95 is the headline of the three: a client-side queue is a
  // contention effect, so the median stays flat while the tail is what moves.
  const queue = summary.client_queue_ms;
  const hasQueue = queue != null && queue.mean != null;
  const statsHtml = `<div class="loadtest-stats">
    ${loadtestStatTile("Sessions", String(summary.total))}
    ${loadtestStatTile("Errors", `${summary.errors} / ${summary.total}`, null, summary.errors > 0)}
    ${
      hasQueue
        ? loadtestStatTile(
            "Client queue p95",
            loadtestFmtMs(queue.p95),
            `p50 ${loadtestFmtMs(queue.p50)} / max ${loadtestFmtMs(queue.max)}`,
            false,
            queue.p95 >= LOADTEST_CLIENT_QUEUE_WARN_MS
          )
        : ""
    }
  </div>`;

  // The percentile ladder for one series, as a bar chart. Both ladders on
  // this screen are the same shape, so they're built the same way -- the
  // only difference is which stats object and note they carry.
  const percentileChart = (title, stats, note) => `<div class="chart-card">
    <div class="chart-card-header"><span class="chart-card-title">${title}</span></div>
    ${loadtestBarSvg(
      LOADTEST_WARMUP_PERCENTILES.map((pct) => ({
        label: pct,
        value: stats[pct],
        title: `${pct}: ${loadtestFmtMs(stats[pct])}`,
      })),
      containerWidth
    )}
    <div class="chart-note">${note}</div>
  </div>`;

  // P99_NOTE rides on whichever ladder comes first: it's about this
  // harness's sample size, which is the same for both, and saying it twice
  // on one screen reads as a rendering bug.
  const platformPercentileChart =
    comparison || !hasSplit
      ? ""
      : percentileChart("Platform startup latency", platform, `${LOADTEST_PLATFORM_STARTUP_NOTE} ${loadtestP99Note(summary.total)}`);

  // The "(per session)" suffix is built here rather than passed in, so the
  // ", by iteration" half can't drift outside the parentheses.
  const scatterChart = (title, points, note) => `<div class="chart-card">
    <div class="chart-card-header"><span class="chart-card-title">${title} (per session${loadtestHasIterationAxis(points) ? ", by iteration" : ""})</span>${loadtestChartLegendHtml}</div>
    ${loadtestScatterSvg(points, containerWidth)}
    ${note ? `<div class="chart-note">${note}</div>` : ""}
  </div>`;

  const platformPoints = loadtestWarmupPoints(results, null, LOADTEST_PLATFORM_STARTUP_METRIC);
  const platformOmitted = results.length - platformPoints.length;
  const platformScatter =
    comparison || !hasSplit
      ? ""
      : scatterChart(
          "Platform startup",
          platformPoints,
          `Every individual session behind those percentiles, left to right in run order${loadtestHasIterationAxis(platformPoints) ? " and grouped by iteration" : ""}.${platformOmitted > 0 ? ` ${platformOmitted} session(s) omitted -- no platform-startup figure was ever reported for them.` : ""}`
        );

  const warmupPercentileChart = comparison
    ? ""
    : percentileChart(
        "Full session warmup",
        warmup,
        hasSplit
          ? LOADTEST_WARMUP_NOTE
          : `${LOADTEST_WARMUP_NOTE} This platform doesn't report how much of it was the platform's own startup versus this agent's initialization, so unlike the AWS runs it can't be compared against another provider's platform number -- only against another run of the same agent. ${loadtestP99Note(summary.total)}`
      );

  const warmupPoints = loadtestWarmupPoints(results);
  const warmupOmitted = results.length - warmupPoints.length;

  // No LOADTEST_WARMUP_NOTE here: the percentile chart directly above
  // already carries it, and repeating that paragraph twice on one screen
  // reads as a rendering bug. In the comparison view the grouped percentile
  // card carries it instead, at that view's own first mention of warmup.
  const warmupChart = comparison
    ? ""
    : scatterChart(
        "Full session warmup",
        warmupPoints,
        `Every individual session behind those percentiles, left to right in run order${loadtestHasIterationAxis(warmupPoints) ? " and grouped by iteration" : ""}.${warmupOmitted > 0 ? ` ${warmupOmitted} errored session(s) omitted -- no warmup signal was ever received.` : ""}`
      );

  let splitHtml = "";
  if (hasSplit) {
    // In the comparison view the platform-startup half is already the
    // headline chart, for both agents at four percentiles -- repeating its
    // p50/p95 as tiles down here is the same numbers a third time. What's
    // left is the half that view doesn't chart: agent initialization.
    const splitStats = `<div class="loadtest-stats">
      ${comparison ? "" : loadtestStatTile("Platform startup p50", loadtestFmtMs(platform.p50))}
      ${comparison ? "" : loadtestStatTile("Platform startup p95", loadtestFmtMs(platform.p95))}
      ${loadtestStatTile("Agent initialization p50", loadtestFmtMs(summary.agent_init_ms.p50))}
      ${loadtestStatTile("Agent initialization p95", loadtestFmtMs(summary.agent_init_ms.p95))}
    </div>`;
    const splitIntro = comparison
      ? "The other half of the session start: agent initialization -- this agent's own imports and per-session setup (Agent + MCP client construction), which the platform-startup chart above deliberately excludes. Add it to that chart's numbers to get the full warmup."
      : "That warmup is these two parts added together: platform startup (everything the provider did before this agent's code got control) and agent initialization (this agent's own imports and per-session setup -- Agent + MCP client construction). Same two parts the chat panel's latency breakdown shows per turn.";
    splitHtml = `<div class="chart-note">${splitIntro}</div>
      ${splitStats}`;
  }

  // In the comparison view this is rendered once above both agents' details
  // (renderComparisonResults), not twice down here -- the two runs shared the
  // one process this warns about, so one warning is the accurate count.
  const queueCallout = comparison ? "" : loadtestClientQueueCalloutHtml([summary]);

  return (
    queueCallout + statsHtml + platformPercentileChart + platformScatter + splitHtml + warmupPercentileChart + warmupChart
  );
}

// Renders a single-series bar chart as a plain SVG string -- the percentile
// ladder for one agent. The grouped variant below is the two-agent version
// of the same idea; they stay separate rather than one function with an
// optional second series because the grouped one's whole geometry (paired
// bars, a gap, half-width bars) exists only to fit two, and threading
// "actually just one" through it made both cases harder to read than either
// is alone.
//
// Values are printed above the bars, which a chart usually shouldn't need
// -- here it earns its place: these bars replaced stat tiles showing those
// exact numbers, and dropping the digits to gain the shape would have been
// a trade, not an improvement. padding.top leaves room for that text so the
// tallest bar's own label can't clip out of the viewBox.
function loadtestBarSvg(bars, containerWidthPx) {
  const width = Math.max(240, containerWidthPx || 600);
  const height = 170;
  const padding = { top: 22, right: 10, bottom: 30, left: 46 };
  const plotW = width - padding.left - padding.right;
  const plotH = height - padding.top - padding.bottom;
  const maxY = Math.max(1, ...bars.map((b) => b.value).filter((v) => v != null));
  const slotW = plotW / bars.length;
  const barW = Math.min(56, slotW * 0.5);
  const baseline = padding.top + plotH;

  const yFor = (v) => padding.top + plotH - (v / maxY) * plotH;

  const grid = [0, 0.5, 1]
    .map((f) => {
      const y = padding.top + plotH * (1 - f);
      const label = Math.round(maxY * f).toLocaleString();
      return (
        `<line x1="${padding.left}" y1="${y.toFixed(1)}" x2="${width - padding.right}" y2="${y.toFixed(1)}" class="chart-gridline" />` +
        `<text x="${padding.left - 6}" y="${(y + 3).toFixed(1)}" class="chart-axis-label" text-anchor="end">${label}</text>`
      );
    })
    .join("");

  const body = bars
    .map((b, i) => {
      const centerX = padding.left + slotW * (i + 0.5);
      const axisLabel = `<text x="${centerX.toFixed(1)}" y="${(height - padding.bottom + 16).toFixed(1)}" class="chart-axis-label" text-anchor="middle">${escapeHtml(b.label)}</text>`;
      if (b.value == null) {
        return `<text x="${centerX.toFixed(1)}" y="${(baseline - 4).toFixed(1)}" class="chart-bar-value" text-anchor="middle">--</text>${axisLabel}`;
      }
      const top = yFor(b.value);
      return (
        `<rect x="${(centerX - barW / 2).toFixed(1)}" y="${top.toFixed(1)}" width="${barW.toFixed(1)}" height="${(baseline - top).toFixed(1)}" rx="3" class="chart-bar"><title>${escapeHtml(b.title)}</title></rect>` +
        `<text x="${centerX.toFixed(1)}" y="${(top - 6).toFixed(1)}" class="chart-bar-value" text-anchor="middle">${escapeHtml(loadtestFmtMs(b.value))}</text>` +
        axisLabel
      );
    })
    .join("");

  return `<svg viewBox="0 0 ${width} ${height}" class="chart-svg" style="height:${height}px">${grid}${body}</svg>`;
}

// Renders a grouped bar chart (2 bars per category) as a plain SVG string
// -- the comparison headline: the same fixed set of summary percentiles,
// side by side. The raw per-session points get overlaid separately (see
// loadtestOverlayScatterSvg), which is a distribution-vs-distribution read
// on a shared y-axis rather than a paired one -- neither run order nor
// completion order pairs across two independently-paced agents, so nothing
// here or there lines up agent A's 3rd session with agent B's 3rd.
function loadtestGroupedBarSvg(groups, containerWidthPx) {
  const width = Math.max(240, containerWidthPx || 600);
  const height = 170;
  // top: 22 rather than 10 so the tallest bar's own value label has room
  // above it inside the viewBox (see showValues below).
  const padding = { top: 22, right: 10, bottom: 30, left: 46 };
  const plotW = width - padding.left - padding.right;
  const plotH = height - padding.top - padding.bottom;
  const maxY = Math.max(1, ...groups.flatMap((g) => [g.a, g.b]).filter((v) => v != null));
  const groupW = plotW / groups.length;
  const barW = Math.min(40, groupW * 0.28);
  const gap = 8;
  const baseline = padding.top + plotH;

  const yFor = (v) => padding.top + plotH - (v / maxY) * plotH;

  const grid = [0, 0.5, 1]
    .map((f) => {
      const y = padding.top + plotH * (1 - f);
      const label = Math.round(maxY * f).toLocaleString();
      return (
        `<line x1="${padding.left}" y1="${y.toFixed(1)}" x2="${width - padding.right}" y2="${y.toFixed(1)}" class="chart-gridline" />` +
        `<text x="${padding.left - 6}" y="${(y + 3).toFixed(1)}" class="chart-axis-label" text-anchor="end">${label}</text>`
      );
    })
    .join("");

  // Values printed above the bars here too, for the same reason the single-
  // series chart does it -- reading a number shouldn't require hovering, and
  // a hover tooltip is invisible in a screenshot and unreachable on a
  // touchscreen. Suppressed only when the container is too narrow for two
  // labels per group to sit side by side without colliding: half a group is
  // the space one label gets, and ~36px is what the widest of them
  // ("10.8s" at 11px mono) needs. The <title> tooltips stay either way, so
  // nothing is lost when they're dropped.
  const showValues = groupW / 2 >= 36;
  const valueText = (x, top, value) =>
    `<text x="${x.toFixed(1)}" y="${(top - 6).toFixed(1)}" class="chart-bar-value" text-anchor="middle">${escapeHtml(loadtestFmtMs(value))}</text>`;

  let bars = "";
  let labels = "";
  groups.forEach((g, i) => {
    const centerX = padding.left + groupW * (i + 0.5);
    if (g.a != null) {
      const top = yFor(g.a);
      const x = centerX - barW - gap / 2;
      bars += `<rect x="${x.toFixed(1)}" y="${top.toFixed(1)}" width="${barW.toFixed(1)}" height="${(baseline - top).toFixed(1)}" rx="3" class="chart-bar agent-a"><title>${escapeHtml(g.titleA)}</title></rect>`;
      if (showValues) labels += valueText(x + barW / 2, top, g.a);
    }
    if (g.b != null) {
      const top = yFor(g.b);
      const x = centerX + gap / 2;
      bars += `<rect x="${x.toFixed(1)}" y="${top.toFixed(1)}" width="${barW.toFixed(1)}" height="${(baseline - top).toFixed(1)}" rx="3" class="chart-bar agent-b"><title>${escapeHtml(g.titleB)}</title></rect>`;
      if (showValues) labels += valueText(x + barW / 2, top, g.b);
    }
    labels += `<text x="${centerX.toFixed(1)}" y="${(height - padding.bottom + 16).toFixed(1)}" class="chart-axis-label" text-anchor="middle">${escapeHtml(g.label)}</text>`;
  });

  return `<svg viewBox="0 0 ${width} ${height}" class="chart-svg" style="height:${height}px">${grid}${bars}${labels}</svg>`;
}

// percentiles defaults to the p50/p75/p95 ladder chat mode compares on;
// warmup mode passes the full one including p99, since this card is the only
// place that view shows warmup percentiles at all (see
// renderComparisonResults) -- so the note goes here too, next to the p99 bar
// it's about.
function loadtestComparisonBarChartCard(title, agentNames, statsA, statsB, containerWidth, { percentiles = ["p50", "p75", "p95"], note = "" } = {}) {
  const groups = percentiles.map((pct) => ({
    label: pct,
    a: statsA[pct],
    b: statsB[pct],
    titleA: `${agentNames[0]} ${pct}: ${loadtestFmtMs(statsA[pct])}`,
    titleB: `${agentNames[1]} ${pct}: ${loadtestFmtMs(statsB[pct])}`,
  }));
  const legend = `<div class="chart-legend">
    <span><span class="chart-legend-swatch agent-a"></span>${escapeHtml(agentNames[0])}</span>
    <span><span class="chart-legend-swatch agent-b"></span>${escapeHtml(agentNames[1])}</span>
  </div>`;
  return `<div class="chart-card">
    <div class="chart-card-header"><span class="chart-card-title">${escapeHtml(title)}</span>${legend}</div>
    ${loadtestGroupedBarSvg(groups, containerWidth)}
    ${note ? `<div class="chart-note">${note}</div>` : ""}
  </div>`;
}

// Both agents' warmup sessions on one shared y-axis, coded by agent twice
// over (color and shape) -- the single most useful chart in the comparison
// view, and the reason it replaces the two per-agent scatters it used to
// sit above: a 3x gap between two clouds is obvious here and invisible
// when each cloud has its own auto-scaled axis.
//
// Error status keeps a channel of its own (a red outline) rather than the
// fill it owns in the single-agent chart, because fill is already spoken
// for by agent identity throughout this view -- the bars, the legend
// swatches and the per-agent headings all use it that way.
const LOADTEST_AGENT_SHAPES = ["circle", "diamond"];

function loadtestComparisonWarmupScatterCard(agentIds, agentNames, resultsByAgent, containerWidth, metric = {}) {
  const label = metric.label || "warmup";
  const series = agentIds.map((agentId, i) => ({
    colorClass: i === 0 ? "agent-a" : "agent-b",
    shape: LOADTEST_AGENT_SHAPES[i],
    points: loadtestWarmupPoints(resultsByAgent[agentId] || [], agentNames[i], metric),
  }));
  const omitted = agentIds.reduce(
    (sum, agentId, i) => sum + (resultsByAgent[agentId] || []).length - series[i].points.length,
    0
  );
  const anyErrors = series.some((s) => s.points.some((p) => p.status === "error"));

  // The legend swatches are the real markers, drawn by the same function as
  // the plot -- a CSS-shaped approximation of a diamond would be one more
  // thing that can silently stop matching what's in the chart.
  const legend = `<div class="chart-legend">
    ${series
      .map(
        (s, i) =>
          `<span><svg class="chart-legend-marker" viewBox="0 0 12 12" aria-hidden="true">${loadtestMarkerSvg(s.shape, 6, 6, s.colorClass, "")}</svg>${escapeHtml(agentNames[i])}</span>`
      )
      .join("")}
    ${anyErrors ? `<span><svg class="chart-legend-marker" viewBox="0 0 12 12" aria-hidden="true">${loadtestMarkerSvg("circle", 6, 6, "errored", "")}</svg>Errored session</span>` : ""}
  </div>`;

  return `<div class="chart-card">
    <div class="chart-card-header"><span class="chart-card-title">${escapeHtml(metric.scatterTitle || "Full session warmup")} (per session, both agents)</span>${legend}</div>
    ${loadtestOverlayScatterSvg(series, containerWidth)}
    <div class="chart-note">Every individual session behind the percentiles above. Both agents share one y-axis, so the vertical gap between the two clouds is the real difference. Left to right is each agent's own run order${series.some((s) => loadtestHasIterationAxis(s.points)) ? ", grouped and numbered by iteration" : ""} -- the two agents run one after the other, not on a shared clock, so read this as one distribution against the other, not point against point.${omitted > 0 ? ` ${omitted} errored session(s) omitted -- no ${escapeHtml(label)} signal was ever received.` : ""}</div>
  </div>`;
}

// A picker rather than a field name so the warmup case can go through
// loadtestWarmupStats and get the same cold_start_ms-preferring choice the
// single-agent view makes -- otherwise the comparison callout would quote a
// different warmup number than the stat tiles right below it.
function loadtestPrimaryMetric(mode, platformOnly) {
  if (mode !== "warmup_only") return { stats: (s) => s.ttfa_ms, label: "TTFA" };
  return platformOnly
    ? { stats: LOADTEST_PLATFORM_STARTUP_METRIC.stats, label: LOADTEST_PLATFORM_STARTUP_METRIC.label }
    : { stats: loadtestWarmupStats, label: "warmup" };
}

// The plain-language headline -- sometimes a sentence beats a chart for
// the instant "so which one do I use" takeaway. Based on p50 of whichever
// metric is primary for the mode (platform startup, or the full warmup where
// that isn't reported, for platform-startup-only; ttfa_ms
// for a full chat turn), since that's the first stat either results view
// already leads with.
function loadtestComparisonCalloutHtml(agentIds, agentNames, summaries, mode, platformOnly) {
  const { stats, label } = loadtestPrimaryMetric(mode, platformOnly);
  const pA = stats(summaries[agentIds[0]]).p50;
  const pB = stats(summaries[agentIds[1]]).p50;
  const [nameA, nameB] = agentNames;

  if (pA == null && pB == null) {
    return `<div class="loadtest-callout">Neither agent had a successful session to compare (${escapeHtml(label)} p50).</div>`;
  }
  if (pA == null) {
    return `<div class="loadtest-callout"><b>${escapeHtml(nameA)}</b> had no successful sessions to compare -- ${escapeHtml(nameB)} completed with a ${escapeHtml(label)} p50 of ${loadtestFmtMs(pB)}.</div>`;
  }
  if (pB == null) {
    return `<div class="loadtest-callout"><b>${escapeHtml(nameB)}</b> had no successful sessions to compare -- ${escapeHtml(nameA)} completed with a ${escapeHtml(label)} p50 of ${loadtestFmtMs(pA)}.</div>`;
  }
  if (pA === pB) {
    return `<div class="loadtest-callout">${escapeHtml(nameA)} and ${escapeHtml(nameB)} were about even (${escapeHtml(label)} p50: ${loadtestFmtMs(pA)}).</div>`;
  }
  const aFaster = pA < pB;
  const fasterName = aFaster ? nameA : nameB;
  const slowerName = aFaster ? nameB : nameA;
  const ratio = (aFaster ? pB / pA : pA / pB).toFixed(1);
  return `<div class="loadtest-callout"><b>${escapeHtml(fasterName)}</b> was ${ratio}x faster than ${escapeHtml(slowerName)} (${escapeHtml(label)} p50: ${loadtestFmtMs(Math.min(pA, pB))} vs ${loadtestFmtMs(Math.max(pA, pB))}).</div>`;
}

// Headline (callout + bar chart per metric), then -- in warmup mode -- the
// two agents' raw sessions overlaid on one chart, then each agent's numbers
// underneath as detail.
//
// Warmup mode leads with platform startup, not the full warmup: the thing
// being compared across two platforms is the platform's own latency, and the
// full warmup folds each agent's imports and construction into it -- so two
// agents with different dependency sets would differ on the full-warmup chart
// for a reason that has nothing to do with the platform. The full warmup
// follows immediately, so the number a user actually waits through is still
// on screen and the two are readable against each other.
//
// Warmup mode also doesn't reuse the single-agent view wholesale the way chat
// mode still does: two copies of the same scatter, each on its own axis, is a
// worse read of a two-agent run than one overlaid chart.
function renderComparisonResults(agentIds, agentNames, resultsByAgent, summaries, mode, containerWidth) {
  const warmupMode = mode === "warmup_only";
  // AWS-only quantity (deployers/aws.py derives it as cold_start_ms -
  // agent_init_ms), and only meaningful here if *both* agents report it -- a
  // platform-startup headline with one bar missing would compare a platform
  // number against a platform-plus-agent one. Anywhere it isn't available on
  // both sides (Azure/Gemini, or an all-errored run) this view stays exactly
  // as it was: full warmup throughout.
  const platformOnly =
    warmupMode &&
    agentIds.every((id) => {
      const stats = LOADTEST_PLATFORM_STARTUP_METRIC.stats(summaries[id]);
      return stats != null && stats.mean != null;
    });
  const callout = loadtestComparisonCalloutHtml(agentIds, agentNames, summaries, mode, platformOnly);
  // The smaller of the two runs decides the p99 wording: one note covers both
  // ladders, so it has to be true of the weaker sample.
  const p99Note = loadtestP99Note(Math.min(...agentIds.map((id) => summaries[id].total)));

  const warmupBars = (title, note) =>
    loadtestComparisonBarChartCard(
      title, agentNames, loadtestWarmupStats(summaries[agentIds[0]]), loadtestWarmupStats(summaries[agentIds[1]]), containerWidth,
      { percentiles: LOADTEST_WARMUP_PERCENTILES, note }
    );

  let barCharts;
  if (platformOnly) {
    // p99 note rides on the first chart only: both ladders are the same four
    // percentiles, and saying the same paragraph twice one card apart reads as
    // a rendering bug.
    barCharts =
      loadtestComparisonBarChartCard(
        "Platform startup", agentNames,
        LOADTEST_PLATFORM_STARTUP_METRIC.stats(summaries[agentIds[0]]),
        LOADTEST_PLATFORM_STARTUP_METRIC.stats(summaries[agentIds[1]]),
        containerWidth,
        { percentiles: LOADTEST_WARMUP_PERCENTILES, note: `${LOADTEST_PLATFORM_STARTUP_NOTE} ${p99Note}` }
      ) +
      warmupBars("Full session warmup (platform startup + agent initialization)", LOADTEST_WARMUP_NOTE);
  } else if (warmupMode) {
    barCharts = warmupBars("Full session warmup", `${LOADTEST_WARMUP_NOTE} ${p99Note}`);
  } else {
    barCharts =
      loadtestComparisonBarChartCard(
        "Time to first activity", agentNames, summaries[agentIds[0]].ttfa_ms, summaries[agentIds[1]].ttfa_ms, containerWidth
      ) +
      loadtestComparisonBarChartCard(
        "Total turn time", agentNames, summaries[agentIds[0]].elapsed_ms, summaries[agentIds[1]].elapsed_ms, containerWidth
      );
  }

  // The scatter plots whatever the headline chart measures, so "the sessions
  // behind those percentiles" is literally true rather than nearly true.
  const overlayChart = warmupMode
    ? loadtestComparisonWarmupScatterCard(
        agentIds, agentNames, resultsByAgent, containerWidth,
        platformOnly ? LOADTEST_PLATFORM_STARTUP_METRIC : {}
      )
    : "";

  const detail = agentIds
    .map((agentId, i) => {
      const results = resultsByAgent[agentId] || [];
      const summary = summaries[agentId];
      const colorClass = i === 0 ? "agent-a" : "agent-b";
      const body = warmupMode
        ? renderWarmupOnlyLoadtestResults(results, summary, containerWidth, { comparison: true })
        : renderChatLoadtestResults(results, summary, containerWidth);
      return `<div class="loadtest-compare-agent-detail">
        <div class="loadtest-compare-agent-title"><span class="agent-color-dot ${colorClass}"></span>${escapeHtml(agentNames[i])}</div>
        ${body}
      </div>`;
    })
    .join("");

  // Above the headline comparison, not below it: if the portal itself was the
  // bottleneck then "A was 1.4x faster than B" is a claim about this machine,
  // and that has to be visible before the claim is read, not after.
  return (
    loadtestClientQueueCalloutHtml(agentIds.map((id) => summaries[id])) + callout + barCharts + overlayChart + detail
  );
}

function renderLoadtestResults(agentIds, agentNames, resultsByAgent, summaries, mode) {
  // Unhidden first -- clientWidth reads 0 while a block is display:none, so
  // measuring before this would silently fall back to the 600px default on
  // every single render, not just when actually appropriate.
  loadtestResultsEl.classList.remove("hidden");
  const containerWidth = loadtestResultsEl.clientWidth;

  if (agentIds.length === 2) {
    loadtestResultsEl.innerHTML = renderComparisonResults(agentIds, agentNames, resultsByAgent, summaries, mode, containerWidth);
    return;
  }
  const agentId = agentIds[0];
  loadtestResultsEl.innerHTML =
    mode === "warmup_only"
      ? renderWarmupOnlyLoadtestResults(resultsByAgent[agentId] || [], summaries[agentId], containerWidth)
      : renderChatLoadtestResults(resultsByAgent[agentId] || [], summaries[agentId], containerWidth);
}

// How many session lines each tail keeps. Small on purpose: this is a
// "what's happening right now" window, not a record of the run -- the run
// itself is the results panel below, and Download JSON is how a run gets
// kept. A 500-session loop run appended to a scrolling list produced a block
// that grew past the viewport and a scrollbar nobody could read at 2
// rows/second; the aggregates next to it are what actually answer "is this
// going well".
const LOADTEST_PROGRESS_TAIL_ROWS = 5;

// Keeps a tail element at its cap, oldest-first. Trimming as rows arrive
// (rather than capping at render time) is what keeps the block a fixed
// height for the whole run, so nothing below it moves.
function loadtestPushTailRow(el, row, cap = LOADTEST_PROGRESS_TAIL_ROWS) {
  el.appendChild(row);
  while (el.childElementCount > cap) el.removeChild(el.firstElementChild);
}

// One collapsible-free progress block per agent (1 or 2), built fresh for
// every run since agent count/identity varies -- rather than static HTML
// for a fixed number of agents. Colored dots only show once there's an
// actual second agent to distinguish from (a single-agent run has nothing
// to compare its color against, so the dot would be noise).
//
// Errors get their own capped tail rather than sharing the session tail: in a
// run where most sessions succeed, five successes would push the one failure
// out of view within seconds, and the failure is the thing worth seeing. That
// separation is the one thing a bounded tail would otherwise lose.
function buildLoadtestProgressBlocks(agentIds, agentNames, perAgentTotal) {
  loadtestProgressEl.innerHTML = "";
  const refs = {};
  agentIds.forEach((agentId, i) => {
    const colorClass = i === 0 ? "agent-a" : "agent-b";
    const dotHtml = agentIds.length === 2 ? `<span class="agent-color-dot ${colorClass}"></span>` : "";
    // Agents run one at a time now (see server.py's loadtest_ws), so every
    // block after the first sits idle until its turn -- "waiting..."
    // instead of "0 / N" makes that a deliberate state, not a stuck one.
    const initialCount = i === 0 ? `0 / ${perAgentTotal}` : "waiting...";
    const wrap = document.createElement("div");
    wrap.className = "loadtest-progress-agent";
    wrap.innerHTML = `
      <div class="loadtest-progress-agent-header">
        <span class="loadtest-progress-agent-name">${dotHtml}${escapeHtml(agentNames[i])}</span>
        <span class="loadtest-progress-agent-count">${initialCount}</span>
      </div>
      <div class="progress-bar-track"><div class="progress-bar-fill"></div></div>
      <div class="loadtest-running-stats"></div>
      <div class="loadtest-log"></div>
      <div class="loadtest-error-tail hidden">
        <div class="loadtest-error-tail-header"></div>
        <div class="loadtest-error-tail-rows"></div>
      </div>
    `;
    loadtestProgressEl.appendChild(wrap);
    refs[agentId] = {
      countEl: wrap.querySelector(".loadtest-progress-agent-count"),
      fillEl: wrap.querySelector(".progress-bar-fill"),
      statsEl: wrap.querySelector(".loadtest-running-stats"),
      logEl: wrap.querySelector(".loadtest-log"),
      errorTailEl: wrap.querySelector(".loadtest-error-tail"),
      errorHeaderEl: wrap.querySelector(".loadtest-error-tail-header"),
      errorRowsEl: wrap.querySelector(".loadtest-error-tail-rows"),
      // Per-agent, not per-run: agents run in sequence, so agent B's elapsed
      // clock has to start when its first session does, not when the run did.
      startedAt: null,
      total: perAgentTotal,
      errors: 0,
      summary: null,
    };
  });
  return refs;
}

// Elapsed / remaining for one agent's leg of the run. The estimate is
// deliberately naive -- completed sessions divided into elapsed time -- which
// is right for a loop run, where sessions are strictly sequential and the
// per-session cost is what's being measured. Suppressed below 3 sessions,
// where one slow cold start would project a wildly wrong finish time.
const loadtestFmtDuration = (ms) => {
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  return s % 60 === 0 ? `${m}m` : `${m}m ${s % 60}s`;
};

function loadtestEtaText(refs, completed, total) {
  if (!refs.startedAt || completed <= 0) return "";
  const elapsedMs = Date.now() - refs.startedAt;
  const parts = [`elapsed ${loadtestFmtDuration(elapsedMs)}`];
  if (completed >= 3 && completed < total) {
    parts.push(`~${loadtestFmtDuration((elapsedMs / completed) * (total - completed))} left`);
  }
  return parts.join(" -- ");
}

// The aggregates that replace the scrolling log: where the distribution sits
// so far, how many sessions failed, and how long this has left to run. Drawn
// from the server's own interim `compute_summary` rather than recomputed here,
// so a mid-run p50 and the final p50 can't disagree about what they mean.
function renderLoadtestRunningStats(refs, mode, completed, total) {
  const bits = [];
  if (refs.summary) {
    const platformOnly =
      mode === "warmup_only" &&
      LOADTEST_PLATFORM_STARTUP_METRIC.stats(refs.summary) != null &&
      LOADTEST_PLATFORM_STARTUP_METRIC.stats(refs.summary).mean != null;
    const { stats, label } = loadtestPrimaryMetric(mode, platformOnly);
    const s = stats(refs.summary);
    if (s && s.p50 != null) bits.push(`${label} p50 ${loadtestFmtMs(s.p50)}`, `p95 ${loadtestFmtMs(s.p95)}`);
  }
  if (refs.errors > 0) bits.push(`${refs.errors} error(s)`);
  const eta = loadtestEtaText(refs, completed, total);
  if (eta) bits.push(eta);
  refs.statsEl.textContent = bits.join("  ·  ");
}

loadtestConfigForm.addEventListener("submit", (e) => {
  e.preventDefault();
  const compare = loadtestCompareToggle.checked;
  const agentIdA = loadtestAgentSelect.value;
  const agentIdB = loadtestAgentBSelect.value;
  if (!agentIdA) return;
  if (compare && !agentIdB) return;
  if (compare && agentIdA === agentIdB) {
    loadtestErrorEl.textContent = "Pick two different agents to compare.";
    loadtestErrorEl.style.display = "block";
    return;
  }
  const agentIds = compare ? [agentIdA, agentIdB] : [agentIdA];
  const agentNames = agentIds.map((id) => {
    const a = agents.find((x) => x.id === id);
    return a ? `${a.name} (${a.platform})` : id;
  });

  loadtestErrorEl.style.display = "none";
  loadtestResultsPlaceholder.classList.add("hidden");
  loadtestResultsEl.classList.add("hidden");
  loadtestResultsEl.innerHTML = "";
  loadtestOverallHeader.classList.remove("hidden");
  loadtestOverallHeader.textContent = "Starting...";
  loadtestProgressEl.innerHTML = "";
  loadtestProgressEl.classList.remove("hidden");
  loadtestRunActions.classList.remove("hidden");
  loadtestDownloadBtn.classList.add("hidden");
  setLoadtestFormEnabled(false);

  const resultsByAgent = {};
  for (const id of agentIds) resultsByAgent[id] = [];
  let mode = loadtestModeSelect.value;
  const loop = loadtestShapeSelect.value === "loop";
  let progressRefs = {};
  // Held for Download JSON, which serializes whatever the page has when it's
  // clicked -- so a stopped or errored run is still downloadable.
  loadtestLastRun = { agentIds, agentNames, resultsByAgent };

  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  loadtestWs = new WebSocket(`${protocol}//${location.host}/ws/loadtest`);

  loadtestWs.onopen = () => {
    // A loop run is not a second execution path -- it's this same
    // users x iterations call with users pinned to 1, so one simulated user
    // works through N sessions in sequence (perf/loadtest.py's run_user is
    // already that loop). Everything downstream -- records, summaries,
    // charts, the CLI's own output -- stays identical.
    loadtestWs.send(
      JSON.stringify({
        type: "start",
        agent_ids: agentIds,
        mode,
        users: loop ? 1 : Number(loadtestUsersInput.value),
        // Not read from the (hidden) input in warmup-only mode: one session
        // start per simulated session is the whole unit there.
        iterations: loop
          ? Number(loadtestSessionsInput.value)
          : mode === "warmup_only"
            ? 1
            : Number(loadtestIterationsInput.value),
        message: loadtestMessageInput.value.trim(),
      })
    );
  };

  loadtestWs.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    if (msg.type === "started") {
      mode = msg.mode; // the server's own clamped/validated mode, not just an echo of what was requested
      progressRefs = buildLoadtestProgressBlocks(msg.agent_ids, agentNames, msg.per_agent_total);
      // "x 1 iteration(s)" is noise, so drop the clause entirely when it
      // multiplies by one -- which is always the case in warmup-only mode.
      // A loop run reads as its own shape rather than as "1 concurrent
      // session x 500 iterations", which is technically what it is and
      // tells nobody anything.
      const scale =
        msg.users === 1 && msg.iterations > 1 && loop
          ? `${msg.iterations} session(s), one at a time`
          : `${msg.users} concurrent session(s)` + (msg.iterations > 1 ? ` x ${msg.iterations} iteration(s)` : "");
      loadtestOverallHeader.textContent =
        agentIds.length === 2
          ? `Running ${scale} against ${agentNames[0]}, then ${agentNames[1]} -- one agent at a time, so neither test's numbers reflect the other running at the same time -- 0 / ${msg.total} complete`
          : `Running ${scale} -- 0 / ${msg.total} complete`;
    } else if (msg.type === "session_result") {
      resultsByAgent[msg.agent_id].push(msg);
      loadtestOverallHeader.textContent = `${msg.completed} / ${msg.total} complete`;
      const refs = progressRefs[msg.agent_id];
      if (refs) {
        if (refs.startedAt == null) refs.startedAt = Date.now();
        const pct = Math.round((msg.agent_completed / msg.agent_total) * 100);
        refs.fillEl.style.width = `${pct}%`;
        refs.countEl.textContent = `${msg.agent_completed} / ${msg.agent_total}`;
        const row = document.createElement("div");
        row.className = "loadtest-log-row" + (msg.error ? " error" : "");
        if (msg.error) {
          row.textContent = `user ${msg.user} iter ${msg.iteration}: ERROR -- ${msg.error}`;
        } else if (mode === "warmup_only") {
          // Platform startup leads, since it's what this mode measures -- with
          // the whole session start after it so the two are visible together.
          // Falls back to the total alone where the platform doesn't report
          // the split, rather than letting a total read as a platform number.
          const total = `${((msg.cold_start_ms != null ? msg.cold_start_ms : msg.warmup_ms) / 1000).toFixed(1)}s`;
          row.textContent =
            msg.platform_startup_ms != null
              ? `user ${msg.user} iter ${msg.iteration}: platform startup ${(msg.platform_startup_ms / 1000).toFixed(1)}s, session ready in ${total}`
              : `user ${msg.user} iter ${msg.iteration}: session ready in ${total}`;
        } else {
          row.textContent = `user ${msg.user} iter ${msg.iteration}: TTFA ${(msg.ttfa_ms / 1000).toFixed(1)}s, total ${(msg.elapsed_ms / 1000).toFixed(1)}s`;
        }
        loadtestPushTailRow(refs.logEl, row);
        if (msg.error) {
          refs.errors += 1;
          refs.errorTailEl.classList.remove("hidden");
          refs.errorHeaderEl.textContent = `${refs.errors} error(s)${refs.errors > LOADTEST_PROGRESS_TAIL_ROWS ? ` -- last ${LOADTEST_PROGRESS_TAIL_ROWS}` : ""}`;
          const errRow = document.createElement("div");
          errRow.className = "loadtest-log-row error";
          errRow.textContent = `user ${msg.user} iter ${msg.iteration}: ${msg.error}`;
          loadtestPushTailRow(refs.errorRowsEl, errRow);
        }
        renderLoadtestRunningStats(refs, mode, msg.agent_completed, msg.agent_total);
      }
    } else if (msg.type === "progress_summary") {
      // Interim numbers, same shape and same compute_summary as the final
      // ones. Re-rendering the whole results panel here is what makes a
      // 20-minute run watchable: charts and percentiles fill in as it goes
      // instead of appearing all at once at the end.
      for (const agentId of msg.agent_ids) {
        const refs = progressRefs[agentId];
        if (refs) {
          refs.summary = msg.summaries[agentId];
          renderLoadtestRunningStats(refs, mode, resultsByAgent[agentId].length, refs.total);
        }
      }
      renderLoadtestResults(msg.agent_ids, agentNames, resultsByAgent, msg.summaries, mode);
    } else if (msg.type === "done") {
      const totalDone = Object.values(resultsByAgent).reduce((n, arr) => n + arr.length, 0);
      loadtestOverallHeader.textContent = msg.stopped
        ? `Stopped -- ${totalDone} session(s) completed before the stop, and they're included below`
        : `Done -- ${totalDone} session(s) complete`;
      renderLoadtestResults(msg.agent_ids, agentNames, resultsByAgent, msg.summaries, mode);
      setLoadtestFormEnabled(true);
      // Only offered once there's something to download; a run that errored
      // out before its first session has nothing to write.
      if (totalDone > 0) loadtestDownloadBtn.classList.remove("hidden");
      else loadtestRunActions.classList.add("hidden");
    } else if (msg.type === "error") {
      loadtestErrorEl.textContent = msg.detail;
      loadtestErrorEl.style.display = "block";
      setLoadtestFormEnabled(true);
      // A rejected run never had a session, so both buttons are moot -- and an
      // empty run-actions row is a stray gap above the error message.
      loadtestRunActions.classList.add("hidden");
    }
  };

  loadtestWs.onclose = () => {
    // Closed before a "done"/"error" ever arrived, e.g. the server process
    // itself restarted mid-test -- otherwise the form would stay disabled
    // forever with no way to retry.
    if (loadtestStartBtn.disabled) {
      loadtestErrorEl.textContent = "Connection closed before the test finished.";
      loadtestErrorEl.style.display = "block";
      setLoadtestFormEnabled(true);
    }
  };
});

// Stop asks the server to end the run and keep what it has (see
// server.py's loadtest_ws) rather than closing the socket, which would throw
// away every completed session. The button disables itself immediately: the
// stop lands after the session already in flight finishes, which on a cold
// start is several seconds of apparently-nothing-happening.
loadtestStopBtn.addEventListener("click", () => {
  if (!loadtestWs || loadtestWs.readyState !== WebSocket.OPEN) return;
  loadtestWs.send(JSON.stringify({ type: "stop" }));
  loadtestStopBtn.disabled = true;
  loadtestStopBtn.textContent = "Stopping...";
});

loadtestDownloadBtn.addEventListener("click", () => {
  if (!loadtestLastRun) return;
  const { agentIds, agentNames, resultsByAgent } = loadtestLastRun;
  // Flat records with the agent they came from, so a comparison run
  // downloads as one file that can still be split by agent. The websocket
  // envelope's own progress fields are dropped: they describe the streaming,
  // not the session, and keeping them would make these records differ from
  // the ones perf/loadtest.py --output writes.
  const records = agentIds.flatMap((id, i) =>
    (resultsByAgent[id] || []).map(({ type, completed, total, agent_completed, agent_total, ...rec }) => ({
      ...rec,
      agent_name: agentNames[i],
    }))
  );
  const blob = new Blob([JSON.stringify(records, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `loadtest-${new Date().toISOString().replace(/[:.]/g, "-")}.json`;
  a.click();
  URL.revokeObjectURL(url);
});

loadConfig();
refreshAgents();
