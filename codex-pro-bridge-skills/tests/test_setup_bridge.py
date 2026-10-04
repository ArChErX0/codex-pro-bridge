"""Clean-home deployment, upgrades and fail-closed readiness (no real Send)."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("setup_bridge", ROOT / "setup_bridge.py")
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


FAKE_MCP = r'''
import json,sys
from pathlib import Path
trace=Path(sys.argv[1])
names=['list_pages','new_page','evaluate_script','take_snapshot','click','fill','upload_file','press_key','select_page']
position=3
labels=['即时','标准','扩展','极高']
for line in sys.stdin:
    m=json.loads(line)
    if 'id' not in m: continue
    method=m['method']; p=m.get('params',{})
    if method=='initialize': result={'protocolVersion':'2024-11-05','capabilities':{},'serverInfo':{'name':'fake','version':'1'}}
    elif method=='tools/list': result={'tools':[{'name':n,'inputSchema':{'type':'object','properties':{'pageId':{'type':'number'}}}} for n in names]}
    else:
        n=p['name']; a=p['arguments']
        with trace.open('a') as f: f.write(json.dumps({'name':n,'arguments':a})+'\n')
        if n in ('fill','upload_file','new_page'): raise RuntimeError('Setup performed a forbidden action')
        if n=='list_pages': text='## Pages\n1: https://chatgpt.com/'
        elif n=='take_snapshot': text='uid=1 button "选择 ChatGPT 模型"\nuid=2 button "打开个人资料菜单"\nuid=3 menuitem "选择模型"'
        elif n=='evaluate_script':
            fn=a['function']
            if 'const selectors =' in fn:
                keys=json.loads(fn.split('const selectors = ')[1].split('; const out')[0])
                v={k: {'account':'Private account','workspace':'Private account','thinking':labels[position],'model':'最新'}[k] for k in keys}
            elif 'aria-valuenow' in fn: v={'value':str(position),'min':'0','max':'3'}
            elif 'document.activeElement' in fn: v=True
            elif 'bridgeComposerText' in fn: v=''
            elif 'const legacy =' in fn: v=[]
            else: v={'url':'https://chatgpt.com/','owner':''}
            text=json.dumps(v)
        else:
            if n=='press_key':
                if a['key']=='ArrowLeft': position-=1
                if a['key']=='ArrowRight': position+=1
            text='OK'
        result={'content':[{'type':'text','text':text}]}
    print(json.dumps({'jsonrpc':'2.0','id':m['id'],'result':result}),flush=True)
'''


@unittest.skipIf(sys.version_info < (3, 11), "Setup requires Python 3.11+; workers still support 3.10")
class SetupTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "codex home"
        self.repo = self.root / "repo with spaces"
        self.repo.mkdir()
        self.fake = self.root / "fake_mcp.py"
        self.fake.write_text(FAKE_MCP, encoding="utf-8")
        self.trace = self.root / "calls.jsonl"
        self.argv = json.dumps([sys.executable, str(self.fake), str(self.trace)])

    def invoke(self, *extra, command="install", expected=0):
        args = [sys.executable, str(ROOT / "setup_bridge.py"), command,
                "--codex-home", str(self.home)]
        if command != "doctor":
            args += ["--repo", str(self.repo), "--topology", "native", "--browser-command-json", self.argv,
                     "--browser-transport", "stdio"]
        result = subprocess.run([*args, *extra], capture_output=True, text=True, encoding="utf-8", timeout=60, check=False)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_clean_install_registers_mcp_and_learns_ui_without_send(self):
        receipt = self.invoke()
        self.assertEqual(receipt['readiness']['status'], 'ready')
        config = setup.tomllib.loads((self.home / 'config.toml').read_text())
        server = config['mcp_servers']['codex-pro-bridge']
        self.assertEqual(server['command'], sys.executable)
        self.assertIn('chrome-devtools', config['mcp_servers'])
        self.assertTrue(Path(server['args'][0]).is_file())
        runtime = json.loads(Path(server['args'][-1]).read_text())
        self.assertEqual(runtime['allowed_repos'], [str(self.repo)])
        self.assertTrue(runtime['setup_ui_verified'])
        self.assertEqual(runtime['ui']['thinking_positions'], {'即时':0,'标准':1,'扩展':2,'极高':3})
        actions = [json.loads(line)['name'] for line in self.trace.read_text().splitlines()]
        self.assertNotIn('upload_file', actions)
        self.assertNotIn('fill', actions)
        self.assertNotIn('new_page', actions)
        self.assertNotIn('Private account', json.dumps(receipt))
        self.assertEqual((self.home / 'skills' / '.shared').is_dir(), True)

    def test_upgrade_keeps_unrelated_config_and_preserves_user_skill(self):
        self.invoke('--no-connect')
        config_path = self.home / 'config.toml'
        previous = config_path.read_text()
        config_path.write_text('model = "example-model"\n' + previous + '\n[mcp_servers.other]\ncommand = "other-tool"\n')
        custom = self.home / 'skills' / 'gpt-pro-question-window' / 'user-note.txt'
        custom.write_text('keep me')
        receipt = self.invoke('--no-connect')
        settings = setup.tomllib.loads(config_path.read_text())
        self.assertEqual(settings['model'], 'example-model')
        self.assertEqual(settings['mcp_servers']['other']['command'], 'other-tool')
        restore = json.loads((Path(receipt['backup']) / 'restore-map.json').read_text())
        skill_backup = next(Path(x['backup']) for x in restore if x['original'].endswith('gpt-pro-question-window'))
        self.assertEqual((skill_backup / 'user-note.txt').read_text(), 'keep me')
        self.assertFalse(custom.exists())
        self.assertEqual(config_path.read_text().count(setup.BEGIN), 1)

    def test_existing_unmanaged_mcp_is_not_overwritten(self):
        self.home.mkdir()
        config = self.home / 'config.toml'
        content = '[mcp_servers.codex-pro-bridge]\ncommand="keep-this"\n'
        config.write_text(content)
        result = self.invoke('--no-connect', expected=2)
        self.assertIn('unmanaged', result['error'])
        self.assertEqual(config.read_text(), content)
        self.assertFalse((self.home / 'skills').exists())

    def test_existing_chrome_configuration_is_preserved(self):
        self.home.mkdir()
        config = self.home / 'config.toml'
        config.write_text('[mcp_servers.chrome-devtools]\ncommand="admin-browser"\nargs=["pinned-entry"]\n')
        self.invoke('--no-connect')
        settings = setup.tomllib.loads(config.read_text())
        self.assertEqual(settings['mcp_servers']['chrome-devtools'], {'command':'admin-browser', 'args':['pinned-entry']})

    @unittest.skipUnless(shutil.which('git'), 'Git is required for repository-local exclusion')
    def test_full_setup_excludes_only_its_own_repository_runtime(self):
        setup.run([shutil.which('git'), '-C', str(self.repo), 'init'])
        self.invoke('--no-connect')
        exclude = (self.repo / '.git' / 'info' / 'exclude').read_text()
        self.assertIn('.codex/codex-pro-bridge/', exclude.splitlines())
        self.assertNotIn('.codex/', exclude.splitlines())
        self.assertNotIn('.agents/', exclude.splitlines())

    def test_no_connect_is_not_live_ready_and_recheck_uses_installed_copy(self):
        result = self.invoke('--no-connect')
        self.assertEqual(result['status'], 'installed-not-verified')
        runtime = self.home / 'codex-pro-bridge' / 'runtime.json'
        self.assertFalse(json.loads(runtime.read_text())['setup_ui_verified'])
        doctor = result['doctor']
        self.assertNotIn(str(ROOT), doctor[1])
        process = subprocess.run(doctor, capture_output=True, text=True, encoding='utf-8', timeout=30, check=False)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertEqual(json.loads(process.stdout)['status'], 'ready')
        self.assertTrue(json.loads(runtime.read_text())['setup_ui_verified'])

    def test_schema_only_does_not_trigger_chrome_authorization(self):
        self.invoke('--no-connect')
        receipt = self.invoke(command='doctor')
        self.assertEqual(receipt['status'], 'configured')
        self.assertFalse(self.trace.exists())

    @unittest.skipIf(os.name == 'nt', 'Symlink creation may need elevated Windows permissions')
    def test_symlink_destination_is_refused_before_copy(self):
        self.home.mkdir()
        other = self.root / 'other'
        other.mkdir()
        (self.home / 'skills').symlink_to(other, target_is_directory=True)
        result = self.invoke('--no-connect', expected=2)
        self.assertIn('symlink', result['error'])
        self.assertEqual(list(other.iterdir()), [])

    def test_windows_argv_is_toml_safe(self):
        block = setup.mcp_block(r'C:\Program Files\Python\python.exe', r'C:\用户\skills\bridge.py',
                                r'C:\用户\runtime.json', {'TEST_PATH': r'G:\资料\stage'})
        parsed = setup.tomllib.loads(setup.config_text('model="unchanged"\n', block))
        self.assertEqual(parsed['mcp_servers']['codex-pro-bridge']['command'], r'C:\Program Files\Python\python.exe')
        self.assertEqual(parsed['mcp_servers']['codex-pro-bridge']['env']['TEST_PATH'], r'G:\资料\stage')

    def test_malformed_or_unrelated_managed_block_is_refused(self):
        for content in (setup.BEGIN + '\n', setup.BEGIN + '\nmodel="private"\n' + setup.END + '\n'):
            with self.subTest(content=content), self.assertRaises(setup.SetupError):
                setup.config_text(content, setup.mcp_block('/python', '/entry', '/runtime', {}))

    def test_unavailable_or_implicit_repository_is_not_authorized(self):
        process = subprocess.run([sys.executable, str(ROOT / 'setup_bridge.py'), 'install',
                                  '--codex-home', str(self.home)], capture_output=True, text=True, check=False)
        self.assertEqual(process.returncode, 2)
        self.assertFalse(self.home.exists())

    def test_pageid_schema_gate_fails_closed(self):
        with self.assertRaisesRegex(setup.SetupError, 'pageId'):
            setup.check_schemas({name: {'properties': {}} for name in
                                 ('list_pages','new_page','evaluate_script','take_snapshot','click','fill','upload_file','press_key','select_page')})

    @unittest.skipUnless(shutil.which('node'), 'Node is required to exercise the actual page guard JavaScript')
    def test_unowned_guard_accepts_null_but_rejects_owner_or_url_drift(self):
        sys.path[:0] = [str(ROOT / '.agents' / 'skills' / 'gpt-pro-question-window' / 'scripts'),
                       str(ROOT / '.agents' / 'skills' / '.shared')]
        browser = setup.readiness_browser(None, {})
        browser.url = 'https://chatgpt.com/'
        browser.evaluate = lambda function: function
        function = browser.owned_evaluate('() => true')
        for url, owner, expected in (('https://chatgpt.com/', None, 'true'),
                                      ('https://chatgpt.com/', 'another-worker', 'blocked'),
                                      ('https://chatgpt.com/c/other', None, 'blocked')):
            script = ('global.location={href:' + json.dumps(url) + '};global.sessionStorage={getItem:()=>'
                      + json.dumps(owner) + '};try{console.log((' + function + ')())}catch(e){console.log("blocked")}')
            self.assertEqual(setup.run([shutil.which('node'), '-e', script]), expected)

    @unittest.skipUnless(os.name == 'nt', 'Windows ACL verification')
    def test_runtime_acl_is_current_user_only(self):
        self.invoke('--no-connect')
        state = self.home / 'codex-pro-bridge' / 'state'
        script = ("$sid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value; "
                  "$rules=(Get-Acl -LiteralPath '" + str(state).replace("'", "''") + "').Access; "
                  "if(@($rules).Count -ne 1 -or $rules[0].IsInherited -or "
                  "$rules[0].IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value -ne $sid) {exit 1}")
        setup.run([shutil.which('powershell.exe'), '-NoProfile', '-NonInteractive', '-Command', script])

    def test_node_version_rejected_before_package_install(self):
        with patch.object(setup.shutil, 'which', return_value='/tools/node'), \
             patch.object(setup, 'run', return_value='v18.20.0'), self.assertRaisesRegex(setup.SetupError, 'Node'):
            setup.node_host('native')

    def test_two_installs_cannot_interleave(self):
        with setup.setup_lock(self.home), self.assertRaisesRegex(setup.SetupError, 'Another setup'), setup.setup_lock(self.home):
            self.fail('Lock was not exclusive')
        self.assertFalse((self.home / '.bridge-setup.lock').exists())


if __name__ == '__main__':
    unittest.main()
