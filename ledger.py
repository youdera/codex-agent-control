"""Durable tasks and cooperative resource leases; never self-accept/retry."""
import hashlib
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

class Ledger:
    def __init__(self,path):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as c:
            c.execute('PRAGMA journal_mode=WAL')
            c.executescript('''CREATE TABLE IF NOT EXISTS cc_tasks(
              id TEXT PRIMARY KEY,idem TEXT UNIQUE,spec_hash TEXT,spec TEXT,state TEXT,attempt INTEGER,
              released INTEGER,pid INTEGER,recovery_pending INTEGER,created_at REAL,updated_at REAL,
              delivery TEXT,review TEXT);
              CREATE TABLE IF NOT EXISTS cc_attempts(task_id TEXT,attempt INTEGER,state TEXT,started_at REAL,
              finished_at REAL,delivery TEXT,error_code TEXT,PRIMARY KEY(task_id,attempt));
              CREATE TABLE IF NOT EXISTS cc_task_events(id TEXT PRIMARY KEY,at REAL,task_id TEXT,kind TEXT,data TEXT);
              CREATE TABLE IF NOT EXISTS cc_resources(name TEXT PRIMARY KEY,owner TEXT,external INTEGER);''')
    @contextmanager
    def connect(self):
        c=sqlite3.connect(self.path,timeout=15);c.row_factory=sqlite3.Row
        c.execute('PRAGMA busy_timeout=15000')
        try:
            with c:yield c
        finally:c.close()
    def _task(self,row):
        if row is None:raise KeyError('task not found')
        d=dict(row);s=json.loads(d.pop('spec'));d['spec']=s
        d.update({k:s[k] for k in ('title','workspace','readonly','dependencies','resources','budgets','scope','max_attempts','coordinator')})
        d['released']=bool(d['released']);d['review']=json.loads(d['review']) if d['review'] else None
        d.pop('spec_hash',None);d.pop('idem',None);return d
    def _event(self,c,id,kind,data=None):
        c.execute('INSERT INTO cc_task_events VALUES(?,?,?,?,?)',(str(uuid.uuid4()),time.time(),id,kind,json.dumps(data or {},ensure_ascii=True)))
    def create(self,spec):
        spec=dict(spec)
        allowed={'id','idempotency_key','title','instruction','context','workspace','readonly','dependencies','resources','budgets','scope','max_attempts','coordinator','alignment'}
        if set(spec)-allowed:raise ValueError('unknown task field')
        if 'alignment' in spec:
            from alignment import validate
            validate(spec['alignment'])
        for k in ('title','instruction','workspace'):
            if not isinstance(spec.get(k),str) or not spec[k].strip():raise ValueError('missing '+k)
        p=Path(spec['workspace'])
        if not p.is_absolute() or not p.is_dir():raise ValueError('workspace must be existing absolute directory')
        # Reject credential/control roots at submission too; upstream remains authoritative.
        from deepseek_mcp.safety import is_unsafe_workspace_root
        if is_unsafe_workspace_root(p.resolve()):raise ValueError('workspace is a protected credential/control directory')
        spec['workspace']=os.path.normcase(os.path.realpath(p))
        spec.setdefault('readonly',True);spec.setdefault('context','');spec.setdefault('coordinator','制作统筹')
        if type(spec['readonly']) is not bool:raise ValueError('readonly must be boolean')
        if not isinstance(spec['context'],str) or len(spec['context'])+len(spec['instruction'])>30000:raise ValueError('context budget exceeded')
        if spec['coordinator']!='制作统筹':raise ValueError('coordinator must be 制作统筹')
        for k in ('dependencies','resources','scope'):
            spec.setdefault(k,[])
            if not isinstance(spec[k],list) or any(not isinstance(x,str) or not x.strip() for x in spec[k]) or len(set(spec[k]))!=len(spec[k]):raise ValueError('invalid '+k)
        b={'max_tokens':120000,'max_turns':20,'max_seconds':600};supplied=spec.get('budgets',{})
        if not isinstance(supplied,dict) or set(supplied)-set(b):raise ValueError('invalid budgets')
        b.update(supplied)
        for k,lo,hi in (('max_tokens',1000,1000000),('max_turns',1,50),('max_seconds',10,18000)):
            if type(b[k]) is not int or not lo<=b[k]<=hi:raise ValueError('invalid '+k)
        spec['budgets']=b;spec.setdefault('max_attempts',3)
        if type(spec['max_attempts']) is not int or not 1<=spec['max_attempts']<=3:raise ValueError('invalid max_attempts')
        requested_id=spec.get('id');key=spec.get('idempotency_key') or requested_id or str(uuid.uuid4())
        if not isinstance(key,str) or len(key)>200:raise ValueError('invalid idempotency key')
        digest=hashlib.sha256(json.dumps(spec,sort_keys=True,ensure_ascii=True).encode()).hexdigest()
        id=requested_id or str(uuid.uuid4());now=time.time()
        if not isinstance(id,str) or not id or len(id)>100 or any(x not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for x in id):raise ValueError('invalid ID')
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            old=c.execute('SELECT * FROM cc_tasks WHERE idem=?',(key,)).fetchone()
            if old:
                if old['spec_hash']!=digest:raise ValueError('idempotency conflict')
                return self._task(old)
            for dep in spec['dependencies']:
                if dep==id or not c.execute('SELECT 1 FROM cc_tasks WHERE id=?',(dep,)).fetchone():raise ValueError('unknown dependency')
            c.execute('INSERT INTO cc_tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',(id,key,digest,json.dumps(spec,ensure_ascii=True),'queued',0,1,None,0,now,now,None,None))
            self._event(c,id,'queued')
        return self.get(id)
    def get(self,id):
        with self.connect() as c:return self._task(c.execute('SELECT * FROM cc_tasks WHERE id=?',(id,)).fetchone())
    def list_tasks(self,limit=100):
        with self.connect() as c:return [self._task(x) for x in c.execute('SELECT * FROM cc_tasks ORDER BY created_at DESC LIMIT ?',(max(1,min(limit,500)),))]
    def claim_next(self,max_parallel=3):
        if type(max_parallel)is not int or not 1<=max_parallel<=3:raise ValueError('invalid parallelism')
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if c.execute("SELECT COUNT(*) FROM cc_tasks WHERE state='running'").fetchone()[0]>=max_parallel:return None
            rows=c.execute('SELECT * FROM cc_tasks ORDER BY created_at').fetchall()
            tasks=[self._task(x) for x in rows];states={t['id']:t['state'] for t in tasks}
            held={r[0] for r in c.execute('SELECT name FROM cc_resources')}
            for t in tasks:
                if t['state']!='queued' or any(states.get(d)!='accepted' for d in t['dependencies']):continue
                if any(x['id']!=t['id'] and not x['released'] and x['state']!='queued' and x['workspace']==t['workspace'] for x in tasks):continue
                if set(t['resources'])&held:continue
                if t['attempt']>=t['max_attempts']:continue
                a=t['attempt']+1;now=time.time()
                c.execute("UPDATE cc_tasks SET state='running',attempt=?,released=0,pid=NULL,updated_at=? WHERE id=?",(a,now,t['id']))
                c.execute('INSERT INTO cc_attempts VALUES(?,?,?,?,?,?,?)',(t['id'],a,'running',now,None,None,None))
                for r in t['resources']:c.execute('INSERT INTO cc_resources VALUES(?,?,0)',(r,t['id']))
                self._event(c,t['id'],'running',{'attempt':a})
                return self._task(c.execute('SELECT * FROM cc_tasks WHERE id=?',(t['id'],)).fetchone())
        return None
    def _current(self,c,id,attempt,running=True):
        t=self._task(c.execute('SELECT * FROM cc_tasks WHERE id=?',(id,)).fetchone())
        if t['attempt']!=attempt or (running and t['state']!='running'):raise ValueError('stale task attempt')
        return t
    def set_pid(self,id,pid,attempt):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE');self._current(c,id,attempt)
            c.execute('UPDATE cc_tasks SET pid=?,updated_at=? WHERE id=?',(pid,time.time(),id))
    def finish(self,id,state,delivery=None,error_code=None,recovery_pending=0,attempt=None):
        if state not in ('delivered','failed','cancelled','interrupted'):raise ValueError('invalid finish state')
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE');t=self._current(c,id,attempt);now=time.time()
            c.execute('UPDATE cc_tasks SET state=?,released=?,recovery_pending=?,updated_at=?,delivery=? WHERE id=?',
              (state,int(t['readonly'] and recovery_pending==0),recovery_pending,now,delivery,id))
            c.execute('UPDATE cc_attempts SET state=?,finished_at=?,delivery=?,error_code=? WHERE task_id=? AND attempt=?',(state,now,delivery,error_code,id,attempt))
            c.execute('DELETE FROM cc_resources WHERE owner=? AND external=0',(id,));self._event(c,id,state,{'attempt':attempt,'error_code':error_code})
    def review(self,id,decision,evidence,reviewer):
        if decision not in ('accepted','rework','closed'):raise ValueError('invalid decision')
        raw=Path(evidence['path']).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=evidence['sha256']:raise ValueError('evidence hash mismatch')
        receipt=json.loads(raw)
        if not isinstance(receipt,dict) or receipt.get('task_id')!=id or receipt.get('decision')!=decision or receipt.get('reviewer')!=reviewer or not isinstance(receipt.get('checks'),list) or not receipt['checks']:raise ValueError('invalid evidence receipt')
        hashes=receipt.get('artifact_hashes')
        if not isinstance(hashes,dict):raise ValueError('missing artifact hashes')
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE');t=self._task(c.execute('SELECT * FROM cc_tasks WHERE id=?',(id,)).fetchone())
            if reviewer!=t['coordinator']:raise ValueError('wrong reviewer')
            if receipt.get('attempt')!=t['attempt']:raise ValueError('evidence belongs to another attempt')
            allowed=('delivered',) if decision=='accepted' else ('delivered','failed','cancelled','interrupted','rework')
            if t['state'] not in allowed:raise ValueError('task is not reviewable')
            if decision=='accepted' and (t['recovery_pending'] or (not t['readonly'] and not hashes)):raise ValueError('recovery or artifact review required')
            if decision=='accepted' and t['delivery'] and str(Path(t['delivery']).resolve()) not in hashes:raise ValueError('delivery hash required in acceptance evidence')
            if decision=='closed' and t['recovery_pending']:raise ValueError('recovery required before closing')
            for path,digest in hashes.items():
                p=Path(path)
                if not p.is_absolute() or not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest()!=digest:raise ValueError('artifact changed since review')
            c.execute('UPDATE cc_tasks SET state=?,released=?,review=?,updated_at=? WHERE id=?',(decision,int(decision in ('accepted','closed') or t['readonly']),json.dumps({'receipt':evidence,'reviewer':reviewer,'decision':decision},ensure_ascii=True),time.time(),id))
            self._event(c,id,'review_'+decision,{'reviewer':reviewer,'evidence_sha256':evidence['sha256']})
        return self.get(id)
    def cancel_queued(self,id):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE');t=self._task(c.execute('SELECT * FROM cc_tasks WHERE id=?',(id,)).fetchone())
            if t['state']!='queued':return False
            c.execute("UPDATE cc_tasks SET state='cancelled',released=1,updated_at=? WHERE id=?",(time.time(),id))
            self._event(c,id,'cancelled',{'attempt':t['attempt'],'before_dispatch':True})
        return True
    def requeue(self,id,reason):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE');t=self._task(c.execute('SELECT * FROM cc_tasks WHERE id=?',(id,)).fetchone())
            if t['state']!='rework' or t['attempt']>=t['max_attempts'] or t['recovery_pending'] or not reason.strip():raise ValueError('requeue not allowed')
            c.execute("UPDATE cc_tasks SET state='queued',released=1,pid=NULL,updated_at=? WHERE id=?",(time.time(),id));self._event(c,id,'requeued',{'reason':reason[:5000]})
        return self.get(id)
    def clear_recovery(self,id,attempt):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE');t=self._current(c,id,attempt,False)
            if t['state']=='running':raise ValueError('cannot clear running recovery')
            c.execute('UPDATE cc_tasks SET recovery_pending=0,updated_at=? WHERE id=?',(time.time(),id));self._event(c,id,'recovery_acknowledged')
    def reserve(self,name,owner):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE');r=c.execute('SELECT * FROM cc_resources WHERE name=?',(name,)).fetchone()
            if r and (r['owner']!=owner or not r['external']):raise ValueError('resource busy')
            c.execute('INSERT OR IGNORE INTO cc_resources VALUES(?,?,1)',(name,owner));self._event(c,None,'resource_reserved',{'name':name,'owner':owner})
    def release_reservation(self,name,owner):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE');r=c.execute('SELECT * FROM cc_resources WHERE name=?',(name,)).fetchone()
            if not r or not r['external'] or r['owner']!=owner:raise ValueError('not reservation owner')
            c.execute('DELETE FROM cc_resources WHERE name=?',(name,));self._event(c,None,'resource_released',{'name':name,'owner':owner})
    def resources(self):
        with self.connect() as c:return [dict(x) for x in c.execute('SELECT * FROM cc_resources')]
    def events(self,task_id=None,limit=100):
        with self.connect() as c:
            rows=c.execute('SELECT * FROM cc_task_events '+('WHERE task_id=? ' if task_id else '')+'ORDER BY at DESC LIMIT ?',((task_id,) if task_id else ())+(max(1,min(limit,500)),))
            result=[]
            for row in rows:
                d=dict(row);d['data']=json.loads(d['data']);result.append(d)
            return result
    def recover_orphans(self,live_pids):
        recovered=[]
        for t in self.list_tasks(500):
            if t['state']=='running' and t['pid'] not in live_pids:
                try:self.finish(t['id'],'interrupted',error_code='runner_not_alive',recovery_pending=0 if t['readonly'] else 1,attempt=t['attempt']);recovered.append(t['id'])
                except ValueError:pass
        return recovered
