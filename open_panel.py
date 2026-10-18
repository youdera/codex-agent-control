"""User-invoked launcher. Start the local service if needed, then open its UI."""
from pathlib import Path
import sys
import subprocess
import time
import urllib.request
import webbrowser
ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))
from settings import DATA,PYTHON,PORT
URL=f'http://127.0.0.1:{PORT}'
def ready():
    try:
        with urllib.request.urlopen(URL+'/api/skins',timeout=1) as r:return r.status==200
    except Exception:return False
if not ready():
    DATA.mkdir(parents=True,exist_ok=True)
    with (DATA/'service.stdout.log').open('ab') as out,(DATA/'service.stderr.log').open('ab') as err:
        subprocess.Popen([str(PYTHON),'-I','-X','utf8',str(ROOT/'control_center.py'),'serve'],cwd=str(ROOT),stdout=out,stderr=err,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    for _ in range(30):
        if ready():break
        time.sleep(.2)
if ready():webbrowser.open(URL)
else:raise SystemExit('Local control center failed to start. Inspect service.stderr.log.')
