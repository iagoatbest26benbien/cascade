#!/usr/bin/env python3
"""Check, reserve and record authorized dispatches; this helper never sends a task."""
import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import types


def helper(name):
    path = Path(__file__).resolve().with_name(name + '.py')
    loaded = types.ModuleType('guard_' + name)
    loaded.__file__ = str(path)
    exec(compile(path.read_bytes(), str(path), 'exec'), loaded.__dict__)
    return loaded


tracker, resume = helper('tracker'), helper('resume')


def require(value, message):
    if not value:
        raise ValueError(message)


def now():
    return datetime.now(timezone.utc).isoformat()


def text(value):
    return isinstance(value, str) and bool(value.strip())


def load(path, expected):
    data, sha = resume.load(path)
    require(isinstance(expected, str) and expected == sha, 'Stale tracker revision; reread before this operation.')
    return data, sha


def mission(data, mid):
    matches = [m for m in data['missions'] if m['id'] == mid]
    require(len(matches) == 1, 'Unknown mission ID.')
    return matches[0]


def save(path, data, sha):
    data['continuation']['reconciled'] = resume.continuation_fingerprints(data)
    tracker.save_state(path, data, sha)
    return tracker.revision(path)


def held(record):
    return record is not None and not (isinstance(record, dict) and record.get('active') is False)


def budget_errors(record, label, reserved=False):
    if (not isinstance(record, dict) or type(record.get('used')) is not int or
            type(record.get('limit')) is not int or record['used'] < 0 or record['limit'] < 1):
        return [label + ': correction budget is missing or malformed.']
    if record['used'] > record['limit'] or not reserved and record['used'] >= record['limit']:
        return [label + ': correction budget exhausted.']
    return []


def check(data, mid, root, purpose='dispatch', finding=None, pending_request=None):
    m = mission(data, mid)
    reasons = []
    status, gaps = resume.continuation_review(data)
    if status != 'recorded':
        reasons.append('Continuation ' + status + ': ' + ' '.join(gaps))
    if held(data.get('hold')) or held(m.get('hold')):
        reasons.append('An active or unresolved hold blocks this mission.')
    if m.get('blocker'):
        reasons.append('Mission has an unresolved blocker.')
    by_id = {x['id']: x for x in data['missions']}
    for dep in m.get('depends_on', []):
        if by_id[dep]['status'] != 'done' or held(by_id[dep].get('hold')):
            reasons.append('Prerequisite is not received or has a hold: ' + dep)
    contract = m.get('guard')
    if not isinstance(contract, dict):
        return {'ok':False, 'reasons':reasons + ['Missing structured mission.guard contract.'], 'sources':[]}
    authorization = contract.get('authorization_source')
    sources = data.get('continuation', {}).get('sources', [])
    if not text(authorization) or authorization not in sources:
        reasons.append('Record the applicable user authorization source in the continuation and guard contract.')
    if not text(contract.get('claim_remote')):
        reasons.append('Missing common claim authority.')
    descriptors = contract.get('sources')
    observed = []
    if not isinstance(descriptors, list):
        reasons.append('Source descriptors are missing.')
    else:
        ids = [s.get('id') for s in descriptors if isinstance(s, dict)]
        if len(ids) != len(descriptors) or not all(text(i) for i in ids) or len(set(ids)) != len(ids):
            reasons.append('Source IDs must be unique nonempty strings.')
        roles = {s.get('role') for s in descriptors if isinstance(s, dict) and isinstance(s.get('role'), str)}
        exceptions = contract.get('not_applicable', {})
        if not isinstance(exceptions, dict) or any(k not in {'ticket','code','ci'} or not text(v) for k,v in exceptions.items()):
            reasons.append('Invalid not-applicable source reasons.')
            exceptions = {}
        for role in ('ticket','code','ci'):
            if role not in roles and role not in exceptions:
                reasons.append('Missing current source role: ' + role)
        observed = helper('sources').evaluate(descriptors, root)
        for item in observed:
            if item.get('status') != 'unchanged':
                reasons.append('Source ' + str(item.get('id')) + ' is ' + str(item.get('status')) + '.')
    dispatch = m.get('dispatch')
    own_pending = (isinstance(dispatch, dict) and text(pending_request) and
                   dispatch.get('request_id') == pending_request and dispatch.get('delivery') == 'unknown')
    if dispatch is not None and not isinstance(dispatch, dict):
        reasons.append('Previous dispatch state is malformed.')
    elif isinstance(dispatch, dict) and not own_pending:
        if dispatch.get('delivery') == 'unknown' or dispatch.get('status') in {'unknown','running','accepted','queued'}:
            reasons.append('Previous dispatch is uncertain or active; reconcile it first.')
        elif dispatch and dispatch.get('status') not in {'completed','failed','cancelled','not_delivered'}:
            reasons.append('Previous execution has no conclusive terminal observation.')
        elif dispatch.get('claim') and dispatch.get('claim_state') != 'released':
            reasons.append('Previous claim must be conclusively released first.')
    if purpose == 'accept':
        if not m['criteria'] or any(c['result'] not in {'passed','not-applicable'} or not c.get('proof') for c in m['criteria']):
            reasons.append('Every acceptance criterion needs verified evidence or a reason.')
        if m['status'] not in {'to-verify','done'}:
            reasons.append('Mission is not awaiting verification/acceptance.')
        binding = contract.get('proof_sources')
        expected = {s.get('id'):s.get('fingerprint') for s in descriptors or [] if isinstance(s,dict)}
        if not expected or not isinstance(binding,dict) or binding != expected:
            reasons.append('Proofs are not bound to all current source fingerprints.')
    else:
        if m['status'] not in {'ready','follow-up'}:
            reasons.append('Mission is not ready for an assignment.')
        prompt = m.get('prompt')
        if not isinstance(prompt, dict) or prompt.get('delivery') != ('unknown' if own_pending else 'prepared'):
            reasons.append('A current prepared prompt is required; pending delivery must be reconciled.')
        if isinstance(prompt, dict) and prompt.get('kind') == 'follow-up' and not text(finding):
            reasons.append('A follow-up requires its stable finding ID and cumulative budget.')
        attempts = m.get('attempts', {})
        if not isinstance(attempts, dict):
            reasons.append('Correction history is malformed.')
        else:
            for key, record in attempts.items():
                if not (isinstance(record,dict) and record.get('resolved') is True):
                    reasons.extend(budget_errors(record, 'Finding ' + key, own_pending and key == finding))
            if finding and finding not in attempts:
                reasons.append('Missing cumulative budget for finding ' + finding)
            elif finding and isinstance(attempts[finding],dict) and attempts[finding].get('resolved') is True:
                reasons.append('Selected finding is resolved; explicitly reconcile/reopen it with its cumulative budget first.')
        if finding and 'automatic_correction_budget' in data:
            reasons.extend(budget_errors(data['automatic_correction_budget'], 'Global budget', own_pending))
    return {'ok':not reasons, 'reasons':reasons, 'sources':observed}


def archive(root, raw):
    sha = hashlib.sha256(raw).hexdigest()
    directory = root/'evidence'/'dispatch'
    require(not directory.is_symlink() and directory.resolve().is_relative_to(root.resolve()), 'Unsafe payload archive directory.')
    directory.mkdir(parents=True,exist_ok=True)
    target = directory/(sha + '.json')
    if target.exists():
        require(not target.is_symlink() and target.read_bytes() == raw, 'Changed payload archive.')
        return str(target.relative_to(root)), sha
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory,delete=False) as stream:
            temporary = Path(stream.name); stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        try:
            os.link(temporary,target)
        except FileExistsError:
            require(not target.is_symlink() and target.read_bytes() == raw, 'Changed concurrent payload archive.')
    finally:
        if temporary is not None:temporary.unlink(missing_ok=True)
    return str(target.relative_to(root)), sha


def prepare(path, mid, expected, request_id, owner, payload_path, finding=None):
    require(text(request_id) and text(owner), 'Stable request ID and coordinator owner are required.')
    data,sha = load(path,expected); m = mission(data,mid)
    verdict = check(data,mid,path.parent,finding=finding)
    require(verdict['ok'], 'Dispatch refused: ' + ' '.join(verdict['reasons']))
    previous = m.get('dispatch')
    require(not previous or previous.get('request_id') != request_id, 'Use a distinct request ID for a new round.')
    raw = payload_path.read_bytes(); payload = json.loads(raw)
    contract = m['guard']
    require(isinstance(payload,dict) and payload.get('tool') == contract.get('tool') and text(payload.get('tool')) and
            isinstance(payload.get('arguments'),dict), 'Payload must name the contracted tool and its exact arguments.')
    prompt_pointer = contract.get('prompt_pointer','/task')
    require(isinstance(prompt_pointer,str) and resume.select(payload['arguments'],prompt_pointer) == m['prompt']['text'], 'Tool payload differs from the prepared prompt.')
    key_pointer = contract.get('request_id_pointer','/clientRequestId')
    if key_pointer is not None:
        require(isinstance(key_pointer,str) and resume.select(payload['arguments'],key_pointer) == request_id, 'Payload client request identity differs.')
    filename,digest = archive(path.parent,raw)
    if previous:m.setdefault('dispatch_history',[]).append(copy.deepcopy(previous))
    reservation = {}
    if finding:
        record=m['attempts'][finding]; reservation['finding']={'id':finding,'before':record['used'],'after':record['used']+1};record['used']+=1
        if 'automatic_correction_budget' in data:
            record=data['automatic_correction_budget']; reservation['global']={'before':record['used'],'after':record['used']+1};record['used']+=1
    m['dispatch']={'request_id':request_id,'owner':owner,'delivery':'unknown','status':'unknown',
                   'tool_payload_file':filename,'tool_payload_sha256':digest,'finding_id':finding,
                   'reserved_budget':reservation,'claim_state':'acquiring','claim_remote':contract['claim_remote']}
    m['prompt']['delivery']='unknown'
    data['continuation']['current_action']={'phase':'dispatch_pending','mission_ids':[mid],
            'next_step':'Reconcile this request before any retry; receipt not yet recorded.',
            'sources':[authorization for authorization in (contract['authorization_source'],'tracker:/missions/'+str(data['missions'].index(m))+'/dispatch')]}
    data['journal'].append(now()+' intent '+request_id+' for '+mid+'; delivery unknown; no outbound call performed by guard.')
    # Persist BEFORE acquiring the remote token, so a lost token receipt is recoverable.
    intent_sha=save(path,data,sha)
    claim = helper('claims').acquire(contract['claim_remote'],m['ticket'],request_id,owner,digest)
    current,current_sha=resume.load(path); current_m=mission(current,mid)
    require(current_sha==intent_sha,'Tracker changed during reservation; claim remains held for reconciliation. Do not send.')
    current_dispatch=current_m.get('dispatch',{})
    require(current_dispatch.get('request_id')==request_id and current_dispatch.get('tool_payload_sha256')==digest,
            'Pending intent changed; claim remains held for reconciliation. Do not send.')
    verdict=check(current,mid,path.parent,finding=finding,pending_request=request_id)
    require(verdict['ok'],'State/source changed after reservation; claim remains held. Do not send. '+ ' '.join(verdict['reasons']))
    require(payload_path.read_bytes()==raw and (path.parent/filename).read_bytes()==raw,'Payload changed during reservation; do not send.')
    current_dispatch.update(claim=claim,claim_state='held')
    new_sha=save(path,current,current_sha)
    return {'ready_for_authorized_call':True,'request_id':request_id,'mission':mid,'revision_sha256':new_sha,
            'payload_file':str(path.parent/filename),'payload_sha256':digest,'claim':claim,
            'boundary':'One successful preparation permits one already-authorized tool call. This helper sends nothing. Unknown delivery is reconciled before any retry.'}


def record(path, mid, expected, request_id, receipt):
    data,sha=load(path,expected);m=mission(data,mid);d=m.get('dispatch',{})
    require(d.get('request_id')==request_id,'Receipt does not match current request.')
    require(isinstance(receipt,dict) and receipt.get('request_id')==request_id and text(receipt.get('source')),'Receipt requires matching request identity and an actual observation source.')
    outcome=receipt.get('outcome')
    require(outcome in {'accepted','terminal','not_delivered'},'Unknown/timeout outcomes cannot clear an uncertain send.')
    if outcome in {'accepted','terminal'}:
        require(d.get('delivery') in {'unknown','accepted'},'Conflicting prior delivery outcome.')
        require(text(receipt.get('task_id')) or text(receipt.get('run_id')),'Record an actual returned task/run ID.')
        known={key:d[key] for key in ('task_id','run_id') if text(d.get(key))}
        require(not known or any(receipt.get(key)==value for key,value in known.items()),
                'Receipt must match at least one known execution identity.')
        for key in ('task_id','run_id'):
            if text(receipt.get(key)):
                require(not d.get(key) or d[key]==receipt[key],'Task/run identity changed.')
                d[key]=receipt[key]
    if outcome=='accepted':
        require(d.get('status') not in {'completed','failed','cancelled'},'Terminal work cannot become active again.')
        d.update(delivery='accepted',status=receipt.get('status','running'))
        require(d['status'] in {'running','queued'},'Accepted is distinct from a conclusive terminal receipt.')
        m['prompt']['delivery']='sent';m['status']='running'
        phase='await_result'
    elif outcome=='terminal':
        require(receipt.get('no_pending_work') is True,'Terminal observation must confirm no remaining child/queued work.')
        require(receipt.get('status') in {'completed','failed','cancelled'},'Invalid terminal status.')
        d.update(delivery='accepted',status=receipt['status']);m['status']='to-verify';m['prompt']['delivery']='sent';phase='verify'
    else:
        require(d.get('delivery')=='unknown' and not d.get('task_id') and not d.get('run_id'),'Cannot mark an accepted execution as not delivered.')
        d.update(delivery='not_delivered',status='not_delivered')
        m['prompt']['delivery']='prepared'
        for key,b in d.get('reserved_budget',{}).items():
            counter=data.get('automatic_correction_budget') if key=='global' else m.get('attempts',{}).get(b.get('id'))
            if isinstance(counter,dict) and counter.get('used')==b['after']:
                counter['used']=b['before']
        phase='reconcile'
    d['receipt']=copy.deepcopy(receipt)
    data['continuation']['current_action']={'phase':phase,'mission_ids':[mid],
        'next_step':'Verify observed evidence; retain claim until conclusively released.','sources':[receipt['source']]}
    data['journal'].append(now()+' receipt '+outcome+' for '+mid+' request '+request_id+' source '+receipt['source'])
    return {'recorded':True,'revision_sha256':save(path,data,sha),'outcome':outcome}


def release(path,mid,expected,request_id):
    data,sha=load(path,expected);m=mission(data,mid);d=m.get('dispatch',{})
    require(d.get('request_id')==request_id and isinstance(d.get('claim'),dict),'No recorded owned claim for this request.')
    require(d.get('claim_state')=='held','Claim is not recorded held.')
    receipt=d.get('receipt',{})
    terminal=d.get('delivery')=='accepted' and d.get('status') in {'completed','failed','cancelled'} and receipt.get('outcome')=='terminal' and receipt.get('no_pending_work') is True
    absent=d.get('delivery')=='not_delivered' and receipt.get('outcome')=='not_delivered'
    require(terminal or absent,'Unknown or active execution cannot release its claim.')
    proof={'outcome':'terminal' if terminal else 'not_delivered','source':receipt.get('source')}
    held_claim=d['claim'];result=helper('claims').release(d['claim_remote'],m['ticket'],request_id,held_claim['claim_commit'],proof)
    # A racing edit fails the save, leaving remote release discoverable for reconciliation.
    d.update(claim_state='released',claim_release=result)
    data['journal'].append(now()+' claim conclusively released for '+mid+' request '+request_id)
    return {'released':True,'revision_sha256':save(path,data,sha),'claim':result}


def recover(path,mid,expected,request_id):
    """Attach an independently observed existing token; never recreate a send permit."""
    data,sha=load(path,expected);m=mission(data,mid);d=m.get('dispatch',{})
    require(d.get('request_id')==request_id and text(d.get('claim_remote')),'No matching persisted intent.')
    observed=helper('claims').inspect(d['claim_remote'],m['ticket'])
    require(isinstance(observed,dict) and observed.get('request_id')==request_id and
            observed.get('owner')==d.get('owner') and observed.get('payload_sha256')==d.get('tool_payload_sha256'),
            'No matching remote claim; retain uncertainty and reconcile without sending.')
    if observed.get('phase')=='held':
        d.update(claim=observed,claim_state='held')
    else:
        receipt=d.get('receipt',{})
        terminal=d.get('delivery')=='accepted' and d.get('status') in {'completed','failed','cancelled'} and receipt.get('outcome')=='terminal' and receipt.get('no_pending_work') is True
        absent=d.get('delivery')=='not_delivered' and receipt.get('outcome')=='not_delivered'
        require(observed.get('phase')=='released' and (terminal or absent),'Remote release needs a matching conclusive local observation.')
        d.update(claim_state='released',claim_release=observed)
    data['journal'].append(now()+' recovered existing claim for '+mid+' request '+request_id+'; no call permit issued.')
    return {'recovered':True,'ready_for_authorized_call':False,'revision_sha256':save(path,data,sha),'claim':observed}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('check','prepare','record','release','recover'))
    parser.add_argument('html',type=Path);parser.add_argument('--mission',required=True);parser.add_argument('--expected',required=True)
    parser.add_argument('--purpose',choices=('dispatch','accept'),default='dispatch');parser.add_argument('--finding')
    parser.add_argument('--request-id');parser.add_argument('--owner');parser.add_argument('--payload',type=Path);parser.add_argument('--receipt',type=Path)
    args=parser.parse_args(argv);path=args.html.resolve()
    if args.command=='check':
        data,sha=load(path,args.expected);result=check(data,args.mission,path.parent,args.purpose,args.finding)
        result.update(revision_sha256=sha,grants_user_authorization=False)
        print(json.dumps(result,ensure_ascii=False,indent=2));return 0 if result['ok'] else 1
    if args.command=='prepare':
        require(args.payload is not None,'prepare requires --payload.');result=prepare(path,args.mission,args.expected,args.request_id,args.owner,args.payload,args.finding)
    elif args.command=='record':
        require(args.receipt is not None,'record requires an actual --receipt JSON.');result=record(path,args.mission,args.expected,args.request_id,json.loads(args.receipt.read_bytes()))
    elif args.command=='recover':result=recover(path,args.mission,args.expected,args.request_id)
    else:result=release(path,args.mission,args.expected,args.request_id)
    print(json.dumps(result,ensure_ascii=False,indent=2));return 0


if __name__=='__main__':
    try:sys.exit(main())
    except (ValueError,OSError,TypeError,KeyError,UnicodeError) as error:
        print(json.dumps({'ok':False,'error':str(error)},ensure_ascii=False),file=sys.stderr);sys.exit(1)
