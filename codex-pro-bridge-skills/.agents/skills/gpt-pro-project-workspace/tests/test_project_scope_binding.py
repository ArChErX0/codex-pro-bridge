from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SKILL = Path(__file__).resolve().parents[1]
SHARED = SKILL.parent / ".shared"
MANAGER = SKILL / "scripts" / "manage_bridge_project.py"
sys.path.insert(0, str(SHARED))

from bridge_store import BridgeError  # noqa: E402
from project_store import BridgeProjectStore  # noqa: E402


ROOT_A = "019d2abc-1111-7222-8333-123456789abc"
ROOT_B = "019d2abc-2222-7333-8444-123456789abc"
REMOTE = "g-p-" + "a" * 32


class ProjectScopeBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name)
        self.store = BridgeProjectStore(self.repo)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_one_project_per_root_and_root_lookup(self) -> None:
        first = self.store.create_project(
            "root-a-project", codex_root_thread_id=ROOT_A
        )
        second = self.store.create_project(
            "root-b-project", codex_root_thread_id=ROOT_B
        )

        self.assertEqual(first["codex_root_thread_id"], ROOT_A)
        self.assertEqual(second["codex_root_thread_id"], ROOT_B)
        self.assertEqual(self.store.root_project_id(ROOT_A), "root-a-project")
        self.assertEqual(self.store.root_project_id(ROOT_B), "root-b-project")
        self.assertEqual(
            self.store.root_project_id("019d2abc-3333-7444-8555-123456789abc"),
            "",
        )
        with self.assertRaisesRegex(BridgeError, "already represented"):
            self.store.create_project("other-root-a", codex_root_thread_id=ROOT_A)

    def test_project_scope_is_immutable_even_for_repeated_project_id(self) -> None:
        original = self.store.create_project(
            "scoped-project", codex_root_thread_id=ROOT_A
        )
        self.assertEqual(
            self.store.create_project(
                "scoped-project", codex_root_thread_id=ROOT_A
            ),
            original,
        )
        with self.assertRaisesRegex(BridgeError, "already scoped"):
            self.store.create_project(
                "scoped-project", codex_root_thread_id=ROOT_B
            )
        with self.assertRaisesRegex(BridgeError, "already scoped"):
            self.store.create_project("scoped-project")

    def test_default_resolution_only_selects_legacy_project(self) -> None:
        self.store.create_project("root-a-project", codex_root_thread_id=ROOT_A)
        with self.assertRaisesRegex(BridgeError, "no legacy Bridge Project"):
            self.store.resolve_project_id()

        self.store.create_project("legacy-project")
        self.store.create_project("root-b-project", codex_root_thread_id=ROOT_B)
        self.assertEqual(self.store.resolve_project_id(), "legacy-project")
        with self.assertRaisesRegex(BridgeError, "legacy repository scope"):
            self.store.create_project("other-legacy-project")

    def test_existing_legacy_project_remains_loadable_and_unscoped(self) -> None:
        project = self.store.create_project("legacy-project")
        self.assertNotIn("codex_root_thread_id", project)
        self.assertEqual(self.store.resolve_project_id(), "legacy-project")
        self.assertEqual(self.store.legacy_project_ids(), ["legacy-project"])

    def test_owner_is_persisted_verbatim_and_conflicts_are_rejected(self) -> None:
        self.store.create_project("legacy-project")
        attached = self.store.attach_thread(
            "legacy-project", "thread-one", owner_agent_id="/root/5_6sm"
        )
        self.assertEqual(attached["owner_agent_id"], "/root/5_6sm")
        event = self.store.load_activity("legacy-project")[-1]
        self.assertEqual(event["data"]["owner_agent_id"], "/root/5_6sm")

        self.assertEqual(
            self.store.attach_thread("legacy-project", "thread-one"), attached
        )
        with self.assertRaisesRegex(BridgeError, "already attached to owner"):
            self.store.attach_thread(
                "legacy-project", "thread-one", owner_agent_id="/root/other"
            )

    def test_legacy_owner_cannot_be_retrofitted_and_controls_are_rejected(self) -> None:
        self.store.create_project("legacy-project")
        legacy = self.store.attach_thread("legacy-project", "thread-one")
        self.assertNotIn("owner_agent_id", legacy)
        with self.assertRaisesRegex(BridgeError, "legacy-unscoped"):
            self.store.attach_thread(
                "legacy-project", "thread-one", owner_agent_id="/root/owner"
            )
        with self.assertRaisesRegex(BridgeError, "without control characters"):
            self.store.attach_thread(
                "legacy-project", "thread-two", owner_agent_id="/root/bad\nowner"
            )

    def test_cli_accepts_root_and_owner_arguments(self) -> None:
        create = subprocess.run(
            [
                sys.executable,
                str(MANAGER),
                "--repo",
                str(self.repo),
                "create",
                "--project-id",
                "scoped-project",
                "--codex-root-thread-id",
                ROOT_A,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(json.loads(create.stdout)["codex_root_thread_id"], ROOT_A)
        attach = subprocess.run(
            [
                sys.executable,
                str(MANAGER),
                "--repo",
                str(self.repo),
                "attach-task",
                "--bridge-project-id",
                "scoped-project",
                "--bridge-thread-id",
                "thread-one",
                "--owner-agent-id",
                "/root/5_6sm",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(json.loads(attach.stdout)["owner_agent_id"], "/root/5_6sm")

    def test_scoped_project_may_explicitly_share_legacy_remote(self) -> None:
        self.store.create_project("legacy-project")
        self.store.bind_remote(
            "legacy-project",
            remote_url=f"https://chatgpt.com/g/{REMOTE}-project",
        )
        self.store.attach_thread(
            "legacy-project", "legacy-thread", owner_agent_id="/root"
        )
        self.store.create_project(
            "scoped-project", codex_root_thread_id=ROOT_A
        )

        with self.assertRaisesRegex(BridgeError, "already bound to legacy-project"):
            self.store.bind_remote(
                "scoped-project",
                remote_url=f"https://chatgpt.com/g/{REMOTE}-project",
            )
        binding = self.store.bind_remote(
            "scoped-project",
            remote_url=f"https://chatgpt.com/g/{REMOTE}-project",
            allow_shared_remote=True,
        )
        self.store.attach_thread(
            "scoped-project", "scoped-thread", owner_agent_id="/root/worker"
        )

        self.assertEqual(binding["remote_project_id"], REMOTE)
        self.assertNotIn(
            "codex_root_thread_id", self.store.load_project("legacy-project")
        )
        self.assertEqual(
            self.store.load_project("scoped-project")["codex_root_thread_id"],
            ROOT_A,
        )
        self.assertEqual(
            set(self.store.task_states("legacy-project")), {"legacy-thread"}
        )
        self.assertEqual(
            set(self.store.task_states("scoped-project")), {"scoped-thread"}
        )

    def test_shared_remote_opt_in_requires_scoped_target(self) -> None:
        self.store.create_project("legacy-project")
        with self.assertRaisesRegex(BridgeError, "requires a Codex root-scoped"):
            self.store.bind_remote(
                "legacy-project",
                remote_url=f"https://chatgpt.com/g/{REMOTE}-project",
                allow_shared_remote=True,
            )

    def test_cli_can_explicitly_share_remote_for_scoped_project(self) -> None:
        self.store.create_project("legacy-project")
        self.store.bind_remote(
            "legacy-project",
            remote_url=f"https://chatgpt.com/g/{REMOTE}-project",
        )
        self.store.create_project(
            "scoped-project", codex_root_thread_id=ROOT_A
        )
        result = subprocess.run(
            [
                sys.executable,
                str(MANAGER),
                "--repo",
                str(self.repo),
                "bind",
                "--bridge-project-id",
                "scoped-project",
                "--remote-url",
                f"https://chatgpt.com/g/{REMOTE}-project",
                "--allow-shared-remote",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(json.loads(result.stdout)["remote_project_id"], REMOTE)


if __name__ == "__main__":
    unittest.main()
