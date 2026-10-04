"""Direct builder CLI regression for moved archive grammar and narrow notes policy."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

SKILLS = Path(__file__).resolve().parents[2]
BUILDER = SKILLS/"bundle-algorithm-context/scripts/build_algorithm_bundle.py"


class BundleCliTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        self.notes = self.repo/"notes.md"
        self.notes.write_bytes("原 notes\r\n".encode())
        self.evidence = self.repo/"nested/evidence.py"
        self.evidence.parent.mkdir()
        self.evidence.write_bytes("# 中文\r\nx = 1\r\n".encode())
        prepared = subprocess.run([sys.executable,str(SKILLS/"bundle-algorithm-context/scripts/prepare_codex_session_notes.py"),
            "--repo",str(self.repo),"--bridge-thread-id","builder-test","--summary-file",str(self.notes),
            "--gpt-pro-question","Read evidence.py"],capture_output=True,text=True,timeout=20)
        self.assertEqual(prepared.returncode,0,prepared.stderr)

    def call(self, notes=None, *extra):
        return subprocess.run([sys.executable,str(BUILDER),"--repo",str(self.repo),"--bridge-thread-id","builder-test",
            "--goal","fixture","--question","Read evidence.py","--repo-context","explicit","--git-context","none",
            "--format","zip","--max-files","1","--include",str(self.evidence),"--out",str(self.repo/"bundle.zip"),
            *(["--codex-session-notes",str(notes)] if notes else []),*extra],capture_output=True,text=True,timeout=20)

    def test_source_archive_names_and_bytes_survive_moved_rule(self):
        result = self.call(self.notes)
        self.assertEqual(result.returncode,0,result.stderr)
        with zipfile.ZipFile(self.repo/"bundle.zip") as archive:
            self.assertEqual(archive.read("source/nested/evidence.py"),self.evidence.read_bytes())
            self.assertEqual(archive.read("context/codex-session-notes.md"),self.notes.read_bytes())

    def test_canonical_snapshot_allowed_but_codex_and_secret_notes_rejected(self):
        result = self.call()
        self.assertEqual(result.returncode,0,result.stderr)
        for index,relative in enumerate((".codex/private.md",".codex/codex-pro-bridge/codex-sessions/x/credentials.md")):
            path = self.repo/relative
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text("benign fixture")
            result = self.call(path,"--out",str(self.repo/f"bad-{index}.zip"))
            self.assertNotEqual(result.returncode,0,result.stdout)
            self.assertIn("safety policy",result.stderr)
            self.assertFalse((self.repo/f"bad-{index}.zip").exists())


if __name__ == "__main__":
    unittest.main()
