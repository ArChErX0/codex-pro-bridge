"""Bounded actual-position discovery and verified restoration."""
import importlib.util
import unittest
from pathlib import Path
from typing import ClassVar

spec = importlib.util.spec_from_file_location("setup_bridge_intensity", Path(__file__).resolve().parents[1] / "setup_bridge.py")
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


class Controller:
    def __init__(self, labels, original=0, fail_at=None):
        self.labels, self.value, self.fail_at = labels, original, fail_at
        self.failed = False
        self.keys = []

    def owned_evaluate(self, script):
        if 'document.activeElement' in script:
            return True
        return {'value': str(self.value), 'min':'0', 'max':str(len(self.labels)-1)}

    def press(self, key):
        self.keys.append(key)
        self.value += 1 if key == 'ArrowRight' else -1

    def read_labels(self, _):
        if self.value == self.fail_at and not self.failed:
            self.failed = True
            raise setup.SetupError('Observation failed')
        return {'thinking': self.labels[self.value]}


class IntensityTest(unittest.TestCase):
    ui: ClassVar[dict] = {'thinking_slider':'observed-slider', 'thinking_keyboard_control':'observed-keyboard'}

    def test_all_five_visible_positions_are_learned_and_restored(self):
        labels = ['Instant', 'Medium', 'High', 'Extra High', '6 Pro']
        browser = Controller(labels, original=2)
        positions, ambiguous = setup.observe_intensity_positions(browser, self.ui)
        self.assertEqual(positions, dict(zip(labels, range(5))))
        self.assertEqual(ambiguous, [])
        self.assertEqual(browser.value, 2)
        self.assertEqual(len(browser.keys), 8)

    def test_same_label_at_different_positions_is_not_guessed(self):
        browser = Controller(['Instant', 'Thinking', 'Thinking', 'Pro'])
        positions, ambiguous = setup.observe_intensity_positions(browser, self.ui)
        self.assertEqual(positions, {'Instant':0, 'Pro':3})
        self.assertEqual(ambiguous, ['Thinking'])
        self.assertEqual(browser.value, 0)

    def test_observation_failure_restores_original_then_propagates(self):
        browser = Controller(['Instant', 'Medium', 'High', 'Pro'], original=1, fail_at=2)
        with self.assertRaisesRegex(setup.SetupError, 'Observation failed'):
            setup.observe_intensity_positions(browser, self.ui)
        self.assertEqual(browser.value, 1)

    def test_unsupported_range_is_rejected_without_pressing(self):
        browser = Controller([str(i) for i in range(8)])
        with self.assertRaisesRegex(setup.SetupError, 'range'):
            setup.observe_intensity_positions(browser, self.ui)
        self.assertEqual(browser.keys, [])

    def test_missing_or_ambiguous_focus_has_no_keypress(self):
        browser = Controller(['Instant', 'Pro'])
        browser.owned_evaluate = lambda script: False if 'document.activeElement' in script else {'value':'0','min':'0','max':'1'}
        with self.assertRaisesRegex(setup.SetupError, 'focus'):
            setup.observe_intensity_positions(browser, self.ui)
        self.assertEqual(browser.keys, [])


if __name__ == '__main__':
    unittest.main()
