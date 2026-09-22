# Reading ChatGPT Shared Conversation Links

This note records the verified handling path for reading ChatGPT shared-conversation
URLs through MCP4ChatGPT's real Chrome bridge. It exists because the two share URL
families currently behave differently under rendered-page extraction.

The goal is to recover the shared conversation content while preserving the user's
real browser session and avoiding accidental collection of unrelated ChatGPT account
or session data.

## Scope

Verified URL shapes:

- `https://chatgpt.com/s/cx_<id>`
- `https://chatgpt.com/share/<uuid>`

The behavior below was verified against real shared conversations in September 2026.
ChatGPT's frontend is not a stable public API, so this should be treated as an
operational fallback procedure rather than a guaranteed parser contract.

## Preferred Path: Chrome Extension Rendered Read

Always start with the normal extension bridge.

1. Call `ext_connection_status`.
2. If connected, call `ext_read_webpage` with the shared URL and a sufficiently
   large `max_chars` value.
3. Inspect the returned title, extraction method, body length, and text.

For the verified `/s/cx_...` share URL, this path returned the complete shared
conversation directly. No DevTools fallback was required.

Expected successful shape:

```text
ChatGPT
<shared conversation title>

<user message>
<assistant message>
...
```

This is the preferred path because it uses the same MCP4ChatGPT Chrome extension,
the real browser session, and the existing rendered-page extractor.

## Detecting An Incomplete Share Read

Do not assume HTTP 200 or a non-empty body means the shared conversation was read.

The verified `/share/<uuid>` page returned only the ChatGPT shell through
`ext_read_webpage`, including items such as:

- sidebar/history entries;
- account navigation;
- the share banner;
- the composer;
- a short notice that the page is a shared ChatGPT copy.

The actual conversation messages were missing.

Treat the read as incomplete when the result is dominated by ChatGPT chrome/navigation
and contains little or none of the expected conversation text.

## DevTools Fallback For `/share/<uuid>`

When rendered extraction is incomplete:

1. Open the URL in a background page with the downstream Chrome DevTools MCP.
2. Take a text/accessibility snapshot to confirm that the share page loaded.
3. If the conversation body is still absent from the accessibility tree, inspect
   page script metadata rather than clicking or visually scraping the page.
4. Locate the React Router hydration payload containing
   `streamController.enqueue(...)`.
5. Decode only the route data for:

```text
routes/share.$shareId.($action)
  -> serverResponse
  -> data
  -> mapping
```

The verified page stored the shared conversation in this hydration payload even
though the messages were not represented in the visible accessibility tree.

## Critical Security Rule: Never Dump The Whole Hydration Script

A logged-in ChatGPT page can place unrelated application/session state in the same
hydration script as the shared-conversation route data.

Therefore:

- do not return the entire script to the model;
- do not write the entire script into normal project logs;
- do not grep broad hydration output into chat;
- do not persist access/session credentials while extracting the conversation;
- do not treat unrelated sidebar/account data as part of the shared conversation.

Extraction code should select the share route first and emit only the minimum
conversation fields required for the task.

In other words:

```text
BAD
document.scripts[n].textContent
  -> dump everything

GOOD
hydration payload
  -> select share route
  -> select serverResponse.data.mapping
  -> extract conversation messages only
```

## Hydration Payload Shape

The verified ChatGPT frontend used an indexed serialization similar to a devalue-style
flat value table rather than ordinary nested JSON.

Observed characteristics:

- the payload itself can be parsed as JSON after extracting the string passed to
  `streamController.enqueue(...)`;
- objects may use keys such as `"_123"`, where the numeric suffix points to another
  entry in the flat table;
- integer values can also be references into the same table;
- negative integer values are special sentinels and must not be treated as normal
  array indexes.

A decoder therefore needs a bounded dereference step before the route object becomes
normal nested data.

Implementation guidance:

- memoize decoded indexes;
- guard against cycles;
- handle negative sentinels explicitly;
- impose output-size limits;
- decode only the subtree needed for the share route.

Do not build a generic "dump all ChatGPT state" decoder.

## Extracting The Conversation

After resolving the share route, use its conversation `mapping`.

For each mapped node:

1. Read `node.message`.
2. Keep only user and assistant messages needed for the requested analysis.
3. Read message text from `message.content.parts` or the corresponding text field.
4. Ignore empty messages and redacted tool placeholders.
5. Exclude system/developer messages, account/session data, and unrelated tool traces.

For a simple linear shared conversation, sorting retained messages by
`message.create_time` reproduced the visible sequence in the verified sample.

If branch metadata such as parent/children/current-node is available, prefer
reconstructing the active conversation branch rather than assuming timestamp order;
shared conversations can theoretically contain branch history.

## Recommended Operational Flow

```text
ChatGPT share URL
      |
      v
ext_connection_status
      |
      v
ext_read_webpage
      |
      +-- conversation text present --> use rendered text
      |
      +-- only ChatGPT shell ----------> DevTools fallback
                                          |
                                          v
                                  open background page
                                          |
                                          v
                                  confirm page loaded
                                          |
                                          v
                              inspect hydration metadata
                                          |
                                          v
                        select share route only (not whole script)
                                          |
                                          v
                              decode conversation mapping
                                          |
                                          v
                          user/assistant messages only
```

## Tool Choice

Use the existing layers in this order:

### 1. MCP4ChatGPT Chrome Extension

Primary tools:

- `ext_connection_status`
- `ext_read_webpage`

This is the normal path and worked directly for the verified `/s/cx_...` URL.

### 2. Chrome DevTools Downstream

Fallback tools:

- `chrome_devtools__new_page`
- `chrome_devtools__take_snapshot`
- `chrome_devtools__evaluate_script`

Use DevTools only when the normal rendered-page extractor cannot see the shared
conversation.

Avoid network-body dumping unless necessary. In the verified `/share/<uuid>`
case, the useful data was already present in the document hydration state.

## Failure Modes And Lessons

### HTTP/read success but no conversation

Cause: the share page shell is rendered, but conversation data is not exposed through
the body extractor.

Action: switch to the hydration fallback.

### Accessibility snapshot contains no messages

Cause: the shared messages are not represented in the currently exposed accessibility
tree.

Action: inspect route hydration; do not waste time clicking or scrolling blindly.

### Large noisy DevTools output

Cause: ChatGPT pages contain many scripts, assets, sidebar entries, and application
state.

Action: search for a narrow conversation-specific marker, then return a bounded
subtree only.

### Sensitive session data appears during investigation

Cause: broad inspection of a logged-in application's hydration state.

Action: stop broad output immediately, narrow extraction to the share route, and do
not persist or repeat credentials in logs or documentation.

## Possible Product Improvement

The verified fallback is useful enough to justify a future helper, for example:

```text
ext_read_chatgpt_share(url)
```

or an internal specialization inside `ext_read_webpage`.

Such a helper should:

- recognize `chatgpt.com/s/` and `chatgpt.com/share/` URLs;
- try rendered extraction first;
- detect the "shell only" failure mode;
- use a narrowly scoped hydration parser as fallback;
- return only title plus user/assistant conversation text;
- never expose raw hydration/session state;
- report the extraction method used.

Until that exists, follow the manual two-stage procedure in this document.

## Verified Takeaway

The important lesson is not "always parse ChatGPT internals." It is:

> Prefer the normal real-browser extraction path, and use narrowly scoped frontend
> hydration recovery only when the shared-conversation page does not expose its
> messages to the rendered DOM/accessibility extractor.

This preserves MCP4ChatGPT's browser-first architecture while keeping the fallback
targeted, auditable, and safer than dumping page application state.
