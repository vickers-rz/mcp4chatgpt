import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

test("repeated observer subscriptions extend expiry and do not shorten it", () => {
  let listener;
  let now = 0;
  let next = 0;
  let disconnects = 0;
  const timers = new Map();
  const context = {
    window: {},
    document: { documentElement: {} },
    Date: { now: () => now },
    MutationObserver: class {
      observe() {}
      disconnect() { disconnects++; }
    },
    setTimeout: (fn, delay) => { timers.set(++next, { fn, at: now + delay }); return next; },
    clearTimeout: id => timers.delete(id),
    chrome: { runtime: { onMessage: { addListener: fn => { listener = fn; } } } },
  };
  vm.runInNewContext(fs.readFileSync("src/chrome_extension/content.js", "utf8"), context);
  const subscribe = durationMs => listener({ type: "start_observer", durationMs }, {}, () => {});
  subscribe(1000);
  now = 500;
  subscribe(2000);
  assert.equal(timers.size, 1);
  assert.equal([...timers.values()][0].at, 2500);
  now = 600;
  subscribe(100);
  assert.equal([...timers.values()][0].at, 2500);
  [...timers.values()][0].fn();
  assert.equal(disconnects, 1);
});
