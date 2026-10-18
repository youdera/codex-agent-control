import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
sys.path.insert(0,str(Path(__file__).resolve().parent))
from workflow import Guard,WorkflowStop,digest,packet,seal,validate_repair
from ledger import Ledger

def call(name,**args):return SimpleNamespace(function=SimpleNamespace(name=name,arguments=json.dumps(args)))

class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);(self.root/'input.json').write_text('{"action":{"frames":80,"fps":60},"errors":["struct"]}',encoding='utf-8')
        self.spec={'id':'one','workspace':str(self.root),'title':'one','instruction':'bounded facts','readonly':True,'execution':{'inputs':['input.json']}}
        self.spec['alignment']={'mode':'existing-contract','goal':'Read known facts','basis':'Test existing agreement','deliverables':['facts'],'acceptance':['exact source facts'],'constraints':['readonly'],'decisions':['no gameplay pass'],'open_questions':[]}
    def tearDown(self):self.tmp.cleanup()
    def test_seal_refuses_large_directory_and_changed_inputs_before_provider(self):
        prepared=seal(self.spec);self.assertEqual(prepared['max_attempts'],2)
        (self.root/'input.json').write_text('changed',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'changed'):seal(prepared)
        with self.assertRaises(ValueError):seal(dict(self.spec,execution={'inputs':['.']}))
        (self.root/'big').write_text('x'*12001)
        with self.assertRaisesRegex(ValueError,'packet budget'):seal(dict(self.spec,execution={'inputs':['big']}))
        with self.assertRaises(ValueError):seal(dict(self.spec,execution={'inputs':['../escape']}))
    def test_packet_preserves_selected_objects_and_does_not_imply_absence(self):
        result=packet({'sources':[{'path':str(self.root/'input.json'),'fields':[{'id':'action','pointer':'/action'},{'id':'errors','pointer':'/errors'}]}]})
        self.assertEqual(result['facts'][0]['value'],{'frames':80,'fps':60})
        self.assertEqual(result['facts'][1]['value'],['struct']);self.assertIn('omitted does not mean absent',result['coverage'])
        self.assertEqual(result['facts'][0]['sha256'],digest(self.root/'input.json'))
        with self.assertRaises(KeyError):packet({'sources':[{'path':str(self.root/'input.json'),'fields':[{'id':'missing','pointer':'/missing'}]}]})
    def test_readonly_never_gains_commands_or_outputs(self):
        for key in ('outputs','commands'):
            with self.assertRaises(ValueError):seal(dict(self.spec,execution={'inputs':['input.json'],key:['x']}))
    def test_guard_blocks_unplanned_command_read_and_write(self):
        guard=Guard(self.root,seal(self.spec)['execution'])
        for request in (call('Bash',command='echo probe'),call('Read',path='unlisted'),call('Write',path='input.json',content='bad')):
            with self.assertRaises(WorkflowStop):guard.before(request)
        self.assertIn('action',(self.root/'input.json').read_text())
    def test_declared_command_still_has_output_limit_and_cannot_append_a_shell_command(self):
        plan=seal(dict(self.spec,readonly=False,execution={'inputs':['input.json'],'commands':['python verify.py']}))['execution'];guard=Guard(self.root,plan)
        request=call('Bash',command='python verify.py');guard.before(request);self.assertEqual(guard.after(request,'PASS'),'PASS')
        with self.assertRaisesRegex(WorkflowStop,'command_output_limit'):guard.after(request,'x'*8001)
        with self.assertRaisesRegex(WorkflowStop,'unplanned_command'):guard.before(call('Bash',command='python verify.py && echo extra'))
    def test_repeated_edit_error_stops_on_second_occurrence(self):
        spec=dict(self.spec,readonly=False,execution={'inputs':['input.json'],'outputs':['input.json']});guard=Guard(self.root,seal(spec)['execution']);request=call('Edit',path='input.json',old_string='missing',new_string='fixed')
        guard.before(request);guard.after(request,'ERROR: old_string not found in input.json')
        guard.before(request)
        with self.assertRaisesRegex(WorkflowStop,'repeated_tool_error:edit_no_match'):guard.after(request,'ERROR: old_string not found in input.json')
    def test_cumulative_read_and_tool_limits_bound_repeated_exploration(self):
        plan=seal(self.spec)['execution'];plan.update(max_read_bytes=100,max_tool_calls=2);guard=Guard(self.root,plan);request=call('Read',path='input.json')
        guard.before(request);guard.after(request,'x'*60)
        with self.assertRaises(WorkflowStop):guard.before(request)
        guard=Guard(self.root,plan);guard.before(request);guard.before(request)
        with self.assertRaisesRegex(WorkflowStop,'tool_call_limit'):guard.before(request)
    def repair(self):
        delivery=self.root/'delivery.json';delivery.write_text('{}');proof=self.root/'proof.json';proof.write_text('{"changed":"new small packet"}')
        task={'id':'one','attempt':1,'delivery':str(delivery),'spec':seal(self.spec)}
        (self.root/'compact.json').write_text('{"exact_fact":80}')
        repair={'task_id':'one','attempt':1,'failure_class':'input','changed_conditions':['new compact input'],'instruction':'Read only new packet','execution':{'inputs':['compact.json']},'artifact_hashes':{str(delivery):digest(delivery),str(proof):digest(proof)}}
        return task,repair,proof
    def test_repair_rejects_fact_errors_stale_evidence_and_repeated_failure(self):
        task,repair,proof=self.repair();self.assertIn('execution',validate_repair(repair,task,[]))
        with self.assertRaisesRegex(ValueError,'controller takeover'):validate_repair(dict(repair,failure_class='factual'),task,[])
        with self.assertRaisesRegex(ValueError,'already retried'):validate_repair(repair,task,[repair])
        with self.assertRaisesRegex(ValueError,'stale'):validate_repair(dict(repair,attempt=0),task,[])
        with self.assertRaisesRegex(ValueError,'unchanged'):validate_repair(dict(repair,execution=task['spec']['execution']),task,[])
        proof.write_text('changed')
        with self.assertRaisesRegex(ValueError,'changed'):validate_repair(repair,task,[])
    def test_new_orders_cannot_turn_retry_into_third_attempt(self):
        with self.assertRaisesRegex(ValueError,'one repaired retry'):seal(dict(self.spec,max_attempts=3))
    def test_task_budget_refuses_requeue_without_another_provider_call(self):
        ledger=Ledger(self.root/'ledger.db');task=ledger.create(self.spec);task=ledger.claim_next();delivery=self.root/'delivery.json';delivery.write_text('{}');ledger.finish(task['id'],'failed',str(delivery),attempt=1)
        evidence=self.root/'review.json';evidence.write_text(json.dumps({'task_id':'one','attempt':1,'decision':'rework','reviewer':'制作统筹','failure_class':'input','checks':['known input problem'],'artifact_hashes':{}}),encoding='utf-8');ledger.review('one','rework',{'path':str(evidence),'sha256':digest(evidence)},'制作统筹')
        t,repair,proof=self.repair();repair['artifact_hashes'][str(delivery)]=digest(delivery)
        with mock.patch.object(ledger,'spent_tokens',return_value=28000):
            with self.assertRaisesRegex(ValueError,'remaining task budget'):ledger.requeue('one','changed packet',repair)
        self.assertEqual(ledger.get('one')['state'],'rework')
    def test_acceptance_requires_every_criterion_current_hash_and_explicit_scope(self):
        ledger=Ledger(self.root/'ledger.db');ledger.create(self.spec);ledger.claim_next();delivery=self.root/'attempt-1.json';delivery.write_text('{}');ledger.finish('one','delivered',str(delivery),attempt=1)
        receipt={'task_id':'one','attempt':1,'decision':'accepted','reviewer':'制作统筹','checks':['independent exact source check'],'artifact_hashes':{str(delivery):digest(delivery)},'adoption_scope':'Static fact extraction only','criteria':[{'criterion':'exact source facts','result':'unknown','evidence':[str(delivery)]}]}
        path=self.root/'accepted.json'
        def review():
            path.write_text(json.dumps(receipt),encoding='utf-8');return ledger.review('one','accepted',{'path':str(path),'sha256':digest(path)},'制作统筹')
        with self.assertRaisesRegex(ValueError,'cannot be accepted'):review()
        receipt['criteria'][0]['result']='pass';receipt['criteria'][0]['criterion']='easier substitute'
        with self.assertRaisesRegex(ValueError,'every agreed criterion'):review()
        receipt['criteria'][0]['criterion']='exact source facts';self.assertEqual(review()['state'],'accepted')
    def test_cli_prepares_and_submits_only_bounded_orders_without_provider(self):
        source=self.root/'order.json';source.write_text(json.dumps(self.spec),encoding='utf-8');prepared=self.root/'prepared.json'
        env=dict(os.environ,DS_CC_HOME=str(self.root/'cli-runtime'))
        script=str(Path(__file__).resolve().parent/'control_center.py')
        for args in (['prepare-order',str(source),str(prepared)],['submit',str(prepared)]):
            result=subprocess.run([sys.executable,'-I','-X','utf8',script,*args],env=env,capture_output=True,text=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stderr)
        ledger=Ledger(self.root/'cli-runtime/control.sqlite3');self.assertEqual(ledger.get('one')['state'],'queued');self.assertEqual(ledger.get('one')['attempt'],0)
    def test_actual_installed_wrapper_stops_repeated_error_and_keeps_private_text_out_of_events(self):
        code=r'''
import sys,os,json
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import deepseek_mcp.agent_loop as loop
from types import SimpleNamespace
from workflow import seal,WorkflowStop
from telemetry import install,snapshot
root=Path(sys.argv[2]);calls=[]
plan=seal({'workspace':str(root),'readonly':False,'execution':{'inputs':['input.json'],'outputs':['input.json']}})['execution']
os.environ['DS_CC_EXECUTION']=json.dumps(plan)
def original_tool(*a,**k):calls.append(1);return 'ERROR: old_string not found in PRIVATE_SENTINEL'
def original_run(task,config,**kw):
 request=SimpleNamespace(function=SimpleNamespace(name='Edit',arguments=json.dumps({'path':'input.json','old_string':'absent','new_string':'fixed'})))
 for _ in range(5):loop._execute_one_tool(config,request,0)
loop._execute_one_tool=original_tool;loop.run_agent=original_run;install()
import deepseek_mcp.job_manager as jobs
config=SimpleNamespace(model='deepseek-flash',max_turns=20,max_run_seconds=60,max_output_tokens=16000,workspace=root,allowed_tools=['Edit'])
try:jobs.run_agent('test',config)
except WorkflowStop:pass
else:raise AssertionError('no stop')
assert len(calls)==2
runs,totals,events=snapshot();assert runs[0]['phase']=='failed' and totals['total_tokens']==0
assert 'PRIVATE_SENTINEL' not in json.dumps(events)
print('installed guard: stopped after two errors; zero provider calls')
'''
        env=dict(os.environ,DS_CC_HOME=str(self.root/'runtime'),DS_CC_TASK_ID='guard-test')
        result=subprocess.run([sys.executable,'-I','-X','utf8','-c',code,str(Path(__file__).resolve().parent),str(self.root)],env=env,capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr);self.assertIn('zero provider calls',result.stdout)

if __name__=='__main__':unittest.main(verbosity=2)
