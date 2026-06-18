/**
 * content.js — MCP4ChatGPT Content Script
 *
 * Injected into all pages. Listens for messages from the background service
 * worker and provides fine-grained DOM access (MutationObserver for change
 * detection, deep element queries, etc.).
 *
 * Communication: chrome.runtime.sendMessage / onMessage
 */

(function () {
  "use strict";

  // Avoid double-injection
  if (window.__mcp4chatgpt_content_loaded) return;
  window.__mcp4chatgpt_content_loaded = true;

  // -------------------------------------------------------------------------
  // MutationObserver for page change monitoring
  // -------------------------------------------------------------------------

  let observer = null;
  let observerDebounce = null;
  let observing = false;

  function startObserver(durationMs) {
    if (observing) return;
    observing = true;

    observer = new MutationObserver((_mutations) => {
      if (observerDebounce) clearTimeout(observerDebounce);
      observerDebounce = setTimeout(() => {
        // Send a content-change event to background
        chrome.runtime.sendMessage({
          type: "content_mutation",
          url: location.href,
          title: document.title,
          timestamp: Date.now() / 1000,
        }).catch(() => {});
      }, 300);
    });

    observer.observe(document.documentElement, {
      childList: true,
      subtree: true,
      characterData: true,
      attributes: false,
    });

    // Auto-stop after duration
    setTimeout(() => stopObserver(), durationMs);
  }

  function stopObserver() {
    if (!observing) return;
    observing = false;
    if (observer) { observer.disconnect(); observer = null; }
    if (observerDebounce) { clearTimeout(observerDebounce); observerDebounce = null; }
  }

  // -------------------------------------------------------------------------
  // Message handler from background
  // -------------------------------------------------------------------------

  chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
    try {
      if (msg.type === "start_observer") {
        startObserver(msg.durationMs || 30000);
        reply({ ok: true });
        return false;
      }
      if (msg.type === "stop_observer") {
        stopObserver();
        reply({ ok: true });
        return false;
      }
      if (msg.type === "get_selection") {
        reply({ selection: String(window.getSelection ? window.getSelection() : "") });
        return false;
      }
      if (msg.type === "get_text") {
        reply({ text: document.body ? document.body.innerText : "" });
        return false;
      }
    } catch (e) {
      reply({ error: e.message });
    }
    return false;
  });

})();
