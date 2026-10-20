"""Optional evidence-derived guidance. No fault ledger or model is consulted."""
import time

SLOW_MS = 300  # request checks slower than this count as degraded, not healthy
INSPECTIONS = ('service', 'resources', 'disk')


def explain(latest, level, now=None):
    now = time.time() if now is None else now
    fresh = {key: value for key, value in latest.items() if 0 <= now-value['at'] <= (30 if key[1] in INSPECTIONS else 6)}
    ids = []
    message = 'There is not enough fresh evidence yet. Start with a liveness check, then compare it with a business request.'
    suggested = {'node': 'gateway-a', 'kind': 'liveness'}
    live = fresh.get(('gateway-a','liveness'))
    business = fresh.get(('gateway-a','business'))
    if live and business:
        ids = [live['id'], business['id']]
        result = business['body'].get('result') or {}
        if live['body']['ok'] and not business['body']['ok'] and (business['body'].get('http_status') == 507 or result.get('errno') == 'ENOSPC'):
            message = 'The gateway is live, but its business request failed with HTTP 507 (insufficient storage) from its own receipt write. Check the gateway\'s disk usage.'
            suggested = {'node':'gateway-a','kind':'disk'}
        elif live['body']['ok'] and not business['body']['ok'] and result.get('upstream_status') == 507:
            message = 'The gateway is live, but the catalogue answered its request with HTTP 507 (insufficient storage). The catalogue is reachable yet cannot write; check its disk usage.'
            suggested = {'node':'catalog-b','kind':'disk'}
        elif live['body']['ok'] and not business['body']['ok']:
            message = 'The gateway responds to liveness checks but its business request fails. Its own process is reachable; inspect the catalogue dependency before concluding the gateway is down.'
            suggested = {'node':'catalog-b','kind':'service'}
        elif not live['body']['ok']:
            message = 'The gateway liveness check failed. This does not by itself distinguish a stopped process from a connectivity problem. Inspect its container state and compare the catalogue independently.'
            suggested = {'node':'gateway-a','kind':'service'}
        elif live['body'].get('slow') or business['body'].get('slow'):
            slow_node = 'gateway-a' if live['body'].get('slow') else 'catalog-b'
            message = (f"Gateway requests succeed but slowly ({business['body'].get('duration_ms')} ms, above {SLOW_MS} ms). "
                       + ('The gateway is slow even to liveness checks, so look at its own resources.' if slow_node == 'gateway-a'
                          else 'Its liveness is quick, so the delay is probably in the catalogue it depends on; compare the catalogue\'s latency and resource usage.'))
            suggested = {'node':slow_node,'kind':'resources'}
        elif business['body']['ok']:
            message = 'Recent gateway liveness and business checks succeeded. These observations do not establish an active failure; compare the incident timeline before changing anything.'
            suggested = {'node':'catalog-b','kind':'liveness'}
    if level == 'action':
        found = remedy(fresh)
        if found:
            ids, message, suggested = found
        else:
            message += ' No fresh inspection yet supports a specific recovery action. Gather the suggested check first.'
    elif level == 'hint':
        message += ' Compare process state, latency and storage with request behaviour; one failed check is not a complete diagnosis.'
    return {'level':level, 'message':message, 'evidence_ids':ids, 'suggested_check':suggested,
            'source':'deterministic_observation_rules', 'observed_at':now}


def newer_success(fresh, record):
    newer = [e for (node, kind), e in fresh.items() if node == record['node'] and kind in ('liveness', 'business')
             and e['at'] > record['at'] and e['body'].get('ok') and not e['body'].get('slow')]
    return max(newer, key=lambda e: e['at']) if newer else None


def remedy(fresh):
    """Most recent inspection that supports one recovery action, unless newer healthy checks contradict it."""
    candidates, contradicted = [], []
    for (node, kind), record in fresh.items():
        body = record['body']
        if not body.get('ok'):
            continue
        if kind == 'service' and body.get('running') is False:
            action = f"The recent service-state observation reports {node} stopped. You can try starting that selected service, then verify fresh business requests. This does not establish why it stopped."
        elif kind == 'resources' and (body.get('cpu_pct_of_limit') or 0) >= 80:
            action = f"The resource check shows {node} using {body['cpu_pct_of_limit']:g}% of its {body.get('cpu_limit', 0):g}-CPU limit, so the kernel is throttling it. Its logs may show what started; you can try stopping the runaway job, then verify latency returns below {SLOW_MS} ms."
        elif kind == 'disk' and (body.get('result') or {}).get('free_bytes') is not None and body['result']['free_bytes'] < 64 * 1024:
            largest = (body['result'].get('largest') or [{}])[0].get('path', 'one file')
            action = f"Disk usage shows {node}'s /tmp full, mostly {largest}. You can try clearing its temporary files, then verify business requests write receipts again."
        else:
            continue
        newer = newer_success(fresh, record) if kind == 'service' else None
        (contradicted if newer else candidates).append((record, action, newer))
    if candidates:
        record, action, _ = max(candidates, key=lambda c: c[0]['at'])
        return [record['id']], action, {'node': record['node'], 'kind': record['kind']}
    if contradicted:
        old, _, newer = max(contradicted, key=lambda c: c[2]['at'])
        return ([old['id'], newer['id']], f"The stopped-state observation for {old['node']} is older than a successful check from that node. Its state may have changed. Inspect its container state again before choosing a recovery action.",
                {'node': old['node'], 'kind': 'service'})
    return None
