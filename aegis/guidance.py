"""Optional evidence-derived guidance. No fault ledger or model is consulted."""
import time


def explain(latest, level, now=None):
    now = time.time() if now is None else now
    fresh = {key: value for key, value in latest.items() if 0 <= now-value['at'] <= (30 if key[1]=='service' else 6)}
    ids = []
    message = 'There is not enough fresh evidence yet. Start with a liveness check, then compare it with a business request.'
    suggested = {'node': 'gateway-a', 'kind': 'liveness'}
    live = fresh.get(('gateway-a','liveness'))
    business = fresh.get(('gateway-a','business'))
    if live and business:
        ids = [live['id'], business['id']]
        if live['body']['ok'] and not business['body']['ok']:
            message = 'The gateway responds to liveness checks but its business request fails. Its own process is reachable; inspect the catalogue dependency before concluding the gateway is down.'
            suggested = {'node':'catalog-b','kind':'service'}
        elif not live['body']['ok']:
            message = 'The gateway liveness check failed. This does not by itself distinguish a stopped process from a connectivity problem. Inspect its container state and compare the catalogue independently.'
            suggested = {'node':'gateway-a','kind':'service'}
        elif business['body']['ok']:
            message = 'Recent gateway liveness and business checks succeeded. These observations do not establish an active failure; compare the incident timeline before changing anything.'
            suggested = {'node':'catalog-b','kind':'liveness'}
    if level == 'action':
        stopped = [e for (node,kind),e in fresh.items() if kind=='service' and e['body'].get('ok') and e['body'].get('running') is False]
        contradicted = []
        supported = []
        for record in stopped:
            newer_successes = [e for (node, kind), e in fresh.items()
                               if node == record['node'] and kind in ('liveness', 'business')
                               and e['at'] > record['at'] and e['body'].get('ok')]
            if newer_successes:
                contradicted.append((record, max(newer_successes, key=lambda e:e['at'])))
            else:
                supported.append(record)
        stopped = supported
        if stopped:
            record = max(stopped,key=lambda e:e['at'])
            ids = [record['id']]
            message = f"The recent service-state observation reports {record['node']} stopped. You can try starting that selected service, then verify fresh business requests. This does not establish why it stopped."
            suggested = {'node':record['node'],'kind':'service'}
        elif contradicted:
            old, newer = max(contradicted, key=lambda pair:pair[1]['at'])
            ids = [old['id'], newer['id']]
            message = f"The stopped-state observation for {old['node']} is older than a successful check from that node. Its state may have changed. Inspect its container state again before choosing a recovery action."
            suggested = {'node':old['node'],'kind':'service'}
        else:
            message += ' No fresh stopped-container observation supports a targeted start yet. Gather the suggested check first.'
    elif level == 'hint':
        message += ' Compare process state with request behaviour; one failed check is not a complete diagnosis.'
    return {'level':level, 'message':message, 'evidence_ids':ids, 'suggested_check':suggested,
            'source':'deterministic_observation_rules', 'observed_at':now}
