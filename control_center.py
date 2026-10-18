"""Local read-only UI, persistent queue and explicit controller commands."""
from __future__ import annotations
import argparse
import asyncio
import ctypes
from datetime import timedelta
import hashlib
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import os
import re
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))
import telemetry
from ledger import Ledger
from settings import DATA,DB,PYTHON,LAUNCHER,PORT
STARTED=time.time()
RUNTIME_FILES=('control_center.py','ledger.py','telemetry.py','settings.py','alignment.py','mcp_launcher.py','service_control.py')
CODE_HASH=hashlib.sha256(b''.join((ROOT/name).read_bytes() for name in RUNTIME_FILES)).hexdigest()
FEATURES=['alignment-contract','waiting-reason','graceful-service-control','queued-cancel','scaled-output-budget']

def save(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    temp.write_text(json.dumps(value,ensure_ascii=True,indent=2)+'\n',encoding='utf-8');os.replace(temp,path)

def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def alive(pid):
    if not pid:return False
    if os.name=='nt':
        kernel=ctypes.windll.kernel32
        kernel.OpenProcess.restype=ctypes.c_void_p
        handle=kernel.OpenProcess(0x1000,False,pid)
        if not handle:return False
        code=ctypes.c_ulong();ok=kernel.GetExitCodeProcess(ctypes.c_void_p(handle),ctypes.byref(code));kernel.CloseHandle(ctypes.c_void_p(handle))
        return bool(ok and code.value==259)
    try:os.kill(pid,0);return True
    except OSError:return False

def take_singleton():
    DATA.mkdir(parents=True,exist_ok=True)
    handle=open(DATA/'service.lock','a+b');handle.seek(0);handle.write(b'0');handle.flush();handle.seek(0)
    if os.name=='nt':
        import msvcrt
        msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
    else:
        import fcntl
        fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
    return handle

def environment(task=None):
    names=('PATH','SYSTEMROOT','WINDIR','TEMP','TMP','COMSPEC','USERPROFILE','APPDATA','LOCALAPPDATA','SYSTEMDRIVE','HOMEDRIVE','HOMEPATH',
      'DEEPSEEK_API_KEY','DS_CC_HOME','DS_CC_MCP_PYTHON','DS_CC_MCP_LAUNCHER','DS_CC_PORT')
    env={k:os.environ[k] for k in names if k in os.environ}
    env['PATH']=str(PYTHON.parent)+os.pathsep+env.get('PATH',os.defpath)
    if task:
        env.update({'DS_CC_TASK_ID':task['id'],'DS_CC_ATTEMPT':str(task['attempt']),
          'DS_CC_MAX_TOKENS':str(task['budgets']['max_tokens']),'DS_CC_MAX_TURNS':str(task['budgets']['max_turns']),
          'DS_CC_MAX_SECONDS':str(task['budgets']['max_seconds'])})
    return env

def parse_mcp(response):
    text='\n'.join(b.text for b in response.content if b.type=='text')
    try:data=json.loads(text)
    except ValueError:raise RuntimeError('non-JSON MCP control response') from None
    if response.isError or not data.get('ok'):
        detail=re.sub(r'sk-[A-Za-z0-9_-]+','[redacted]',str(data.get('error','unknown')))
        raise RuntimeError('MCP control rejected: '+detail[:250])
    return data

def scope_hashes(task):
    root=Path(task['workspace']).resolve();result={}
    for item in task['scope']:
        p=(root/item).resolve()
        if not p.is_relative_to(root):raise ValueError('scope escapes workspace')
        paths=[p] if p.is_file() else sorted(p.rglob('*')) if p.is_dir() else []
        for file in paths:
            if file.is_file() and '.git' not in file.relative_to(root).parts:
                if not file.resolve().is_relative_to(root):raise ValueError('scope symlink escapes workspace')
                if len(result)>=1000 or file.stat().st_size>512*1024*1024:raise ValueError('scope hash budget exceeded; narrow scope')
                result[str(file.resolve())]=sha(file)
    return result

async def execute_task(id,attempt):
    from mcp import ClientSession,StdioServerParameters
    from mcp.client.stdio import stdio_client
    ledger=Ledger(DB);task=ledger.get(id);ledger.set_pid(id,os.getpid(),attempt)
    destination=DATA/'deliveries'/id/f'attempt-{attempt}.json'
    before={};after={};recovery=None;result=None;state='failed';error=None;diagnostic=None
    try:
        before=scope_hashes(task)
        params=StdioServerParameters(command=str(PYTHON),args=['-I','-X','utf8',str(LAUNCHER)],cwd=task['workspace'],env=environment(task))
        async with stdio_client(params) as (reader,writer):
            async with ClientSession(reader,writer,read_timeout_seconds=timedelta(seconds=90)) as session:
                await session.initialize()
                recovery=parse_mcp(await session.call_tool('get_deepseek_recovery',{}))
                if recovery['count']:raise RuntimeError('pending upstream recovery; inspect before dispatch')
                feedback=[e['data'].get('reason','') for e in reversed(ledger.events(id,100)) if e['kind']=='requeued']
                instruction=task['spec']['instruction']
                if task['spec'].get('alignment'):
                    instruction+='\n\nAgreed task contract (execute and report against these exact acceptance criteria):\n'+json.dumps(task['spec']['alignment'],ensure_ascii=False)
                if feedback:instruction+='\n\nController rework feedback:\n'+'\n'.join(feedback)
                context=task['spec']['context']+'\nController: 制作统筹, official Sol. Execute only this task, use bounded targeted reads, return concise evidence. Never self-accept. Respect all supplied project/GUI/resource constraints. Scope: '+json.dumps(task['scope'],ensure_ascii=True)
                started=parse_mcp(await session.call_tool('start_deepseek_readonly' if task['readonly'] else 'start_deepseek',{'task':instruction,'context':context,'model':'flash'}))
                job=started['job_id'];deadline=time.monotonic()+task['budgets']['max_seconds']+30;cancelled=False
                request_dir=DATA/'requests'/id/str(attempt)
                while True:
                    if (request_dir/'cancel.json').exists() and not cancelled:
                        parse_mcp(await session.call_tool('cancel_deepseek',{'job_id':job}));cancelled=True
                    result=parse_mcp(await session.call_tool('get_deepseek_result',{'job_id':job}))
                    if result.get('ready'):break
                    if time.monotonic()>deadline:
                        if not cancelled:
                            parse_mcp(await session.call_tool('cancel_deepseek',{'job_id':job}));cancelled=True;deadline=time.monotonic()+30
                        else:raise TimeoutError('worker did not reach terminal state after cancellation')
                    await asyncio.sleep(2)
                state={'completed':'delivered','failed':'failed','cancelled':'cancelled'}.get(result.get('status'),'failed')
                recovery=parse_mcp(await session.call_tool('get_deepseek_recovery',{}))
                after=scope_hashes(task)
    except BaseException as exc:
        state='failed'
        error=type(exc).__name__
        def leaf(e):
            if isinstance(e,BaseExceptionGroup):return [item for child in e.exceptions for item in leaf(child)]
            return [{'type':type(e).__name__,'message':re.sub(r'sk-[A-Za-z0-9_-]+','[redacted]',str(e))[:350]}]
        diagnostic=leaf(exc)
        try:after=scope_hashes(task)
        except Exception:pass
    pending=recovery.get('count',0) if recovery is not None else (0 if task['readonly'] else 1)
    payload={'task_id':id,'attempt':attempt,'state':state,'result':result,'recovery':recovery,'before_hashes':before,'after_hashes':after,'error_code':error,'diagnostic':diagnostic,'saved_at':time.time()}
    save(destination,payload)
    ledger.finish(id,state,str(destination),error,pending,attempt)

async def acknowledge(id,receipt):
    """Only explicit host review can acknowledge exact mutation transaction IDs."""
    from mcp import ClientSession,StdioServerParameters
    from mcp.client.stdio import stdio_client
    ledger=Ledger(DB);task=ledger.get(id)
    if task['state']=='running':raise ValueError('task still running')
    data=json.loads(Path(receipt).read_text(encoding='utf-8'))
    if data.get('task_id')!=id or data.get('attempt')!=task['attempt'] or data.get('reviewer')!='制作统筹' or not data.get('checks') or not isinstance(data.get('transaction_ids'),list) or not isinstance(data.get('artifact_hashes'),dict):raise ValueError('invalid recovery receipt')
    for path,digest in data['artifact_hashes'].items():
        if not Path(path).is_absolute() or sha(path)!=digest:raise ValueError('recovery artifact changed')
    params=StdioServerParameters(command=str(PYTHON),args=['-I','-X','utf8',str(LAUNCHER)],cwd=task['workspace'],env=environment())
    async with stdio_client(params) as (reader,writer):
      async with ClientSession(reader,writer) as session:
        await session.initialize();pending=parse_mcp(await session.call_tool('get_deepseek_recovery',{}))
        actual={r['transaction_id'] for r in pending['pending']}
        if set(data['transaction_ids'])!=actual:raise ValueError('receipt must cover exact currently pending transaction IDs')
        if actual and not data['artifact_hashes']:raise ValueError('artifact hashes required')
        root=Path(task['workspace']).resolve()
        for record in pending['pending']:
            artifact=(root/record['path']).resolve()
            if not artifact.is_relative_to(root) or data['artifact_hashes'].get(str(artifact))!=sha(artifact):raise ValueError('receipt must verify every pending file')
        if actual:
            result=parse_mcp(await session.call_tool('acknowledge_deepseek_mutations',{'transaction_ids':sorted(actual)}))
            if result.get('pending'):raise ValueError('upstream recovery still pending')
        ledger.clear_recovery(id,task['attempt'])
        save(DATA/'deliveries'/id/f"recovery-review-{task['attempt']}.json",data)
    return {'task_id':id,'recovery_cleared':True}

def public_snapshot():
    ledger=Ledger(DB);runs,totals,events=telemetry.snapshot();latest={}
    for r in runs:
        if r['task_id'] and r['task_id'] not in latest:latest[r['task_id']]=r
    tasks=[]
    for task in ledger.list_tasks(200):
        task['alignment']=task['spec'].get('alignment')
        task.pop('spec',None);task['runtime']=latest.get(task['id']);tasks.append(task)
    by_id={t['id']:t for t in tasks};held={r['name'] for r in ledger.resources()}
    for t in tasks:
        t['waiting_reason']=None
        if t['state']=='queued':
            missing=[d for d in t['dependencies'] if by_id.get(d,{}).get('state')!='accepted']
            busy=[r for r in t['resources'] if r in held]
            if missing:t['waiting_reason']='等待依赖验收: '+', '.join(missing)
            elif busy:t['waiting_reason']='等待资源: '+', '.join(busy)
            elif any(x['id']!=t['id'] and not x['released'] and x['state']!='queued' and x['workspace']==t['workspace'] for x in tasks):t['waiting_reason']='等待同工位执行/验收回收'
            else:t['waiting_reason']='等待执行槽'
    # Prompts and final model text are deliberately absent from the read-only API.
    return {'generated_at':time.time(),'service':{'pid':os.getpid(),'started_at':STARTED,'max_parallel':3,'code_hash':CODE_HASH,'features':FEATURES},
      'summary':{'tasks':len(tasks),'running':sum(t['state']=='running' for t in tasks),**totals},
      'tasks':tasks,'runs':runs,'events':sorted(events+ledger.events(limit=100),key=lambda e:e['at'],reverse=True)[:100],
      'resources':ledger.resources(),'notes':['仅接入后新启动的 MCP 进程有实时用量，旧调用不追溯。',
      '模型调用 deepseek-flash；官方于 2026-10-17 映射至 DeepSeek-V4.1-Flash-1017。',
      'Token 为 API 返回用量；缓存命中包含在输入中。费用未定价，API 账单为准。',
      '资源队列约束登记任务；外部 UE/Blender 进程由制作统筹登记占用。',
      '已交付需独立验收；中断不自动重派。面板仅查看，派工与审核由统筹命令执行。']}

def skins():
    result=[]
    for file in sorted((ROOT/'skins').glob('*/skin.json')):
        try:
            data=json.loads(file.read_text(encoding='utf-8'))
            if data.get('id')==file.parent.name and isinstance(data.get('name'),str):result.append(data)
        except Exception:pass
    return result

def serve():
    singleton=take_singleton();ledger=Ledger(DB);telemetry.initialize()
    stop_event=threading.Event();dispatch_lock=threading.Lock();shutdown_request={}
    running=[t for t in ledger.list_tasks(500) if t['state']=='running']
    ledger.recover_orphans([t['pid'] for t in running if alive(t['pid'])])
    def schedule():
        last_sweep=0
        while not stop_event.is_set():
            task=None
            try:
                if time.monotonic()-last_sweep>5:
                    active=[t for t in ledger.list_tasks(500) if t['state']=='running']
                    live=[t['pid'] for t in active if alive(t['pid']) or (t['pid'] is None and time.time()-t['updated_at']<15)]
                    ledger.recover_orphans(live)
                    last_sweep=time.monotonic()
                with dispatch_lock:
                    if stop_event.is_set():break
                    task=ledger.claim_next(3)
                    if task:
                        log=DATA/'runner-logs'/f"{task['id']}-{task['attempt']}.log";log.parent.mkdir(parents=True,exist_ok=True)
                        with log.open('ab') as handle:
                            subprocess.Popen([str(PYTHON),'-I','-X','utf8',str(ROOT/'control_center.py'),'run',task['id'],str(task['attempt'])],cwd=str(ROOT),env=environment(),stdout=handle,stderr=handle,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
                if not task:stop_event.wait(1)
            except Exception as exc:
                if task:
                    current=ledger.get(task['id'])
                    if current['state']=='running' and current['pid'] is None:
                        ledger.finish(task['id'],'interrupted',error_code='runner_launch_failed',recovery_pending=0 if task['readonly'] else 1,attempt=task['attempt'])
                save(DATA/'scheduler-error.json',{'at':time.time(),'error_type':type(exc).__name__});time.sleep(2)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):self.send_error(405)
        do_PUT=do_DELETE=do_POST
        def do_GET(self):
            if self.headers.get('Host') not in (f'127.0.0.1:{PORT}',f'localhost:{PORT}'):
                self.send_error(403);return
            path=self.path.split('?',1)[0]
            try:
                if path=='/api/snapshot':body=json.dumps(public_snapshot(),ensure_ascii=True).encode();mime='application/json'
                elif path=='/api/skins':body=json.dumps(skins(),ensure_ascii=True).encode();mime='application/json'
                elif path in ('/','/index.html'):body=(ROOT/'index.html').read_bytes();mime='text/html; charset=utf-8'
                elif path.startswith('/skins/'):
                    from urllib.parse import unquote
                    file=(ROOT/unquote(path).lstrip('/')).resolve()
                    if not file.is_relative_to((ROOT/'skins').resolve()) or not file.is_file() or file.suffix.lower() not in ('.png','.webp','.jpg','.jpeg','.json'):
                        self.send_error(404);return
                    body=file.read_bytes();mime={'.png':'image/png','.webp':'image/webp','.json':'application/json','.jpg':'image/jpeg','.jpeg':'image/jpeg'}[file.suffix.lower()]
                else:self.send_error(404);return
                self.send_response(200);self.send_header('Content-Type',mime);self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
                self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
                self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
            except (BrokenPipeError,ConnectionResetError):pass
            except Exception:self.send_error(500)
    http=ThreadingHTTPServer(('127.0.0.1',PORT),Handler)
    def lifecycle():
        handled=set()
        while not stop_event.wait(.2):
            try:
                request=json.loads((DATA/'service-request.json').read_text(encoding='utf-8'))
            except (FileNotFoundError,json.JSONDecodeError):continue
            if not isinstance(request,dict):continue
            rid=request.get('request_id')
            if not isinstance(rid,str) or len(rid)!=32 or any(c not in '0123456789abcdef' for c in rid) or rid in handled:continue
            if request.get('pid')!=os.getpid() or request.get('started_at')!=STARTED or request.get('action')!='stop':continue
            handled.add(rid)
            with dispatch_lock:
                busy=any(t['state'] in ('queued','running') for t in ledger.list_tasks(500))
                if busy:
                    save(DATA/f'service-response-{os.getpid()}.json',{'request_id':rid,'state':'busy'});continue
                shutdown_request.update(request);stop_event.set()
            http.shutdown()
    save(DATA/'service.json',{'pid':os.getpid(),'started_at':STARTED,'url':f'http://127.0.0.1:{PORT}','code_hash':CODE_HASH,'features':FEATURES})
    scheduler=threading.Thread(target=schedule,daemon=True);scheduler.start()
    threading.Thread(target=lifecycle,daemon=True).start()
    try:http.serve_forever()
    finally:
        stop_event.set();http.server_close();scheduler.join(timeout=3);singleton.close()
        if shutdown_request:save(DATA/f'service-response-{os.getpid()}.json',{'request_id':shutdown_request['request_id'],'state':'stopped','pid':os.getpid()})

def main():
    p=argparse.ArgumentParser();sub=p.add_subparsers(dest='cmd',required=True)
    sub.add_parser('serve');sub.add_parser('status')
    s=sub.add_parser('submit');s.add_argument('spec')
    s=sub.add_parser('run');s.add_argument('id');s.add_argument('attempt',type=int)
    s=sub.add_parser('review');s.add_argument('id');s.add_argument('decision',choices=['accepted','rework','closed']);s.add_argument('evidence')
    s=sub.add_parser('requeue');s.add_argument('id');s.add_argument('reason')
    s=sub.add_parser('ack-recovery');s.add_argument('id');s.add_argument('receipt')
    s=sub.add_parser('cancel');s.add_argument('id')
    for name in ('reserve','release'):
        s=sub.add_parser(name);s.add_argument('resource');s.add_argument('--owner',default='制作统筹')
    args=p.parse_args();ledger=Ledger(DB)
    if args.cmd=='serve':return serve()
    if args.cmd=='run':return asyncio.run(execute_task(args.id,args.attempt))
    if args.cmd=='status':result=public_snapshot()
    elif args.cmd=='submit':
        spec=json.loads(Path(args.spec).read_text(encoding='utf-8-sig'))
        from alignment import validate
        validate(spec.get('alignment'))
        result=ledger.create(spec)
    elif args.cmd=='review':result=ledger.review(args.id,args.decision,{'path':str(Path(args.evidence).resolve()),'sha256':sha(args.evidence)},'制作统筹')
    elif args.cmd=='requeue':result=ledger.requeue(args.id,args.reason)
    elif args.cmd=='ack-recovery':result=asyncio.run(acknowledge(args.id,args.receipt))
    elif args.cmd=='cancel':
        if ledger.cancel_queued(args.id):result=ledger.get(args.id)
        else:
            t=ledger.get(args.id)
            if t['state']!='running':raise ValueError('only queued or running tasks can be cancelled')
            save(DATA/'requests'/args.id/str(t['attempt'])/'cancel.json',{'at':time.time()});result={'cancel_requested':args.id}
    elif args.cmd=='reserve':ledger.reserve(args.resource,args.owner);result=ledger.resources()
    elif args.cmd=='release':ledger.release_reservation(args.resource,args.owner);result=ledger.resources()
    print(json.dumps(result,ensure_ascii=True))

if __name__=='__main__':
    try:main()
    except Exception as exc:
        print(json.dumps({'ok':False,'error_type':type(exc).__name__,'message':str(exc)[:300]},ensure_ascii=True),file=sys.stderr);sys.exit(1)
