/**
 * background.js — MCP4ChatGPT Chrome Extension Service Worker
 *
 * Maintains a WebSocket connection to the local MCP4ChatGPT bridge server.
 * Routes MCP commands to the appropriate Chrome APIs and returns results.
 *
 * Message protocol (JSON):
 *   Client → Server:  { type: "auth", token, version, platform }
 *   Server → Client:  { type: "auth_ok" }
 *   Server → Client:  { type: "command", id, cmd, args }
 *   Client → Server:  { type: "response", id, result } | { type: "response", id, error }
 *   Client → Server:  { type: "event", event, tabId, url, title, timestamp }
 *   Client → Server:  { type: "ping" }
 *   Server → Client:  { type: "pong" }
 */

// ---------------------------------------------------------------------------
// Configuration (stored in chrome.storage.local)
// ---------------------------------------------------------------------------

const DEFAULT_CONFIG = {
  bridgePort: 8765,
  bridgeToken: "",         // must be set by user in popup
  allowJsExecution: false, // user must opt-in for ext_run_js
  autoReconnect: true,
  reconnectDelayMs: 3000,
};

let config = { ...DEFAULT_CONFIG };
let ws = null;
let reconnectTimer = null;
let reconnectAttempts = 0;
let isAuthed = false;
let changeSubscription = null; // { tabId, expiresAt, abortController }

// ---------------------------------------------------------------------------
// Storage helpers
// ---------------------------------------------------------------------------

async function loadConfig() {
  const stored = await chrome.storage.local.get("mcp4chatgpt_config");
  config = { ...DEFAULT_CONFIG, ...(stored.mcp4chatgpt_config || {}) };
}

async function saveConfig(patch) {
  config = { ...config, ...patch };
  await chrome.storage.local.set({ mcp4chatgpt_config: config });
}

// ---------------------------------------------------------------------------
// WebSocket connection
// ---------------------------------------------------------------------------

function connect() {
  if (ws && ws.readyState === WebSocket.OPEN) return;
  if (!config.bridgeToken) {
    console.warn("[MCP4ChatGPT] No bridge token configured. Open the extension popup to set it.");
    return;
  }

  const url = `ws://127.0.0.1:${config.bridgePort}?token=${encodeURIComponent(config.bridgeToken)}`;
  console.log("[MCP4ChatGPT] Connecting to", url);

  ws = new WebSocket(url);

  ws.onopen = () => {
    console.log("[MCP4ChatGPT] WebSocket opened, sending auth...");
    ws.send(JSON.stringify({
      type: "auth",
      token: config.bridgeToken,
      version: chrome.runtime.getManifest().version,
      platform: navigator.platform,
    }));
  };

  ws.onmessage = (event) => {
    let msg;
    try { msg = JSON.parse(event.data); } catch { return; }
    handleMessage(msg);
  };

  ws.onerror = (err) => {
    console.error("[MCP4ChatGPT] WebSocket error:", err);
  };

  ws.onclose = (evt) => {
    console.log("[MCP4ChatGPT] WebSocket closed:", evt.code, evt.reason);
    isAuthed = false;
    ws = null;
    updateIcon("disconnected");
    if (config.autoReconnect) scheduleReconnect();
  };
}

function scheduleReconnect() {
  if (reconnectTimer) return;
  const delay = Math.min(30000, config.reconnectDelayMs * Math.pow(1.5, reconnectAttempts));
  reconnectAttempts++;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connect();
  }, delay);
}

function disconnect() {
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
  if (ws) { ws.close(1000, "user disconnect"); ws = null; }
  isAuthed = false;
  updateIcon("disconnected");
}

function send(payload) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(payload));
  }
}

// ---------------------------------------------------------------------------
// Tab/content helpers
// ---------------------------------------------------------------------------

async function getTargetTab(tabId) {
  const tab = tabId
    ? await chrome.tabs.get(tabId)
    : (await chrome.tabs.query({ active: true, lastFocusedWindow: true }))[0];
  if (!tab || !tab.id) throw new Error("Tab not found");
  return tab;
}

async function ensureContentScript(tabId) {
  try {
    await chrome.tabs.sendMessage(tabId, { type: "get_text" });
    return true;
  } catch (_e) {
    // Content scripts are declared in manifest.json, but this fallback covers
    // pages opened before install/reload or where the script has been unloaded.
  }

  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      files: ["content.js"],
    });
    await chrome.tabs.sendMessage(tabId, { type: "get_text" });
    return true;
  } catch (_e) {
    return false;
  }
}

function activeChangeSubscriptionFor(tabId) {
  if (!changeSubscription) return false;
  if (Date.now() > changeSubscription.expiresAt) {
    changeSubscription = null;
    return false;
  }
  return !changeSubscription.tabId || changeSubscription.tabId === tabId;
}

function serializeScriptValue(value) {
  const resultType = typeof value;
  let result;
  if (value === null || value === undefined) result = String(value);
  else if (resultType === "object") {
    try { result = JSON.stringify(value); } catch { result = String(value); }
  } else result = String(value);
  return { result, resultType };
}

// ---------------------------------------------------------------------------
// Icon state
// ---------------------------------------------------------------------------

function updateIcon(state) {
  const titles = {
    connected: "MCP4ChatGPT — Connected ✓",
    disconnected: "MCP4ChatGPT — Disconnected",
    error: "MCP4ChatGPT — Error",
  };
  chrome.action.setTitle({ title: titles[state] || titles.disconnected });
  // Badge
  const badges = { connected: ["", "#22c55e"], disconnected: ["●", "#ef4444"], error: ["!", "#f59e0b"] };
  const [text, color] = badges[state] || badges.disconnected;
  chrome.action.setBadgeText({ text });
  if (text) chrome.action.setBadgeBackgroundColor({ color });
}

// ---------------------------------------------------------------------------
// Message dispatch
// ---------------------------------------------------------------------------

function handleMessage(msg) {
  if (msg.type === "auth_ok") {
    console.log("[MCP4ChatGPT] Authenticated ✓");
    isAuthed = true;
    reconnectAttempts = 0;
    updateIcon("connected");
    return;
  }

  if (msg.type === "pong") return;

  if (msg.type === "command" && msg.id && msg.cmd) {
    executeCommand(msg.id, msg.cmd, msg.args || {});
    return;
  }
}

async function executeCommand(id, cmd, args) {
  try {
    let result;
    switch (cmd) {
      case "list_tabs":         result = await cmdListTabs(args); break;
      case "get_active_tab":    result = await cmdGetActiveTab(args); break;
      case "get_dom":           result = await cmdGetDom(args); break;
      case "get_selection":     result = await cmdGetSelection(args); break;
      case "screenshot":        result = await cmdScreenshot(args); break;
      case "navigate":          result = await cmdNavigate(args); break;
      case "click_element":     result = await cmdClickElement(args); break;
      case "fill_input":        result = await cmdFillInput(args); break;
      case "run_js":            result = await cmdRunJs(args); break;
      case "subscribe_changes": result = await cmdSubscribeChanges(args); break;
      default:
        throw new Error(`Unknown command: ${cmd}`);
    }
    send({ type: "response", id, result });
  } catch (err) {
    console.error("[MCP4ChatGPT] Command error:", cmd, err);
    send({ type: "response", id, error: err.message || String(err) });
  }
}

// ---------------------------------------------------------------------------
// Command implementations
// ---------------------------------------------------------------------------

/** list_tabs: Return all open tabs */
async function cmdListTabs({ maxTabs = 100 }) {
  const tabs = await chrome.tabs.query({});
  const limited = tabs.slice(0, maxTabs);
  return {
    tabs: limited.map(t => ({
      windowId: t.windowId,
      tabId: t.id,
      index: t.index,
      active: t.active,
      pinned: t.pinned,
      title: t.title || "",
      url: t.url || "",
      status: t.status || "",
    })),
    truncated: tabs.length > maxTabs,
  };
}

/** get_active_tab: Return rich context from the frontmost tab */
async function cmdGetActiveTab({ includeText = true, includeSelection = true, includeMeta = true }) {
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (!tab) throw new Error("No active tab found");

  let pageData = {};
  try {
    const [{ result }] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: () => {
        const meta = {};
        for (const el of Array.from(document.querySelectorAll("meta"))) {
          const k = el.getAttribute("name") || el.getAttribute("property");
          const v = el.getAttribute("content");
          if (k && v && k.length < 120 && v.length < 2000) meta[k] = v;
        }
        const selection = String(window.getSelection ? window.getSelection() : "");
        const text = document.body ? document.body.innerText : "";
        return { title: document.title, url: location.href, meta, selection, text };
      },
    });
    pageData = result || {};
  } catch (e) {
    // Restricted page (chrome://, extensions page, etc.)
    pageData = { title: tab.title, url: tab.url, meta: {}, selection: "", text: "" };
  }

  return {
    tabId: tab.id,
    windowId: tab.windowId,
    title: pageData.title || tab.title || "",
    url: pageData.url || tab.url || "",
    meta: includeMeta ? (pageData.meta || {}) : undefined,
    selection: includeSelection ? (pageData.selection || "") : undefined,
    text: includeText ? (pageData.text || "") : undefined,
  };
}

/** get_dom: Return outerHTML of a CSS selector */
async function cmdGetDom({ tabId, selector = "body" }) {
  const tab = await getTargetTab(tabId);

  const sel = selector;
  const [{ result }] = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    func: (s) => {
      const el = document.querySelector(s);
      return el ? { html: el.outerHTML, url: location.href } : { html: "", url: location.href, error: `No element matching: ${s}` };
    },
    args: [sel],
  });

  return { tabId: tab.id, url: result.url, html: result.html || "", error: result.error };
}

/** get_selection: Return currently selected text */
async function cmdGetSelection({ tabId }) {
  const tab = await getTargetTab(tabId);

  const [{ result }] = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    func: () => ({
      selection: String(window.getSelection ? window.getSelection() : ""),
      url: location.href,
    }),
  });

  return { tabId: tab.id, url: result.url, selection: result.selection || "" };
}

/** screenshot: Capture visible tab as PNG */
async function cmdScreenshot({ tabId, quality = 80 }) {
  // captureVisibleTab needs the window ID
  let windowId;
  if (tabId) {
    const t = await chrome.tabs.get(tabId);
    windowId = t.windowId;
    // Activate the tab so it can be captured
    await chrome.tabs.update(tabId, { active: true });
  } else {
    const [t] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
    if (!t) throw new Error("No active tab");
    tabId = t.id;
    windowId = t.windowId;
  }

  const dataUrl = await chrome.tabs.captureVisibleTab(windowId, {
    format: "png",
    quality: Math.max(10, Math.min(100, quality)),
  });

  // Get tab info for dimensions hint
  const tab = await chrome.tabs.get(tabId);
  return {
    tabId,
    url: tab.url,
    dataUrl,
    width: null,  // not directly available without additional JS injection
    height: null,
  };
}

/** navigate: Go to URL in a tab */
async function cmdNavigate({ url, tabId, newTab = false }) {
  if (newTab) {
    const tab = await chrome.tabs.create({ url, active: true });
    return { tabId: tab.id, url, status: "created" };
  }
  const targetTab = await getTargetTab(tabId);
  await chrome.tabs.update(targetTab.id, { url });
  return { tabId: targetTab.id, url, status: "navigating" };
}

/** click_element: Click a CSS selector */
async function cmdClickElement({ selector, tabId }) {
  const tab = await getTargetTab(tabId);
  const sel = selector;
  const [{ result }] = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    func: (s) => {
      const el = document.querySelector(s);
      if (!el) return { clicked: false, elementText: "", error: `No element: ${s}` };
      el.click();
      return { clicked: true, elementText: (el.innerText || el.textContent || "").trim().slice(0, 200) };
    },
    args: [sel],
  });
  return { tabId: tab.id, ...result };
}

/** fill_input: Set value on an input/textarea */
async function cmdFillInput({ selector, value, tabId, submit = false }) {
  const tab = await getTargetTab(tabId);
  const sel = selector;
  const val = value;
  const doSubmit = submit;
  const [{ result }] = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    func: (s, v, doSub) => {
      const el = document.querySelector(s);
      if (!el) return { filled: false, submitted: false, error: `No element: ${s}` };

      if (!("value" in el)) {
        return {
          filled: false,
          submitted: false,
          tagName: el.tagName,
          error: `Element does not support a value: ${s}`,
        };
      }

      try {
        const valuePrototype = el instanceof HTMLTextAreaElement
          ? HTMLTextAreaElement.prototype
          : el instanceof HTMLInputElement
            ? HTMLInputElement.prototype
            : Object.getPrototypeOf(el);
        const valueSetter = Object.getOwnPropertyDescriptor(valuePrototype, "value")?.set;

        el.focus();
        if (valueSetter) valueSetter.call(el, v);
        else el.value = v;

        const inputEvent = typeof InputEvent === "function"
          ? new InputEvent("input", {
              bubbles: true,
              inputType: "insertText",
              data: v,
            })
          : new Event("input", { bubbles: true });
        el.dispatchEvent(inputEvent);
        el.dispatchEvent(new Event("change", { bubbles: true }));
      } catch (err) {
        return {
          filled: false,
          submitted: false,
          tagName: el.tagName,
          error: err.message || String(err),
        };
      }

      const filled = String(el.value) === String(v);
      let submitted = false;
      if (doSub && filled) {
        const form = el.form || el.closest("form");
        if (!form) {
          for (const type of ["keydown", "keypress", "keyup"]) {
            el.dispatchEvent(new KeyboardEvent(type, {
              key: "Enter",
              code: "Enter",
              keyCode: 13,
              which: 13,
              bubbles: true,
              cancelable: true,
            }));
          }
          return { filled, submitted: true, tagName: el.tagName, submitMethod: "enter_key" };
        }

        try {
          if (typeof form.requestSubmit === "function") form.requestSubmit();
          else HTMLFormElement.prototype.submit.call(form);
          submitted = true;
        } catch (err) {
          return {
            filled,
            submitted: false,
            tagName: el.tagName,
            error: err.message || String(err),
          };
        }
      }
      return { filled, submitted, tagName: el.tagName };
    },
    args: [sel, val, doSubmit],
  });
  return { tabId: tab.id, selector, ...result };
}

/** run_js: Execute arbitrary JS (requires user opt-in) */
async function cmdRunJs({ code, tabId }) {
  if (!config.allowJsExecution) {
    throw new Error(
      "JavaScript execution is disabled. Open the MCP4ChatGPT extension popup and enable 'Allow JS execution' first."
    );
  }
  const tab = await getTargetTab(tabId);

  if (chrome.userScripts && typeof chrome.userScripts.execute === "function") {
    try {
      const [injection] = await chrome.userScripts.execute({
        target: { tabId: tab.id },
        world: "USER_SCRIPT",
        js: [{ code }],
      });

      if (injection.error) {
        return { tabId: tab.id, result: null, error: injection.error, resultType: "error" };
      }

      return { tabId: tab.id, ...serializeScriptValue(injection.result), executionWorld: "USER_SCRIPT" };
    } catch (err) {
      const message = err.message || String(err);
      if (!message.includes("userScripts") && !message.includes("User Scripts")) {
        return { tabId: tab.id, result: null, error: message, resultType: "error" };
      }
    }
  }

  try {
    const [injection] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      world: "MAIN",
      func: (source) => {
        return (0, eval)(source);
      },
      args: [code],
    });
    return { tabId: tab.id, ...serializeScriptValue(injection?.result), executionWorld: "MAIN" };
  } catch (err) {
    return {
      tabId: tab.id,
      result: null,
      error: err.message || String(err),
      resultType: "error",
    };
  }
}

/** subscribe_changes: Listen for tab navigation/update events */
async function cmdSubscribeChanges({ durationSec = 30, tabId }) {
  const seconds = Math.max(1, Math.min(120, Number(durationSec) || 30));
  const durationMs = seconds * 1000;
  const tab = await getTargetTab(tabId);
  const expiresAt = Date.now() + durationMs;
  changeSubscription = { tabId: tabId ? tab.id : null, expiresAt };

  const contentReady = await ensureContentScript(tab.id);
  let observerStarted = false;
  if (contentReady) {
    try {
      await chrome.tabs.sendMessage(tab.id, { type: "start_observer", durationMs });
      observerStarted = true;
    } catch (_e) {
      observerStarted = false;
    }
  }

  return {
    subscribed: true,
    durationSec: seconds,
    tabId: tab.id,
    observerStarted,
  };
}

// ---------------------------------------------------------------------------
// Tab change listener → push events to server
// ---------------------------------------------------------------------------

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (!isAuthed) return;
  if (!activeChangeSubscriptionFor(tabId)) return;

  // Only emit on meaningful changes
  if (!changeInfo.url && !changeInfo.title && changeInfo.status !== "complete") return;

  send({
    type: "event",
    event: changeInfo.status === "complete" ? "page_loaded" : "page_navigating",
    tabId,
    url: changeInfo.url || tab.url || "",
    title: changeInfo.title || tab.title || "",
    timestamp: Date.now() / 1000,
  });
});

chrome.tabs.onActivated.addListener(({ tabId }) => {
  if (!isAuthed || !activeChangeSubscriptionFor(tabId)) return;
  chrome.tabs.get(tabId, (tab) => {
    if (chrome.runtime.lastError) return;
    send({ type: "event", event: "tab_activated", tabId, url: tab.url || "", title: tab.title || "", timestamp: Date.now() / 1000 });
  });
});

// ---------------------------------------------------------------------------
// Alarm for keepalive ping
// ---------------------------------------------------------------------------

chrome.alarms.create("mcp4chatgpt_ping", { periodInMinutes: 0.25 }); // every 15s

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "mcp4chatgpt_ping") {
    if (ws && ws.readyState === WebSocket.OPEN && isAuthed) {
      send({ type: "ping" });
    } else if (!ws || ws.readyState === WebSocket.CLOSED) {
      if (config.autoReconnect && config.bridgeToken) connect();
    }
  }
});

// ---------------------------------------------------------------------------
// Messages from popup
// ---------------------------------------------------------------------------

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  if (msg.type === "content_mutation") {
    const tabId = sender.tab && sender.tab.id;
    if (isAuthed && tabId && activeChangeSubscriptionFor(tabId)) {
      send({
        type: "event",
        event: "dom_mutation",
        tabId,
        url: msg.url || sender.tab.url || "",
        title: msg.title || sender.tab.title || "",
        timestamp: msg.timestamp || Date.now() / 1000,
      });
    }
    reply({ ok: true });
    return false;
  }

  if (msg.type === "get_status") {
    reply({
      connected: isAuthed,
      bridgePort: config.bridgePort,
      allowJsExecution: config.allowJsExecution,
      autoReconnect: config.autoReconnect,
      hasToken: !!config.bridgeToken,
      tokenPrefix: config.bridgeToken ? config.bridgeToken.slice(0, 8) : "",
    });
    return false;
  }
  if (msg.type === "connect") {
    reconnectAttempts = 0;
    connect();
    reply({ ok: true });
    return false;
  }
  if (msg.type === "disconnect") {
    disconnect();
    reply({ ok: true });
    return false;
  }
  if (msg.type === "save_config") {
    saveConfig(msg.patch).then(() => {
      reply({ ok: true });
      // Reconnect if token/port changed
      if ("bridgeToken" in msg.patch || "bridgePort" in msg.patch) {
        disconnect();
        setTimeout(() => { reconnectAttempts = 0; connect(); }, 500);
      }
    });
    return true; // async
  }
  return false;
});

// ---------------------------------------------------------------------------
// Startup
// ---------------------------------------------------------------------------

(async () => {
  await loadConfig();
  updateIcon("disconnected");
  if (config.bridgeToken && config.autoReconnect) {
    connect();
  }
})();
