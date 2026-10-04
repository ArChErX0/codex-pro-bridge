"""Explicit local fixture proof; never bypass the production append gate."""
import hashlib
import json

from bridge_store import file_sha256


def input_proof(repo, thread, codex, source_path, project="", question="fixture question"):
    values = {"thread":thread,"session":codex,"project":project,"goal":"","question":question,
              "summary":source_path.read_text().strip(),"raw_history":"","history_source":"","title":""}
    text = json.dumps(values,ensure_ascii=False,sort_keys=True)
    path = source_path.with_suffix(".fixture-inputs.json")
    path.write_text(text)
    prompt = source_path.with_suffix(".fixture-prompt.md")
    prompt.write_text(question)
    return {"inputs_sha256":hashlib.sha256(text.encode()).hexdigest(), "input_proof":{
        "inputs":{"path":str(path.relative_to(repo)),"sha256":file_sha256(path)},
        "source_notes":{"path":str(source_path.relative_to(repo)),"sha256":file_sha256(source_path)},"request":None,
        "delivery_prompt":{"path":str(prompt.relative_to(repo)),"sha256":file_sha256(prompt)}}}


def exchange_proof(repo, snapshot):
    prompt = snapshot["data"]["input_proof"]["delivery_prompt"]
    return {"raw_prompt":prompt,"prompt_sha256":prompt["sha256"],"notes_reference":{
        "path":snapshot["artifact"]["path"],"sha256":snapshot["artifact"]["sha256"]}}
