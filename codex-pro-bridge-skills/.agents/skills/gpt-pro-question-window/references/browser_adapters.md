# Browser Adapters

Read this reference whenever a Bridge round uploads a Task Bundle, mutates a ChatGPT page, performs browser fallback capture, or combines local browser control with SSH execution.

## Use the DevTools-first route

Use this order for every browser-mutating critical section:

1. Use the `chrome-devtools` MCP tools as the primary browser route. Prove that the tools are callable and connected to the intended signed-in Chrome profile; configuration alone is not connection proof.
2. Use the Codex Chrome connector only as a compatibility fallback when DevTools MCP is unavailable or fails before ChatGPT starts an upload and the composer remains empty.
3. Use Computer Use only for a native or graphical boundary that neither browser route can control.

Keep one route active from destination verification through upload, preflight, and Send. Switch to the connector at most once, only before Send, after proving that no attachment, upload, or prompt was submitted. Do not probe both routes merely to compare speed.

Completion criterion: record the selected route, browser host/profile, exact conversation ID, visible attachment, and selected model; unresolved values fail closed.

## Resolve the host topology

Keep these hosts distinct:

- **execution host:** repository, tests, jobs, or GPU work;
- **browser host:** the signed-in Chrome profile displaying ChatGPT;
- **MCP host:** the process running `chrome-devtools-mcp`.

The default supported topology requires the MCP host and browser host to be the same machine. The execution host may be different:

| Topology | Bridge behavior |
| --- | --- |
| Local Codex + local MCP/Chrome + local repository | Use the local absolute bundle path directly. |
| Local Codex + local MCP/Chrome + repository or compute reached through SSH | Keep browser control local. Fetch only the approved bundle or evidence to a local staging path, compare its SHA-256 across the handoff, then upload that local path. |
| Remote Codex + remote MCP/Chrome on the same graphical host | Use only after the remote Chrome profile, remote-debugging permission, and upload path are visibly verified. |
| Remote Codex + local Chrome on another machine | Treat as unsupported by default. `--autoConnect` searches the MCP host, not the operator's Mac. Prefer a local Bridge dispatcher. An explicitly authorized SSH tunnel plus `--browser-url` may support inspection, but do not submit until the debugging endpoint is loopback-only and the bundle path is proven readable by the browser host. |

SSH access to an execution host is not browser access. Never expose a Chrome debugging port on a public or shared interface. Key browser serialization by the browser host/profile; when the repository-local lease cannot coordinate multiple repositories or hosts, funnel mutations through one declared dispatcher.

## Chrome DevTools MCP

Treat MCP configuration as discoverability, not connection proof. Acquire the browser lease, then:

1. Let the user approve Chrome's remote-debugging prompt when required. Stop for login, account security, CAPTCHA, or unexpected profile selection.
2. Call `list_pages`, choose the page whose URL contains the exact saved conversation or Project ID, and call `select_page`. Titles and MCP page IDs are not durable conversation identity.
3. Call `take_snapshot` and locate ChatGPT's visible attachment control or its associated file input.
4. Confirm that the absolute Task Bundle path is available on the browser/MCP host, then call `upload_file` with that path and the snapshot UID. The tool accepts a file input or a visible element that opens the chooser.
5. Wait for the exact filename, then take another snapshot and verify the attachment chip. Use redacted network diagnostics only when the upload fails or stalls.
6. Pass `--upload-control devtools-mcp-upload-file` to preflight and exchange capture.
7. For a dry run, remove the attachment and verify an empty composer.

Do not enable unrestricted filesystem paths to fix a missing root. Keep uploads inside the user-approved repository or MCP roots. The MCP adapter bypasses the Codex extension's file-URL permission, but it does not bypass ChatGPT file-size, type, quota, model, service, or account restrictions.

The MCP may see every open window in the connected Chrome profile. Use a dedicated Bridge profile for persistent operation. During the initial `--autoConnect` pilot, keep unrelated sensitive pages closed and use one declared browser dispatcher.

## Codex Chrome connector fallback

Use this fallback only after a qualifying pre-submit DevTools failure and after verifying that the Codex Chrome extension is installed and enabled:

1. Open the extension details and enable **Allow access to file URLs**.
2. Start `waitForEvent("filechooser")` before clicking ChatGPT's visible attachment button and **Upload from computer** item.
3. Call `chooser.setFiles([absolute_path])`.
4. Verify the filename or attachment chip.
5. Pass `--upload-control codex-chrome-visible-menu` to preflight and exchange capture.

Use semantic visible controls. Treat a hidden input as an implementation detail, not a click target.

## Failure routing

- If DevTools MCP is absent from the current task, fails to initialize or connect, or fails before ChatGPT begins an upload while the composer remains empty, capture diagnostics and try the connector once under the same lease.
- If ChatGPT begins the upload but rejects, stalls, or returns a service error, record that remote failure and stop. Changing routes does not change the server-side result.
- If Send may have been accepted, preserve the pre-submit boundary and inspect the exact conversation. Never resend to test the adapter.
- Release the lease in every terminal path. After accepted Send, use native `read_thread`; do not keep MCP or Chrome occupied while GPT Pro generates.

The current lease is advisory and repository-local even though the browser profile is host-local. Across repositories or worktrees, route all browser mutations through one declared dispatcher until a host-global lease exists.
