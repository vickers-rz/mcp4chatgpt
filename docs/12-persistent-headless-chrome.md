# Persistent Headless Chrome downstream

Date: 2026-09-22

## Purpose

MCP4ChatGPT now exposes two independent Chrome DevTools downstreams:

1. `chrome_devtools__*` — attaches to the user's already-running daily Chrome through `chrome-devtools-mcp --autoConnect`.
2. `chrome_headless__*` — launches and owns a separate headless Chrome instance with a persistent browser profile.

The two channels are intentionally separate. The headless channel must not silently fall back to the user's daily Chrome, and the attach channel must not reuse the headless profile.

## Configuration

`downstream_mcp.toml` contains:

```toml
[downstream_mcp.chrome_headless]
enabled = true
transport = "stdio"
command = "/opt/homebrew/bin/npx"
args = ["-y", "chrome-devtools-mcp@1.9.0", "--headless", "--viewport", "1440x900", "--userDataDir", "/Users/vickers/Library/Application Support/MCP4ChatGPT/chrome-headless-profile"]
startup_timeout = 30.0
call_timeout = 60.0
namespace = "chrome_headless"
```

No `--isolated` flag is supplied. The browser uses a dedicated persistent profile at:

```text
/Users/vickers/Library/Application Support/MCP4ChatGPT/chrome-headless-profile
```

This profile survives MCP4ChatGPT and downstream-process restarts. The browser process itself remains owned by the downstream MCP process and is shut down when the downstream is stopped. Run one headless downstream against this profile at a time; Chrome's profile lock rejects concurrent use. The package version is pinned to 1.9.0 so updates require explicit review.

## Live acceptance performed

The final configuration was verified against `chrome-devtools-mcp@1.9.0` with the dedicated profile. Two successive client processes navigated to `https://example.com`; a `localStorage` value written by the first was read back by the second. The browser exited after each downstream client stopped.

### Startup and discovery

A real `DownstreamMCPManager` startup produced:

- `chrome_devtools`: running, 29 tools
- `chrome_headless`: running, 29 tools
- `wps`: running, 6 tools

`chrome_headless__list_pages` returned the owned headless browser:

```text
1: about:blank [selected]
```

### Persistence across restart

The following stateful test was performed:

1. Start the manager.
2. Navigate the headless profile to `https://example.com`.
3. Write `localStorage["mcp4chatgpt_persist_probe"] = "persisted"`.
4. Stop all downstreams.
5. Start a fresh manager and a fresh headless Chrome process.
6. Navigate again to `https://example.com`.
7. Read the localStorage key.

The second process returned:

```json
"persisted"
```

Therefore browser profile state is confirmed to survive a complete downstream restart.

## Page-ID routing

The current `chrome-devtools-mcp` enables page-ID routing by default. Page-scoped tools such as `navigate_page` and `evaluate_script` therefore require `pageId`.

Typical sequence:

```text
chrome_headless__list_pages
  -> obtain page ID
chrome_headless__navigate_page(pageId=..., ...)
chrome_headless__evaluate_script(pageId=..., ...)
```

Do not assume that omitting `pageId` will target the currently selected page.

## Authentication/bootstrap

A persistent profile can retain cookies, localStorage and other browser profile state. Sites that require interactive login, MFA, CAPTCHA, device approval, or a non-headless login flow may still need a one-time bootstrap using a visible browser pointed at the same profile directory. That is an operational login step rather than a new MCP architecture requirement.

## Relationship to native Computer Use

Use the channels according to the target:

- Web DOM/CDP automation: `chrome_devtools__*` or `chrome_headless__*`
- Extension-backed browser integration: `ext_*`
- Native macOS application UI: `computer_*`

No implicit cross-channel fallback should be added. Each path has different identity, permission and state semantics.
