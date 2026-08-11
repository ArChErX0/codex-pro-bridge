import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    ROOT
    / ".agents"
    / "skills"
    / "coordinate-auto-research"
    / "scripts"
    / "validate_auto_research_mapping.py"
)
SPEC = importlib.util.spec_from_file_location("validate_auto_research_mapping", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


def valid_mapping():
    return {
        "schema_version": 1,
        "sop_revision": "sha256:abc",
        "bindings": [
            {
                "binding_id": "student-1",
                "role_type": "student",
                "local_thread_id": "local-1",
                "research_scope_id": "RL-1",
                "student_id": "S-1",
                "track_id": "M-AI",
                "writer_scopes": ["research-line:RL-1"],
                "status": "active",
            }
        ],
        "pro_lineages": [
            {
                "lineage_id": "PROL-1",
                "owner_binding_id": "student-1",
                "research_scope_id": "RL-1",
                "status": "active",
                "conversations": [
                    {
                        "conversation_id": "web-1",
                        "kind": "primary",
                        "status": "active",
                        "bridge_thread_id": "bridge-1",
                        "active_submission_id": "submission-1",
                    },
                    {
                        "conversation_id": "web-probe-1",
                        "kind": "probe",
                        "status": "completed",
                        "bridge_thread_id": "bridge-probe-1",
                        "active_submission_id": None,
                    },
                ],
            }
        ],
    }


class AutoResearchMappingTest(unittest.TestCase):
    def test_valid_mapping(self):
        result = MODULE.validate_mapping(valid_mapping())
        self.assertTrue(result["valid"], result)
        self.assertEqual(result["counts"]["web_conversations"], 2)

    def test_duplicate_writer_scope_fails(self):
        mapping = valid_mapping()
        mapping["bindings"].append(
            {
                "binding_id": "mentor-1",
                "role_type": "mentor",
                "local_thread_id": "local-mentor",
                "research_scope_id": "M-AI",
                "writer_scopes": ["research-line:RL-1"],
                "status": "active",
            }
        )
        result = MODULE.validate_mapping(mapping)
        self.assertFalse(result["valid"])
        self.assertTrue(any("owned by both" in item for item in result["errors"]))

    def test_two_active_primary_conversations_fail(self):
        mapping = valid_mapping()
        mapping["pro_lineages"][0]["conversations"].append(
            {
                "conversation_id": "web-2",
                "kind": "primary",
                "status": "active",
                "bridge_thread_id": "bridge-2",
                "active_submission_id": None,
            }
        )
        result = MODULE.validate_mapping(mapping)
        self.assertFalse(result["valid"])
        self.assertTrue(any("live primary-path" in item for item in result["errors"]))

    def test_waiting_primary_and_active_continuation_fail(self):
        mapping = valid_mapping()
        mapping["pro_lineages"][0]["conversations"][0]["status"] = "waiting"
        mapping["pro_lineages"][0]["conversations"].append(
            {
                "conversation_id": "web-continuation-1",
                "kind": "continuation",
                "status": "active",
                "bridge_thread_id": "bridge-continuation-1",
                "active_submission_id": None,
            }
        )
        result = MODULE.validate_mapping(mapping)
        self.assertFalse(result["valid"])
        self.assertTrue(any("live primary-path" in item for item in result["errors"]))

    def test_repeated_web_conversation_fails(self):
        mapping = valid_mapping()
        duplicate = dict(mapping["pro_lineages"][0]["conversations"][1])
        duplicate["conversation_id"] = "web-1"
        duplicate["bridge_thread_id"] = "bridge-duplicate"
        mapping["pro_lineages"][0]["conversations"].append(duplicate)
        result = MODULE.validate_mapping(mapping)
        self.assertFalse(result["valid"])
        self.assertTrue(any("Web conversation" in item for item in result["errors"]))

    def test_duplicate_active_submission_fails(self):
        mapping = valid_mapping()
        mapping["pro_lineages"][0]["conversations"][1]["active_submission_id"] = (
            "submission-1"
        )
        result = MODULE.validate_mapping(mapping)
        self.assertFalse(result["valid"])
        self.assertTrue(any("active submission" in item for item in result["errors"]))

    def test_active_submission_on_terminal_conversation_fails(self):
        mapping = valid_mapping()
        mapping["pro_lineages"][0]["conversations"][0]["status"] = "completed"
        result = MODULE.validate_mapping(mapping)
        self.assertFalse(result["valid"])
        self.assertTrue(any("status is 'completed'" in item for item in result["errors"]))

    def test_lineage_scope_must_match_owner_scope(self):
        mapping = valid_mapping()
        mapping["pro_lineages"][0]["research_scope_id"] = "RL-OTHER"
        result = MODULE.validate_mapping(mapping)
        self.assertFalse(result["valid"])
        self.assertTrue(any("does not match owner binding" in item for item in result["errors"]))

    def test_two_active_lineages_for_same_owner_and_scope_fail(self):
        mapping = valid_mapping()
        second = dict(mapping["pro_lineages"][0])
        second["lineage_id"] = "PROL-2"
        second["conversations"] = []
        mapping["pro_lineages"].append(second)
        result = MODULE.validate_mapping(mapping)
        self.assertFalse(result["valid"])
        self.assertTrue(any("multiple active Pro lineages" in item for item in result["errors"]))

    def test_track_wip_over_three_warns(self):
        mapping = valid_mapping()
        mapping["pro_lineages"] = []
        for number in range(2, 5):
            mapping["bindings"].append(
                {
                    "binding_id": f"student-{number}",
                    "role_type": "student",
                    "local_thread_id": f"local-{number}",
                    "research_scope_id": f"RL-{number}",
                    "student_id": f"S-{number}",
                    "track_id": "M-AI",
                    "writer_scopes": [f"research-line:RL-{number}"],
                    "status": "active",
                }
            )
        result = MODULE.validate_mapping(mapping)
        self.assertTrue(result["valid"], result)
        self.assertTrue(any("normal WIP is 1-3" in item for item in result["warnings"]))


if __name__ == "__main__":
    unittest.main()
