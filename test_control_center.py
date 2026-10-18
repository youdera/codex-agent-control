import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import subprocess
from types import SimpleNamespace
import unittest
from unittest import mock
sys.path.insert(0,str(Path(__file__).resolve().parent))
from ledger import Ledger
import telemetry

class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.a=self.root/'a';self.b=self.root/'b';self.a.mkdir();self.b.mkdir();self.db=self.root/'db.sqlite';self.l=Ledger(self.db)
    def tearDown(self):self.tmp.cleanup()
    def task(self,id,workspace=None,**kw):
        return self.l.create({'id':id,'title':id,'instruction':'Read x only','workspace':str(workspace or self.a),**kw})
    def receipt(self,t,decision='accepted',**kw):
        path=self.root/(t['id']+'-receipt.json');hashes={}
        if t['delivery']:hashes[t['delivery']]=hashlib.sha256(Path(t['delivery']).read_bytes()).hexdigest()
        d={'task_id':t['id'],'attempt':t['attempt'],'decision':decision,'reviewer':'制作统筹','checks':['independently checked artifact'],'artifact_hashes':hashes};d.update(kw);path.write_text(json.dumps(d),encoding='utf-8')
        return {'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
    def deliver(self,t,state='delivered',pending=0):
        path=self.root/(t['id']+str(t['attempt'])+'.json');path.write_text('{}',encoding='utf-8');self.l.finish(t['id'],state,str(path),recovery_pending=pending,attempt=t['attempt']);return self.l.get(t['id'])
    def test_concurrent_claims_serialize_workspace_and_resource(self):
        self.task('one',resources=['heavy']);self.task('two',self.b,resources=['heavy']);self.task('three',self.a)
        with concurrent.futures.ThreadPoolExecutor(3) as pool:results=list(pool.map(lambda _:Ledger(self.db).claim_next(),range(3)))
        self.assertEqual(sum(x is not None for x in results),1)
        self.assertEqual(len(self.l.resources()),1)
    def test_independent_workspaces_parallel(self):
        self.task('one');self.task('two',self.b)
        self.assertIsNotNone(self.l.claim_next());self.assertIsNotNone(self.l.claim_next());self.assertIsNone(self.l.claim_next())
    def test_dependency_waits_for_independent_acceptance(self):
        self.task('one');self.task('two',self.b,dependencies=['one']);t=self.l.claim_next();t=self.deliver(t)
        self.assertEqual(t['state'],'delivered');self.assertIsNone(self.l.claim_next())
        self.l.review('one','accepted',self.receipt(t),'制作统筹');self.assertEqual(self.l.claim_next()['id'],'two')
    def test_mutations_hold_workspace_until_review_and_recovery(self):
        self.task('one',readonly=False);self.task('two');t=self.deliver(self.l.claim_next(),pending=1)
        self.assertIsNone(self.l.claim_next())
        with self.assertRaises(ValueError):self.l.review('one','accepted',self.receipt(t),'制作统筹')
        self.l.clear_recovery('one',t['attempt']);self.l.review('one','accepted',self.receipt(t),'制作统筹');self.assertEqual(self.l.claim_next()['id'],'two')
    def test_rework_preserves_artifact_and_attempt_history_is_bounded(self):
        self.task('one',readonly=False)
        for attempt in range(1,4):
            t=self.l.claim_next();self.assertEqual(t['attempt'],attempt);t=self.deliver(t,'failed')
            self.l.review('one','rework',self.receipt(t,'rework'),'制作统筹');self.assertTrue(Path(t['delivery']).exists())
            if attempt<3:self.l.requeue('one','fix the independently found error')
        with self.assertRaises(ValueError):self.l.requeue('one','again')
        with self.l.connect() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM cc_attempts').fetchone()[0],3)
    def test_idempotency_conflict_and_stale_attempt(self):
        self.task('one');self.assertEqual(self.task('one')['id'],'one')
        with self.assertRaises(ValueError):self.task('one',readonly=False)
        t=self.l.claim_next()
        with self.assertRaises(ValueError):self.l.set_pid('one',123,99)
        self.deliver(t)
        with self.assertRaises(ValueError):self.l.finish('one','delivered',attempt=1)
    def test_evidence_rejects_changed_artifact_actor_and_attempt(self):
        self.task('one');t=self.deliver(self.l.claim_next());e=self.receipt(t);Path(t['delivery']).write_text('changed')
        with self.assertRaises(ValueError):self.l.review('one','accepted',e,'制作统筹')
        with self.assertRaises(ValueError):self.l.review('one','accepted',self.receipt(t,attempt=99),'制作统筹')
        with self.assertRaises(ValueError):self.l.review('one','accepted',self.receipt(t),'Flash')
    def test_orphan_recovery_never_retries_or_deletes(self):
        self.task('one',readonly=False);t=self.l.claim_next();file=self.a/'valuable.txt';file.write_text('keep')
        self.l.set_pid('one',123,1);self.assertEqual(self.l.recover_orphans([]),['one']);self.assertEqual(self.l.get('one')['state'],'interrupted');self.assertIsNone(self.l.claim_next());self.assertEqual(file.read_text(),'keep')
    def test_external_reservation_not_released_with_task(self):
        self.l.reserve('heavy','制作统筹');self.task('one',resources=['heavy']);self.task('two',self.b);t=self.l.claim_next();self.assertEqual(t['id'],'two');self.deliver(t)
        self.assertEqual(self.l.resources()[0]['owner'],'制作统筹');self.l.release_reservation('heavy','制作统筹');self.assertEqual(self.l.claim_next()['id'],'one')
    def test_close_releases_workspace_without_passing_dependencies(self):
        self.task('one',readonly=False);self.task('dependent',self.b,dependencies=['one']);self.task('other')
        t=self.deliver(self.l.claim_next(),'failed');self.l.review('one','closed',self.receipt(t,'closed'),'制作统筹')
        self.assertTrue(self.l.get('one')['released']);self.assertEqual(self.l.claim_next()['id'],'other')
    def test_alignment_reuses_agreement_and_rejects_open_questions(self):
        brief={'mode':'existing-contract','goal':'Read fixture','deliverables':['exact text'],'acceptance':['matches input'],'constraints':['readonly'],'decisions':['reuse user-approved scope'],'open_questions':[],'basis':'existing explicit instruction'}
        t=self.task('aligned',alignment=brief);self.assertEqual(t['spec']['alignment'],brief)
        brief=dict(brief,mode='grill-me',open_questions=['which source?'])
        with self.assertRaises(ValueError):self.task('unresolved',alignment=brief)
    def test_cancel_blocked_queue_never_dispatches_or_releases_external_reservation(self):
        self.l.reserve('heavy','external');self.task('one',resources=['heavy']);self.task('dependent',self.b,dependencies=['one'])
        self.assertTrue(self.l.cancel_queued('one'))
        t=self.l.get('one');self.assertEqual((t['state'],t['attempt'],t['released']),('cancelled',0,True))
        self.assertIsNone(self.l.claim_next());self.assertEqual(self.l.resources()[0]['owner'],'external')
        self.assertTrue(self.l.cancel_queued('dependent'));self.assertFalse(self.l.cancel_queued('one'))
        self.task('running');t=self.l.claim_next();self.assertFalse(self.l.cancel_queued(t['id']))
        self.assertEqual(self.l.get(t['id'])['state'],'running')

class UsageTests(unittest.TestCase):
    def test_small_order_can_fund_provider_request_without_relaxing_total_budget(self):
        import deepseek_mcp.agent_loop as loop
        for total,used,prompt in ((15000,0,3000),(30000,17000,2200)):
            state=SimpleNamespace(prompt_tokens=used,completion_tokens=0,budget_tokens=used,
                last_request_bytes=6000,last_prompt_tokens=prompt,
                config=SimpleNamespace(max_output_tokens=telemetry.output_budget(total,16384)))
            with self.subTest(total=total),mock.patch.object(loop,'MAX_TOTAL_TOKENS_PER_RUN',total):
                loop._ensure_request_budget(state,6000)
                state.config.max_output_tokens=16384
                with self.assertRaises(loop.AgentBudgetExceeded):loop._ensure_request_budget(state,6000)
                state.config.max_output_tokens=telemetry.output_budget(total,16384)
                state.budget_tokens=total
                with self.assertRaises(loop.AgentBudgetExceeded):loop._ensure_request_budget(state,6000)

    def test_provider_delta_dedup_and_cache_subset(self):
        with tempfile.TemporaryDirectory() as directory:
            old_data,old_db=telemetry.DATA,telemetry.DB
            telemetry.DATA=Path(directory);telemetry.DB=Path(directory)/'usage.sqlite';telemetry.initialize()
            try:
                with telemetry.connection() as c:c.execute('INSERT INTO rt_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',('r','t',1,'w','deepseek-flash',None,'request',1,None,1,None,None,1,None,1))
                telemetry.LOCAL.run_id='r';telemetry.LOCAL.task_id='t'
                class Response:
                    def __init__(self,id,prompt,output):self.raw={'id':id,'model':'deepseek-flash','usage':{'prompt_tokens':prompt,'completion_tokens':output,'prompt_cache_hit_tokens':4}}
                    def model_dump(self,**kw):return self.raw
                state=SimpleNamespace(config=SimpleNamespace(model='deepseek-flash'))
                telemetry.record_usage(state,Response('a',10,3));telemetry.record_usage(state,Response('a',10,3));telemetry.record_usage(state,Response('b',20,5))
                runs,total,events=telemetry.snapshot();self.assertEqual(total['total_tokens'],38);self.assertEqual(total['cached_tokens'],8);self.assertEqual(len(runs),1)
                # An interrupted run retains received request usage even without a final result.
                telemetry.emit('run_failed',phase='failed',error_code='Interrupted')
                self.assertEqual(telemetry.snapshot()[1]['total_tokens'],38)
                telemetry.LOCAL.request_key='turn:3';telemetry.record_usage(state,Response(None,2,1));telemetry.record_usage(state,Response(None,2,1))
                self.assertEqual(telemetry.snapshot()[1]['total_tokens'],41)
            finally:telemetry.DATA,telemetry.DB=old_data,old_db;telemetry.LOCAL.run_id=telemetry.LOCAL.task_id=None

class ServiceControlTests(unittest.TestCase):
    def test_worker_python_command_uses_configured_interpreter(self):
        import control_center
        env=control_center.environment()
        self.assertEqual(env['PATH'].split(os.pathsep)[0],str(control_center.PYTHON.parent))
        command=['cmd.exe','/d','/s','/c','python executable_probe.py'] if os.name=='nt' else ['/bin/sh','-c','python executable_probe.py']
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory)/'executable_probe.py').write_text('import sys; print(sys.executable)',encoding='utf-8')
            result=subprocess.run(command,cwd=directory,env=env,capture_output=True,text=True,timeout=10,check=True)
        self.assertEqual(Path(result.stdout.strip()).resolve(),control_center.PYTHON.resolve())

    def test_restart_refuses_queued_or_running_work_before_writing_request(self):
        import service_control
        for state in ('queued','running'):
            with self.subTest(state=state),mock.patch.object(service_control,'status',return_value={'service':{'features':['graceful-service-control']},'tasks':[{'state':state}]}),mock.patch.object(service_control,'save') as write:
                with self.assertRaisesRegex(RuntimeError,'queued/running'):service_control.stop()
                write.assert_not_called()
    def test_legacy_service_is_not_terminated_automatically(self):
        import service_control
        with mock.patch.object(service_control,'status',return_value={'service':{'features':[]},'tasks':[]}),mock.patch.object(service_control,'save') as write:
            with self.assertRaisesRegex(RuntimeError,'Legacy service'):service_control.stop()
            write.assert_not_called()
    def test_start_does_not_launch_duplicate_service(self):
        import service_control
        with mock.patch.object(service_control,'status',return_value={'service':{'pid':123}}),mock.patch.object(service_control.subprocess,'Popen') as launch:
            self.assertEqual(service_control.start(),{'state':'already_running','pid':123})
            launch.assert_not_called()

if __name__=='__main__':unittest.main(verbosity=2)
