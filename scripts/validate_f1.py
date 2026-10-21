"""Exercise the running Docker lab through its operator API; explicit --run required.

Never starts Docker or changes host configuration. Refuses to interrupt an existing
investigation. Only AegisRack's allow-listed actions are reachable through this API.
"""
import argparse
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from http.client import HTTPException as HTTPProtocolError
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE = 'http://localhost:8787'


class Operator:
    def request(self, path, payload=None):
        headers = {}
        if payload is not None:
            snapshot = self.request('/api/state')
            headers = {'Content-Type': 'application/json', 'Origin': BASE, 'X-Aegis-Token': snapshot['token']}
        request = Request(BASE + path, data=json.dumps(payload).encode() if payload is not None else None, headers=headers)
        try:
            with urlopen(request, timeout=25) as response:
                return json.load(response)
        except HTTPError as error:
            try: detail = json.load(error).get('detail', 'Request failed')
            except ValueError: detail = 'Request failed'
            raise RuntimeError(f'{path}: HTTP {error.code}: {detail}') from error
        except (URLError, OSError, HTTPProtocolError) as error:
            raise RuntimeError('Local controller unreachable; no completion assumed') from error

    def action(self, path, sid, **fields):
        return self.request('/api/' + path, {'session_id': sid, 'key': uuid.uuid4().hex, **fields})

    def wait(self, predicate, description, timeout=45, sid=None):
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            state = self.request('/api/state')
            if sid and (not state['session'] or state['session']['id'] != sid):
                raise RuntimeError('Investigation changed unexpectedly; refusing to control another session')
            if predicate(state):
                return state
            time.sleep(1)
        raise RuntimeError('Timed out: ' + description)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true', help='Authorize stop/start of the two running project services for this validation')
    parser.add_argument('--rounds', type=int, choices=(1, 2, 3), default=3)
    args = parser.parse_args()
    if not args.run:
        parser.error('--run is required; this test deliberately interrupts project services')
    operator = Operator()
    try:
        initial = operator.request('/api/state')
    except RuntimeError as error:
        parser.error(str(error) + '. Start the project and wait for the controller to become ready.')
    if initial['session'] and initial['session']['status'] != 'ended':
        parser.error('An investigation is already active; finish it before validation')
    report = {'started_at': datetime.now(timezone.utc).isoformat(), 'kind': 'Docker operator API F1 walkthrough', 'rounds': [], 'passed': False}
    sid = None
    try:
        for number in range(1, args.rounds+1):
            operator.wait(lambda s:s['baseline_cycles']>=5, 'five healthy baseline cycles')
            round_start = time.monotonic()
            session = operator.request('/api/sessions', {'mode':'practice', 'target':'catalog-b', 'key':uuid.uuid4().hex})
            sid = session['id']
            state = operator.wait(lambda s:s['session']['status']=='investigating', 'fault detection', sid=sid)
            detected = time.monotonic()-round_start
            health = {n['id']:n['health'] for n in state['nodes']}
            if health != {'gateway-a':'degraded','catalog-b':'unavailable','gateway-c':'degraded'}:
                raise RuntimeError('Unexpected measured fault effects: '+str(health))
            evidence = [operator.request('/api/check', {'node':n,'kind':k}) for n,k in
                        [('gateway-a','liveness'),('gateway-a','business'),('catalog-b','service')]]
            if not evidence[0]['body']['ok'] or evidence[1]['body']['ok'] or evidence[2]['body'].get('running') is not False:
                raise RuntimeError('Independent diagnosis evidence did not establish the expected failure')
            operator.action('recover', sid, node='gateway-a')
            time.sleep(7)
            after_wrong = operator.request('/api/state')
            if after_wrong['session']['outcome']=='verified_recovery':
                raise RuntimeError('Wrong-node recovery was incorrectly credited')
            recovery_started=time.monotonic()
            operator.action('recover', sid, node='catalog-b')
            recovered=operator.wait(lambda s:s['session']['status']=='recovered', 'three healthy verification cycles', sid=sid)
            recovery_seconds=time.monotonic()-recovery_started
            operator.request('/api/note', {'session_id':sid, 'explanation':'Catalogue unavailability explains gateway business failures while gateway liveness remained healthy.',
                                          'uncertainty':'The diagnostic evidence alone does not establish why the catalogue stopped.',
                                          'evidence_ids':[e['id'] for e in evidence]})
            operator.action('reset', sid)
            operator.wait(lambda s:s['session']['status']=='ended', 'verified baseline reset', sid=sid)
            report['rounds'].append({'round':number,'session_id':sid,'detection_seconds':round(detected,2),
                                     'recovery_seconds':round(recovery_seconds,2),'evidence_ids':[e['id'] for e in evidence],
                                     'outcome':recovered['session']['outcome']})
            sid = None
            print(f'Round {number}: measured fault, wrong-node attempt, verified recovery and baseline reset passed.', flush=True)
        report['passed'] = True
    except Exception as error:
        report['error'] = str(error)
        print('VALIDATION FAILED:', error)
    finally:
        if sid:
            try:
                current = operator.request('/api/state').get('session')
                if current and current['id']==sid and current['status']!='ended':
                    operator.action('reset',sid)
                    operator.wait(lambda s:s['session']['status']=='ended','cleanup baseline reset',sid=sid)
                    report['cleanup']='baseline verified'
            except Exception as error:
                report['cleanup']='UNRESOLVED: '+str(error)
                print('Cleanup unresolved. Inspect the lab and use the documented project-only manual reset.')
        directory = Path(__file__).resolve().parents[1]/'state'/'validation'
        directory.mkdir(parents=True,exist_ok=True)
        output = directory / ('f1-'+uuid.uuid4().hex+'.json')
        output.write_text(json.dumps(report,indent=2)+'\n')
        print('Validation report:', output)
    return 0 if report['passed'] else 1


if __name__=='__main__':
    raise SystemExit(main())
