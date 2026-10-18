"""Local, content-minimal telemetry. Provider usage only; no prompts/keys/output."""
from __future__ import annotations
import functools
import hashlib
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent))
from settings import DATA,DB

ROOT=Path(__file__).resolve().parent
LOCAL=threading.local()

@contextmanager
def connection():
    DATA.mkdir(parents=True,exist_ok=True)
    c=sqlite3.connect(DB,timeout=15)
    c.row_factory=sqlite3.Row
    c.execute('PRAGMA busy_timeout=15000')
    try:
        with c:yield c
    finally:c.close()

def initialize():
    with connection() as c:
        c.execute('PRAGMA journal_mode=WAL')
        c.executescript('''
        CREATE TABLE IF NOT EXISTS rt_runs(run_id TEXT PRIMARY KEY, task_id TEXT, attempt INTEGER,
          workspace TEXT, model_requested TEXT, model TEXT, phase TEXT, started_at REAL,
          finished_at REAL, last_activity REAL, tool_name TEXT, tool_path TEXT, turn INTEGER,
          error_code TEXT, pid INTEGER);
        CREATE TABLE IF NOT EXISTS rt_events(id TEXT PRIMARY KEY, at REAL, run_id TEXT,
          task_id TEXT, kind TEXT, data TEXT);
        CREATE TABLE IF NOT EXISTS rt_usage(request_id TEXT, run_id TEXT, at REAL, model TEXT,
          prompt_tokens INTEGER, completion_tokens INTEGER, cached_tokens INTEGER,
          PRIMARY KEY(run_id,request_id));
        ''')

def emit(kind,data=None,phase=None,**fields):
    run_id=getattr(LOCAL,'run_id',None)
    if not run_id:return
    allowed={'tool_name','tool_path','turn','error_code','model','finished_at'}
    if not fields.keys()<=allowed:raise ValueError('unsupported telemetry field')
    now=time.time()
    with connection() as c:
        c.execute('INSERT INTO rt_events VALUES(?,?,?,?,?,?)',
          (str(uuid.uuid4()),now,run_id,getattr(LOCAL,'task_id',None),kind,json.dumps(data or {},ensure_ascii=True)))
        fields['last_activity']=now
        if phase:fields['phase']=phase
        c.execute('UPDATE rt_runs SET '+','.join(f'{k}=?' for k in fields)+' WHERE run_id=?',(*fields.values(),run_id))

def _safe_emit(*args,**kwargs):
    try:emit(*args,**kwargs)
    except Exception:
        if getattr(LOCAL,'task_id',None):raise RuntimeError('telemetry persistence failed') from None

def _number(value):
    return value if isinstance(value,int) and not isinstance(value,bool) and value>=0 else 0

def record_usage(state,response):
    raw=response.model_dump(exclude_none=True)
    usage=raw.get('usage')
    if not isinstance(usage,dict):
        _safe_emit('usage_unavailable',{'estimated':False},phase='response')
        return
    requested=state.config.model
    actual=raw.get('model')
    actual=actual if isinstance(actual,str) and len(actual)<100 else None
    details=usage.get('prompt_tokens_details') or {}
    cached=_number(usage.get('prompt_cache_hit_tokens',details.get('cached_tokens',0)))
    prompt=_number(usage.get('prompt_tokens'))
    completion=_number(usage.get('completion_tokens'))
    cached=min(cached,prompt)
    provider_id=raw.get('id')
    request_id=('provider:'+hashlib.sha256(str(provider_id).encode()).hexdigest()) if provider_id else getattr(LOCAL,'request_key',None)
    if not request_id:raise RuntimeError('request identity unavailable')
    with connection() as c:
        c.execute('INSERT OR IGNORE INTO rt_usage VALUES(?,?,?,?,?,?,?)',
          (request_id,LOCAL.run_id,time.time(),actual,prompt,completion,cached))
    _safe_emit('provider_usage',{'prompt_tokens':prompt,'completion_tokens':completion,'cached_tokens':cached,
      'model_requested':requested,'model_observed':actual},phase='response',model=actual)

def _path_hint(call):
    try:
        args=json.loads(call.function.arguments)
        value=args.get('file_path',args.get('path',''))
        if isinstance(value,str) and len(value)<=500 and not any(x in value.lower() for x in ('sk-','api_key','authorization','token=')):
            return value
    except Exception:pass
    return ''

def output_budget(total_tokens,configured):
    # Reserve room for prompt/history and later tool turns in small work orders.
    return min(configured,16384,max(256,total_tokens//4))

def install():
    """Wrap stable loop boundaries without replacing tool/lease/recovery semantics."""
    import deepseek_mcp.agent_loop as loop
    import deepseek_mcp.job_manager as jobs
    import deepseek_mcp.server as server
    if getattr(loop,'_control_center_installed',False):return
    initialize()
    max_tokens=int(os.environ.get('DS_CC_MAX_TOKENS','120000'))
    max_turns=int(os.environ.get('DS_CC_MAX_TURNS','20'))
    max_seconds=int(os.environ.get('DS_CC_MAX_SECONDS','600'))
    if not (1000<=max_tokens<=1000000 and 1<=max_turns<=50 and 10<=max_seconds<=18000):
        raise RuntimeError('invalid controller budgets')
    loop.MAX_TOTAL_TOKENS_PER_RUN=max_tokens
    loop.MAX_PROVIDER_HISTORY_BYTES=min(loop.MAX_PROVIDER_HISTORY_BYTES,1024*1024)
    original_run=loop.run_agent
    original_turn=loop._run_turn
    original_response=loop._record_response
    original_tool=loop._execute_one_tool

    @functools.wraps(original_run)
    def run(task,config,**kwargs):
        if config.model!='deepseek-flash':raise RuntimeError('controller policy requires Flash; model change needs user approval')
        config.max_turns=min(config.max_turns,max_turns)
        config.max_run_seconds=min(config.max_run_seconds,max_seconds)
        config.max_output_tokens=output_budget(max_tokens,config.max_output_tokens)
        LOCAL.run_id=str(uuid.uuid4())
        LOCAL.task_id=os.environ.get('DS_CC_TASK_ID') or None
        now=time.time()
        with connection() as c:
            c.execute('INSERT INTO rt_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
              (LOCAL.run_id,LOCAL.task_id,int(os.environ.get('DS_CC_ATTEMPT','0')),str(config.workspace),
               config.model,None,'starting',now,None,now,None,None,0,None,os.getpid()))
        _safe_emit('run_started',{'max_tokens':max_tokens,'max_turns':config.max_turns,'max_seconds':config.max_run_seconds})
        try:
            result=original_run(task,config,**kwargs)
            _safe_emit('run_delivered',phase='delivered',finished_at=time.time(),tool_name=None,tool_path=None)
            return result
        except BaseException as error:
            _safe_emit('run_failed',{'error_type':type(error).__name__},phase='failed',finished_at=time.time(),error_code=type(error).__name__)
            raise
        finally:
            LOCAL.run_id=LOCAL.task_id=None

    @functools.wraps(original_turn)
    def turn(state,number):
        LOCAL.request_key='turn:'+str(number+1)
        _safe_emit('turn_started',phase='request',turn=number+1,tool_name=None,tool_path=None)
        return original_turn(state,number)

    @functools.wraps(original_response)
    def response(state,value,request_bytes=0):
        try:record_usage(state,value)
        except Exception:
            if getattr(LOCAL,'task_id',None):raise RuntimeError('usage persistence failed') from None
        return original_response(state,value,request_bytes)

    @functools.wraps(original_tool)
    def tool(config,call,number,**kwargs):
        name=call.function.name if call.function.name in config.allowed_tools else 'unknown'
        started=time.monotonic()
        _safe_emit('tool_started',{'name':name},phase='tool',tool_name=name,tool_path=_path_hint(call),turn=number+1)
        try:
            result=original_tool(config,call,number,**kwargs)
            status='error' if isinstance(result,str) and result.startswith('ERROR:') else 'returned'
            _safe_emit('tool_finished',{'name':name,'status':status,'duration_seconds':round(time.monotonic()-started,3)},phase='response')
            return result
        except BaseException as error:
            _safe_emit('tool_failed',{'name':name,'error_type':type(error).__name__},phase='response')
            raise

    loop.run_agent=run
    jobs.run_agent=run
    server.run_agent=run
    loop._run_turn=turn
    loop._record_response=response
    loop._execute_one_tool=tool
    loop._control_center_installed=True

def snapshot():
    initialize()
    with connection() as c:
        runs=[dict(x) for x in c.execute('''SELECT r.*,COALESCE(u.prompt_tokens,0) prompt_tokens,
          COALESCE(u.completion_tokens,0) completion_tokens,COALESCE(u.cached_tokens,0) cached_tokens,
          COALESCE(u.prompt_tokens+u.completion_tokens,0) total_tokens FROM rt_runs r LEFT JOIN
          (SELECT run_id,SUM(prompt_tokens) prompt_tokens,SUM(completion_tokens) completion_tokens,
          SUM(cached_tokens) cached_tokens FROM rt_usage GROUP BY run_id) u ON r.run_id=u.run_id
          ORDER BY r.started_at DESC LIMIT 100''')]
        totals=dict(c.execute('''SELECT COALESCE(SUM(prompt_tokens),0) prompt_tokens,
          COALESCE(SUM(completion_tokens),0) completion_tokens,COALESCE(SUM(cached_tokens),0) cached_tokens,
          COALESCE(SUM(prompt_tokens+completion_tokens),0) total_tokens FROM rt_usage''').fetchone())
        events=[]
        for row in c.execute('SELECT at,run_id,task_id,kind,data FROM rt_events ORDER BY at DESC LIMIT 100'):
            event=dict(row);event['data']=json.loads(event['data']);events.append(event)
    return runs,totals,events
