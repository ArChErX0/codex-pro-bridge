"""Actual local child/lock startup receipts; no browser or external model."""
import concurrent.futures
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_bridge_runtime as fixtures
from bridge_runtime import jobs as runtime


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.RuntimeTests("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.jobs = self.fixture.jobs
        self.job = self.fixture.submit()
        self.job.update(state="blocked",stage="preparing",error="previous blocker")

    def test_lock_held_reports_already_running_and_never_spawns(self):
        with runtime.worker_lock(self.job.directory) as acquired, patch.object(self.jobs,"_spawn") as spawn:
            self.assertTrue(acquired)
            result = self.jobs.resume(self.job.data["job_id"])
        self.assertEqual(result["continuation"]["status"],"already-running")
        self.assertEqual(result["next_action"],"wait")
        spawn.assert_not_called()

    def test_real_worker_handshake_and_competing_resumes_have_one_child(self):
        children = []
        code = ("import sys,time;from pathlib import Path;"
            "sys.path[:0]=sys.argv[1:3];from bridge_runtime import pipeline;from bridge_runtime.jobs import run_worker;"
            "def_exec='def execute(job,config):\\n job.update(state=\"waiting\",stage=\"waiting\")\\n time.sleep(1)';"
            "namespace={\"time\":time};exec(def_exec,namespace);pipeline.execute=namespace['execute'];run_worker(Path(sys.argv[3]))")
        scripts = Path(runtime.__file__).resolve().parents[1]
        def spawn(directory):
            env = {**os.environ,"CODEX_BRIDGE_CONTINUATION_REQUEST_ID":runtime.read_json(directory/"job.json")["continuation"]["request_id"]}
            child = subprocess.Popen([sys.executable,"-c",code,str(scripts),str(scripts.parents[1]/".shared"),str(directory)],env=env,
                                     stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            children.append(child)
            return child.pid
        with patch.object(self.jobs,"_spawn",side_effect=spawn), concurrent.futures.ThreadPoolExecutor(4) as pool:
            results = list(pool.map(lambda _:self.jobs.resume(self.job.data["job_id"]),range(4)))
        self.assertEqual(len(children),1,results)
        self.assertIn("started",[r["continuation"]["status"] for r in results])
        self.assertTrue(all(r["continuation"]["status"] in {"started","already-running"} for r in results))
        for child in children:
            stdout,stderr = child.communicate(timeout=10)
            self.assertEqual(child.returncode,0,stdout+stderr)

    def test_dead_child_error_is_distinct_from_accepted(self):
        child = subprocess.Popen([sys.executable,"-c","raise SystemExit(17)"])
        child.wait(timeout=10)
        with patch.object(self.jobs,"_spawn",return_value=child.pid):
            result = self.jobs.resume(self.job.data["job_id"])
        self.assertEqual(result["continuation"]["status"],"worker-exited-before-start")
        self.assertNotEqual(result["state"],"complete")

    def test_exit_observation_cannot_overwrite_racing_started_ack(self):
        def observe_exit(pid):
            current = runtime.read_json(self.job.path)
            runtime.Job(self.job.directory).update(continuation={**current["continuation"],"status":"started"})
            return False
        with patch.object(self.jobs,"_spawn",return_value=999999999), patch.object(runtime,"process_start",return_value="start"), \
                patch.object(runtime,"process_alive",side_effect=observe_exit):
            result = self.jobs.resume(self.job.data["job_id"])
        self.assertEqual(result["continuation"]["status"],"started")

    def test_delayed_child_cannot_claim_newer_request_token(self):
        self.job.update(continuation={"request_id":"new-token","status":"accepted"})
        with patch.dict(os.environ,{"CODEX_BRIDGE_CONTINUATION_REQUEST_ID":"old-token"}), \
                patch("bridge_runtime.pipeline.execute") as execute:
            runtime.run_worker(self.job.directory)
        execute.assert_not_called()
        self.assertEqual(runtime.read_json(self.job.path)["continuation"]["status"],"accepted")

    def test_job_update_merges_resume_fields_and_pid_reuse_is_not_pending(self):
        stale = runtime.Job(self.job.directory)
        self.job.update(continuation={"status":"accepted","request_id":"old","pid":os.getpid(),"process_start":"not-this-process"})
        stale.update(stage="preparing")
        self.assertEqual(runtime.read_json(self.job.path)["continuation"]["request_id"],"old")
        with patch.object(self.jobs,"_spawn",side_effect=OSError("injected spawn failure")) as spawn:
            result = self.jobs.resume(self.job.data["job_id"])
        spawn.assert_called_once()
        self.assertEqual(result["continuation"]["status"],"spawn-failed")

    def test_live_accepted_child_status_wait_and_new_failure_are_consistent(self):
        scripts = Path(runtime.__file__).resolve().parents[1]
        code = ("import sys,time;from pathlib import Path;sys.path[:0]=sys.argv[1:3];"
                "from bridge_runtime import pipeline;from bridge_runtime.jobs import run_worker;"
                "time.sleep(2.8);"
                "def_exec='def execute(job,config):\\n raise RuntimeError(\"fresh worker failure\")';"
                "namespace={};exec(def_exec,namespace);pipeline.execute=namespace['execute'];run_worker(Path(sys.argv[3]))")
        children = []
        def spawn(directory):
            env = {**os.environ,"CODEX_BRIDGE_CONTINUATION_REQUEST_ID":runtime.read_json(directory/"job.json")["continuation"]["request_id"]}
            child = subprocess.Popen([sys.executable,"-c",code,str(scripts),str(scripts.parents[1]/".shared"),str(directory)],
                env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            children.append(child)
            return child.pid
        with patch.object(self.jobs,"_spawn",side_effect=spawn):
            resumed = self.jobs.resume(self.job.data["job_id"])
            self.assertEqual(resumed["continuation"]["status"],"pending")
            for result in (resumed,self.jobs.status(self.job.data["job_id"]),self.jobs.wait(self.job.data["job_id"],0.01)):
                self.assertEqual(result["state"],"running")
                self.assertEqual(result["next_action"],"wait")
                self.assertEqual(result["error"],"")
        for child in children:
            stdout,stderr = child.communicate(timeout=10)
            self.assertEqual(child.returncode,0,stdout+stderr)
        failed = self.jobs.status(self.job.data["job_id"])
        self.assertEqual(failed["state"],"blocked")
        self.assertEqual(failed["next_action"],"inspect-blocker")
        self.assertIn("fresh worker failure",failed["error"])


if __name__ == "__main__":
    unittest.main()
