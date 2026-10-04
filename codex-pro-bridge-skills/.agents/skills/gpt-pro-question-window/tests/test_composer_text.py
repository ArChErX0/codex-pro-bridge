import json
from pathlib import Path
import subprocess
import unittest

SOURCE=Path(__file__).resolve().parents[1]/'scripts/composer_text.js'
HARNESS=r'''
const fs=require('fs'),vm=require('vm');
const text=value=>({nodeType:3,textContent:value});
const node=(tag,children=[],classes=[],attrs={})=>({nodeType:1,tagName:tag,childNodes:children,
 children:children.filter(n=>n.nodeType===1),classList:{contains:n=>classes.includes(n)},getAttribute:n=>attrs[n]});
const icon=node('SPAN',[],['ProseMirror-widget'],{'aria-hidden':'true'});
const link=node('SPAN',[icon,text('https://example.com')]);
const root=node('DIV',[node('P',[text('## `x` a  b'),node('BR'),text('URL '),link,
 node('BR'),node('BR',[],['ProseMirror-trailingBreak'])])]);
const scope={};vm.createContext(scope);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),scope);
console.log(JSON.stringify(scope.bridgeComposerText(root)));
'''


class ComposerTextTests(unittest.TestCase):
    def test_rich_link_decoration_does_not_add_layout_newline_or_erase_spaces(self):
        result=subprocess.run(['node','-e',HARNESS,str(SOURCE)],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout),'## `x` a  b\nURL https://example.com')


if __name__=='__main__': unittest.main()
