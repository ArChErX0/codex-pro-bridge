"""Live Chrome MCP closed-selected-page prefix, with strict list agreement."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'.shared'))
from bridge_store import BridgeError
from browser_observations import normalize_pages_observation


def observation(text):
    return {'content':[{'type':'text','text':text}]}


class PageNoteTests(unittest.TestCase):
    note = 'Note: the previously selected page was closed. Page 6 is now selected.'
    listing = '## Pages\n6: chat (https://chatgpt.com/c/test) [selected]\n12: Gemini (https://gemini.google.com/)\n16: about:blank\n'

    def test_live_prefix_preserves_complete_page_list(self):
        parsed = normalize_pages_observation(observation(self.note+'\n'+self.listing))
        self.assertEqual(parsed,normalize_pages_observation(observation(self.listing)))
        self.assertEqual([row['page_id'] for row in parsed],['6','12','16'])

    def test_prefix_must_match_one_actual_selected_page(self):
        for listing in [self.listing.replace('Page 6','Page 8').replace('6:','8:'),
                        self.listing.replace(' [selected]',''),
                        self.listing.replace('16: about:blank','16: about:blank [selected]')]:
            with self.subTest(listing=listing),self.assertRaisesRegex(BridgeError,'conflicts'):
                normalize_pages_observation(observation(self.note+'\n'+listing))

    def test_unknown_extra_or_duplicate_prefix_fails(self):
        for prefix in ['Page navigated to https://chatgpt.com/',
                       self.note+'\n'+self.note,
                       self.note+'\nunknown message']:
            with self.subTest(prefix=prefix),self.assertRaises(BridgeError):
                normalize_pages_observation(observation(prefix+'\n'+self.listing))

    def test_missing_header_and_error_result_still_fail(self):
        for data in [observation(self.note), {**observation(self.note+'\n'+self.listing),'isError':True}]:
            with self.subTest(data=data),self.assertRaises(BridgeError):
                normalize_pages_observation(data)


if __name__=='__main__':
    unittest.main()
