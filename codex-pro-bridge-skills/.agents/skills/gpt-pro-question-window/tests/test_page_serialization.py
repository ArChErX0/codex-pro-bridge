"""Actual JS producer, declared Draft7 schema and shared semantic consumers."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SKILLS = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(SKILLS/".shared"))
from bridge_store import BridgeError, atomic_write_text, now_iso
from capture_provenance import (SCHEMA_PATH, freeze_page_serialization, private_proof,
                               source_digest, validate_page_serialization)


def raw_observation(attempt, assistant_id="assistant", *, node_parent=False):
    user = {"id":attempt["remote_turn_id"],"author":{"role":"user"},
            "content":{"content_type":"text","parts":[attempt["prompt"].removesuffix("\n")]}}
    assistant = {"id":assistant_id,"author":{"role":"assistant"},"channel":"final",
        "status":"finished_successfully","end_turn":True,
        "content":{"content_type":"text","parts":["# 原始 Markdown\n\n| X | Y |\n|---|---|\n| 1 | 2 |\n\n$$x^2$$\n\n```py\nx = 1\n```\n\n[supplement](sandbox:/mnt/data/file.txt)\n"]}}
    node = {"id":assistant_id,"parent":user["id"],"message":assistant} if node_parent else None
    if not node_parent:
        assistant["parent_id"] = user["id"]
    return {"source":"react-message-props/v1","conversation_url":attempt["conversation_url"],
            "owner_token":attempt["tab_owner_token"],"observed_at":now_iso(),"complete":True,"truncated":False,
            "messages":[{"message":user,"node":{"id":user["id"],"message":user,"children":[assistant_id]}},
                        {"message":assistant,"node":node}]}


class PageSerializationTests(unittest.TestCase):
    def setUp(self):
        self.attempt = {"remote_turn_id":"user","prompt":"frozen prompt\n", "tab_owner_token":"owner",
            "conversation_url":"https://chatgpt.com/c/chat", "preflight":{"observed_page_id":"7"}}

    def freeze(self, observation=None):
        return freeze_page_serialization(observation or raw_observation(self.attempt),attempt=self.attempt,
            prompt=self.attempt["prompt"],page_id="7",owner="owner",url=self.attempt["conversation_url"])

    def test_both_real_parent_shapes_match_full_declared_schema(self):
        from jsonschema import Draft7Validator
        schema = json.loads(SCHEMA_PATH.read_text())
        for mapped in (False,True):
            proof = self.freeze(raw_observation(self.attempt,node_parent=mapped))
            Draft7Validator(schema).validate(proof)
            result = validate_page_serialization(proof,attempt=self.attempt,prompt=self.attempt["prompt"])
            self.assertEqual(result["answer"],proof["messages"][1]["message"]["content"]["parts"][0])

    def test_owner_url_page_prompt_parent_final_and_digest_fail_closed(self):
        base = self.freeze()
        changes = [lambda p:p.update(owner_token="wrong"), lambda p:p.update(conversation_url="https://chatgpt.com/c/other"),
            lambda p:p.update(page_id="8"), lambda p:p.update(source="clipboard"),lambda p:p.update(truncated=True),
            lambda p:p.update(complete=False),lambda p:p.update(answer_sha256="0"*64),
            lambda p:p["messages"][0]["message"].update(id="other-user"),
            lambda p:p["messages"][0]["message"]["content"].update(parts=["wrong prompt"]),
            lambda p:p["messages"][1]["message"].update(parent_id="other-user"),
            lambda p:p["messages"][1]["message"].pop("parent_id"),
            lambda p:p["messages"][1]["message"].update(channel="analysis"),
            lambda p:p["messages"][1]["message"].update(status="in_progress"),
            lambda p:p["messages"][1]["message"].update(end_turn=False),
            lambda p:p["messages"][1]["message"]["content"].update(parts=["first","second"]),
            lambda p:p["messages"][0]["node"].update(children=["assistant","second-final"]),
            lambda p:p["messages"].append(copy.deepcopy(p["messages"][1]))]
        for index, change in enumerate(changes):
            proof = copy.deepcopy(base)
            change(proof)
            proof["source_sha256"] = source_digest(proof["messages"])
            with self.subTest(index=index),self.assertRaises(BridgeError):
                validate_page_serialization(proof,attempt=self.attempt,prompt=self.attempt["prompt"])
        with self.assertRaisesRegex(BridgeError,"saved Markdown"):
            validate_page_serialization(base,attempt=self.attempt,prompt=self.attempt["prompt"],answer="DOM innerText")

    def test_actual_js_reads_raw_fiber_message_and_parent_without_innertext(self):
        script = SKILLS/"gpt-pro-question-window/scripts/page_serialization.js"
        harness = r'''
const fs=require('fs'); const source=fs.readFileSync(process.argv[1],'utf8');
const fixture=JSON.parse(fs.readFileSync(0,'utf8'));
global.location={href:fixture.conversation_url};global.sessionStorage={getItem:()=>fixture.owner_token};
const nodes=fixture.messages.map(r=>({getAttribute:()=>r.message.id,
  __reactFiber$fixture:{memoizedProps:{children:[{props:{message:r.message,node:r.node?{...r.node,message:r.message}:null}}]}},
  get innerText(){throw Error('DOM text must never be read');}}));
global.document={querySelectorAll:()=>nodes};
try{const fn=new Function(source+';return bridgePageSerialized;')();
process.stdout.write(JSON.stringify(fn('user','assistant',fixture.conversation_url,fixture.owner_token,'key')));}
catch(e){process.stderr.write(e.message);process.exitCode=1;}
'''
        for mapped in (False,True):
            proc = subprocess.run(["node","-e",harness,str(script)],input=json.dumps(raw_observation(self.attempt,node_parent=mapped)),
                                  text=True,capture_output=True,timeout=10)
            self.assertEqual(proc.returncode,0,proc.stderr)
            proof = self.freeze(json.loads(proc.stdout))
            validate_page_serialization(proof,attempt=self.attempt,prompt=self.attempt["prompt"])
        missing = raw_observation(self.attempt)
        missing["messages"][1]["message"].pop("parent_id")
        with self.assertRaisesRegex(BridgeError,"lacks assistant parent"):
            self.freeze(missing)

    def test_private_proof_and_malformed_types_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"proof.json"
            atomic_write_text(path,json.dumps(self.freeze()))
            private_proof(path)
            path.chmod(0o644)
            with self.assertRaisesRegex(BridgeError,"private"):
                private_proof(path)
        proof = self.freeze()
        proof["messages"][1]["message"]["content"] = []
        with self.assertRaises(BridgeError):
            validate_page_serialization(proof,attempt=self.attempt,prompt=self.attempt["prompt"])

    @unittest.skipIf(os.name == "nt", "POSIX FIFO local boundary")
    def test_fifo_private_proof_and_digest_are_rejected_without_blocking(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"proof.fifo"
            os.mkfifo(path,0o600)
            code = ("import sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);"
                    "from capture_provenance import private_proof;from bridge_store import file_sha256;"
                    "private_proof(Path(sys.argv[2])) if sys.argv[3]=='proof' else file_sha256(Path(sys.argv[2]))")
            for kind in ("proof","digest"):
                result = subprocess.run([sys.executable,"-c",code,str(SKILLS/".shared"),str(path),kind],
                    capture_output=True,text=True,timeout=5)
                self.assertNotEqual(result.returncode,0)
                self.assertIn("regular",result.stderr)


if __name__ == "__main__":
    unittest.main()
