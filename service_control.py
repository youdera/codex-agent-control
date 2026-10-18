"""Explicit lifecycle commands. Graceful only: never kill an unknown/legacy process."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
sys.path.insert(0,str(Path(__file__).resolve().parent))
from settings import ROOT,DATA,PYTHON,PORT
URL=f'http://127.0.0.1:{PORT}'

def save(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    tmp.write_text(json.dumps(value,ensure_ascii=True),encoding='utf-8');os.replace(tmp,path)

def status():
    try:
        with urllib.request.urlopen(URL+'/api/snapshot',timeout=2) as response:
            data=json.load(response)
        return data
    except (urllib.error.URLError,TimeoutError,ConnectionError):return None

def stop(timeout=15):
    current=status()
    if current is None:return {'state':'already_stopped'}
    service=current['service']
    if 'graceful-service-control' not in service.get('features',[]):
        raise RuntimeError('Legacy service: a one-time verified operator stop is required; no process will be killed automatically.')
    if any(t['state'] in ('queued','running') for t in current['tasks']):
        raise RuntimeError('Service has queued/running tasks; finish or cancel them before restarting.')
    request={'request_id':uuid.uuid4().hex,'pid':service['pid'],'started_at':service['started_at'],'action':'stop'}
    save(DATA/'service-request.json',request)
    response_path=DATA/f"service-response-{service['pid']}.json"
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        try:
            response=json.loads(response_path.read_text(encoding='utf-8'))
            if response.get('request_id')==request['request_id']:
                if response.get('state')=='busy':raise RuntimeError('New tasks appeared; service kept running.')
                if response.get('state')=='stopped':return response
        except (FileNotFoundError,json.JSONDecodeError):pass
        time.sleep(.15)
    raise TimeoutError('Graceful shutdown not acknowledged; no process was killed.')

def start(timeout=15):
    current=status()
    if current is not None:return {'state':'already_running',**current['service']}
    DATA.mkdir(parents=True,exist_ok=True)
    with (DATA/'service.stdout.log').open('ab') as out,(DATA/'service.stderr.log').open('ab') as err:
        proc=subprocess.Popen([str(PYTHON),'-I','-X','utf8',str(ROOT/'control_center.py'),'serve'],cwd=str(ROOT),stdout=out,stderr=err,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        current=status()
        if current is not None:return {'state':'running',**current['service']}
        if proc.poll() is not None:raise RuntimeError('Service startup failed; inspect private service.stderr.log.')
        time.sleep(.15)
    raise TimeoutError('Service startup timed out; inspect its private logs.')

def restart():
    previous=stop();current=start()
    return {'previous':previous,'current':current}

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['start','stop','restart','status']);args=parser.parse_args()
    try:
        result=status() if args.action=='status' else globals()[args.action]()
        if args.action=='status' and result:result=result['service']
        print(json.dumps(result,ensure_ascii=True))
    except Exception as error:
        print(json.dumps({'ok':False,'error_type':type(error).__name__,'message':str(error)},ensure_ascii=True),file=sys.stderr);sys.exit(1)
