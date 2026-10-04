#!/usr/bin/env python3
"""按明确请求组合既有bundle和staging；不创建会话、不上传、不Send。"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import uuid
import zipfile
from pathlib import Path

SHARED_DIR = Path(__file__).resolve().parents[2] / ".shared"
BUILDER_DIR = Path(__file__).resolve().parents[2] / "bundle-algorithm-context/scripts"
sys.path.insert(0, str(SHARED_DIR))
sys.path.insert(0, str(BUILDER_DIR))
from bridge_store import BridgeError, atomic_write_text, bridge_root, file_sha256
from build_algorithm_bundle import is_candidate
from material_prompt import (delivery_prompt, material_guide as material_guide,
                             material_references as material_references,
                             question_with_material_paths as question_with_material_paths)
from evidence_safety import is_excluded_by_name, scan_for_secrets
from host_config import load_browser_host_config
from executor_handoff import CONTEXT_POLICIES
from collaboration_scope import validate_scope_binding


def read_request(path: Path) -> dict:
    if not path.is_file():
        raise BridgeError("Request must be a regular file")
    request = json.loads(path.read_text(encoding="utf-8"))
    required = {"repo", "bridge_thread_id", "goal", "question", "notes", "files"}
    optional = {"bridge_project_id", "mode", "file_digests", "context_policy", "max_files",
                "codex_root_thread_id", "owner_agent_id"}
    if not isinstance(request, dict) or set(request) - required - optional or required - set(request):
        raise ValueError(
            "request requires repo/bridge_thread_id/goal/question/notes/files; "
            "optional bridge_project_id/mode/file_digests/context_policy/max_files"
        )
    string_keys = (required - {"files"}) | (
        (set(request) & optional) - {"file_digests", "max_files"}
    )
    for key in string_keys:
        if not isinstance(request[key], str) or not request[key].strip():
            raise ValueError(f"{key} must be a nonempty string")
    root = Path(request["repo"]).expanduser()
    if not root.is_absolute() or not root.is_dir():
        raise ValueError("repo must be an absolute existing directory")
    root = root.resolve()
    validate_scope_binding(request)
    if not isinstance(request["files"], list):
        raise TypeError("files must be a list")
    context_policy = request.get("context_policy", "explicit")
    if not isinstance(context_policy, str) or context_policy not in CONTEXT_POLICIES:
        raise ValueError("context_policy must be one of: " + ", ".join(CONTEXT_POLICIES))
    if context_policy == "none" and request["files"]:
        raise ValueError("context_policy none cannot be combined with files")
    if context_policy == "explicit" and not request["files"]:
        raise ValueError("context_policy explicit requires at least one file")
    configured_max = request.get("max_files", 24 if context_policy == "auto" else len(request["files"]))
    if isinstance(configured_max, bool) or not isinstance(configured_max, int):
        raise TypeError("max_files must be an integer")
    if configured_max < 0:
        raise ValueError("max_files must not be negative")
    if context_policy == "none" and configured_max != 0:
        raise ValueError("context_policy none requires max_files=0")
    if context_policy == "explicit" and configured_max != len(request["files"]):
        raise ValueError("context_policy explicit requires max_files to equal file count")
    if context_policy == "auto" and configured_max <= 0:
        raise ValueError("max_files must be positive for auto context policy")
    request["context_policy"] = context_policy
    request["max_files"] = configured_max
    for value in [request["notes"], *request["files"]]:
        if not isinstance(value, str) or not value:
            raise ValueError("notes/files must contain nonempty paths")
        raw_file = Path(value).expanduser()
        raw_file = raw_file if raw_file.is_absolute() else root / raw_file
        if raw_file.is_symlink():
            raise ValueError(f"file must be a regular non-symlink file: {value}")
        file = raw_file.resolve(strict=True)
        if not file.is_file() or not file.is_relative_to(root):
            raise ValueError(f"file must stay inside repo: {value}")
    declared_paths = {
        str((root / value).resolve().relative_to(root).as_posix())
        for value in [request["notes"], *request["files"]]
    }
    if "file_digests" in request:
        digests = request["file_digests"]
        if not isinstance(digests, dict) or set(digests) != declared_paths:
            raise ValueError("file_digests must cover exactly notes and files")
        for relative, expected in digests.items():
            if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
                raise ValueError(f"file_digests[{relative!r}] must be lowercase SHA-256")
            actual = file_sha256(root / relative)
            if actual != expected:
                raise ValueError(f"file drift detected: {relative}")
    request["repo"] = str(root)
    request["notes"] = str((root / request["notes"]).resolve())
    request["files"] = [str((root / value).resolve()) for value in request["files"]]
    return request


def admit_request(request: dict, *, stage=False) -> None:
    """Read-only admission, before snapshot/package/output/staging side effects."""
    root = Path(request["repo"])
    notes = Path(request["notes"])
    canonical = bridge_root(root) / "codex-sessions"
    if (is_excluded_by_name(notes, canonical) if notes.is_relative_to(canonical)
            else is_excluded_by_name(notes, root)):
        raise BridgeError("Codex notes path is excluded by the safety policy")
    explicit_paths = (
        tuple(Path(value) for value in request["files"])
        if request.get("context_policy") == "explicit"
        else ()
    )
    for value in request["files"]:
        if not is_candidate(
            Path(value),
            root,
            include_logs=True,
            explicit_paths=explicit_paths,
        ):
            raise BridgeError("Explicit evidence path is excluded by the safety policy")
    inputs = [notes, *(Path(value) for value in request["files"])]
    if scan_for_secrets(inputs, max(path.stat().st_size for path in inputs)):
        raise BridgeError("High-confidence secret-like content in requested materials")
    if stage:
        config = load_browser_host_config()
        for path in (config.execution_root, config.lock_path):
            if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
                raise BridgeError("Browser-host staging mapping contains a symlink")
        if config.execution_root.exists() and not config.execution_root.is_dir():
            raise BridgeError("Browser-host staging root is not a directory")


def packaging_inputs(request_path: Path, request: dict) -> dict:
    owners = [Path(__file__), BUILDER_DIR / "build_algorithm_bundle.py", SHARED_DIR / "material_prompt.py",
              SHARED_DIR / "bridge_store.py", SHARED_DIR / "evidence_safety.py",
              SHARED_DIR / "host_config.py", SHARED_DIR / "browser_host.py", SHARED_DIR / "evidence_graph.py",
              SHARED_DIR / "executor_handoff.py", SHARED_DIR / "collaboration_scope.py", SHARED_DIR / "project_store.py"]
    return {"request_sha256": file_sha256(request_path),
            "input_sha256": {str(path): file_sha256(path) for path in
                             [Path(request["notes"]), *(Path(v) for v in request["files"])]},
            "code_sha256": {str(path): file_sha256(path) for path in owners}}


def verify_packaging(receipt: dict, request_path: Path, request: dict) -> dict:
    if receipt.get("schema_version") != "verified-packaging/v1" or receipt.get("packaging_verified") is not True:
        raise BridgeError("Preparation lacks verified packaging receipt")
    for key, expected in packaging_inputs(request_path, request).items():
        if receipt.get(key) != expected:
            raise BridgeError(f"Verified packaging drift: {key}")
    root = Path(request["repo"])
    if receipt.get("context_policy") != request["context_policy"] or receipt.get("max_files") != request["max_files"]:
        raise BridgeError("Verified packaging policy drift")
    keys = [("prompt_file", "prompt_sha256")]
    if request["context_policy"] != "none":
        keys += [("bundle", "bundle_sha256"), ("materials_file", "materials_sha256")]
    for path_key, hash_key in keys:
        raw = Path(receipt.get(path_key, ""))
        path = raw.resolve(strict=True)
        if raw.is_symlink() or not path.is_relative_to(root) or file_sha256(path) != receipt.get(hash_key):
            raise BridgeError(f"Verified packaging artifact drift: {path_key}")
    expected_prompt = delivery_prompt(request)
    if Path(receipt["prompt_file"]).read_bytes() != expected_prompt.encode("utf-8"):
        raise BridgeError("Verified packaging prompt/request bytes drift")
    if request["context_policy"] != "none":
        expected = material_references(request, root)
        materials = receipt.get("material_map")
        if not isinstance(materials, list) or len(materials) != len(expected):
            raise BridgeError("Verified packaging material map drift")
        with zipfile.ZipFile(receipt["bundle"]) as archive:
            if archive.testzip() or len(archive.namelist()) != len(set(archive.namelist())):
                raise BridgeError("Verified packaging ZIP integrity drift")
            for actual, declared in zip(materials, expected):
                if any(actual.get(k) != v for k, v in declared.items()):
                    raise BridgeError("Verified packaging material identity drift")
                packed = archive.read(declared["archive_path"])
                if packed != (root / declared["source_path"]).read_bytes() or hashlib.sha256(packed).hexdigest() != actual.get("sha256"):
                    raise BridgeError("Verified packaging source/packed bytes drift")
        material_file = json.loads(Path(receipt["materials_file"]).read_text(encoding="utf-8"))
        if material_file != {"bundle_sha256": receipt["bundle_sha256"], "materials": materials}:
            raise BridgeError("Verified packaging material sidecar drift")
    return receipt


def publish_packaging(result, request_path, request, receipt_path):
    result.update(schema_version="verified-packaging/v1", packaging_verified=True,
                  **packaging_inputs(request_path, request))
    if result.get("materials_file"):
        result["materials_sha256"] = file_sha256(Path(result["materials_file"]))
    if receipt_path:
        path = receipt_path.resolve()
        if not path.is_relative_to(Path(request["repo"])) or receipt_path.is_symlink():
            raise BridgeError("Packaging receipt must stay in the repository")
        frozen = {**result, "ready": False, "phase": "packaged", "sent": False}
        if path.exists() and json.loads(path.read_text(encoding="utf-8")) != frozen:
            raise BridgeError("Immutable packaging receipt already exists with different contents")
        if not path.exists():
            atomic_write_text(path, json.dumps(frozen, ensure_ascii=False, sort_keys=True) + "\n")


def stage_packaging(result, request, receipt_path):
    from browser_host import stage_browser_file
    stage_path = receipt_path.with_suffix(".staging.json") if receipt_path else None
    result["phase"] = "stage"
    if stage_path and stage_path.exists():
        if stage_path.is_symlink() or not stage_path.is_file():
            raise BridgeError("Staging receipt must be a regular non-symlink file")
        staged = json.loads(stage_path.read_text(encoding="utf-8"))
        verify_staging(staged, result)
    else:
        staged = stage_browser_file(result["bundle"], thread_id=request["bridge_thread_id"])
        staged["staged_sha256"] = staged["source_sha256"]
        verify_staging(staged, result)
        if stage_path:
            atomic_write_text(stage_path, json.dumps(staged, sort_keys=True) + "\n")
    if staged.get("ready") is not True or staged.get("source_sha256") != result["bundle_sha256"]:
        raise BridgeError("Staging receipt differs from verified packaging")
    result["staging"] = staged


def verify_staging(staged, preparation):
    from browser_host import verify_staged_file
    actual = verify_staged_file(staged["staged_execution_path"], source_path=preparation["bundle"],
                               expected_sha256=preparation["bundle_sha256"],
                               expected_browser_path=staged["staged_browser_path"])
    for key in ("topology", "source_path", "source_sha256", "staged_execution_path", "staged_browser_path",
                "attachment_name", "size_bytes", "staged_wsl_path", "staged_windows_path"):
        if staged.get(key) != actual[key]:
            raise BridgeError(f"Frozen staging receipt drift: {key}")
    if "staged_sha256" in staged and staged["staged_sha256"] != actual["staged_sha256"]:
        raise BridgeError("Frozen staging receipt drift: staged_sha256")
    if staged.get("ready") is not True:
        raise BridgeError("Frozen staging receipt is not ready")
    return actual


def verify_preparation(preparation, request_path, request, *, packaging_receipt_path=None):
    expected_text = delivery_prompt(request)
    expected_prompt = expected_text.encode("utf-8")
    prompt_path = Path(preparation["prompt_file"])
    anchor = packaging_receipt_path or (Path(preparation["packaging_receipt"]) if preparation.get("packaging_receipt") else None)
    markers = {"schema_version","packaging_verified","request_sha256","input_sha256","code_sha256","materials_sha256","packaging_receipt"}
    legacy = not (set(preparation) & markers) and not (anchor is not None and anchor.exists())
    if not prompt_path.is_file():
        raise BridgeError("Frozen preparation prompt must be a regular file")
    actual_prompt = prompt_path.read_bytes()
    codec = "utf8-exact"
    if actual_prompt != expected_prompt:
        writer_bytes = expected_text.replace("\n","\r\n").encode("utf-8")
        normalized = actual_prompt.decode("utf-8").replace("\r\n","\n").replace("\r","\n")
        if (not legacy or actual_prompt != writer_bytes or normalized != expected_text
                or preparation.get("prompt_sha256") != hashlib.sha256(expected_prompt).hexdigest()):
            raise BridgeError("Frozen preparation prompt/request bytes drift")
        codec = "legacy-windows-text/v1"
    if anchor is not None and anchor.exists():
        if anchor.is_symlink() or not anchor.is_file():
            raise BridgeError("Frozen packaging receipt is unsafe")
        frozen = verify_packaging(json.loads(anchor.read_text(encoding="utf-8")),request_path,request)
        for key in ("schema_version","packaging_verified","request_sha256","input_sha256","code_sha256",
                    "context_policy","max_files","attachment_policy","bundle","bundle_sha256","prompt_file",
                    "prompt_sha256","materials_file","materials_sha256","material_map"):
            if key in frozen and preparation.get(key) != frozen[key]:
                raise BridgeError(f"Frozen packaging receipt/envelope drift: {key}")
    if preparation.get("schema_version") == "verified-packaging/v1":
        verify_packaging(preparation, request_path, request)
    else:
        if not legacy:
            raise BridgeError("New preparation verified packaging markers are missing or invalid")
        # Qualified older preparations retain their original exact hashes and
        # material map. Missing new fields are not a reason to replay packaging.
        for name, digest in (("prompt_file", "prompt_sha256"), ("bundle", "bundle_sha256")):
            if name == "bundle" and request["context_policy"] == "none":
                continue
            path = Path(preparation[name])
            actual_sha = hashlib.sha256(expected_prompt).hexdigest() if name == "prompt_file" and codec == "legacy-windows-text/v1" else file_sha256(path)
            if path.is_symlink() or not path.resolve().is_relative_to(Path(request["repo"])) or actual_sha != preparation[digest]:
                raise BridgeError(f"Frozen preparation artifact drift: {name}")
        if request["context_policy"] != "none":
            sidecar = Path(preparation["materials_file"])
            if sidecar.is_symlink() or not sidecar.is_file():
                raise BridgeError("Frozen preparation material sidecar is unsafe")
            material = json.loads(sidecar.read_text(encoding="utf-8"))
            if material != {"bundle_sha256":preparation["bundle_sha256"],"materials":preparation["material_map"]}:
                raise BridgeError("Frozen preparation material sidecar drift")
            with zipfile.ZipFile(preparation["bundle"]) as archive:
                for item in preparation["material_map"]:
                    if hashlib.sha256(archive.read(item["archive_path"])).hexdigest() != item["sha256"]:
                        raise BridgeError("Frozen preparation packed material digest drift")
    if request["context_policy"] != "none":
        expected_materials = material_references(request,Path(request["repo"]))
        actual_materials = preparation.get("material_map")
        if not isinstance(actual_materials,list) or len(actual_materials) != len(expected_materials):
            raise BridgeError("Frozen preparation material map/request drift")
        with zipfile.ZipFile(preparation["bundle"]) as archive:
            if len(archive.namelist()) != len(set(archive.namelist())) or archive.testzip():
                raise BridgeError("Frozen preparation ZIP integrity drift")
            for actual,expected in zip(actual_materials,expected_materials):
                if any(actual.get(key) != value for key,value in expected.items()):
                    raise BridgeError("Frozen preparation material identity/request drift")
                source = Path(request["repo"])/expected["source_path"]
                if not source.is_file() or archive.read(expected["archive_path"]) != source.read_bytes():
                    raise BridgeError("Frozen preparation source/packed bytes drift")
        if not preparation.get("staging"):
            raise BridgeError("Frozen bundled preparation lacks staging receipt")
        if anchor is not None and anchor.with_suffix(".staging.json").exists():
            stage_anchor = anchor.with_suffix(".staging.json")
            if stage_anchor.is_symlink() or not stage_anchor.is_file() or json.loads(stage_anchor.read_text()) != preparation["staging"]:
                raise BridgeError("Frozen staging receipt/envelope drift")
        verify_staging(preparation["staging"], preparation)
    elif preparation.get("staging"):
        raise BridgeError("Attachment-free preparation has unexpected staging")
    return {"prompt":expected_text,"codec":codec}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True, help="明确目标、问题、notes和文件清单的JSON")
    parser.add_argument("--stage", action="store_true", help="同时调用既有Windows暂存器；不上传")
    parser.add_argument("--out", type=Path, help="可选非覆盖bundle路径，须在repo内")
    parser.add_argument("--packaging-receipt", type=Path, help="不可变打包收据，供同job分段恢复")
    parser.add_argument("--reuse-packaging", action="store_true", help="仅复核既有打包收据，禁止重打包")
    args = parser.parse_args()
    result = {"ready": False, "phase": "request", "sent": False}
    try:
        request = read_request(args.request)
        admit_request(request, stage=args.stage)
        root = Path(request["repo"])
        if args.packaging_receipt and (not args.packaging_receipt.resolve().is_relative_to(root) or args.packaging_receipt.is_symlink()):
            raise BridgeError("Packaging receipt must stay in the repository")
        context_policy = request["context_policy"]
        if args.stage and context_policy == "none":
            raise ValueError("context_policy none has no browser attachment to stage")
        if args.out is not None and context_policy == "none":
            raise ValueError("context_policy none cannot create a bundle with --out")
        if args.reuse_packaging:
            if not args.packaging_receipt:
                raise BridgeError("--reuse-packaging requires --packaging-receipt")
            if not args.packaging_receipt.is_file() or args.packaging_receipt.is_symlink():
                raise BridgeError("Packaging receipt must be a regular non-symlink file")
            result = dict(verify_packaging(json.loads(args.packaging_receipt.read_text(encoding="utf-8")),
                                          args.request, request))
            if args.stage:
                stage_packaging(result, request, args.packaging_receipt)
            result.update(ready=True, phase="prepared", return_code=0)
            print(json.dumps(result, ensure_ascii=False))
            return 0
        if context_policy == "none":
            prompt_root = root / ".codex" / "codex-pro-bridge" / "prompts"
            prompt_root.mkdir(parents=True, exist_ok=True)
            prompt_path = prompt_root / f"prepare-{uuid.uuid4().hex}.prompt.md"
            final_prompt = delivery_prompt(request)
            with prompt_path.open("x", encoding="utf-8",newline="") as handle:
                handle.write(final_prompt)
            result.update(
                ready=True,
                phase="prepared",
                return_code=0,
                context_policy=context_policy,
                max_files=0,
                attachment_policy="none",
                prompt_file=str(prompt_path),
                prompt_sha256=hashlib.sha256(final_prompt.encode()).hexdigest(),
                source_files=0,
            )
            publish_packaging(result, args.request, request, args.packaging_receipt)
            print(json.dumps(result, ensure_ascii=False))
            return 0
        bundle = (args.out if args.out is not None else
                  Path(".codex/codex-pro-bridge/bundles") / f"prepare-{uuid.uuid4().hex}.zip")
        bundle = (root / bundle).resolve()
        if not bundle.is_relative_to(root) or bundle.suffix != ".zip" or bundle.exists():
            raise ValueError("out must be a new .zip path inside repo")
        prompt_path = bundle.with_suffix(".prompt.md")
        material_path = bundle.with_suffix(".materials.json")
        if prompt_path.exists() or material_path.exists():
            raise ValueError("prompt/material output already exists; choose a new bundle path")
        materials = material_references(request, root)
        question = (
            request["question"]
            if context_policy == "none"
            else question_with_material_paths(request["question"], materials, root)
        )
        final_prompt = delivery_prompt(request)
        skills = Path(__file__).resolve().parents[2]
        command = [sys.executable, str(skills / "bundle-algorithm-context/scripts/build_algorithm_bundle.py"),
                   "--repo", str(root), "--bridge-thread-id", request["bridge_thread_id"],
                   "--goal=" + request["goal"], "--question=" + question,
                   "--codex-session-notes", request["notes"],
                   "--mode", request.get("mode", "implementation_check"),
                   "--repo-context", context_policy, "--git-context", "none", "--format", "zip",
                   "--max-files", str(request["max_files"]), "--out", str(bundle)]
        if context_policy == "explicit":
            # Every explicit input is already frozen by path and digest. Raise the
            # builder's per-file admission threshold only to this round's actual
            # largest frozen input; auto selection retains the conservative default.
            frozen_inputs = [Path(request["notes"]), *(Path(value) for value in request["files"])]
            command += ["--skip-files-over-bytes",
                        str(max(1, max(path.stat().st_size for path in frozen_inputs)))]
        if "bridge_project_id" in request:
            command += ["--bridge-project-id", request["bridge_project_id"]]
        if request["files"]:
            command += ["--include", *request["files"]]
        result["phase"] = "build"
        completed = subprocess.run(command, cwd=root, capture_output=True, text=True, check=False)
        if completed.returncode:
            result.update(return_code=completed.returncode, error=completed.stderr, output=completed.stdout)
            print(json.dumps(result, ensure_ascii=False))
            return completed.returncode
        result.update(
            bundle=str(bundle),
            phase="verify",
            context_policy=context_policy,
            max_files=request["max_files"],
            attachment_policy="none" if context_policy == "none" else "bundle",
        )
        with zipfile.ZipFile(bundle) as archive:
            bad = archive.testzip()
            if bad:
                raise ValueError(f"ZIP integrity failed: {bad}")
            if len(archive.namelist()) != len(set(archive.namelist())):
                raise ValueError("ZIP contains duplicate member paths")
            for item in materials:
                packed = archive.read(item["archive_path"])
                if packed != (root / item["source_path"]).read_bytes():
                    raise ValueError(f"packed material differs from source: {item['source_path']}")
                item["sha256"] = hashlib.sha256(packed).hexdigest()
            result["source_files"] = sum(name.startswith("source/") for name in archive.namelist())
        result["bundle_sha256"] = hashlib.sha256(bundle.read_bytes()).hexdigest()
        result["phase"] = "material-guide"
        with prompt_path.open("x", encoding="utf-8",newline="") as handle:
            handle.write(final_prompt)
        with material_path.open("x", encoding="utf-8",newline="") as handle:
            json.dump({"bundle_sha256": result["bundle_sha256"], "materials": materials},
                      handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        result.update(prompt_file=str(prompt_path), prompt_sha256=hashlib.sha256(final_prompt.encode()).hexdigest(),
                      materials_file=str(material_path), material_map=materials)
        publish_packaging(result, args.request, request, args.packaging_receipt)
        if args.stage:
            stage_packaging(result, request, args.packaging_receipt)
        result.update(ready=True, phase="prepared", return_code=0)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (TypeError, ValueError, OSError, KeyError, zipfile.BadZipFile) as exc:
        result.update(error=str(exc), return_code=2)
        print(json.dumps(result, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
