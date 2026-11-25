const $ = (selector) => document.querySelector(selector);

const elements = {
  form: $("#history-form"),
  account: $("#history-account"),
  accountHelp: $("#account-help"),
  source: $("#source"),
  destination: $("#destination"),
  restart: $("#restart"),
  message: $("#history-message"),
  channels: $("#channel-options"),
  startButton: $("#start-button"),
  stopButton: $("#stop-button"),
  statusStrip: $("#status-strip"),
  statusTitle: $("#status-title"),
  statusDetail: $("#status-detail"),
  success: $("#success-count"),
  failed: $("#failed-count"),
  total: $("#total-count"),
  progress: $("#progress"),
  activityList: $("#activity-list"),
  activityEmpty: $("#activity-empty"),
  refreshButton: $("#refresh-button"),
};

let lastAccounts = null;
let routeRestored = false;

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  if (response.status === 204) return null;
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = Array.isArray(payload.detail)
      ? payload.detail.map((item) => item.msg).join(" ")
      : payload.detail;
    throw new Error(detail || `Request failed (${response.status})`);
  }
  return payload;
}

function setMessage(message = "", type = "") {
  elements.message.textContent = message;
  elements.message.className = `inline-message ${type}`.trim();
}

async function withBusy(button, label, action) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = label;
  try {
    return await action();
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

function renderAccounts(accounts) {
  const selected = elements.account.value;
  elements.account.replaceChildren(new Option("Choose an account", ""));
  for (const phone of accounts) elements.account.add(new Option(phone, phone));
  if (accounts.includes(selected)) elements.account.value = selected;
  elements.accountHelp.hidden = accounts.length > 0;
  lastAccounts = accounts;
}

async function loadChats() {
  elements.channels.replaceChildren();
  if (!elements.account.value) return;
  try {
    const chats = await api(`/api/accounts/${encodeURIComponent(elements.account.value)}/chats`);
    for (const chat of chats) elements.channels.append(new Option(chat.title, String(chat.id)));
  } catch (error) {
    setMessage(error.message, "error");
  }
}

// Bring back the stopped route once, so carrying on is one click.
function restoreRoute(saved) {
  if (routeRestored) return;
  routeRestored = true;
  if (!saved || !lastAccounts.includes(saved.phone)) return;
  elements.account.value = saved.phone;
  elements.source.value = saved.source;
  elements.destination.value = saved.destination;
  loadChats();
}

function statusCopy(job) {
  const paused = job.saved && job.last_id
    ? `Stopped after message ${job.last_id}. Start the same route to carry on.`
    : "Waiting for a history job.";
  return {
    stopped: ["Stopped", paused],
    starting: ["Starting", "Connecting to Telegram and checking the route…"],
    running: ["Copying", "Re-uploading the source channel's history, oldest first."],
    stopping: ["Stopping", "Finishing the current operation safely…"],
    reconnecting: ["Reconnecting", "Connection lost. Retrying automatically…"],
    completed: ["Complete", "Every message up to the one that was latest at the start has been handled."],
    reauth_required: ["Sign-in required", "Telegram ended this session. Add the account again, then start the same route to carry on."],
    error: ["Needs attention", "Review the latest activity message."],
  }[job.status] || ["Unknown", "Refresh to check the worker state."];
}

function renderJob(job) {
  const [title, detail] = statusCopy(job);
  elements.statusStrip.className = `status-strip ${job.status}`;
  elements.statusTitle.textContent = title;
  elements.statusDetail.textContent = detail;
  elements.success.textContent = String(job.success);
  elements.failed.textContent = String(job.failed);
  elements.total.textContent = String(job.total);
  elements.progress.textContent = job.end_id == null
    ? "—"
    : `${job.last_id ?? 0} / ${job.end_id}`;
  elements.startButton.disabled = job.active || lastAccounts.length === 0;
  elements.stopButton.disabled = !job.active;

  elements.activityList.replaceChildren();
  elements.activityEmpty.hidden = job.logs.length > 0;
  for (const entry of job.logs) {
    const item = document.createElement("li");
    item.className = "activity-item";
    item.dataset.level = entry.level;
    const time = document.createElement("time");
    time.className = "activity-time";
    time.dateTime = entry.time;
    time.textContent = new Date(entry.time).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    const level = document.createElement("span");
    level.className = "activity-level";
    level.textContent = entry.level;
    const message = document.createElement("span");
    message.className = "activity-message";
    message.textContent = entry.message;
    item.append(time, level, message);
    elements.activityList.append(item);
  }
}

async function refreshState({ quiet = false } = {}) {
  try {
    const state = await api("/api/state");
    if (JSON.stringify(state.accounts) !== JSON.stringify(lastAccounts)) renderAccounts(state.accounts);
    restoreRoute(state.backfill.saved);
    renderJob(state.backfill);
    if (!quiet) setMessage("Status refreshed.", "success");
  } catch (error) {
    if (!quiet) setMessage(error.message, "error");
  }
}

elements.account.addEventListener("change", loadChats);

elements.form.addEventListener("submit", async (event) => {
  event.preventDefault();
  setMessage();
  const data = new FormData(elements.form);
  const body = {
    phone: data.get("phone"),
    source: data.get("source"),
    destination: data.get("destination"),
    restart: elements.restart.checked,
  };
  try {
    await withBusy(elements.startButton, "Starting…", () => api("/api/backfill/start", {
      method: "POST",
      body: JSON.stringify(body),
    }));
    elements.restart.checked = false;
    setMessage("History job is starting.", "success");
    await refreshState({ quiet: true });
  } catch (error) {
    setMessage(error.message, "error");
  }
});

elements.stopButton.addEventListener("click", async () => {
  setMessage();
  try {
    await withBusy(elements.stopButton, "Stopping…", () => api("/api/backfill/stop", {
      method: "POST",
      body: "{}",
    }));
    setMessage("History job stopped. Its place is saved.", "success");
    await refreshState({ quiet: true });
  } catch (error) {
    setMessage(error.message, "error");
  }
});

elements.refreshButton.addEventListener("click", () => refreshState());

refreshState({ quiet: true });
window.setInterval(() => refreshState({ quiet: true }), 2500);
