import json
from pathlib import Path
import subprocess
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "scripts/copy_with_proof.js"
HARNESS = r'''
const fs = require('fs'), vm = require('vm');
const test = JSON.parse(fs.readFileSync(0,'utf8'));
let now = 0, owner = 'owner';
const body = {textContent:'rendered rich answer'};
const clipboard = {writeText: async text => {
  if(test.failure) throw Error('OS refused');
  if(test.drift) owner='other';
  if(test.change) body.textContent='changed';
}};
const original=clipboard.writeText;
const button={disabled:false,getClientRects:()=>[1],click:()=>{
  if(test.missing) return;
  clipboard.writeText('# Original\n\n**raw Markdown**').catch(()=>{});
  if(test.multiple) clipboard.writeText('other').catch(()=>{});
}};
const context={navigator:{clipboard},location:{href:'https://chatgpt.com/c/test'},
 sessionStorage:{getItem:()=>owner},document:{hasFocus:()=>!test.unfocused},
 bridgePinnedMessage:()=>({}),bridgeMessageBody:()=>body,bridgeCopyButton:()=>button,
 Date:{now:()=>now},setTimeout:(f,n)=>{now+=n;queueMicrotask(f)}};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
context.bridgeCopyWithProof('answer','assistant',context.location.href,'owner','key')
 .then(result=>console.log(JSON.stringify({result,restored:clipboard.writeText===original})))
 .catch(e=>console.log(JSON.stringify({error:e.message,restored:clipboard.writeText===original})));
'''


class CopyProofTests(unittest.TestCase):
    def run_copy(self, **kwargs):
        p = subprocess.run(['node','-e',HARNESS,str(SOURCE)], input=json.dumps(kwargs),
                           text=True,capture_output=True,timeout=15)
        self.assertEqual(p.returncode,0,p.stderr)
        result=json.loads(p.stdout)
        self.assertTrue(result['restored'])
        return result

    def test_raw_markdown_from_exact_write_not_global_clipboard(self):
        result=self.run_copy()['result']
        self.assertEqual(result['text'],'# Original\n\n**raw Markdown**')
        self.assertEqual(result['writes'],1)
        self.assertEqual(result['provenance'],'visible-copy-write/v1')

    def test_failure_ambiguity_identity_drift_and_no_write_fail_closed(self):
        for case in ('failure','multiple','drift','change','missing','unfocused'):
            with self.subTest(case=case):
                self.assertIn('error',self.run_copy(**{case:True}))


if __name__ == '__main__':
    unittest.main()
