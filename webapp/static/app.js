const $ = (selector) => document.querySelector(selector);

const elements = {
  accountCount: $("#account-count"),
  accountEmpty: $("#account-empty"),
  accountList: $("#account-list"),
  accountMessage: $("#account-message"),
  phoneForm: $("#phone-form"),
  codeForm: $("#code-form"),
  passwordForm: $("#password-form"),
  phone: $("#phone"),
  loginCode: $("#login-code"),
  loginPassword: $("#login-password"),
  forwardForm: $("#forward-form"),
  forwardAccount: $("#forward-account"),
  forwardMessage: $("#forward-message"),
  channels: $("#channel-options"),
  startButton: $("#start-button"),
  stopButton: $("#stop-button"),
  statusStrip: $("#status-strip"),
  statusTitle: $("#status-title"),
  statusDetail: $("#status-detail"),
  success: $("#success-count"),
  failed: $("#failed-count"),
  total: $("#total-count"),
  lastForwarded: $("#last-forwarded"),
  activityList: $("#activity-list"),
  activityEmpty: $("#activity-empty"),
  refreshButton: $("#refresh-button"),
};

let pendingPhone = "";
let lastAccounts = [];

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

function setMessage(element, message = "", type = "") {
  element.textContent = message;
  element.className = `inline-message ${type}`.trim();
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
  const selected = elements.forwardAccount.value;
  elements.accountList.replaceChildren();
  elements.forwardAccount.replaceChildren(new Option("Choose an account", ""));
  elements.accountEmpty.hidden = accounts.length > 0;
  document.body.classList.toggle("has-accounts", accounts.length > 0);
  elements.accountCount.textContent = String(accounts.length);
  elements.accountCount.setAttribute("aria-label", `${accounts.length} saved accounts`);

  for (const phone of accounts) {
    const item = document.createElement("li");
    item.className = "account-item";
    const name = document.createElement("span");
    name.className = "account-name";
    name.textContent = phone;
    const remove = document.createElement("button");
    remove.className = "button account-remove";
    remove.type = "button";
    remove.textContent = "Remove";
    remove.setAttribute("aria-label", `Remove account ${phone}`);
    remove.addEventListener("click", () => removeAccount(phone));
    item.append(name, remove);
    elements.accountList.append(item);
    elements.forwardAccount.add(new Option(phone, phone));
  }
  if (accounts.includes(selected)) elements.forwardAccount.value = selected;
  lastAccounts = accounts;
}

function statusCopy(status) {
  return {
    stopped: ["Stopped", "Waiting for a forwarding job."],
    starting: ["Starting", "Connecting to Telegram and checking the route…"],
    running: ["Active", "Watching the source channel for new media."],
    stopping: ["Stopping", "Finishing the current operation safely…"],
    reconnecting: ["Reconnecting", "Connection lost. Retrying automatically…"],
    reauth_required: ["Sign-in required", "Telegram ended this session. Add the account again."],
    error: ["Needs attention", "Review the latest activity message."],
  }[status] || ["Unknown", "Refresh to check the worker state."];
}

function renderForwarding(forwarding) {
  const [title, detail] = statusCopy(forwarding.status);
  elements.statusStrip.className = `status-strip ${forwarding.status}`;
  elements.statusTitle.textContent = title;
  elements.statusDetail.textContent = detail;
  elements.success.textContent = String(forwarding.success);
  elements.failed.textContent = String(forwarding.failed);
  elements.total.textContent = String(forwarding.total);
  elements.lastForwarded.textContent = forwarding.last_forwarded ?? "—";
  elements.startButton.disabled = forwarding.active || lastAccounts.length === 0;
  elements.stopButton.disabled = !forwarding.active;

  elements.activityList.replaceChildren();
  elements.activityEmpty.hidden = forwarding.logs.length > 0;
  for (const entry of forwarding.logs) {
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
    renderForwarding(state.forwarding);
    if (!quiet) setMessage(elements.forwardMessage, "Status refreshed.", "success");
  } catch (error) {
    if (!quiet) setMessage(elements.forwardMessage, error.message, "error");
  }
}

async function removeAccount(phone) {
  if (!window.confirm(`Remove ${phone} from this console?`)) return;
  try {
    await api(`/api/accounts/${encodeURIComponent(phone)}`, { method: "DELETE" });
    setMessage(elements.accountMessage, "Account removed.", "success");
    await refreshState({ quiet: true });
  } catch (error) {
    setMessage(elements.accountMessage, error.message, "error");
  }
}

elements.phoneForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.submitter;
  pendingPhone = elements.phone.value.trim();
  setMessage(elements.accountMessage);
  try {
    await withBusy(button, "Sending…", () => api("/api/accounts/request-code", {
      method: "POST",
      body: JSON.stringify({ phone: pendingPhone }),
    }));
    elements.codeForm.hidden = false;
    elements.passwordForm.hidden = true;
    elements.loginCode.focus();
    setMessage(elements.accountMessage, "Login code sent. Check Telegram.", "success");
  } catch (error) {
    setMessage(elements.accountMessage, error.message, "error");
  }
});

elements.codeForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.submitter;
  try {
    const result = await withBusy(button, "Verifying…", () => api("/api/accounts/verify-code", {
      method: "POST",
      body: JSON.stringify({ phone: pendingPhone, code: elements.loginCode.value.trim() }),
    }));
    if (result.status === "password_required") {
      elements.passwordForm.hidden = false;
      elements.loginPassword.focus();
      setMessage(elements.accountMessage, "Two-step verification is enabled.");
      return;
    }
    completeLogin();
  } catch (error) {
    setMessage(elements.accountMessage, error.message, "error");
  }
});

elements.passwordForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.submitter;
  try {
    await withBusy(button, "Signing in…", () => api("/api/accounts/verify-password", {
      method: "POST",
      body: JSON.stringify({ phone: pendingPhone, password: elements.loginPassword.value }),
    }));
    completeLogin();
  } catch (error) {
    setMessage(elements.accountMessage, error.message, "error");
  }
});

async function completeLogin() {
  elements.phoneForm.reset();
  elements.codeForm.reset();
  elements.passwordForm.reset();
  elements.codeForm.hidden = true;
  elements.passwordForm.hidden = true;
  pendingPhone = "";
  setMessage(elements.accountMessage, "Account added successfully.", "success");
  await refreshState({ quiet: true });
}

elements.forwardAccount.addEventListener("change", async () => {
  elements.channels.replaceChildren();
  if (!elements.forwardAccount.value) return;
  try {
    const chats = await api(`/api/accounts/${encodeURIComponent(elements.forwardAccount.value)}/chats`);
    for (const chat of chats) elements.channels.append(new Option(chat.title, String(chat.id)));
  } catch (error) {
    setMessage(elements.forwardMessage, error.message, "error");
  }
});

elements.forwardForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  setMessage(elements.forwardMessage);
  const data = new FormData(elements.forwardForm);
  try {
    await withBusy(elements.startButton, "Starting…", () => api("/api/forward/start", {
      method: "POST",
      body: JSON.stringify(Object.fromEntries(data.entries())),
    }));
    setMessage(elements.forwardMessage, "Forwarding job is starting.", "success");
    await refreshState({ quiet: true });
  } catch (error) {
    setMessage(elements.forwardMessage, error.message, "error");
  }
});

elements.stopButton.addEventListener("click", async () => {
  setMessage(elements.forwardMessage);
  try {
    await withBusy(elements.stopButton, "Stopping…", () => api("/api/forward/stop", {
      method: "POST",
      body: "{}",
    }));
    setMessage(elements.forwardMessage, "Forwarding stopped.", "success");
    await refreshState({ quiet: true });
  } catch (error) {
    setMessage(elements.forwardMessage, error.message, "error");
  }
});

elements.refreshButton.addEventListener("click", () => refreshState());

refreshState({ quiet: true });
window.setInterval(() => refreshState({ quiet: true }), 2500);
