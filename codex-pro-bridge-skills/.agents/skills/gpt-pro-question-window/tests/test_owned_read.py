import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock

SCRIPTS=Path(__file__).resolve().parents[1]/'scripts'
sys.path[:0]=[str(SCRIPTS),str(SCRIPTS.parents[1]/'.shared')]
from bridge_runtime.browser import Browser
from bridge_store import BridgeError


class OwnedReadTests(unittest.TestCase):
    def test_real_focus_guard_rejects_drift_before_reading_focus(self):
        for mode in ('ok', 'owner-drift', 'url-drift', 'unfocused'):
            with self.subTest(mode=mode):
                state = {'url': 'https://chatgpt.com/c/exact', 'owner': 'owner', 'focus_reads': 0}
                calls = []
                class Client:
                    def call(self, name, **args):
                        calls.append(name)
                        if name == 'select_page':
                            if mode == 'owner-drift': state['owner'] = 'other'
                            if mode == 'url-drift': state['url'] = 'https://chatgpt.com/c/other'
                            return {}
                        source = 'const state=' + json.dumps(state) + ';'
                        source += 'const location={href:state.url},sessionStorage={getItem:()=>state.owner};'
                        source += 'const document={hasFocus:()=>{state.focus_reads++;return ' + str(mode != 'unfocused').lower() + ';}};'
                        source += 'try{console.log(JSON.stringify({value:('+args['function']+')(),reads:state.focus_reads}));}'
                        source += 'catch(e){console.log(JSON.stringify({error:e.message,reads:state.focus_reads}));}'
                        proc = subprocess.run(['node', '-e', source], capture_output=True, text=True, check=True, timeout=10)
                        result = json.loads(proc.stdout)
                        state['focus_reads'] = result['reads']
                        if 'error' in result: raise BridgeError(result['error'])
                        return {'content': [{'type': 'text', 'text': json.dumps(result['value'])}]}
                browser = Browser(Client(), {})
                browser.page_id, browser.url, browser.owner = 7, state['url'], state['owner']
                if mode == 'ok':
                    browser.focus_for_copy()
                else:
                    with self.assertRaises(BridgeError): browser.focus_for_copy()
                self.assertEqual(calls, ['evaluate_script', 'select_page', 'evaluate_script'])
                self.assertEqual(state['focus_reads'], 0 if mode.endswith('drift') else 1)

    def test_identity_is_checked_before_content_in_single_rpc(self):
        browser=Browser(Mock(),{})
        browser.url,browser.owner='https://chatgpt.com/c/exact','exact-owner'
        browser.evaluate=Mock(return_value='result')
        self.assertEqual(browser.owned_evaluate('() => { touched=true; return 42; }'),'result')
        browser.evaluate.assert_called_once()
        function=browser.evaluate.call_args.args[0]
        for owner,url,passed in [('exact-owner',browser.url,True),('wrong',browser.url,False),
                                 ('exact-owner','https://chatgpt.com/c/wrong',False)]:
            source='let touched=false; const location={href:'+json.dumps(url)+'}; const sessionStorage={getItem:()=>'+json.dumps(owner)+'};'
            source+='try { const result=('+function+')(); console.log(JSON.stringify({result,touched})); } catch(e) {console.log(JSON.stringify({error:e.message,touched}));}'
            proc=subprocess.run(['node','-e',source],capture_output=True,text=True,timeout=10)
            self.assertEqual(proc.returncode,0,proc.stderr)
            result=json.loads(proc.stdout)
            self.assertEqual(result['touched'],passed)
            self.assertEqual('error' not in result,passed)


if __name__=='__main__': unittest.main()
