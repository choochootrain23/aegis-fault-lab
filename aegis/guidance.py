"""Optional evidence-derived guidance. No fault ledger or model is consulted."""
from datetime import datetime, timezone
import time

SLOW_MS = 300  # request checks slower than this count as degraded, not healthy
INSPECTIONS = ('service', 'resources', 'disk', 'config', 'trace', 'network', 'traffic', 'releases', 'memory', 'facility')
TIMEOUTS = ('ConnectTimeout', 'ReadTimeout', 'WriteTimeout', 'PoolTimeout')
GATEWAYS = ('gateway-a', 'gateway-c')


def healthy(live, business):
    return all(e['body'].get('ok') and not e['body'].get('slow') for e in (live, business))


def explain(latest, level, now=None):
    now = time.time() if now is None else now
    fresh = {key: value for key, value in latest.items() if 0 <= now-value['at'] <= (30 if key[1] in INSPECTIONS else 6)}
    ids = []
    message = 'There is not enough fresh evidence yet. Start with a liveness check, then compare it with a business request.'
    suggested = {'node': 'gateway-a', 'kind': 'liveness'}
    observed = [(g, fresh[(g, 'liveness')], fresh[(g, 'business')]) for g in GATEWAYS
                if (g, 'liveness') in fresh and (g, 'business') in fresh]
    if observed:
        # Focus on a gateway that shows a problem; the others show how far it reaches.
        g, live, business = next((o for o in observed if not healthy(o[1], o[2])), observed[0])
        ids = [live['id'], business['id']]
        result = business['body'].get('result') or {}
        if live['body']['ok'] and not business['body']['ok'] and (business['body'].get('http_status') == 507 or result.get('errno') == 'ENOSPC'):
            message = f'{g} is live, but its business request failed with HTTP 507 (insufficient storage) from its own receipt write. Check its disk usage.'
            suggested = {'node':g,'kind':'disk'}
        elif live['body']['ok'] and not business['body']['ok'] and result.get('upstream_status') == 507:
            message = f'{g} is live, but the catalogue answered its request with HTTP 507 (insufficient storage). The catalogue is reachable yet cannot write; check its disk usage.'
            suggested = {'node':'catalog-b','kind':'disk'}
        elif live['body']['ok'] and not business['body']['ok'] and business['body'].get('http_status') == 500 and result.get('error') == 'TypeError':
            message = f"{g} is live, but its own code failed the business request with HTTP 500 ({result.get('error')}{', version ' + result['version'] if result.get('version') else ''}). Nothing about the machine is implied: an error from the service's own code after it was working usually follows a change. Check what changed recently in its release history."
            suggested = {'node':g,'kind':'releases'}
        elif live['body']['ok'] and not business['body']['ok'] and result.get('upstream_status') == 500 and result.get('upstream_error') == 'TypeError':
            message = f'{g} is live, but the catalogue answered its request with HTTP 500 from an error in its own code (TypeError). The catalogue is reachable but its code is failing; compare its release history with its running config to see what changed.'
            suggested = {'node':'catalog-b','kind':'releases'}
        elif live['body']['ok'] and not business['body']['ok'] and result.get('upstream_status') == 429:
            message = f'{g} is live, but the catalogue refused its request with HTTP 429 (too many requests: {result.get("upstream_error") or "quota exceeded"}). The catalogue is up and answering; it is rejecting this caller. Its own health checks may still pass, because they come from a different caller. Check the catalogue\'s traffic and quotas.'
            suggested = {'node':'catalog-b','kind':'traffic'}
        elif live['body']['ok'] and not business['body']['ok'] and result.get('error') == 'NameResolutionError':
            message = (f"{g} is live, but its business request failed before reaching the catalogue: it could not resolve the catalogue's name ({result.get('detail') or 'no address'}). "
                       "A name stops resolving when the resolver is broken, and also when the service behind it is gone: Docker's resolver drops the name of a stopped container. "
                       + (f"The catalogue answers its own requests, so look at {g}'s resolver in its network path." if catalogue_healthy(fresh)
                          else "Check whether the catalogue is running before blaming the resolver."))
            suggested = {'node':g,'kind':'network'} if catalogue_healthy(fresh) else {'node':'catalog-b','kind':'service'}
        elif live['body']['ok'] and not business['body']['ok'] and (result.get('error') == 'UpstreamBusy' or result.get('upstream_error') == 'DeadlineExceeded'):
            message = (f"{g} is live, but its business request failed because {g} itself is backed up: "
                       + ("all its slots for calling the catalogue were busy" if result.get('error') == 'UpstreamBusy' else "its call reached the catalogue only after the deadline had passed")
                       + ". The catalogue is not the bottleneck; something is slowing the gateway. Check its own resources.")
            suggested = {'node':g,'kind':'resources'}
        elif live['body']['ok'] and not business['body']['ok'] and result.get('error') in TIMEOUTS and catalogue_healthy(fresh):
            ids.append(fresh[('catalog-b', 'business')]['id'])
            message = (f"{g} is live, but its call to the catalogue timed out ({result.get('error')}) rather than being refused, while the catalogue answers its own business requests quickly. "
                       "A timeout without a slow service means packets went missing on the way: a lost connection setup or reply. If the same request sometimes works, compare the network path of both ends.")
            suggested = {'node':'catalog-b','kind':'network'}
        elif live['body']['ok'] and not business['body']['ok'] and result.get('upstream_status') == 500:
            message = f'{g} is live, but the catalogue answered its request with HTTP 500. The catalogue is reachable yet failing internally; compare its logs and running config with what was deployed.'
            suggested = {'node':'catalog-b','kind':'config'}
        elif live['body']['ok'] and not live['body'].get('slow') and not business['body']['ok'] and catalogue_healthy(fresh):
            catalogue = fresh[('catalog-b', 'business')]
            ids.append(catalogue['id'])
            message = f'{g} is live but its business request fails, while the catalogue answers its own business requests quickly. The catalogue works, so the break is on {g}\'s path to it: trace one of its requests and compare its running config with its deployed settings.'
            suggested = {'node':g,'kind':'config'}
        elif live['body']['ok'] and not business['body']['ok'] and slow_catalogue(fresh):
            catalogue = slow_catalogue(fresh)
            ids.append(catalogue['id'])
            message = f"{g} is live but its business request fails, and the catalogue it depends on answers slowly ({catalogue['body'].get('duration_ms')} ms). Slow upstream replies make the gateway's requests time out; compare the catalogue's resource use, traffic and network path."
            suggested = {'node':'catalog-b','kind':'resources'}
        elif live['body']['ok'] and live['body'].get('slow') and not business['body']['ok']:
            message = (f"{g}'s business request fails and it is slow even to liveness checks ({live['body'].get('duration_ms')} ms), which do almost no work. "
                       "Something is holding the gateway itself back: check its own resources before looking further.")
            suggested = {'node':g,'kind':'resources'}
        elif live['body']['ok'] and not business['body']['ok']:
            message = f'{g} responds to liveness checks but its business request fails. Its own process is reachable; inspect the catalogue dependency before concluding the gateway is down.'
            suggested = {'node':'catalog-b','kind':'service'}
        elif not live['body']['ok']:
            message = f'The {g} liveness check failed. This does not by itself distinguish a stopped process from a connectivity problem. Inspect its container state and compare the catalogue independently.'
            suggested = {'node':g,'kind':'service'}
        elif live['body'].get('slow') or business['body'].get('slow'):
            slow_node = g if live['body'].get('slow') else 'catalog-b'
            message = (f"{g}'s requests succeed but slowly ({business['body'].get('duration_ms')} ms, above {SLOW_MS} ms). "
                       + ('It is slow even to liveness checks, so look at its own resources.' if slow_node == g
                          else 'Its liveness is quick, so the delay is probably in the catalogue it depends on; compare the catalogue\'s latency, resource usage and traffic.'))
            suggested = {'node':slow_node,'kind':'resources'}
            cpu = fresh.get((slow_node, 'resources'))
            if cpu and cpu['body'].get('ok') and (cpu['body'].get('cpu_pct_of_limit') or 0) < 50:
                ids.append(cpu['id'])
                message += f" Its recent CPU use is normal ({cpu['body'].get('cpu_pct_of_limit')}% of its limit), so the time is probably spent elsewhere: check its network path."
                suggested = {'node':slow_node,'kind':'network'}
        else:
            message = 'Recent gateway liveness and business checks succeeded. These observations do not establish an active failure; compare the incident timeline before changing anything.'
            suggested = {'node':'catalog-b','kind':'liveness'}
        # The other gateway shows the boundary: a shared fault reaches both, a local one does not.
        for other, other_live, other_business in observed:
            if other == g or healthy(live, business):
                continue
            ids.append(other_business['id'])
            if healthy(other_live, other_business):
                message += f' {other} is healthy at the same time, so whatever is wrong is probably local to {g} rather than the catalogue they share.'
            else:
                message += f' {other} is affected too, which points at what both gateways share: the catalogue.'
    if level == 'action':
        found = remedy(fresh)
        if found:
            ids, message, suggested = found
        else:
            message += ' No fresh inspection yet supports a specific recovery action. Gather the suggested check first.'
    elif level == 'hint':
        message += ' Compare process state, latency, storage and running config with request behaviour; one failed check is not a complete diagnosis.'
    return {'level':level, 'message':message, 'evidence_ids':ids, 'suggested_check':suggested,
            'source':'deterministic_observation_rules', 'observed_at':now}


def started_seconds_before(body, at):
    """How long the container had been running when a broker observation was taken, from Docker's start time."""
    try:
        started = datetime.strptime(body['started_at'][:19], '%Y-%m-%dT%H:%M:%S').replace(tzinfo=timezone.utc).timestamp()
    except (KeyError, TypeError, ValueError):
        return None
    return at - started


DEPLOYED_CPU = 0.25  # every service's deployed CPU limit
HEAVY_RPS = 20  # a caller without a quota sending more than this a second, now, is flooding a service this small


def heavy_callers(record):
    """Callers in a traffic observation sending heavily with no quota of any kind, busiest first."""
    result = (record or {}).get('body', {}).get('result') or {}
    limited = {q.get('client') for q in result.get('quotas', [])}
    return sorted((c for c in result.get('clients', []) if (c.get('recent_rps') or 0) > HEAVY_RPS and c.get('client') not in limited and c.get('client') != 'controller'
                   and retry_share(c) <= 0.5),
                  key=lambda c: -(c.get('requests') or 0))


def retry_storm(record):
    """The busiest caller whose requests are mostly retries of failed ones, if it is sending heavily now."""
    result = (record or {}).get('body', {}).get('result') or {}
    stormy = [c for c in result.get('clients', []) if (c.get('recent_rps') or 0) > HEAVY_RPS and retry_share(c) > 0.5]
    return max(stormy, key=lambda c: c['requests']) if stormy else None


def retry_share(caller):
    """The share of a caller's requests that are retries, over the last 10 seconds when reported, else the last minute."""
    if caller.get('recent_retries') is not None and caller.get('recent_rps'):
        return caller['recent_retries'] / (caller['recent_rps'] * 10)
    return (caller.get('retries') or 0) / caller['requests'] if caller.get('requests') else 0


def sagging(body):
    """A facility record whose active feed is below what its power supplies tolerate."""
    feeds = (body or {}).get('feeds') or {}
    return (feeds.get((body or {}).get('active_feed', 'A')) or {}).get('status') == 'sagging'


def resolver_broken(dns):
    """A lookup that failed at a changed resolver, or while the service behind the name still answers: a broken resolver
    rather than a name that is gone, as a stopped container's is."""
    return bool(dns and dns.get('ok') is False and (dns.get('changed') or dns.get('connect_last_good_ms') is not None))


def catalogue_healthy(fresh):
    record = fresh.get(('catalog-b', 'business'))
    return bool(record and record['body'].get('ok') and not record['body'].get('slow'))


def slow_catalogue(fresh):
    record = next((fresh.get(('catalog-b', kind)) for kind in ('business', 'liveness') if fresh.get(('catalog-b', kind))), None)
    return record if record and record['body'].get('ok') and record['body'].get('slow') else None


def newer_success(fresh, record):
    newer = [e for (node, kind), e in fresh.items() if node == record['node'] and kind in ('liveness', 'business')
             and e['at'] > record['at'] and e['body'].get('ok') and not e['body'].get('slow')]
    return max(newer, key=lambda e: e['at']) if newer else None


def remedy(fresh):
    """Most recent inspection that supports one recovery action, unless newer healthy checks contradict it."""
    candidates, contradicted, suggest = [], [], None
    for (node, kind), record in fresh.items():
        body = record['body']
        if not body.get('ok'):
            continue
        if kind == 'service' and body.get('running') is False and (fresh.get((node, 'facility')) or {}).get('body', {}).get('breaker') == 'open':
            continue  # the facility check explains it: no power, so starting cannot work
        if kind == 'service' and body.get('running') is False and sagging((fresh.get((node, 'facility')) or {}).get('body')):
            continue  # the facility check explains it: starting only lasts until the next dip
        if kind == 'facility' and sagging(body):
            feed = body['feeds'][body.get('active_feed', 'A')]
            drops = [e for e in body.get('events', []) if e.get('event') == 'power supply dropout']
            action = (f"The facility check (simulated) shows feed {body.get('active_feed', 'A')} to {body.get('pdu')} sagging to {feed['voltage_v']} V, below the {body.get('psu_min_v')} V its power supplies need, "
                      f"with {len(drops)} power-supply dropout{'s' if len(drops) != 1 else ''} on {' and '.join(body.get('fed_services', []))}; the redundant feed is normal. "
                      "Each dropout cuts the server's power, so starting the service only lasts until the next dip. You can try moving the rack to the redundant feed, then verify it stays up for ten cycles.")
        elif kind == 'facility' and body.get('breaker') == 'open':
            action = (f"The facility check (simulated) shows the {body.get('pdu')} breaker open: rack {body.get('rack')} has no power, so "
                      f"{' and '.join(body.get('fed_services', []))} lost power together. Starting them cannot work without power. "
                      "You can try resetting the breaker, then verify every service it feeds comes back.")
        elif kind == 'facility' and (body.get('power_cap') or {}).get('active') and (body.get('cooling') or {}).get('crac_2') == 'failed':
            action = (f"The facility check (simulated) for {node}'s rack shows cooling unit CRAC-2 failed, the inlet at {body.get('inlet_c')} °C and a power cap active on every rack. "
                      "Lifting the cap while the room is this hot would overheat it, so there is no safe fix yet: check the facility again until cooling is restored.")
            suggest = 'facility'
        elif kind == 'facility' and (body.get('power_cap') or {}).get('active'):
            action = (f"The facility check (simulated) for {node}'s rack shows cooling restored and the inlet back to {body.get('inlet_c')} °C, but the power cap is still active. "
                      "You can try lifting the power cap, then verify latency returns to normal everywhere.")
        elif kind == 'resources' and body.get('cpu_limit') and body['cpu_limit'] < DEPLOYED_CPU:
            action = (f"The resource check shows {node}'s CPU limit at {body['cpu_limit']:g}, below the {DEPLOYED_CPU:g} it was deployed with: something has capped it, "
                      "which a power or thermal cap does. Check the facility before changing anything on the service.")
            suggest = 'facility'
        elif kind == 'service' and body.get('running') is False and body.get('oom_killed'):
            action = f"The recent service-state observation reports {node} stopped by the kernel for running out of memory (OOMKilled, exit code {body.get('exit_code')}). Starting it may only repeat the crash: check its memory trend and release history before choosing a fix."
            suggest = 'memory'
        elif kind == 'service' and body.get('running') is False:
            action = f"The recent service-state observation reports {node} stopped. You can try starting that selected service, then verify fresh business requests. This does not establish why it stopped."
        elif kind == 'resources' and (body.get('cpu_pct_of_limit') or 0) >= 80 and (age := started_seconds_before(body, record['at'])) is not None and age < 15:
            action = f"The resource check shows {node} at {body['cpu_pct_of_limit']:g}% of its CPU limit, but its container started only seconds earlier, and a process uses its whole limit while it boots. That alone is not starvation: check resources again in a few seconds, and its container state and memory to see why it restarted."
            suggest = 'memory'
        elif kind == 'resources' and (body.get('cpu_pct_of_limit') or 0) >= 80:
            if node == 'catalog-b' and heavy_callers(fresh.get((node, 'traffic'))):
                continue  # the traffic check explains the CPU better; its own rule recommends the fix
            action = (f"The resource check shows {node} using {body['cpu_pct_of_limit']:g}% of its {body.get('cpu_limit', 0):g}-CPU limit, so the kernel is throttling it. "
                      "Something is using that CPU: if its logs show a background job starting, you can try stopping the runaway job; "
                      + ("if its traffic shows one caller sending most requests, rate-limit that client instead." if node == 'catalog-b'
                         else "if its traffic shows most requests are retries, callers are feeding an overload and load shedding is the fix.")
                      + f" Then verify latency returns below {SLOW_MS} ms.")
            suggest = None if (node, 'traffic') in fresh else 'traffic'
        elif kind == 'disk' and (body.get('result') or {}).get('free_bytes') is not None and body['result']['free_bytes'] < 64 * 1024:
            largest = (body['result'].get('largest') or [{}])[0].get('path', 'one file')
            action = f"Disk usage shows {node}'s /tmp full, mostly {largest}. You can try clearing its temporary files, then verify business requests write receipts again."
        elif kind == 'config' and (body.get('result') or {}).get('drifted'):
            setting = next(s for s in body['result']['settings'] if s['drifted'])
            action = f"The running config on {node} differs from its deployed settings: {setting['key']} is {setting['value']} but {setting['deployed']} was deployed. You can try restoring the deployed config, then verify business requests succeed again."
        elif kind == 'network' and resolver_broken((body.get('result') or {}).get('dns')):
            dns = body['result']['dns']
            reachable = (f" A TCP connection to its last known address, {dns['last_good_address']}, took {dns['connect_last_good_ms']:g} ms, so the service itself is reachable; only the name fails."
                         if dns.get('connect_last_good_ms') is not None else '')
            changed = (f" The resolver uses {dns['nameserver']}, set by {dns.get('pushed_by')}, rather than the {dns['deployed_nameserver']} it was deployed with."
                       if dns.get('changed') else '')
            action = (f"{node} cannot resolve {dns['name']}: {dns.get('error')}.{changed}{reachable} "
                      "You can try restoring the resolver, then verify business requests succeed again.")
        elif kind == 'network' and any(q.get('kind') == 'netem' and q.get('loss_pct') for q in (body.get('result') or {}).get('qdiscs', [])):
            queue = next(q for q in body['result']['qdiscs'] if q.get('kind') == 'netem')
            tcp = body['result'].get('tcp') or {}
            resent = f", and {tcp['retrans_pct']:g}% of the TCP segments it sent had to be resent" if tcp.get('retrans_pct') is not None else ''
            action = f"Traffic leaving {node} passes a netem queue that drops {queue['loss_pct']:g}% of packets{resent}. You can try removing the packet loss, then verify requests succeed reliably, not just once."
        elif kind == 'traffic' and retry_storm(record):
            caller = retry_storm(record)
            share = round(100 * retry_share(caller))
            action = (f"{node} received {caller['requests']} requests in the last minute from {caller['client']}, and {share}% of them were retries of requests that had just failed. "
                      f"Callers are retrying faster than it can serve, so the overload feeds itself even if whatever started it has stopped. You can try enabling load shedding on {node}, then verify its requests succeed again.")
        elif kind == 'traffic' and node == 'catalog-b' and heavy_callers(record):  # only the catalogue rate-limits; heavy users at a gateway are its traffic
            top = heavy_callers(record)[0]
            action = (f"{node} received {top['requests']} requests in the last minute from {top['client']}, about {top['recent_rps']:g} a second right now, from a caller with no quota: "
                      "one client is using most of its capacity. You can try rate-limiting the top client, then verify the other callers' requests are fast again.")
        elif kind == 'traffic' and any(q.get('limit_rps') == 0 for q in (body.get('result') or {}).get('quotas', [])):
            zero = [q['client'] for q in body['result']['quotas'] if q.get('limit_rps') == 0]
            rejected = sum(c.get('rejected', 0) for c in body['result'].get('clients', []))
            action = f"{node} enforces a request quota of 0 per second for {' and '.join(zero)}, set by {(body['result'].get('quota_source') or {}).get('pushed_by', 'the quota system')} rather than its deployed {body['result']['quotas'][0].get('deployed_rps')}; {rejected} requests were rejected in the last minute. You can try resetting the client quotas, then verify the gateways' business requests succeed."
        elif kind == 'memory' and (body.get('oom_killed') or ((body.get('result') or {}).get('growth_mib_s') or 0) >= 4):
            memory = body.get('result') or {}
            seen = (f"memory growing {memory['growth_mib_s']:g} MiB a second, now {memory.get('current_mib')} of {memory.get('limit_mib')} MiB" if (memory.get('growth_mib_s') or 0) >= 4
                    else f"its last exit an out-of-memory kill (exit code {body.get('exit_code')})")
            running = f" on release {memory['version']}" if memory.get('version') else ''
            action = f"The memory check shows {node}{running} with {seen}. A leak returns after every restart, so starting it again will not hold. You can try rolling back the release, then verify it stays up for ten cycles."
        elif kind == 'releases' and (body.get('result') or {}).get('history') and body['result']['running'] != body['result']['history'][0].get('version'):
            history = body['result']['history']
            action = f"{node} runs {body['result']['running']}, deployed by {history[-1].get('by')} after this process started on {history[0].get('version')}. If its failures began with that deploy, you can try rolling back the release, then verify business requests succeed reliably."
        elif kind == 'network' and any(q.get('kind') == 'netem' and q.get('delay_ms') for q in (body.get('result') or {}).get('qdiscs', [])):
            queue = next(q for q in body['result']['qdiscs'] if q.get('kind') == 'netem')
            action = f"Traffic leaving {node} passes a netem queue that adds {queue['delay_ms']:g} ms, and new connections take about {body.get('median_connect_ms')} ms. You can try removing the traffic delay, then verify its latency returns below {SLOW_MS} ms."
        else:
            continue
        newer = newer_success(fresh, record) if kind == 'service' else None
        (contradicted if newer else candidates).append((record, action, newer, suggest))
        suggest = None
    if candidates:
        record, action, _, then = max(candidates, key=lambda c: c[0]['at'])
        return [record['id']], action, {'node': record['node'], 'kind': then or record['kind']}
    if contradicted:
        old, _, newer, _ = max(contradicted, key=lambda c: c[2]['at'])
        return ([old['id'], newer['id']], f"The stopped-state observation for {old['node']} is older than a successful check from that node. Its state may have changed. Inspect its container state again before choosing a recovery action.",
                {'node': old['node'], 'kind': 'service'})
    return None
