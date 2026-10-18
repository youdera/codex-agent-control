"""Bounded dispatch, deterministic evidence selection and evidence-backed repair."""
import copy
import hashlib
import json
from pathlib import Path

class WorkflowStop(RuntimeError):pass

def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def resolve(root,name):
    if not isinstance(name,str) or not name or Path(name).is_absolute():raise ValueError('use exact workspace-relative paths')
    path=(Path(root)/name).resolve()
    if not path.is_relative_to(Path(root).resolve()):raise ValueError('execution path escapes workspace')
    return path

def pointer(value,selector):
    if selector=='':return value
    if not isinstance(selector,str) or not selector.startswith('/'):raise ValueError('invalid JSON pointer')
    for key in selector[1:].split('/'):
        key=key.replace('~1','/').replace('~0','~')
        value=value[int(key)] if isinstance(value,list) else value[key]
    return value

def packet(selection):
    facts=[];seen=set()
    for source in selection['sources']:
        path=Path(source['path'])
        if not path.is_file() or path.stat().st_size>32*1024*1024:raise ValueError('source size/path invalid')
        raw=path.read_bytes();value=json.loads(raw.decode('utf-8-sig'));sha=hashlib.sha256(raw).hexdigest()
        for item in source['fields']:
            if item['id'] in seen:raise ValueError('duplicate fact id')
            seen.add(item['id']);facts.append({'id':item['id'],'source':path.name,'sha256':sha,
                'pointer':item['pointer'],'value':pointer(value,item['pointer'])})
    result={'coverage':'selected_fields_only; omitted does not mean absent; clocks/units/rig direction require controller review','facts':facts}
    if len(json.dumps(result,ensure_ascii=False,indent=2).encode())+1>12000:raise ValueError('packet exceeds 12000 bytes; select fewer exact fields')
    return result

def seal(spec):
    spec=copy.deepcopy(spec);plan=spec.get('execution')
    if not isinstance(plan,dict):raise ValueError('execution plan required; declare exact inputs/outputs/commands')
    if set(plan)-{'inputs','outputs','commands','max_read_bytes','max_tool_calls','max_output_tokens','max_task_tokens'}:raise ValueError('unknown execution field')
    defaults={'max_read_bytes':12000 if spec.get('readonly',True) else 24000,
        'max_tool_calls':4 if spec.get('readonly',True) else 12,
        'max_output_tokens':4096 if spec.get('readonly',True) else 8192,
        'max_task_tokens':30000 if spec.get('readonly',True) else 60000,'outputs':[],'commands':[]}
    for key,value in defaults.items():plan.setdefault(key,value)
    for key,low,high in [('max_read_bytes',1000,64000),('max_tool_calls',1,24),('max_output_tokens',256,8192),('max_task_tokens',5000,120000)]:
        if type(plan[key]) is not int or not low<=plan[key]<=high:raise ValueError('invalid '+key)
    if not isinstance(plan.get('inputs'),list) or not 1<=len(plan['inputs'])<=16:raise ValueError('declare 1-16 exact input files')
    total=0;inputs=[];seen=set()
    for item in plan['inputs']:
        name=item if isinstance(item,str) else item['path'];path=resolve(spec['workspace'],name)
        if path in seen or not path.is_file():raise ValueError('input must be a unique existing file')
        seen.add(path);total+=path.stat().st_size
        if total>plan['max_read_bytes']:raise ValueError('input bytes exceed packet budget; select fields/excerpts before dispatch')
        sha=digest(path)
        if isinstance(item,dict) and item.get('sha256')!=sha:raise ValueError('input changed since preparation')
        inputs.append({'path':name,'sha256':sha,'bytes':path.stat().st_size})
    plan['inputs']=inputs
    for field in ('outputs','commands'):
        if not isinstance(plan[field],list) or any(not isinstance(s,str) or not s.strip() for s in plan[field]) or len(set(plan[field]))!=len(plan[field]):raise ValueError('invalid '+field)
    for name in plan['outputs']:resolve(spec['workspace'],name)
    if spec.get('readonly',True) and (plan['outputs'] or plan['commands']):raise ValueError('readonly plan cannot write or run commands')
    spec.setdefault('max_attempts',2)
    if spec['max_attempts']>2:raise ValueError('new orders allow at most one repaired retry; controller takeover after that')
    return spec

def validate_repair(repair,task,prior_repairs):
    if not isinstance(repair,dict):raise ValueError('structured repair receipt required')
    if repair.get('failure_class') not in ('input','tool','budget'):raise ValueError('factual/capability failures require controller takeover, not another model retry')
    if any(r.get('failure_class')==repair['failure_class'] for r in prior_repairs):raise ValueError('same failure class already retried')
    if repair.get('task_id')!=task['id'] or repair.get('attempt')!=task['attempt']:raise ValueError('stale repair receipt')
    if not isinstance(repair.get('changed_conditions'),list) or not repair['changed_conditions']:raise ValueError('missing concrete changed conditions')
    hashes=repair.get('artifact_hashes')
    if not isinstance(hashes,dict) or not hashes:raise ValueError('repair proof hashes required')
    if task.get('delivery') not in hashes:raise ValueError('current delivery hash required in repair')
    if len(hashes)<2:raise ValueError('repair needs independent changed-condition evidence, not only the failure receipt')
    for name,sha in hashes.items():
        if not Path(name).is_absolute() or digest(name)!=sha:raise ValueError('repair proof changed')
    if not repair.get('instruction') or len(repair['instruction'])>5000:raise ValueError('replacement repair instruction required')
    spec=dict(task['spec'],execution=repair['execution']);prepared=seal(spec)
    old=task['spec'].get('execution');new=prepared['execution']
    if old:
        for key in ('max_task_tokens','max_read_bytes','max_tool_calls','max_output_tokens'):
            if new[key]>old[key]:raise ValueError('repair may not increase '+key)
        for key in ('outputs','commands'):
            if not set(new[key])<=set(old[key]):raise ValueError('repair may not expand '+key)
        if repair['failure_class'] in ('input','budget') and new==old:raise ValueError('unchanged execution plan is not a repair')
    result=copy.deepcopy(repair);result['execution']=new;return result

def error_code(result):
    if not isinstance(result,str) or not result.startswith('ERROR:'):return None
    # Store categories only, never tool arguments, content, commands or raw errors.
    for text,code in [('old_string not found','edit_no_match'),('old_string appears','edit_not_unique'),
        ('already exists','write_existing_file'),('missing required','invalid_arguments'),
        ('not allowed','not_allowed'),('failed to write','write_failed'),('file not found','file_missing')]:
        if text in result:return code
    return 'tool_error'

class Guard:
    def __init__(self,workspace,plan):
        self.root=Path(workspace).resolve();self.plan=plan;self.calls=0;self.read_bytes=0;self.errors={}
        self.reads={resolve(self.root,x['path']) for x in plan['inputs']};self.writes={resolve(self.root,x) for x in plan['outputs']}
    def before(self,call):
        self.calls+=1
        if self.calls>self.plan['max_tool_calls']:raise WorkflowStop('tool_call_limit; controller review required')
        name=call.function.name;args=json.loads(call.function.arguments)
        if name=='Bash':
            if args.get('command') not in self.plan['commands']:raise WorkflowStop('unplanned_command; controller review required')
            return
        if name not in ('Read','Edit','Write'):raise WorkflowStop('unplanned_tool; use declared exact files')
        path=resolve(self.root,args.get('path',''))
        if name=='Read':
            if path not in self.reads|self.writes:raise WorkflowStop('unplanned_read')
            if path.stat().st_size+self.read_bytes>self.plan['max_read_bytes']:raise WorkflowStop('read_byte_limit')
        elif path not in self.writes:raise WorkflowStop('unplanned_write')
    def after(self,call,result):
        code=error_code(result);name=call.function.name
        if name=='Bash' and len(result.encode('utf-8'))>8000:raise WorkflowStop('command_output_limit; controller must prepare a bounded result')
        if name=='Read' and not code:
            self.read_bytes+=len(result.encode('utf-8'))
            if self.read_bytes>self.plan['max_read_bytes']:raise WorkflowStop('read_byte_limit')
        if code:
            args=json.loads(call.function.arguments);key=(name,args.get('path',''),code)
            self.errors[key]=self.errors.get(key,0)+1
            if self.errors[key]>=2:raise WorkflowStop('repeated_tool_error:'+code+'; preserve mutations and return to controller')
        return result
