"""Small stdio MCP client; browser requests never pass through model context."""
from __future__ import annotations

import json
import queue
import subprocess
import threading


class RpcError(RuntimeError):
    pass


class RpcTransportError(RpcError):
    """A disconnected or indeterminate stream must not accept more actions."""


class Client:
    def __init__(self, command, *, env, stderr, timeout=90, connect_timeout=300):
        if not isinstance(command, list) or not command or not all(isinstance(s, str) for s in command):
            raise RpcError("browser_command must be a non-empty argv array")
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=stderr, text=True, encoding="utf-8", env=env)
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self.browser_connected = False
        self.sequence = 0
        self.messages = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()
        try:
            self.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                         "clientInfo": {"name": "codex-bridge-worker", "version": "0.1.0"}})
            self.notify("notifications/initialized", {})
            self.schemas = {}
            cursor = None
            while True:
                result = self.request("tools/list", {"cursor": cursor} if cursor else {})
                self.schemas.update({tool["name"]: tool["inputSchema"] for tool in result["tools"]})
                cursor = result.get("nextCursor")
                if not cursor:
                    break
        except BaseException:
            self.close()
            raise

    def _read(self):
        try:
            for line in self.process.stdout:
                self.messages.put(json.loads(line))
        except Exception as exc:
            self.messages.put(RpcTransportError(f"Invalid browser MCP stream: {exc}"))
        finally:
            self.messages.put(RpcTransportError("Browser MCP disconnected"))

    def notify(self, method, params):
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _write(self, message):
        try:
            self.process.stdin.write(json.dumps(message, ensure_ascii=True) + "\n")
            self.process.stdin.flush()
        except (OSError, ValueError) as exc:
            raise RpcTransportError("Browser MCP write failed; outcome may be unknown") from exc

    def request(self, method, params, *, timeout=None):
        self.sequence += 1
        identifier = self.sequence
        self._write({"jsonrpc": "2.0", "id": identifier, "method": method, "params": params})
        import time
        budget = self.timeout if timeout is None else timeout
        # Only the persistent connection service uses zero for its first,
        # read-only authorization handshake; normal tool calls stay bounded.
        deadline = None if budget == 0 else time.monotonic() + budget
        while True:
            try:
                message = self.messages.get(timeout=None if deadline is None else max(0, deadline - time.monotonic()))
            except queue.Empty as exc:
                operation = params.get("name", method) if method == "tools/call" else method
                raise RpcTransportError(f"Browser MCP timeout in {operation}; outcome may be unknown") from exc
            if isinstance(message, Exception):
                raise message
            if message.get("id") != identifier or "method" in message:
                if "method" in message and "id" in message:
                    self._write({"jsonrpc": "2.0", "id": message["id"], "error": {
                        "code": -32601, "message": "Worker does not support server requests"}})
                continue
            if "error" in message:
                raise RpcError(str(message["error"]))
            return message["result"]

    def call(self, name, **arguments):
        if name not in self.schemas:
            raise RpcError(f"Browser MCP lacks {name}")
        if name != "list_pages" and name != "new_page":
            if "pageId" not in self.schemas[name].get("properties", {}):
                raise RpcError(f"{name} must support explicit pageId; selected-tab fallbacks are disabled")
        connecting = name == "list_pages" and not self.browser_connected
        # Auto-connect is lazy: initialize/tools-list never connect to Chrome.
        # Give the first read-only handshake its own human-authorization budget;
        # never apply that longer budget or a retry to Send/upload operations.
        result = self.request("tools/call", {"name": name, "arguments": arguments},
                              timeout=self.connect_timeout if connecting else self.timeout)
        if result.get("isError"):
            raise RpcError(f"{name} failed: {text_content(result)}")
        if connecting:
            self.browser_connected = True
        return result

    def close(self):
        if self.process.poll() is None:
            # MCP stdio shutdown is EOF. In WSL, terminating cmd.exe's local
            # wrapper does not reliably terminate its Windows Node child and
            # can leave an outstanding Chrome connection/permission request.
            # Give the server its documented EOF disconnect path first.
            try:
                self.process.stdin.close()
            except (OSError, ValueError):
                pass
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)


def text_content(result):
    return "\n".join(block["text"] for block in result.get("content", []) if block.get("type") == "text")


def evaluated(result):
    """Decode exactly one evaluate_script payload.

    The MCP wraps the script value in one outer fenced envelope, optionally
    behind its single prose header line. The value is a JSON string that may
    itself contain code fences, so the payload is located by decoding one JSON
    value and then validating the closing fence: scanning for fence pairs
    instead would cut one result into several blocks. The MCP may append its
    navigation receipt; accept that only when the payload independently reports
    the exact same URL. This is metadata, not permission to navigate or rebind.
    """
    text = text_content(result).strip()
    if not text:
        raise RpcError("Empty evaluate_script result")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("```")
    if start < 0:
        raise RpcError("evaluate_script result is neither raw JSON nor a fenced JSON envelope")
    if len([line for line in text[:start].splitlines() if line.strip()]) > 1:
        raise RpcError("evaluate_script result carries prose before its JSON envelope")
    header_end = text.find("\n", start)
    if header_end < 0:
        raise RpcError("evaluate_script fenced envelope is not followed by a payload")
    info = text[start + 3:header_end].strip().lower()
    if info not in ("", "json"):
        raise RpcError(f"evaluate_script fenced envelope declares unsupported format {info!r}")
    try:
        value, end = json.JSONDecoder().raw_decode(text, header_end + 1)
    except ValueError as exc:
        raise RpcError(f"evaluate_script envelope does not hold one complete JSON value: {exc}") from exc
    tail = text[end:].strip()
    observed_url = value.get("url") if isinstance(value, dict) else None
    navigation_tail = None
    if isinstance(observed_url, str) and observed_url and not any(
            c in observed_url for c in "\r\n"):
        navigation_tail = "```\nPage navigated to " + observed_url + "."
    if tail != "```" and tail != navigation_tail:
        raise RpcError("evaluate_script result carries content after its JSON envelope")
    return value
