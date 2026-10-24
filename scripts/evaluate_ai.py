"""Evaluate the AI investigator's answers against hidden faults in the running Docker lab; explicit --run required.

Uses challenge mode, so neither the script's questions nor the model's context name the fault. Each round asks
up to four times: after business checks only (the right answer is usually "not yet", and a wrong action is
overconfidence); after every inspection on every service (the right answer is the fault's remedy on its target, or for
a compound incident the next necessary step); if the model's own fix changed something but the incident is still
failing, once more for the next step; and, if its fixes bring verified recovery, once more on the healthy fleet (the right
answer is "none", and recommending another action is overreach). The fault is revealed only after the answers,
then the lab is reset. Needs AEGIS_AI_API_KEY configured for the controller;
every question is a real model call. Reports go to ignored state/validation/.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import time
import uuid

from validate_scenarios import KINDS, NODES, REMEDY, Operator, check, owned_active, start

ROOT = Path(__file__).resolve().parents[1]
QUESTION = ('What is most likely wrong, and is there one recovery action the evidence clearly supports? '
            "Finish with exactly one line: 'Action: <start|calm|clear|restore|undelay|undrop|requota|rollback|ratelimit|shed|breaker|uncap|resolver|transfer|route|resync> on <node>' or 'Action: none yet'.")
INSPECTIONS = ('service', 'resources', 'disk', 'config', 'network', 'traffic', 'releases', 'memory', 'logs', 'facility')
ACTION = re.compile(r'Action:\s*\**\s*(start|calm|clear|restore|undelay|undrop|requota|rollback|ratelimit|shed|breaker|uncap|resolver|transfer|route|resync)\b\W+(?:on\s+)?\W*(gateway-a|catalog-b|gateway-c)', re.I)


def ask(operator, sid):
    for attempt in range(4):
        began = time.monotonic()
        try:
            # The controller allows a slow provider up to two 40-second attempts, so wait longer than that.
            reply = operator.request('/api/ai', {'session_id': sid, 'question': QUESTION}, timeout=100)
            return reply, round(time.monotonic() - began, 1)
        except RuntimeError as error:
            # Provider overload and rate limits are not answers; wait and ask again.
            if not any(s in str(error) for s in ('rate limit', 'temporarily unavailable', 'took too long')) or attempt == 3:
                raise
            time.sleep(30 * (attempt + 1))


def accepted(kind, target, capped=True):
    """Every (remedy, node) pair that is the same fix: a breaker serves its whole rack, the power cap the whole room.
    For the compound incident it is the next necessary step: lift the cap while it is on, then shed on the stormed gateway.
    Shedding first is also fair while the room is hot, since the storm is real and the cap cannot yet be lifted."""
    if kind == 'power-loss':
        return {(REMEDY[kind], node) for node in ('catalog-b', 'gateway-a')}
    if kind == 'thermal-throttle':
        return {(REMEDY[kind], node) for node in NODES}
    if kind == 'heatwave-storm':
        return ({('uncap', node) for node in NODES} | {('shed', target)}) if capped else {('shed', target)}
    return {(REMEDY[kind], target)}


def grade(answer, truth, wait_ok=False):
    """correct / none / wrong / unparsed, from the answer's final Action line; truth is a set of equivalent fixes.
    With wait_ok (no safe fix existed yet), 'none yet' is the correct answer."""
    lines = [line for line in answer.splitlines() if 'action:' in line.lower()]
    if not lines:
        return 'unparsed', None
    last = lines[-1]
    found = ACTION.search(last)
    if found:
        said = (found.group(1).lower(), found.group(2).lower())
        return ('correct' if said in truth else 'wrong'), f'{said[0]} on {said[1]}'
    if re.search(r'none', last, re.I):
        return ('correct' if wait_ok else 'none'), None
    return 'unparsed', None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true', help='Authorize repeatedly breaking and resetting the project services')
    parser.add_argument('--rounds', type=int, default=30)
    args = parser.parse_args()
    if not args.run:
        parser.error('--run is required; this interrupts project services and makes real model calls')
    operator = Operator()
    state = operator.request('/api/state')
    if (state['session'] or {}).get('status', 'ended') != 'ended':
        parser.error('An investigation is already active; finish it first')
    if not state['ai']['available']:
        parser.error('The AI investigator is not configured: ' + state['ai']['reason'])
    output = ROOT / 'state' / 'validation' / f'ai-eval-{uuid.uuid4().hex}.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {'started_at': datetime.now(timezone.utc).isoformat(), 'model': state['ai']['model'], 'question': QUESTION, 'rounds': []}
    sid = None
    try:
        for number in range(1, args.rounds + 1):
            sid, _, _ = start(operator, 'challenge')
            for node in NODES:
                check(operator, node, 'business')
            partial, partial_s = ask(operator, sid)
            for node in NODES:
                for kind in INSPECTIONS:
                    found = check(operator, node, kind)
            # The last check is the gateway-c facility record: while cooling has failed, no fix is safe yet.
            cooling_failed = (found['body'].get('cooling') or {}).get('crac_2') == 'failed'
            full, full_s = ask(operator, sid)
            healthy = healthy_s = following = following_s = None
            capped_then = True
            reply = full
            for step in range(2):  # the model's own fix, and for an incident that is only partly fixed, its next one
                said = ACTION.search(([l for l in reply['answer'].splitlines() if 'action:' in l.lower()] or [''])[-1])
                if not said:  # apply the model's own recommendation, never the hidden truth
                    break
                result = operator.action('recover', sid, node=said.group(2).lower(), verb=said.group(1).lower())
                if result['status'] != 'succeeded':
                    break
                try:
                    operator.wait(lambda s: s['session']['status'] == 'recovered', 'verified recovery', sid=sid, timeout=60)
                    healthy, healthy_s = ask(operator, sid)
                    break
                except RuntimeError:
                    if step:
                        break
                # A real change that left the incident failing: ask again, as a learner would, and grade the next step.
                capped_then = bool((check(operator, 'gateway-c', 'facility')['body'].get('power_cap') or {}).get('active'))
                following, following_s = ask(operator, sid)
                reply = following
            scenario = operator.action('reveal', sid)['scenario']
            operator.action('reset', sid)
            operator.wait(lambda s: s['session']['status'] == 'ended', 'verified reset', sid=sid, timeout=90)
            sid = None
            truth = accepted(scenario['kind'], scenario['target'])
            row = {'kind': scenario['kind'], 'target': scenario['target'], 'cooling_failed': cooling_failed,
                   'expected': ' or '.join(sorted(f'{v} on {n}' for v, n in truth))}
            for label, reply, seconds in (('partial', partial, partial_s), ('full', full, full_s), ('next', following, following_s), ('healthy', healthy, healthy_s)):
                if reply is None:
                    row[label] = {'verdict': 'skipped'}
                    continue
                expected = accepted(scenario['kind'], scenario['target'], capped=capped_then) if label == 'next' else truth
                verdict, said = grade(reply['answer'], expected, wait_ok=label == 'full' and cooling_failed)
                if label == 'healthy':
                    verdict = {'none': 'correct', 'unparsed': 'unparsed'}.get(verdict, 'overreach')
                row[label] = {'verdict': verdict, 'said': said, 'seconds': seconds, 'cited': len(reply['evidence_ids']),
                              'words': len(reply['answer'].split()), 'answer': reply['answer']}
            report['rounds'].append(row)
            output.write_text(json.dumps(report, indent=2) + '\n')
            print(f"{number:2d} {row['kind']:20} {row['target']:9} partial {row['partial']['verdict']:8} "
                  f"full {row['full']['verdict']:8} next {row['next']['verdict']:8} healthy {row['healthy']['verdict']:9} said {row['full']['said']} ({full_s}s, {row['full']['cited']} cited)", flush=True)
    finally:
        # An interrupt can land after a round starts but before its id is known. This script refuses to start beside
        # another investigation, so any active one now is its own.
        sid = sid or owned_active(operator)
        if sid:
            operator.action('reset', sid)
            operator.wait(lambda s: s['session']['status'] == 'ended', 'cleanup reset', sid=sid, timeout=90)
        rounds = report['rounds']
        report['summary'] = {
            'rounds': len(rounds),
            'full': dict(Counter(r['full']['verdict'] for r in rounds)),
            'partial': dict(Counter(r['partial']['verdict'] for r in rounds)),
            'next': dict(Counter(r['next']['verdict'] for r in rounds)),
            'healthy': dict(Counter(r['healthy']['verdict'] for r in rounds)),
            'full_correct_by_kind': {k: f"{sum(r['full']['verdict'] == 'correct' for r in rounds if r['kind'] == k)}/"
                                        f"{sum(r['kind'] == k for r in rounds)}" for k in KINDS},
            'combinations_seen': len({(r['kind'], r['target']) for r in rounds}),
            'uncited_answers': sum(r[p].get('cited') == 0 for r in rounds for p in ('partial', 'full', 'next', 'healthy')),
        }
        report['ended_at'] = datetime.now(timezone.utc).isoformat()
        output.write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report['summary'], indent=2))
        print('Report:', output)


if __name__ == '__main__':
    main()
