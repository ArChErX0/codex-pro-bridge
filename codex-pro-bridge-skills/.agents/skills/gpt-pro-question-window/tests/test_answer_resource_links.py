import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path[:0] = [str(SCRIPTS), str(SCRIPTS.parents[1] / '.shared')]
from bridge_runtime import pipeline


class ResourceLinkTests(unittest.TestCase):
    def test_rendered_file_rows_count_once_and_preserve_markdown(self):
        fixtures = [
            # New preview + download row is one link, not two buttons.
            ({'anchors': 0, 'legacy': 0, 'rows': [{'title': True, 'download': True}]}, 1),
            ({'anchors': 1, 'legacy': 0, 'rows': [{'title': True, 'download': True, 'anchor': True}]}, 1),
            ({'anchors': 0, 'legacy': 1, 'rows': [{'title': True, 'download': True, 'legacy': True}]}, 1),
            ({'anchors': 0, 'legacy': 0, 'rows': [{'title': True}, {'download': True}]}, 0),
            ({'anchors': 1, 'legacy': 1, 'rows': [{'title': True, 'download': True}, {'title': True, 'download': True}]}, 4),
        ]
        for fixture, count in fixtures:
            with self.subTest(fixture=fixture), tempfile.TemporaryDirectory() as temp:
                event = {'status': 'ready-for-capture', 'assistant_turn_id': 'answer', 'observed_at': 'now'}
                browser = Mock(url='https://chatgpt.com/c/exact')
                browser.assert_identity = Mock()
                browser.copied_link_source.return_value = {'status': 'unsupported'}
                text = '\n'.join(f'[file{i}](sandbox:/mnt/data/file{i}.txt)' for i in range(count)) or 'No link'
                browser.copy_with_proof.return_value = {'text': text, 'provenance': 'visible-copy-write/v1'}

                def evaluate(function):
                    if function == 'wait':
                        return event
                    source = 'const fixture=' + json.dumps(fixture) + r''';
                    const row = f => ({closest:s=>f.anchor?{}:null, querySelector:s=>
                      s==='[title]' ? (f.title?{}:null) : s.includes('library-file-icon') ?
                      (f.legacy?{}:null) : (f.download?{}:null)});
                    const BODY={querySelectorAll:s=> s==='a'?Array(fixture.anchors).fill({}):
                      s==='button'?Array.from({length:fixture.legacy},()=>row({legacy:true})):
                      s==='[class~="group/resource-row"]'?fixture.rows.map(row):[]};
                    const BUTTON={disabled:false,getClientRects:()=>[{}]};
                    console.log(JSON.stringify((''' + function + ')()));'
                    result = subprocess.run(['node', '-e', source], capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    metrics = json.loads(result.stdout)
                    self.assertEqual(metrics['links'], count)
                    return metrics

                browser.evaluate.side_effect = evaluate
                dom = ('function bridgePinnedMessage(){return {};}'
                       'function bridgeMessageBody(){return BODY;}'
                       'function bridgeCopyButton(){return BUTTON;}')
                with patch.object(pipeline, 'message_dom_source', return_value=dom), patch.object(
                        pipeline, 'browser_wait_script', return_value={'function': 'wait'}):
                    output = Path(temp) / 'answer.md'
                    pipeline.capture(browser, {}, output, {'state_dir': temp})
                    self.assertEqual(output.read_text(encoding='utf-8'), text)


if __name__ == '__main__':
    unittest.main()
