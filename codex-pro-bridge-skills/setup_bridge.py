#!/usr/bin/env python3
"""Portable, backup-first Bridge deployment and non-sending readiness checks."""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

try:
    import tomllib
except ImportError:
    tomllib = None

ROOT = Path(__file__).resolve().parent
MANAGED = (".shared", "bundle-algorithm-context", "coordinate-auto-research",
           "experiment-plan-generator", "gpt-pro-algorithm-pipeline",
           "gpt-pro-paper-brainstormer", "gpt-pro-project-workspace",
           "gpt-pro-question-window", "gpt-pro-research-algorithm-reviewer",
           "gpt-pro-review-probe", "implementation-consistency-checker")
MCP_VERSION = "1.8.0"
BEGIN = "# BEGIN CODEX PRO BRIDGE SETUP v1"
END = "# END CODEX PRO BRIDGE SETUP v1"


class SetupError(RuntimeError):
    pass


def run(command, *, timeout=120):
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                            timeout=timeout, errors="replace", check=False)
    if result.returncode:
        # Child output can include proxy credentials. Do not echo it into receipts.
        raise SetupError(f"Command failed ({result.returncode}): {Path(command[0]).name}; inspect the tool locally")
    return result.stdout.strip()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise SetupError(f"Refusing symlink destination: {path}")
    descriptor, temporary = tempfile.mkstemp(prefix=".bridge-write-", dir=path.parent)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
        stream.write(value)
    os.replace(temporary, path)


def write_json(path, value):
    write(path, json.dumps(value, ensure_ascii=True, indent=2) + "\n")


def home_path(value=None):
    path = Path(value or os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser().absolute()
    if path == Path(path.anchor) or path == Path.home() or path.is_symlink():
        raise SetupError("CODEX_HOME must be a dedicated non-symlink directory, not a home or filesystem root")
    return path


def stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def private_directory(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise SetupError(f"Refusing private directory symlink: {path}")
    if os.name != "nt":
        os.chmod(path, 0o700)
        return
    whoami, icacls = shutil.which("whoami.exe"), shutil.which("icacls.exe")
    if not whoami or not icacls:
        raise SetupError("Windows whoami/icacls are required to secure runtime state and backups")
    row = next(csv.reader(run([whoami, "/user", "/fo", "csv", "/nh"]).splitlines()))
    sid = row[-1]
    if not sid.startswith("S-1-") or any(ch not in "S0123456789-" for ch in sid):
        raise SetupError("Could not resolve the current Windows user SID")
    # icacls modifies the DACL only. Set-Acl may request audit privileges on some
    # hosts, which would incorrectly require admin rights for a user-owned directory.
    run([icacls, str(path), "/reset"], timeout=30)
    run([icacls, str(path), "/inheritance:r", "/grant:r", "*" + sid + ":(OI)(CI)F"], timeout=30)


class Backups:
    """Retain replaced artifacts; never delete existing installations."""
    def __init__(self, home):
        self.root = home / "_archive" / ("bridge-setup-" + stamp())
        self.entries = []

    def preserve(self, path, *, move=False):
        if not path.exists() and not path.is_symlink():
            return
        if path.is_symlink():
            raise SetupError(f"Refusing to replace symlink: {path}")
        if not self.root.exists():
            private_directory(self.root)
        target = self.root / f"{len(self.entries):03d}-{path.name}"
        if move:
            shutil.move(str(path), target)
        elif path.is_dir():
            shutil.copytree(path, target)
        else:
            shutil.copy2(path, target)
        self.entries.append({"original": str(path), "backup": str(target), "moved": move})
        write_json(self.root / "restore-map.json", self.entries)


@contextmanager
def setup_lock(home):
    # Exclusive file avoids importing a not-yet-installed shared module.
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = home / ".bridge-setup.lock"
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise SetupError(f"Another setup may be running; inspect {lock} before removing this exact lock") from exc
    try:
        os.write(descriptor, str(os.getpid()).encode())
        os.close(descriptor)
        yield
    finally:
        lock.unlink()


def install_skills(destination, backups):
    source = ROOT / ".agents" / "skills"
    for name in MANAGED:
        if not (source / name).is_dir():
            raise SetupError(f"Incomplete checkout: missing skill {name}")
        if (destination / name).is_symlink():
            raise SetupError(f"Refusing managed symlink: {destination / name}")
    destination.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".bridge-stage-", dir=destination))
    for name in MANAGED:
        shutil.copytree(source / name, stage / name,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))
    for name in MANAGED:
        backups.preserve(destination / name, move=True)
        shutil.move(str(stage / name), destination / name)
    stage.rmdir()


def update_git_exclude(repo, backups, patterns=(".agents/", ".codex/")):
    if not shutil.which("git"):
        return
    result = subprocess.run(["git", "-C", str(repo), "rev-parse", "--path-format=absolute", "--git-path", "info/exclude"],
                            capture_output=True, text=True, encoding="utf-8", check=False)
    if result.returncode:
        return  # An invalid or cross-host .git must not break skills deployment.
    path = Path(result.stdout.strip())
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    missing = [entry for entry in patterns if entry not in original.splitlines()]
    if missing:
        backups.preserve(path)
        write(path, original.rstrip("\r\n") + "\n" + "\n".join(missing) + "\n")


def config_text(original, block):
    if tomllib is None:
        raise SetupError("The setup command requires Python 3.11+ (tomllib); Bridge workers support 3.10+")
    document = tomllib.loads(original)
    lines = original.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.rstrip("\r\n") == BEGIN]
    ends = [i for i, line in enumerate(lines) if line.rstrip("\r\n") == END]
    if starts or ends:
        if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
            raise SetupError("Malformed managed config block; no configuration was changed")
        old = "".join(lines[starts[0] + 1:ends[0]])
        parsed = tomllib.loads(old)
        if set(parsed) != {"mcp_servers"} or "codex-pro-bridge" not in parsed["mcp_servers"] or not set(parsed["mcp_servers"]) <= {"codex-pro-bridge", "chrome-devtools"}:
            raise SetupError("Managed block contains unrelated settings; refusing replacement")
        # Only replace a standalone block, not marker text inside a multiline TOML value.
        if any(document.get("mcp_servers", {}).get(name) != value for name, value in parsed["mcp_servers"].items()):
            raise SetupError("Managed block is not the actual Bridge server section")
        result = "".join(lines[:starts[0]]) + block + "".join(lines[ends[0] + 1:])
    else:
        if "codex-pro-bridge" in document.get("mcp_servers", {}):
            raise SetupError("An unmanaged codex-pro-bridge MCP already exists. Keep it, or manually archive its section before setup")
        # New headers must not inherit a trailing table or commented EOF line.
        result = original.rstrip() + "\n\n" + block
    tomllib.loads(result)
    owned = set(tomllib.loads(block.split(BEGIN, 1)[1].split(END, 1)[0])["mcp_servers"])
    before = {k: v for k, v in document.get("mcp_servers", {}).items() if k not in owned}
    after = {k: v for k, v in tomllib.loads(result).get("mcp_servers", {}).items() if k not in owned}
    if before != after:
        raise SetupError("Unrelated MCP configuration would change")
    final = tomllib.loads(result)
    for value in (document, final):
        for name in owned:
            value.get("mcp_servers", {}).pop(name, None)
        if not value.get("mcp_servers"):
            value.pop("mcp_servers", None)
    if document != final:
        raise SetupError("Unrelated Codex configuration would change")
    return result


def mcp_block(python, entry, runtime, environment, browser=None, browser_env=None):
    literal = lambda value: json.dumps(str(value), ensure_ascii=True)
    lines = [BEGIN, "[mcp_servers.codex-pro-bridge]", "command = " + literal(python),
             "args = [" + literal(entry) + ', "--config", ' + literal(runtime) + "]",
             "startup_timeout_sec = 30", "tool_timeout_sec = 90", "",
             "[mcp_servers.codex-pro-bridge.env]"]
    lines.extend(literal(key) + " = " + literal(value) for key, value in environment.items())
    if browser:
        lines.extend(["", "[mcp_servers.chrome-devtools]", "command = " + literal(browser[0]),
                      "args = " + json.dumps(browser[1:], ensure_ascii=True),
                      "startup_timeout_sec = 60", "tool_timeout_sec = 120", "",
                      "[mcp_servers.chrome-devtools.env]"])
        lines.extend(literal(key) + " = " + literal(value) for key, value in (browser_env or {}).items())
    return "\n".join([*lines, END, ""])


def topology(value):
    if value != "auto":
        return value
    if os.name == "nt":
        return "windows-native"
    if "microsoft" in run(["uname", "-r"]).lower():
        return "wsl-windows"
    return "native"


def node_host(kind):
    if kind == "wsl-windows":
        executable = shutil.which("node.exe")
    else:
        executable = shutil.which("node")
    if not executable:
        raise SetupError("Node.js is missing on the browser host. Install Node 22.12+ or 24 LTS there, then rerun")
    node = Path(executable).resolve()
    version = run([str(node), "--version"]).lstrip("v").split(".")
    major, minor, patch = map(int, version[:3])
    if not (major >= 23 or major == 22 and minor >= 12 or major == 20 and (minor, patch) >= (19, 0)):
        raise SetupError(f"Node {'.'.join(version)} does not meet Chrome MCP 1.8.0 requirements")
    return node


def winpath(path):
    return run(["wslpath", "-w", str(path)])


def browser_command(home, kind, supplied, skip, host_root=None):
    if supplied:
        command = json.loads(supplied)
        if not isinstance(command, list) or not command or not all(isinstance(x, str) and x for x in command):
            raise SetupError("--browser-command-json must be a non-empty argv array")
        return command
    node = node_host(kind)
    npm = node.parent / "node_modules" / "npm" / "bin" / "npm-cli.js"
    if host_root:
        browser_home = Path(host_root).expanduser().resolve()
    elif kind == "wsl-windows":
        # Keep browser-side dependencies and uploads on the Windows host.
        win_home = run([str(node), "-p", "require('os').homedir()"])
        browser_home = Path(run(["wslpath", "-u", win_home])) / ".codex-pro-bridge"
    else:
        browser_home = home / "codex-pro-bridge"
    dependency = browser_home / "browser" / ("chrome-mcp-" + MCP_VERSION)
    package = dependency / "node_modules" / "chrome-devtools-mcp" / "package.json"
    if not package.is_file() or json.loads(package.read_text(encoding="utf-8")).get("version") != MCP_VERSION:
        if skip:
            raise SetupError("Pinned browser dependency is missing; rerun without --skip-browser-install or supply an existing argv")
        dependency.mkdir(parents=True, exist_ok=True)
        if npm.is_file():
            command = [str(node), winpath(npm) if kind == "wsl-windows" else str(npm)]
        elif kind != "wsl-windows" and shutil.which("npm") and os.name != "nt":
            command = [shutil.which("npm")]
        else:
            raise SetupError("Cannot find the Node-host npm-cli.js; repair the browser-host Node/npm installation")
        prefix = winpath(dependency) if kind == "wsl-windows" else str(dependency)
        run([*command, "install", "--prefix", prefix, "--ignore-scripts", "--no-audit", "--no-fund",
             "--save-exact", "chrome-devtools-mcp@" + MCP_VERSION], timeout=240)
    if json.loads(package.read_text(encoding="utf-8")).get("version") != MCP_VERSION:
        raise SetupError("Installed browser MCP version differs from the pin")
    entry = dependency / "node_modules" / "chrome-devtools-mcp" / "build" / "src" / "bin" / "chrome-devtools-mcp.js"
    if not entry.is_file():
        raise SetupError("Pinned MCP executable is missing")
    return [str(node), winpath(entry) if kind == "wsl-windows" else str(entry), "--autoConnect", "--no-usage-statistics"]


def host_environment(home, kind, host_root=None):
    private = home / "codex-pro-bridge"
    env = {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    if kind == "native":
        return env, None
    if kind == "wsl-windows":
        if host_root:
            stage = Path(host_root).expanduser().resolve() / "staging"
        else:
            win_home = run([str(node_host(kind)), "-p", "require('os').homedir()"])
            stage = Path(run(["wslpath", "-u", win_home])) / ".codex-pro-bridge" / "staging"
        mapped = winpath(stage)
        if len(mapped) < 3 or mapped[1:3] != ":\\":
            raise SetupError("WSL browser-host root must be on a Windows drive, not an ext4/UNC path")
        env.update(CODEX_BRIDGE_STAGING_EXECUTION_ROOT=str(stage),
                   CODEX_BRIDGE_STAGING_BROWSER_ROOT=mapped)
        example = "browser-host.wsl-windows.json.example"
    else:
        stage = (Path(host_root).expanduser().resolve() if host_root else private) / "staging"
        env["CODEX_BRIDGE_STAGING_ROOT"] = str(stage)
        example = "browser-host.windows.json.example"
    stage.mkdir(parents=True, exist_ok=True)
    env.update(CODEX_PRO_BRIDGE_HOST_CONFIG=str(private / "browser-host.json"),
               CODEX_BRIDGE_STAGING_LOCK=str(private / "staging.lock"))
    return env, json.loads((ROOT / "config" / example).read_text(encoding="utf-8"))


def install(args):
    if os.name == "nt" and str(ROOT).lower().startswith(("\\\\wsl.localhost\\", "\\\\wsl$\\")):
        raise SetupError("Windows setup must use a checkout on a Windows drive, not the slow WSL UNC share. For WSL execution use Linux Python and --topology wsl-windows")
    if args.command == "install" and sys.version_info < (3, 11):
        raise SetupError("Setup requires Python 3.11+")
    home = home_path(args.codex_home)
    repo = Path(args.repo).expanduser().resolve(strict=True) if args.repo else None
    if repo and not repo.is_dir():
        raise SetupError("--repo must be an existing directory")
    if args.command == "install" and repo is None:
        raise SetupError("Full setup requires --repo to grant one explicit repository; it never authorizes all folders")
    skills = repo / ".agents" / "skills" if args.repo_local else home / "skills"
    if args.repo_local and repo is None:
        raise SetupError("--repo-local requires --repo")
    if args.command == "skills":
        backups = Backups(home)
        with setup_lock(home):
            install_skills(skills, backups)
            if args.repo_local:
                update_git_exclude(repo, backups)
        return {"status": "skills-installed", "skills": str(skills),
                "backup": str(backups.root) if backups.entries else None, "mcp_configured": False}, 0
    private = home / "codex-pro-bridge"
    runtime = private / "runtime.json"
    config_path = home / "config.toml"
    original = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    if tomllib is None:
        raise SetupError("Setup requires Python 3.11+")
    settings = tomllib.loads(original)
    owns_chrome = "chrome-devtools" not in settings.get("mcp_servers", {})
    if BEGIN in original and END in original:
        previous_block = tomllib.loads(original.split(BEGIN, 1)[1].split(END, 1)[0])
        owns_chrome = owns_chrome or "chrome-devtools" in previous_block.get("mcp_servers", {})
    environment, host = {}, None
    config = None
    for path in (private, home / "skills", config_path, runtime, private / "setup", private / "state"):
        if path.is_symlink():
            raise SetupError(f"Refusing symlink destination: {path}")
    for name in MANAGED:
        if not (ROOT / ".agents" / "skills" / name).is_dir():
            raise SetupError(f"Incomplete checkout: missing skill {name}")
    if args.command == "install":
        entry = skills / "gpt-pro-question-window" / "scripts" / "bridge_mcp.py"
        # Fail conflicts before dependency installation or copying any skills.
        config_text(original, mcp_block(sys.executable, entry, runtime, {}, ["preflight-only"] if owns_chrome else None))
        if args.repo_local:
            raise SetupError("Full MCP setup uses global skills; use skills --repo-local for a skills-only install")
    backups = Backups(home)
    with setup_lock(home):
        if args.command == "install":
            kind = topology(args.topology)
            existing = json.loads(runtime.read_text(encoding="utf-8")) if runtime.exists() else None
            if existing and args.topology == "auto":
                kind = existing.get("setup_topology", kind)
            supplied = args.browser_command_json or (json.dumps(existing["browser_command"]) if existing else None)
            host_root = args.browser_host_root or (existing or {}).get("setup_browser_host_root")
            command = browser_command(home, kind, supplied, args.skip_browser_install, host_root)
            environment, host = host_environment(home, kind, host_root)
            ui = json.loads(Path(args.ui_profile).read_text(encoding="utf-8")) if args.ui_profile else (
                existing["ui"] if existing else json.loads((ROOT / "config" / "ui-chatgpt-20260930.zh-CN.json.example").read_text(encoding="utf-8")))
            state = private / "state"
            private_directory(private)
            private_directory(state)
            config = {**(existing or {}), "state_dir": str(state),
                      "allowed_repos": sorted(set((existing or {}).get("allowed_repos", []) + [str(repo)])),
                      "browser_command": command, "browser_transport": args.browser_transport or (existing or {}).get("browser_transport", "persistent"), "ui": ui,
                      "browser_connect_timeout_seconds": 60, "browser_tool_timeout_seconds": 90}
            config["setup_topology"] = kind
            if host_root:
                config["setup_browser_host_root"] = str(Path(host_root).expanduser().resolve())
            config["setup_ui_verified"] = False
            # Preserve an observed UI profile across reinstall. New profiles are hints until doctor passes.
            config["setup_ui_source"] = "custom" if args.ui_profile else (existing or {}).get("setup_ui_source", "builtin")
            config["browser_env"] = {**config.get("browser_env", {}), "NODE_DISABLE_COMPILE_CACHE": "1"}
            updated = config_text(original, mcp_block(sys.executable, entry, runtime, environment,
                                  command if owns_chrome else None, config["browser_env"]))
        install_skills(skills, backups)
        private.mkdir(parents=True, exist_ok=True, mode=0o700)
        tools = private / "setup"
        backups.preserve(tools, move=True)
        tools.mkdir()
        shutil.copy2(ROOT / "setup_bridge.py", tools / "setup_bridge.py")
        shutil.copytree(ROOT / "config", tools / "config")
        (tools / "docs").mkdir()
        shutil.copy2(ROOT / "docs" / "INSTALL.md", tools / "docs" / "INSTALL.md")
        if config is not None:
            if (config_path.read_text(encoding="utf-8") if config_path.exists() else "") != original:
                raise SetupError("Codex configuration changed during setup; refusing to overwrite it")
            for path in (runtime, config_path, private / "browser-host.json"):
                backups.preserve(path)
            write_json(runtime, config)
            if host:
                write_json(private / "browser-host.json", host)
            write(config_path, updated)
            update_git_exclude(repo, backups, patterns=(".codex/codex-pro-bridge/",))
        receipt = {"schema_version": "bridge-install/v1", "status": "installed-not-verified",
                   "skills": str(skills), "runtime": str(runtime) if config else None,
                   "backup": str(backups.root) if backups.entries else None,
                   "setup_docs": str(tools / "docs" / "INSTALL.md"),
                   "doctor": [sys.executable, str(tools / "setup_bridge.py"), "doctor", "--codex-home", str(home), "--connect"],
                   "restart_codex": True}
        write_json(private / "install-receipt.json", receipt)
    if args.command == "skills" or args.no_connect:
        return receipt, 0
    result, code = doctor(argparse.Namespace(codex_home=str(home), connect=True))
    return {**receipt, "status": result["status"], "readiness": result}, code


def check_schemas(schemas):
    missing = []
    for name in ("list_pages", "new_page", "evaluate_script", "take_snapshot", "click", "fill",
                 "upload_file", "press_key", "select_page"):
        if name not in schemas:
            missing.append(name)
        elif name not in ("list_pages", "new_page") and "pageId" not in schemas[name].get("properties", {}):
            missing.append(name + ".pageId")
    if missing:
        raise SetupError("Browser MCP incompatible: missing " + ", ".join(missing))


def doctor(args):
    home = home_path(args.codex_home)
    with setup_lock(home):
        return _doctor(args, home)


def _doctor(args, home):
    private = home / "codex-pro-bridge"
    receipt_path = private / "install-receipt.json"
    if not receipt_path.is_file():
        raise SetupError("No installation receipt; run install --repo first")
    installed = json.loads(receipt_path.read_text(encoding="utf-8"))
    if not installed.get("runtime"):
        raise SetupError("Skills-only installation; run full install --repo to configure MCP")
    scripts = Path(installed["skills"]) / "gpt-pro-question-window" / "scripts"
    sys.path[:0] = [str(scripts), str(Path(installed["skills"]) / ".shared")]
    from bridge_runtime.jobs import load_config
    from bridge_runtime.rpc import Client
    config = load_config(installed["runtime"])
    for key in ("account", "workspace"):
        if not isinstance(config["ui"].get(key), str) or not config["ui"][key].strip():
            raise SetupError(f"Readiness profile requires an observed {key} selector")
    if not all(Path(p).is_dir() for p in config["allowed_repos"]):
        raise SetupError("An authorized repository no longer exists")
    state = Path(config["state_dir"])
    if os.name != "nt" and state.stat().st_mode & 0o077:
        raise SetupError("Runtime state permissions must be 0700")
    # Load only our managed server's environment, never arbitrary provider values.
    settings = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
    server = settings["mcp_servers"]["codex-pro-bridge"]
    if Path(server["args"][-1]).resolve() != Path(installed["runtime"]).resolve():
        raise SetupError("Installed runtime and registered MCP disagree")
    environment = {**os.environ, **server.get("env", {}), **config.get("browser_env", {})}
    with (private / "doctor-browser.log").open("a", encoding="utf-8") as log:
        if args.connect and config.get("browser_transport") == "persistent":
            from bridge_runtime.connection_service import SharedClient
            # Keep the authorized route alive for subsequent workers; do not force another consent connection.
            client = SharedClient(config)
        else:
            client = Client(config["browser_command"], env=environment, stderr=log,
                            timeout=30, connect_timeout=60)
        try:
            check_schemas(client.schemas)
            if not args.connect:
                result = {"status": "configured", "browser_schema": "passed", "live_browser": "not-tested",
                          "next": "Run doctor --connect; allow Chrome remote debugging and log in to ChatGPT"}
                return result, 0
            result = probe_ui(client, config)
        finally:
            client.close()
    backups = Backups(home)
    backups.preserve(Path(installed["runtime"]))
    config["ui"] = result.pop("ui", config["ui"])
    config["setup_ui_verified"] = True
    write_json(Path(installed["runtime"]), config)
    write_json(private / "readiness.json", result)
    installed["status"] = result["status"]
    installed["readiness_path"] = str(private / "readiness.json")
    write_json(receipt_path, installed)
    return result, 0


def readiness_browser(client, ui):
    from bridge_runtime.browser import Browser
    from bridge_store import TAB_OWNER_STORAGE_KEY

    class UnownedReadinessBrowser(Browser):
        # Worker Browser deliberately requires a nonempty owner token. Setup observes
        # only unowned pages, where absent sessionStorage is null, not an empty string.
        # Keep that distinction local; never relax worker ownership semantics.
        def assert_identity(self):
            observed = self.evaluate("() => ({url:location.href,owner:sessionStorage.getItem(" +
                                     json.dumps(TAB_OWNER_STORAGE_KEY) + ") || ''})")
            if observed != {"url": self.url, "owner": ""}:
                raise SetupError("Setup page owner/URL changed; no further observation authorized")

        def owned_evaluate(self, function):
            guard = ("if(location.href !== " + json.dumps(self.url) + " || sessionStorage.getItem(" +
                     json.dumps(TAB_OWNER_STORAGE_KEY) + ")) throw Error('Setup page owner/URL changed');")
            return self.evaluate("() => {" + guard + " return (" + function + ")(); }")

    return UnownedReadinessBrowser(client, ui)


def observe_intensity_positions(browser, ui):
    def state():
        value = browser.owned_evaluate("() => { const ns=[...document.querySelectorAll(" + json.dumps(ui["thinking_slider"]) + ")].filter(n=>n.getClientRects().length); if(ns.length!==1) throw Error('Slider ambiguity'); const n=ns[0]; return {value:n.getAttribute('aria-valuenow'),min:n.getAttribute('aria-valuemin'),max:n.getAttribute('aria-valuemax')}; }")
        if any(value.get(key) is None for key in ("value", "min", "max")):
            raise SetupError("Slider does not expose observable positions")
        return {key: int(value[key]) for key in ("value", "min", "max")}

    original = state()
    if not 0 <= original["min"] <= original["value"] <= original["max"] or original["max"] - original["min"] > 6:
        raise SetupError("Slider position/range cannot be safely observed")
    selector = ui["thinking_keyboard_control"]
    focused = browser.owned_evaluate("() => {const ns=[...document.querySelectorAll(" + json.dumps(selector) + ")].filter(n=>n.getClientRects().length);if(ns.length!==1)throw Error('Keyboard controller ambiguity');ns[0].focus();return document.activeElement===ns[0];}")
    if focused is not True:
        raise SetupError("Intensity observation cannot focus the actual keyboard controller")
    seen = {}

    def label():
        value = browser.read_labels(("thinking",))["thinking"]
        if value in ui.get("thinking_placeholder_labels", ()):
            raise SetupError("Thinking control still shows a placeholder")
        return value

    original_label = label()

    def move(target, collect=False):
        current = state()
        if (current["min"], current["max"]) != (original["min"], original["max"]):
            raise SetupError("Intensity range changed during setup")
        if collect:
            seen.setdefault(label(), set()).add(current["value"])
        while current["value"] != target:
            direction = 1 if target > current["value"] else -1
            browser.press("ArrowRight" if direction == 1 else "ArrowLeft")
            updated = state()
            if updated["value"] != current["value"] + direction or (updated["min"], updated["max"]) != (original["min"], original["max"]):
                raise SetupError("Intensity did not advance exactly one observed step; no retry")
            current = updated
            if collect:
                seen.setdefault(label(), set()).add(current["value"])

    try:
        move(original["min"])
        move(original["max"], collect=True)
    finally:
        # Restoration still checks the exact unowned URL and each real step. Never
        # restore over a new owner or an unobservable/ambiguous slider.
        move(original["value"])
        if label() != original_label:
            raise SetupError("Intensity restoration did not recover the original visible label")
    positions = {name: next(iter(values)) for name, values in seen.items() if len(values) == 1}
    if not positions:
        raise SetupError("No intensity has a unique observed position")
    return positions, sorted(name for name, values in seen.items() if len(values) > 1)


def probe_ui(client, config, *, can_open_tab=True):
    from bridge_runtime.browser import unique_uid
    browser = readiness_browser(client, config["ui"])
    pages = [p for p in browser.pages() if p["url"].startswith("https://chatgpt.com/")]
    if not pages and not can_open_tab:
        raise SetupError("Open https://chatgpt.com/ in the authorized Chrome profile and log in, then run doctor --connect")
    for page in pages:
        identity = browser.evaluate("() => ({url:location.href,owner:sessionStorage.getItem('codex-pro-bridge.tab-owner.v1')||''})", page["page_id"])
        if identity["url"] != page["url"] or identity["owner"]:
            continue  # Never inspect menus or drafts belonging to a live Bridge task.
        browser.page_id, browser.url, browser.owner = page["page_id"], page["url"], ""
        snapshot = browser.snapshot()
        ui = config["ui"]
        if config.get("setup_ui_source") == "builtin":
            deadline = time.monotonic() + 15
            while True:
                if unique_uid(snapshot, ("button",), ["选择 ChatGPT 模型"], allow_missing=True):
                    profile = "ui-chatgpt-20260930.zh-CN.json.example"
                    break
                if unique_uid(snapshot, ("button",), ["Select ChatGPT model"], allow_missing=True):
                    profile = "ui-chatgpt-nested.en.json.example"
                    break
                if time.monotonic() >= deadline:
                    raise SetupError("Unknown ChatGPT UI or login pending; log in, or ask the installing agent to observe and supply --ui-profile. Do not guess selectors")
                time.sleep(0.5)
                snapshot = browser.snapshot()
            ui = json.loads((ROOT / "config" / profile).read_text(encoding="utf-8"))
            browser.ui = ui
        if browser.composer_text() or browser.attachments():
            continue
        account = browser.read_labels(("account", "workspace"))
        if not account["account"] or not account["workspace"]:
            raise SetupError("Account identity is not visible; log in before readiness verification")
        if ui.get("control_layout") == "nested-slider":
            browser.click(ui["thinking_menu_labels"])
            try:
                browser.read_labels(("thinking",))
                positions, ambiguous = observe_intensity_positions(browser, ui)
                ui = {**ui, "thinking_positions": positions}
                browser.ui = ui
                browser.click(ui["model_menu_labels"])
                browser.read_labels(("model",))
            finally:
                browser.press("Escape")
                browser.press("Escape")
            if ui.get("thinking_label_location", "closed-trigger") == "closed-trigger":
                browser.read_labels(("thinking",))
        else:
            browser.read_labels(("model", "thinking"))
        browser.assert_identity()
        # No upload, prompt, Send, model selection, ownership claim or Project adoption occurred.
        return {"schema_version": "bridge-readiness/v1", "status": "ready", "browser_schema": "passed",
                "login": "observed", "ui_profile": "observed", "send_test": "not-run",
                "intensity_restored": True, "ambiguous_intensities": ambiguous if ui.get("control_layout") == "nested-slider" else [],
                "checked_at": datetime.now(timezone.utc).isoformat(), "ui": ui,
                "next": "Restart Codex. Invoke gpt-pro-question-window with an explicitly authorized question"}
    if can_open_tab:
        # One dedicated empty tab is safer than asking users to clear a draft or disturb another job.
        client.call("new_page", url="https://chatgpt.com/")
        return probe_ui(client, config, can_open_tab=False)
    raise SetupError("No empty, unowned ChatGPT tab is available; inspect the dedicated setup tab and log in before rerunning doctor")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("install", "skills"):
        command = commands.add_parser(name)
        command.add_argument("--repo")
        command.add_argument("--repo-local", action="store_true")
        command.add_argument("--codex-home")
        command.add_argument("--topology", choices=("auto", "windows-native", "wsl-windows", "native"), default="auto")
        command.add_argument("--ui-profile")
        command.add_argument("--browser-command-json")
        command.add_argument("--browser-host-root", help="Private browser-host directory; in WSL pass its Windows-drive mount path")
        command.add_argument("--browser-transport", choices=("persistent", "stdio"))
        command.add_argument("--skip-browser-install", action="store_true")
        command.add_argument("--no-connect", action="store_true")
    command = commands.add_parser("doctor")
    command.add_argument("--codex-home")
    command.add_argument("--connect", action="store_true")
    args = parser.parse_args(argv)
    try:
        result, code = doctor(args) if args.command == "doctor" else install(args)
    except (SetupError, OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        result, code = {"status": "blocked", "error": str(exc), "send_attempted": False}, 2
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
