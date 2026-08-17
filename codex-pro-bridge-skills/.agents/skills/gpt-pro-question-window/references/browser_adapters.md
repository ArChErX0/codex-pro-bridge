# Browser Adapters

Read this reference whenever a Bridge round uploads a Task Bundle, mutates a ChatGPT page, performs browser fallback capture, or combines local browser control with SSH execution.

## Use the DevTools-first route

Use this order for every browser-mutating critical section:

1. On the Mac browser host, use `chrome-devtools` MCP `--autoConnect` against the user's existing signed-in stable Chrome profile. Prove that the tools are callable and connected to the intended profile; configuration alone is not connection proof. Let the user approve each newly attached dispatcher MCP process.
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
| Local Codex + local MCP/Chrome + local repository | Copy the approved bundle with `stage_bridge_attachment.py local`, then upload the returned OS-temp path. |
| Local Codex + local MCP/Chrome + repository or compute reached through SSH | Keep browser control local. Build the focused bundle remotely, then use `stage_bridge_attachment.py remote` to stream it to the browser host and verify SHA-256 before upload. |
| Remote Codex + remote MCP/Chrome on the same graphical host | Use only after the remote Chrome profile, remote-debugging permission, and upload path are visibly verified. |
| Remote Codex + local Chrome on another machine | Treat as unsupported by default. `--autoConnect` searches the MCP host, not the operator's Mac. Prefer a local Bridge dispatcher. An explicitly authorized SSH tunnel plus `--browser-url` may support inspection, but do not submit until the debugging endpoint is loopback-only and the bundle path is proven readable by the browser host. |

SSH access to an execution host is not browser access. Never expose a Chrome debugging port on a public or shared interface. Key browser serialization by the browser host/profile and use the host-global lease on that browser machine; the lease does not coordinate another machine.

## Chrome DevTools MCP

Normal Bridge operation uses the user's existing signed-in stable Chrome profile through `--autoConnect`. This preserves the user's current ChatGPT login, but Chrome requires an explicit human Allow for each new incoming debugging connection and does not currently offer persistent pre-authorization. Funnel all Bridge browser mutations through one long-lived Mac dispatcher so normal repeated reviews reuse one approved MCP process. Expect a new approval after the dispatcher MCP, Chrome, or Codex restarts. Treat configuration as discoverability, not connection proof. Acquire the host-global browser lease, then:

1. Ensure ordinary stable Chrome is already running and `chrome://inspect/#remote-debugging` is enabled. Let the user approve the incoming dispatcher connection. Stop for login, account security, CAPTCHA, or unexpected profile selection.
2. Call `list_pages`, choose the page whose URL contains the exact saved conversation or Project ID, and call `select_page`. Titles and MCP page IDs are not durable conversation identity.
3. Call `take_snapshot` and locate ChatGPT's visible attachment control or its associated file input.
4. Stage the Task Bundle with `stage_bridge_attachment.py local` or `remote`. Call `upload_file` with the returned OS-temp `staged_file` and the snapshot UID. Prefer the visible control or an associated input already present in the snapshot.
5. If the current ChatGPT DOM keeps `input#upload-files[type=file]` under a `display:none` parent and suppresses it from the accessibility snapshot, use the tested compatibility shim: save the exact input/parent attributes, temporarily expose that parent, append one visible semantic `<label for="upload-files" role="button" aria-label="Bridge upload control">`, take a new snapshot, and call `upload_file` on that label's UID. In `finally`, remove the label, restore every changed input/parent attribute, delete the rollback handle, and verify the current DOM has no shim label/handle and the parent is hidden again. Record this route only as `devtools-mcp-temporary-exposed-file-input-restored`; an unrestored DOM fails the critical section.
6. Wait for the exact filename, then take another snapshot and verify the attachment chip. Use redacted network diagnostics only when the upload fails or stalls.
7. Pass the exact successful adapter route to preflight and exchange capture.
8. For a dry run, remove the attachment and verify the exact chip is absent and the composer is empty. ChatGPT may re-render the clicked remove button quickly enough that `click` reports a stale-element error after removal; take a fresh snapshot/DOM observation and use the postcondition as authority instead of clicking again. Never close the selected ChatGPT page as cleanup.
9. In `finally`, release the lease and delete only this run's staging directory with `stage_bridge_attachment.py cleanup`.

Before step 1, claim the one dispatcher task with
`manage_bridge_dispatcher.py claim`. Pass its token to
`check_browser_preflight.py`. A second task that cannot claim the dispatcher
must hand off its request instead of calling any DevTools tool. This prevents
future per-task Bridge connections; it does not kill MCP processes owned by
other open Codex tasks.

If Codex app-server has accumulated old MCP process trees and the user asks for
cleanup, first run `reap_idle_chrome_mcp.py --parent-pid <verified-app-server-pid>`
without `--apply`. If it reports no established TCP connection, rerun with
`--apply`. If it reports exactly one established dispatcher tree, rerun with
`--apply --keep-active` to preserve that tree and remove only idle siblings.
More than one established tree fails closed. The script matches only
chrome-devtools-mcp 1.7.0
wrappers directly owned by that exact Codex process and sends SIGTERM; it never
stops Chrome. Do not run it during a browser lease or active Bridge mutation.

For a browser fallback, never persist `innerText`. Locate the already pinned
assistant turn in a fresh snapshot, verify its conversation ID, remote turn ID,
and preceding prompt digest. Run `build_browser_capture_spec.py` with those
values and a request-owned OS-temp `.json` path, then pass its exact `args`
object to `evaluate_script`. The generated function returns `schema_version`,
`conversation_id`, `remote_turn_id`, `prompt_sha256`, and `html: el.innerHTML`.
The builder safely escapes the identity literals and passes only the assistant
UID as the element argument.
Set the tool's `filePath` to a request-owned JSON file below the OS temp root so
the long HTML does not cross or truncate in model context. Run
`capture_browser_markdown.py --payload-file <that-file>` to preserve headings,
lists, tables, fenced code, links, and citation anchors before calling
`save_bridge_turn.py`. Treat a missing/ambiguous DOM identity or a tool result
that was returned inline instead of fully written as a failed capture.

Do not enable unrestricted filesystem paths to fix a missing root. Keep uploads inside the user-approved repository or MCP roots. The MCP adapter bypasses the Codex extension's file-URL permission, but it does not bypass ChatGPT file-size, type, quota, model, service, or account restrictions.

The MCP can see every open window in the selected ordinary Chrome profile. Keep unrelated sensitive pages closed while Bridge control is active, and funnel all local and SSH-backed reviews through this one dispatcher.

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

The browser lease is advisory and host-global on the browser machine. It serializes local repositories, worktrees, and SSH-backed reviews that share the user's ordinary stable Chrome profile; it is not a distributed lock across machines. The separate account-level generation slot serializes formal Pro generations after the browser lease is released.
