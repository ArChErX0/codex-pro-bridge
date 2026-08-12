# Browser Adapters

Read this reference whenever a Bridge round uploads a Task Bundle, mutates a ChatGPT page, or performs browser fallback capture.

## Select one adapter

Use this order for one browser-mutating critical section:

1. Use the `chrome-devtools` MCP tools when they are available and connected to the intended signed-in Chrome profile.
2. Otherwise use the Codex Chrome connector after verifying its extension and file-URL permission.
3. Use Computer Use only for a native or graphical boundary that neither browser adapter can control.

Keep one adapter active from destination verification through upload, preflight, and Send. Switching adapters is allowed only before Send, after confirming that no attachment or prompt was submitted.

Completion criterion: name the selected adapter, exact conversation ID, visible attachment, and selected model; unresolved values fail closed.

## Chrome DevTools MCP

Treat MCP configuration as discoverability, not connection proof. Acquire the browser lease, then:

1. Let the user approve Chrome's remote-debugging prompt when required. Stop for login, account security, CAPTCHA, or unexpected profile selection.
2. Call `list_pages`, choose the page whose URL contains the exact saved conversation or Project ID, and call `select_page`. Titles are not identity.
3. Call `take_snapshot` and locate ChatGPT's visible attachment control or its associated file input.
4. Call `upload_file` with the absolute Task Bundle path and the snapshot UID. The tool accepts a file input or a visible element that opens the chooser.
5. Wait for the exact filename, then take another snapshot and verify the attachment chip. Use redacted network diagnostics only when the upload fails or stalls.
6. Pass `--upload-control devtools-mcp-upload-file` to preflight and exchange capture.
7. For a dry run, remove the attachment and verify an empty composer.

Do not enable unrestricted filesystem paths to fix a missing root. Keep uploads inside the user-approved repository or MCP roots. The MCP adapter bypasses the Codex extension's file-URL permission, but it does not bypass ChatGPT file-size, type, quota, model, service, or account restrictions.

The MCP may see every open window in the connected Chrome profile. Use a dedicated Bridge profile for persistent operation. During the initial `--autoConnect` pilot, keep unrelated sensitive pages closed and use one declared browser dispatcher.

## Codex Chrome connector

Use this fallback only after verifying that the Codex Chrome extension is installed and enabled:

1. Open the extension details and enable **Allow access to file URLs**.
2. Start `waitForEvent("filechooser")` before clicking ChatGPT's visible attachment button and **Upload from computer** item.
3. Call `chooser.setFiles([absolute_path])`.
4. Verify the filename or attachment chip.
5. Pass `--upload-control codex-chrome-visible-menu` to preflight and exchange capture.

Use semantic visible controls. Treat a hidden input as an implementation detail, not a click target.

## Failure routing

- If an adapter fails before ChatGPT begins an upload and the composer remains empty, capture diagnostics and try the other adapter once under the same lease.
- If ChatGPT begins the upload but rejects, stalls, or returns a service error, record that remote failure and stop. Changing adapters does not change the server-side result.
- If Send may have been accepted, preserve the pre-submit boundary and inspect the exact conversation. Never resend to test the adapter.
- Release the lease in every terminal path. After accepted Send, use native `read_thread`; do not keep MCP or Chrome occupied while GPT Pro generates.

The current lease is advisory and repository-local even though the browser profile is host-local. Across repositories or worktrees, route all browser mutations through one declared dispatcher until a host-global lease exists.
