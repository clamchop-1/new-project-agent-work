"""Syntax-check the rendered chat page's inline JavaScript with Node.js."""
import re
import subprocess
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from chat_dashboard_page import PAGE
from chat_streaming import STREAMING_CLIENT_PATCH
from project_chat_patch import PROJECT_CHAT_PATCH


scripts=re.findall(r'<script\b[^>]*>(.*?)</script>',PAGE,re.DOTALL|re.IGNORECASE)
if not scripts:
    raise SystemExit('No inline chat script was found.')
scripts[-1]+=STREAMING_CLIENT_PATCH+PROJECT_CHAT_PATCH
for index,script in enumerate(scripts,1):
    result=subprocess.run(['node','--check'],input=script,text=True,encoding='utf-8',capture_output=True)
    if result.returncode:
        sys.stderr.write(result.stderr)
        raise SystemExit(f'Inline chat script {index} has a syntax error.')
print(f'Validated {len(scripts)} inline chat script(s).')
