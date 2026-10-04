"""One frozen source/archive/prompt grammar for packaging and ledger proof."""
import hashlib
import json
import os
from pathlib import Path
import re

CODEX_NOTES_ARCHIVE_PATH = "context/codex-session-notes.md"
README_ARCHIVE_PATH = "README_FOR_GPT_PRO.md"


def archive_name(path, root):
    path = Path(os.path.normpath(str(path)))
    try:
        return "source/" + path.relative_to(root).as_posix()
    except ValueError:
        fingerprint = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:10]
        return f"source/external/{fingerprint}-{path.name}"


def material_references(request, root):
    def absolute(value):
        path = Path(value)
        return Path(os.path.normpath(str(path if path.is_absolute() else root/path)))
    sources = [(absolute(request["notes"]),CODEX_NOTES_ARCHIVE_PATH,"notes")]
    sources.extend((absolute(value),archive_name(absolute(value),root),"evidence") for value in request["files"])
    return [{"source_path":path.relative_to(root).as_posix(),"archive_path":target,"role":role} for path,target,role in sources]


def question_with_material_paths(question, materials, root):
    aliases,preferred = {},{}
    for item in materials:
        preferred.setdefault(item["source_path"],item["archive_path"])
    for source,target in preferred.items():
        for alias in (source,str(root/source),Path(source).name):
            aliases.setdefault(alias,set()).add(target)
    for item in materials:
        aliases[item["archive_path"]] = {item["archive_path"]}
    aliases[README_ARCHIVE_PATH] = {README_ARCHIVE_PATH}
    pattern = re.compile(r"(?<![A-Za-z0-9_./-])(?:" + "|".join(re.escape(k) for k in sorted(aliases,key=len,reverse=True)) + r")(?![A-Za-z0-9_./-])")
    def replace(match):
        targets = aliases[match.group()]
        if len(targets) != 1:
            raise ValueError(f"ambiguous material reference {match.group()!r}; use its repository-relative path")
        return next(iter(targets))
    return pattern.sub(replace,question)


def material_guide(materials, context_policy):
    if context_policy == "none":
        return "## 证据范围\n本轮不附加仓库文件；仅使用问题和对话上下文。不要假定存在或要求读取 ZIP 附件。"
    lines = ["## 附件阅读路径",f"先阅读 ZIP 内 `{README_ARCHIVE_PATH}`。材料路径如下："]
    for item in materials:
        lines.append(f"- {json.dumps(item['source_path'],ensure_ascii=False)} → {json.dumps(item['archive_path'],ensure_ascii=False)}")
    lines.append("以上均为附件内路径；不需要访问本地仓库，也不要将原文件名当作另一个缺失附件。")
    return "\n".join(lines)


def delivery_prompt(request):
    root = Path(request["repo"])
    policy = request.get("context_policy","explicit")
    materials = material_references(request,root) if policy != "none" else []
    question = question_with_material_paths(request["question"],materials,root) if policy != "none" else request["question"]
    return material_guide(materials,policy) + "\n\n## 问题\n" + question + "\n"
