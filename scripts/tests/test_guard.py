"""Real local guard/claim/freshness workflows, refusals and independent coordinators."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT=Path(__file__).resolve().parents[1]/'guard.py'
spec=importlib.util.spec_from_file_location('dispatch_guard',SCRIPT)
guard=importlib.util.module_from_spec(spec);spec.loader.exec_module(guard)

class GuardTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='dispatch-guard-');self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.root=self.base/'coordinator-one';self.root.mkdir()
        self.remote=self.base/'claims.git'
        subprocess.run(['git','init','--bare','--quiet',str(self.remote)],check=True)
        self.html=self.root/'tracker.html'
        (self.root/'dependencies').mkdir()
        descriptors=[]
        for role in ('ticket','code','ci'):
            path='dependencies/'+role+'.json';raw=(role+' current v1').encode();(self.root/path).write_bytes(raw)
            descriptors.append({'id':role,'role':role,'kind':'file','path':path,'fingerprint':hashlib.sha256(raw).hexdigest()})
        self.data={'title':'Dispatch fixture','spec':'fixture://spec/dispatch','scope':'Synthetic only','updated_at':'fixture',
          'next_action':'Prepare M1','journal':[],'decisions':[],
          'missions':[{'id':'M1','title':'M1','ticket':'https://example.invalid/issues/M1','status':'ready','depends_on':[],
            'criteria':[{'text':'Criterion','result':'unverified'}],
            'prompt':{'kind':'implementation','delivery':'prepared','text':'$implement https://example.invalid/issues/M1\nImplement current scope.','file':'prompts/M1.md'},
            'guard':{'authorization_source':'fixture://user/mandate','claim_remote':str(self.remote),
                     'tool':'fixture_delegate','sources':descriptors}}],
          'continuation':{'mandate':'Authorized synthetic dispatch only','sources':['fixture://user/mandate'],
             'constraints':[],'read_next':['tracker:/missions/0'],
             'current_action':{'phase':'prepare','mission_ids':['M1'],'next_step':'Prepare M1','sources':['fixture://user/mandate']}}}
        self.write()
        self.payload=self.root/'payload.json';self.make_payload('request-one')

    def make_payload(self,key):
        self.payload.write_text(json.dumps({'tool':'fixture_delegate','arguments':{'clientRequestId':key,'task':self.data['missions'][0]['prompt']['text']}}))

    def write(self):
        self.data['continuation']['reconciled']=guard.resume.continuation_fingerprints(self.data)
        template=(SCRIPT.parent.parent/'assets/tracker.html').read_text()
        self.html.write_text(guard.tracker.render(template,self.data))
        for path,text in guard.tracker.validate(self.data,self.root).items():
            path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text)

    def read(self):
        self.data,sha=guard.resume.load(self.html);return self.data,sha

    def prepare(self,key='request-one',owner='coordinator-one',finding=None):
        return guard.prepare(self.html,'M1',guard.tracker.revision(self.html),key,owner,self.payload,finding)

    def check(self,purpose='dispatch',finding=None):
        data,_=self.read();return guard.check(data,'M1',self.root,purpose,finding)

    def receipt(self,outcome,**extra):
        return dict(request_id='request-one',outcome=outcome,source='fixture://actual-observation',**extra)

    def record(self,outcome,**extra):
        return guard.record(self.html,'M1',guard.tracker.revision(self.html),'request-one',self.receipt(outcome,**extra))

    def test_blocking_matrix_never_writes_or_reserves(self):
        baseline=copy.deepcopy(self.data)
        cases=[('hold',{'active':True}),('hold',{}),('blocker','External decision'),
               ('dispatch',{'request_id':'old','delivery':'unknown'}),
               ('dispatch',{'request_id':'old','delivery':'accepted','status':'running'}),
               ('attempts',{'F9':{'used':2,'limit':2}}),('attempts',{'F9':{'used':True,'limit':2}})]
        for key,value in cases:
            self.data=copy.deepcopy(baseline);self.data['missions'][0][key]=value
            # A blocker is valid on blocked/follow-up state; ready + blocker is rejected by schema.
            if key=='blocker':self.data['missions'][0]['status']='blocked'
            self.write();before=self.html.read_bytes()
            with self.subTest(key=key,value=value):
                self.assertFalse(self.check()['ok'])
                with self.assertRaisesRegex(ValueError,'Dispatch refused'):self.prepare()
                self.assertEqual(self.html.read_bytes(),before)
                self.assertIsNone(guard.helper('claims').inspect(str(self.remote),'https://example.invalid/issues/M1'))

    def test_prepare_persists_unknown_and_repeated_preparation_has_no_second_permit(self):
        result=self.prepare();self.assertTrue(result['ready_for_authorized_call'])
        data,_=self.read();m=data['missions'][0]
        self.assertEqual(m['dispatch']['delivery'],'unknown')
        self.assertEqual(m['dispatch']['claim_state'],'held')
        self.assertEqual(guard.resume.contract_status(data),'recorded')
        self.assertIn('uncertain_delivery',guard.resume.mission_flags(m))
        self.assertEqual(Path(result['payload_file']).read_bytes(),self.payload.read_bytes())
        with self.assertRaisesRegex(ValueError,'Dispatch refused'):self.prepare()
        self.make_payload('request-two')
        with self.assertRaisesRegex(ValueError,'Dispatch refused'):self.prepare('request-two')

    def test_distinct_tracker_copies_share_exclusive_ticket_claim(self):
        other=self.base/'coordinator-two';shutil.copytree(self.root,other)
        one=self.prepare()
        args=['--mission','M1','--request-id','request-two','--owner','coordinator-two','--payload',str(other/'payload.json')]
        payload=json.loads((other/'payload.json').read_text());payload['arguments']['clientRequestId']='request-two';(other/'payload.json').write_text(json.dumps(payload))
        second=subprocess.run([sys.executable,'-B',str(SCRIPT),'prepare',str(other/'tracker.html'),'--expected',guard.tracker.revision(other/'tracker.html'),*args],capture_output=True,text=True)
        self.assertNotEqual(second.returncode,0)
        self.assertNotIn('ready_for_authorized_call',second.stdout)
        actual=guard.helper('claims').inspect(str(self.remote),'https://example.invalid/issues/M1')
        self.assertEqual(actual['request_id'],'request-one')
        self.assertEqual(actual['claim_commit'],one['claim']['claim_commit'])

    def test_changed_source_blocks_before_intent_and_ignores_unchanged_timestamp(self):
        source=self.root/'dependencies/ticket.json';before=self.html.read_bytes();stamp=source.stat().st_mtime_ns
        source.write_text('Changed acceptance criteria')
        import os;os.utime(source,ns=(stamp,stamp))
        self.assertFalse(self.check()['ok'])
        with self.assertRaisesRegex(ValueError,'Dispatch refused'):self.prepare()
        self.assertEqual(self.html.read_bytes(),before)

    def test_source_change_during_claim_acquisition_never_emits_permit(self):
        actual_helper=guard.helper;claims=actual_helper('claims');acquire=claims.acquire
        def changed(*args):
            result=acquire(*args);(self.root/'dependencies/code.json').write_text('new revision');return result
        claims.acquire=changed
        with patch.object(guard,'helper',side_effect=lambda name:claims if name=='claims' else actual_helper(name)):
            with self.assertRaisesRegex(ValueError,'State/source changed'):self.prepare()
        data,_=self.read();self.assertEqual(data['missions'][0]['dispatch']['delivery'],'unknown')
        self.assertIsNotNone(actual_helper('claims').inspect(str(self.remote),'https://example.invalid/issues/M1'))

    def test_payload_and_stale_tracker_refused_before_reservation(self):
        old=guard.tracker.revision(self.html);self.html.write_bytes(self.html.read_bytes()+b'\n<!-- concurrent update -->')
        with self.assertRaisesRegex(ValueError,'Stale tracker'):guard.prepare(self.html,'M1',old,'request-one','owner',self.payload)
        self.payload.write_text(json.dumps({'tool':'other','arguments':{}}))
        with self.assertRaisesRegex(ValueError,'contracted tool'):self.prepare()
        self.assertIsNone(guard.helper('claims').inspect(str(self.remote),'https://example.invalid/issues/M1'))

    def test_recorded_terminal_observation_required_before_release(self):
        self.prepare()
        with self.assertRaisesRegex(ValueError,'Unknown or active'):guard.release(self.html,'M1',guard.tracker.revision(self.html),'request-one')
        self.record('accepted',task_id='actual-task',status='running')
        for receipt in (self.receipt('terminal',task_id='wrong',status='completed',no_pending_work=True),
                        self.receipt('terminal',task_id='actual-task',status='completed',no_pending_work=False),
                        self.receipt('not_delivered')):
            with self.assertRaises(ValueError):guard.record(self.html,'M1',guard.tracker.revision(self.html),'request-one',receipt)
        self.record('terminal',task_id='actual-task',status='completed',no_pending_work=True)
        result=guard.release(self.html,'M1',guard.tracker.revision(self.html),'request-one');self.assertTrue(result['released'])
        self.assertEqual(self.read()[0]['missions'][0]['status'],'to-verify')

    def test_followup_reserves_budget_once_and_confirmed_nondelivery_refunds_it(self):
        m=self.data['missions'][0];m['prompt']['kind']='follow-up';m['attempts']={'F9':{'used':1,'limit':2}}
        self.data['automatic_correction_budget']={'used':3,'limit':4};self.write()
        self.assertFalse(self.check()['ok'])
        self.make_payload('request-one');self.prepare(finding='F9')
        data,_=self.read();self.assertEqual(data['missions'][0]['attempts']['F9']['used'],2)
        self.assertEqual(data['automatic_correction_budget']['used'],4)
        with self.assertRaisesRegex(ValueError,'Unknown/timeout'):self.record('unknown')
        self.record('not_delivered')
        data,_=self.read();self.assertEqual(data['missions'][0]['attempts']['F9']['used'],1)
        self.assertEqual(data['automatic_correction_budget']['used'],3)
        guard.release(self.html,'M1',guard.tracker.revision(self.html),'request-one')

    def test_acceptance_rejects_old_proof_even_after_current_sources_are_recaptured(self):
        m=self.data['missions'][0];m['status']='to-verify';m['criteria'][0].update(result='passed',proof='fixture://test-report')
        m['guard']['proof_sources']={s['id']:s['fingerprint'] for s in m['guard']['sources']};self.write()
        self.assertTrue(self.check('accept')['ok'])
        (self.root/'dependencies/code.json').write_text('new revision')
        self.assertFalse(self.check('accept')['ok'])
        data,_=self.read();m=data['missions'][0]
        m['guard']['sources']=[guard.helper('sources').capture(s,self.root) for s in m['guard']['sources']]
        self.write()
        result=self.check('accept');self.assertFalse(result['ok']);self.assertIn('Proofs are not bound',' '.join(result['reasons']))
        data,_=self.read();m=data['missions'][0];m['guard']['proof_sources']={s['id']:s['fingerprint'] for s in m['guard']['sources']};self.write()
        self.assertTrue(self.check('accept')['ok'])

    def test_missing_source_roles_require_explicit_not_applicable_reason(self):
        self.data['missions'][0]['guard']['sources'].pop();self.write();self.assertFalse(self.check()['ok'])
        self.data['missions'][0]['guard']['not_applicable']={'ci':'Documentation-only project with no configured CI'};self.write()
        self.assertTrue(self.check()['ok'])

    def test_lost_claim_response_is_recovered_without_a_second_call_permit(self):
        actual_helper=guard.helper;claims=actual_helper('claims');acquire=claims.acquire
        def lost(*args):
            acquire(*args)
            raise ValueError('Simulated response lost after accepted claim')
        claims.acquire=lost
        with patch.object(guard,'helper',side_effect=lambda name:claims if name=='claims' else actual_helper(name)):
            with self.assertRaisesRegex(ValueError,'response lost'):self.prepare()
        data,_=self.read();self.assertNotIn('claim',data['missions'][0]['dispatch'])
        recovered=guard.recover(self.html,'M1',guard.tracker.revision(self.html),'request-one')
        self.assertFalse(recovered['ready_for_authorized_call'])
        with self.assertRaisesRegex(ValueError,'Dispatch refused'):self.prepare()
        self.record('not_delivered')
        guard.release(self.html,'M1',guard.tracker.revision(self.html),'request-one')

    def test_lost_release_response_is_reconciled_without_reacquiring(self):
        self.prepare();self.record('not_delivered')
        data,sha=self.read();d=data['missions'][0]['dispatch']
        guard.helper('claims').release(d['claim_remote'],data['missions'][0]['ticket'],'request-one',d['claim']['claim_commit'],
                                       {'outcome':'not_delivered','source':'fixture://confirmed-lookup'})
        result=guard.recover(self.html,'M1',sha,'request-one')
        self.assertFalse(result['ready_for_authorized_call'])
        self.assertEqual(self.read()[0]['missions'][0]['dispatch']['claim_state'],'released')

    def test_simultaneous_processes_issue_only_one_permit(self):
        second=self.base/'coordinator-two';shutil.copytree(self.root,second)
        other_payload=second/'payload.json'
        envelope=json.loads(other_payload.read_bytes());envelope['arguments']['clientRequestId']='request-two'
        other_payload.write_text(json.dumps(envelope))
        processes=[]
        for root,key in ((self.root,'request-one'),(second,'request-two')):
            html=root/'tracker.html'
            args=[sys.executable,'-B',str(SCRIPT),'prepare',str(html),'--mission','M1',
                  '--expected',guard.tracker.revision(html),'--request-id',key,'--owner',root.name,
                  '--payload',str(root/'payload.json')]
            processes.append(subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True))
        results=[(p.communicate(timeout=30),p.returncode) for p in processes]
        self.assertEqual(sorted(code for _,code in results),[0,1],results)
        permits=[json.loads(out) for (out,_),code in results if code==0]
        self.assertEqual(len(permits),1);self.assertTrue(permits[0]['ready_for_authorized_call'])
        for root in (self.root,second):
            data,_=guard.resume.load(root/'tracker.html')
            self.assertEqual(data['missions'][0]['dispatch']['delivery'],'unknown')

    def test_checkpoint_restores_exact_intent_and_recovery_without_send_permit(self):
        self.prepare();before,_=self.read();d=before['missions'][0]['dispatch']
        # A host-local bare authority is not a portable service dependency. Route
        # this test's advertised SSH endpoint to the local authority below.
        endpoint='ssh://git@example.invalid/shared.git'
        before['missions'][0]['guard']['claim_remote']=endpoint;d['claim_remote']=endpoint
        before['missions'][0]['guard'].update(prompt_pointer='/task',request_id_pointer='/clientRequestId')
        self.write()
        raw=(self.root/d['tool_payload_file']).read_bytes()
        checkpoint=guard.helper('checkpoint')
        files=checkpoint.make_snapshot(self.root,'tracker.html',[])
        checkpoint.verify_files(files)
        destination=self.base/'restored';checkpoint.restore(files,destination)
        restored=destination/'tracker.html'
        data,sha=guard.resume.load(restored);recovered=data['missions'][0]['dispatch']
        self.assertEqual(d,recovered)
        self.assertEqual(data['missions'][0]['guard']['prompt_pointer'],'/task')
        self.assertEqual(data['missions'][0]['guard']['request_id_pointer'],'/clientRequestId')
        self.assertEqual((destination/recovered['tool_payload_file']).read_bytes(),raw)
        script=destination/'.checkpoint/tools/guard.py'
        module_spec=importlib.util.spec_from_file_location('restored_guard',script)
        recovered_guard=importlib.util.module_from_spec(module_spec);module_spec.loader.exec_module(recovered_guard)
        actual_helper=recovered_guard.helper;claims=actual_helper('claims');inspect=claims.inspect
        claims.inspect=lambda remote,ticket:inspect(str(self.remote),ticket) if remote==endpoint else None
        with patch.object(recovered_guard,'helper',side_effect=lambda name:claims if name=='claims' else actual_helper(name)):
            result=recovered_guard.recover(restored,'M1',sha,'request-one')
        self.assertFalse(result['ready_for_authorized_call'])

    def test_synchronously_completed_receipt_records_delivery_and_terminal_together(self):
        self.prepare()
        self.record('terminal',task_id='actual-synchronous-task',status='completed',no_pending_work=True)
        data,_=self.read();m=data['missions'][0]
        self.assertEqual(m['status'],'to-verify');self.assertEqual(m['dispatch']['delivery'],'accepted')
        self.assertEqual(m['prompt']['delivery'],'sent')
        with self.assertRaises(ValueError):self.record('accepted',task_id='actual-synchronous-task',status='running')
        self.assertTrue(guard.release(self.html,'M1',guard.tracker.revision(self.html),'request-one')['released'])

    def test_selected_resolved_finding_requires_reconciliation_before_any_reservation(self):
        m=self.data['missions'][0];m['prompt']['kind']='follow-up'
        for used in (0,2):
            m['attempts']={'F9':{'used':used,'limit':2,'resolved':True}};self.write()
            before=self.html.read_bytes()
            with self.assertRaisesRegex(ValueError,'Selected finding is resolved'):self.prepare(finding='F9')
            self.assertEqual(self.html.read_bytes(),before)
            self.assertIsNone(guard.helper('claims').inspect(str(self.remote),m['ticket']))

    def test_checkpoint_rejects_missing_changed_and_historical_tool_envelopes(self):
        self.prepare();data,_=self.read();m=data['missions'][0]
        m['guard']['claim_remote']='ssh://git@example.invalid/claims.git'
        m['dispatch']['claim_remote']=m['guard']['claim_remote'];self.write()
        cp=guard.helper('checkpoint');archive=self.root/m['dispatch']['tool_payload_file'];raw=archive.read_bytes()
        files=cp.make_snapshot(self.root,'tracker.html',[])
        for changed in (None,b'altered tool envelope'):
            if changed is None:archive.unlink()
            else:archive.write_bytes(changed)
            with self.assertRaisesRegex(ValueError,'exact tool envelope'):cp.make_snapshot(self.root,'tracker.html',[])
        archive.write_bytes(raw)
        m['dispatch_history']=[copy.deepcopy(m.pop('dispatch'))];self.write();archive.unlink()
        with self.assertRaisesRegex(ValueError,'exact tool envelope'):cp.make_snapshot(self.root,'tracker.html',[])
        member='source/'+m['dispatch_history'][0]['tool_payload_file'];files[member]=b'changed archived member'
        manifest=json.loads(files['manifest.json']);manifest['files'][member]=cp.digest(files[member]);files['manifest.json']=cp.json_bytes(manifest)
        with self.assertRaisesRegex(ValueError,'exact tool envelope'):cp.verify_files(files)

    def test_separate_git_source_recovery_is_pinned_verified_and_requires_rehydration(self):
        repo=self.base/'implementation';repo.mkdir()
        def git(*args):
            return subprocess.run(['git','-C',str(repo),*args],check=True,capture_output=True,text=True).stdout.strip()
        git('init','--quiet');git('config','user.name','Fixture');git('config','user.email','fixture@example.invalid')
        (repo/'code.py').write_text('print(1)\n');git('add','code.py');git('-c','commit.gpgsign=false','commit','--quiet','-m','initial')
        commit=git('rev-parse','HEAD');remote=self.base/'code-origin.git'
        subprocess.run(['git','init','--bare','--quiet',str(remote)],check=True);git('push','--quiet',str(remote),'HEAD:refs/heads/main')
        descriptor=guard.helper('sources').capture({'id':'code','role':'code','kind':'git','path':str(repo),'ref':'HEAD'},self.root)
        descriptor['recovery']={'remote':str(remote),'commit':commit}
        m=self.data['missions'][0];m['guard']['sources'][1]=descriptor;m['guard']['claim_remote']='ssh://git@example.invalid/claims.git'
        m['workspace']=str(repo);m['prompt']['text']+='\nUse checkout `'+str(repo)+'`.';self.write()
        cp=guard.helper('checkpoint');files=cp.make_snapshot(self.root,'tracker.html',[]);cp.verify_files(files)
        shutil.rmtree(repo)
        restored=self.base/'restored-code';cp.restore(files,restored)
        data,_=guard.resume.load(restored/'tracker.html')
        self.assertEqual(data['missions'][0]['guard']['sources'][1]['recovery'],descriptor['recovery'])
        self.assertFalse(guard.check(data,'M1',restored)['ok'])
        subprocess.run(['git','clone','--quiet','--no-checkout',str(remote),str(repo)],check=True,capture_output=True)
        git('checkout','--quiet','--detach',commit)
        self.assertTrue(guard.check(data,'M1',restored)['ok'])
        descriptor['recovery']['commit']='0'*40;self.write()
        with self.assertRaisesRegex(ValueError,'expected source fingerprint'):cp.make_snapshot(self.root,'tracker.html',[])
        descriptor['recovery'].update(commit=commit,remote=str(self.base/'missing-remote'));self.write()
        with self.assertRaises(ValueError):cp.make_snapshot(self.root,'tracker.html',[])

    def test_hidden_dirty_head_cannot_accept_prior_proof(self):
        repo=self.base/'hidden-index-code';repo.mkdir()
        def git(*args):
            return subprocess.run(['git','-C',str(repo),*args],check=True,capture_output=True,text=True).stdout.strip()
        git('init','--quiet');git('config','user.name','Fixture');git('config','user.email','fixture@example.invalid')
        code=repo/'code.py';code.write_text('print(1)\n');git('add','code.py');git('-c','commit.gpgsign=false','commit','--quiet','-m','initial')
        source=guard.helper('sources').capture({'id':'code','role':'code','kind':'git','path':str(repo),'ref':'HEAD'},self.root)
        m=self.data['missions'][0];m.update(status='to-verify');m['criteria'][0].update(result='passed',proof='fixture://actual-proof')
        m['guard']['sources'][1]=source;m['guard']['proof_sources']={s['id']:s['fingerprint'] for s in m['guard']['sources']};self.write()
        self.assertTrue(self.check('accept')['ok'])
        for flag in ('assume-unchanged','skip-worktree'):
            git('update-index','--'+flag,'code.py');code.write_text('print(2)\n')
            self.assertEqual(git('status','--porcelain'),'')
            self.assertFalse(self.check('accept')['ok'])
            code.write_text('print(1)\n');git('update-index','--no-'+flag,'code.py')
            self.assertTrue(self.check('accept')['ok'])

    def test_receipt_must_match_known_execution_before_extending_identity(self):
        self.prepare();self.record('accepted',task_id='actual-task',status='running')
        for outcome,status in (('accepted','running'),('terminal','completed')):
            before=self.html.read_bytes()
            with self.assertRaisesRegex(ValueError,'known execution identity'):
                self.record(outcome,run_id='unrelated-run',status=status,no_pending_work=True)
            self.assertEqual(self.html.read_bytes(),before)
            with self.assertRaisesRegex(ValueError,'Unknown or active'):
                guard.release(self.html,'M1',guard.tracker.revision(self.html),'request-one')
        self.record('accepted',task_id='actual-task',run_id='actual-run',status='running')
        with self.assertRaisesRegex(ValueError,'identity changed'):
            self.record('terminal',task_id='actual-task',run_id='other-run',status='completed',no_pending_work=True)
        self.record('terminal',run_id='actual-run',status='completed',no_pending_work=True)
        self.assertTrue(guard.release(self.html,'M1',guard.tracker.revision(self.html),'request-one')['released'])

    def test_hidden_nested_submodule_edit_blocks_acceptance(self):
        def git(path,*args):
            return subprocess.run(['git','-C',str(path),*args],check=True,capture_output=True,text=True).stdout.strip()
        def repository(name):
            path=self.base/name;path.mkdir();git(path,'init','--quiet')
            git(path,'config','user.name','Fixture');git(path,'config','user.email','fixture@example.invalid')
            (path/'code.py').write_text('print(1)\n');git(path,'add','code.py')
            git(path,'-c','commit.gpgsign=false','commit','--quiet','-m','initial');return path
        leaf=repository('leaf');middle=repository('middle');top=repository('top')
        for parent,child in ((middle,leaf),(top,middle)):
            git(parent,'-c','protocol.file.allow=always','submodule','add','--quiet',str(child),'module')
            git(parent,'-c','commit.gpgsign=false','commit','--quiet','-am','module')
        git(top,'-c','protocol.file.allow=always','submodule','update','--init','--recursive','--quiet')
        source=guard.helper('sources').capture({'id':'code','role':'code','kind':'git','path':str(top),'ref':'HEAD'},self.root)
        m=self.data['missions'][0];m['status']='to-verify';m['criteria'][0].update(result='passed',proof='fixture://module-proof')
        m['guard']['sources'][1]=source;m['guard']['proof_sources']={s['id']:s['fingerprint'] for s in m['guard']['sources']};self.write()
        self.assertTrue(self.check('accept')['ok'])
        nested=top/'module/module';code=nested/'code.py'
        for flag in ('assume-unchanged','skip-worktree'):
            git(nested,'update-index','--'+flag,'code.py');code.write_text('print(2)\n')
            self.assertEqual(git(top,'status','--porcelain'),'')
            self.assertFalse(self.check('accept')['ok'])
            code.write_text('print(1)\n');git(nested,'update-index','--no-'+flag,'code.py')
            self.assertTrue(self.check('accept')['ok'])
        git(nested,'config','user.name','Fixture');git(nested,'config','user.email','fixture@example.invalid')
        code.write_text('print(3)\n');git(nested,'add','code.py')
        git(nested,'-c','commit.gpgsign=false','commit','--quiet','-m','new-module-version')
        git(top/'module','add','module')
        self.assertFalse(self.check('accept')['ok'])

if __name__=='__main__':unittest.main()
