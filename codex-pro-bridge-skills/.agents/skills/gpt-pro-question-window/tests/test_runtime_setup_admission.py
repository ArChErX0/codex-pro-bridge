"""Installer-created profiles cannot submit before a successful live observation."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SKILL = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(SKILL / "scripts"), str(SKILL.parent / ".shared")]
from bridge_runtime.jobs import Jobs
from bridge_store import BridgeError


class SetupAdmissionTest(unittest.TestCase):
    def test_unverified_install_blocks_before_preparation(self):
        jobs = Jobs.__new__(Jobs)
        jobs.config = {"setup_ui_verified": False}
        with patch("bridge_runtime.jobs.validate_inputs") as validate, self.assertRaisesRegex(BridgeError, "doctor"):
            jobs.submit("unused", "unused")
        validate.assert_not_called()

    def test_legacy_manual_configuration_keeps_existing_admission(self):
        jobs = Jobs.__new__(Jobs)
        jobs.config = {}
        with patch("bridge_runtime.jobs.validate_inputs", side_effect=BridgeError("normal-input-check")) as validate, \
             self.assertRaisesRegex(BridgeError, "normal-input-check"):
            jobs.submit("existing", "digest")
        validate.assert_called_once_with("existing", "digest", {})
