/**
 * popup.js — MCP4ChatGPT Extension Popup Logic
 */

// ── DOM refs ──────────────────────────────────────────────────────────────
const statusBadge    = document.getElementById("status-badge");
const statusDot      = document.getElementById("status-dot");
const statusText     = document.getElementById("status-text");
const logoIcon       = document.getElementById("logo-icon");
const bridgePortEl   = document.getElementById("bridge-port");
const bridgeTokenEl  = document.getElementById("bridge-token");
const toggleTokenBtn = document.getElementById("toggle-token");
const btnConnect     = document.getElementById("btn-connect");
const btnDisconnect  = document.getElementById("btn-disconnect");
const permJsEl       = document.getElementById("perm-js");
const permReconnect  = document.getElementById("perm-reconnect");
const infoPort       = document.getElementById("info-port");
const infoTokenPfx   = document.getElementById("info-token-prefix");
const infoVersion    = document.getElementById("info-version");
const footerStatus   = document.getElementById("footer-status");

// ── Helpers ───────────────────────────────────────────────────────────────

function sendBg(msg) {
  return new Promise((resolve) => chrome.runtime.sendMessage(msg, resolve));
}

function setStatus(connected, text) {
  const cls = connected ? "connected" : "disconnected";
  statusBadge.className = "status-badge " + cls;
  statusDot.className   = "status-dot " + cls;
  statusText.textContent = text;
  logoIcon.className = "logo-icon" + (connected ? " connected" : "");
}

function toast(msg, isError = false) {
  footerStatus.textContent = msg;
  footerStatus.style.color = isError ? "#ef4444" : "#22c55e";
  setTimeout(() => {
    footerStatus.textContent = "Ready";
    footerStatus.style.color = "";
  }, 3000);
}

// ── Initial load ──────────────────────────────────────────────────────────

async function refresh() {
  const status = await sendBg({ type: "get_status" });
  if (!status) return;

  // Status badge
  setStatus(
    status.connected,
    status.connected ? "Connected" : (status.hasToken ? "Disconnected" : "No token")
  );

  // Fields
  bridgePortEl.value = status.bridgePort || 8765;
  if (status.tokenPrefix) {
    bridgeTokenEl.placeholder = `${status.tokenPrefix}... (set)`;
  }

  // Toggles
  permJsEl.checked      = status.allowJsExecution;
  permReconnect.checked = status.autoReconnect;

  // Info panel
  infoPort.textContent        = status.bridgePort || 8765;
  infoTokenPfx.textContent    = status.tokenPrefix ? status.tokenPrefix + "..." : "—";
  infoVersion.textContent     = chrome.runtime.getManifest().version;
}

// ── Event listeners ───────────────────────────────────────────────────────

// Toggle token visibility
toggleTokenBtn.addEventListener("click", () => {
  bridgeTokenEl.type = bridgeTokenEl.type === "password" ? "text" : "password";
  toggleTokenBtn.textContent = bridgeTokenEl.type === "password" ? "👁" : "🙈";
});

// Connect button
btnConnect.addEventListener("click", async () => {
  const port  = parseInt(bridgePortEl.value, 10);
  const token = bridgeTokenEl.value.trim();

  if (!token) {
    toast("Please enter your bridge token", true);
    bridgeTokenEl.focus();
    return;
  }
  if (isNaN(port) || port < 1024 || port > 65535) {
    toast("Invalid port number", true);
    return;
  }

  btnConnect.disabled = true;
  btnConnect.textContent = "Connecting…";

  await sendBg({ type: "save_config", patch: { bridgePort: port, bridgeToken: token } });
  await sendBg({ type: "connect" });

  // Wait briefly then refresh
  setTimeout(async () => {
    await refresh();
    btnConnect.disabled = false;
    btnConnect.textContent = "Connect";
    const s = await sendBg({ type: "get_status" });
    toast(s.connected ? "Connected ✓" : "Could not connect — check port & token", !s.connected);
  }, 1200);
});

// Disconnect
btnDisconnect.addEventListener("click", async () => {
  await sendBg({ type: "disconnect" });
  await refresh();
  toast("Disconnected");
});

// JS execution toggle
permJsEl.addEventListener("change", async () => {
  if (permJsEl.checked) {
    const ok = confirm(
      "⚠️ Enabling JS execution allows ChatGPT to run arbitrary JavaScript in your browser tabs.\n\n" +
      "Only enable this if you trust the agent and understand the risks.\n\n" +
      "Enable anyway?"
    );
    if (!ok) { permJsEl.checked = false; return; }
  }
  await sendBg({ type: "save_config", patch: { allowJsExecution: permJsEl.checked } });
  toast(permJsEl.checked ? "JS execution enabled" : "JS execution disabled");
});

// Auto-reconnect toggle
permReconnect.addEventListener("change", async () => {
  await sendBg({ type: "save_config", patch: { autoReconnect: permReconnect.checked } });
});

// ── Init ──────────────────────────────────────────────────────────────────
refresh();

// Refresh every 2 seconds while popup is open (to show live status)
setInterval(refresh, 2000);
