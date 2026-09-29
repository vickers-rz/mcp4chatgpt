# P5: reuse one MCP4ChatGPT instance as an HTTP downstream

Date: 2026-09-29. The downstream manager now accepts an explicitly configured
Streamable HTTP MCP endpoint in addition to stdio. A consumer can point at the
already-running MCP4ChatGPT `/mcp` instead of launching another Chrome Extension
bridge or Headless sidecar. No second browser runtime is started by this client.

Example for the **consumer** instance's `downstream_mcp.toml`:

```toml
[downstream_mcp.local_browser]
transport = "http"
url = "https://mcp.example.com/mcp"
bearer_token_env = "MCP4CHATGPT_DOWNSTREAM_TOKEN"
namespace = "local_browser"
allow_tools = ["search_web", "read_webpage", "capability_search", "capability_get", "capability_call"]
allow_capability_targets = ["ext_list_tabs", "ext_get_dom", "ext_navigate", "ext_fill_input"]
```

The token is read from the named environment variable, never stored in TOML.
Only HTTPS or HTTP loopback URLs ending in `/mcp` are accepted. HTTP downstreams
require an explicit `allow_tools`; `capability_call` is additionally checked
against `allow_capability_targets`, so hiding a direct tool cannot be bypassed
by calling the discovery gateway. Consumer and target must be distinct
instances; pointing an instance at its own `/mcp` would create recursion.

The client uses MCP `server/discover`, `tools/list`, and `tools/call` with the
current protocol headers, compares the target `server_instance_id` and catalog
digest before calls, refuses redirects so bearer tokens cannot follow them, and
returns original MCP content, `structuredContent`, resource links and `isError`
without rewriting them. The outer catalog exposes a namespaced capability
revision and backend instance binding. The target's own `expected_revision`
argument is forwarded unchanged through nested `capability_call`.

Browser `backend`, page/tab handle and URL remain in the individual target
results. `ext_connection_status` now exposes a fresh `connection_generation`
per Extension WebSocket connection. Consumers must obtain fresh handles, compare
that generation before later stateful actions, keep handles tied to the same
target instance, and stop on an identity or revision mismatch. The Extension
does not expose a browser profile identity, and the HTTP transport cannot
manufacture it. Server restarts change `server_instance_id` even when tool
definitions are identical; an isolated restart test covers this case.

An isolated two-server test covers HTTP discovery, namespaced calls, raw result
preservation, catalog binding and denial of a nested shell target. This has not
yet been rolled out to an external consumer or the live public instance.
