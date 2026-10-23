"""Contract tests against the broker's Docker HTTP transport, not live Docker."""
import asyncio
import copy
import json

import httpx
import pytest

from aegis.broker import Docker, Operation, PROJECT
from test_boundaries import target


def test_broker_filter_identity_and_fixed_operation_contract():
    async def exercise():
        inspected=target()
        inspected.update(Id='a'*64,State={'Running':True,'Status':'running','ExitCode':0})
        calls=[]
        def transport(request):
            calls.append(request)
            if request.url.path.endswith('/containers/json'):
                filters=json.loads(request.url.params['filters'])
                assert filters=={'label':[f'com.docker.compose.project={PROJECT}','com.docker.compose.service=catalog-b']}
                return httpx.Response(200,json=[{'Id':inspected['Id']}])
            if request.url.path.endswith('/stop'):
                assert request.method=='POST'
                assert request.url.params['t']=='3'
                inspected['State'].update(Running=False,Status='exited')
                return httpx.Response(204)
            assert request.url.path.endswith('/'+inspected['Id']+'/json')
            return httpx.Response(200,json=copy.deepcopy(inspected))
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport),base_url='http://docker/v1.45') as client:
            result=await Docker(client).operate(Operation(target='catalog-b',verb='stop',generation='a'*64))
            assert result['running'] is False
            assert len([r for r in calls if r.method=='POST'])==1
    asyncio.run(exercise())


def fake_docker(inspected, calls):
    def transport(request):
        calls.append(request)
        if request.url.path.endswith('/containers/json'):
            return httpx.Response(200,json=[{'Id':inspected['Id']}])
        if request.url.path.endswith('/stats'):
            assert request.url.params['stream']=='false'
            return httpx.Response(200,json={'cpu_stats':{'cpu_usage':{'total_usage':1_250_000_000},'system_cpu_usage':20_000_000_000,'online_cpus':2,
                                                         'throttling_data':{'periods':100,'throttled_periods':80,'throttled_time':4_000_000_000}},
                                            'precpu_stats':{'cpu_usage':{'total_usage':1_000_000_000},'system_cpu_usage':18_000_000_000},
                                            'memory_stats':{'usage':40*2**20,'limit':192*2**20}})
        return httpx.Response(200,json=copy.deepcopy(inspected))
    return transport


def test_resources_reports_cpu_rate_against_limit_without_mutation():
    async def exercise():
        inspected=target();inspected.update(Id='a'*64,State={'Running':True,'Status':'running','ExitCode':0})
        calls=[]
        async with httpx.AsyncClient(transport=httpx.MockTransport(fake_docker(inspected,calls)),base_url='http://docker') as client:
            report=await Docker(client).operate(Operation(target='catalog-b',verb='resources'))
            # 0.25 s of CPU over 2 s of system time across 2 CPUs = 0.25 cores, all of a 0.25 limit.
            assert report['cpu_cores']==0.25 and report['cpu_pct_of_limit']==100.0
            assert report['throttled_periods']==80 and report['throttled_ms']==4000.0 and report['memory_mib']==40.0
            assert all(r.method=='GET' for r in calls)
    asyncio.run(exercise())


def test_start_or_stop_in_current_state_changes_nothing():
    async def exercise():
        inspected=target();inspected.update(Id='a'*64,State={'Running':True,'Status':'running','ExitCode':0})
        calls=[]
        async with httpx.AsyncClient(transport=httpx.MockTransport(fake_docker(inspected,calls)),base_url='http://docker') as client:
            result=await Docker(client).operate(Operation(target='catalog-b',verb='start',generation='a'*64))
            assert result['changed'] is False and result['running'] is True
            assert all(r.method=='GET' for r in calls)
    asyncio.run(exercise())


def test_operation_schema_rejects_resource_changes():
    for verb in ('throttle','restore','update','exec'):
        with pytest.raises(ValueError): Operation(target='catalog-b',verb=verb,generation='a')


def test_duplicate_label_matches_fail_closed():
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,json=[{'Id':'a'},{'Id':'b'}])),base_url='http://docker') as client:
            with pytest.raises(ValueError,match='exactly one'):
                await Docker(client).operate(Operation(target='catalog-b',verb='stop',generation='a'))
    asyncio.run(exercise())


def test_replaced_generation_rejected_before_mutation():
    async def exercise():
        data=target();data.update(Id='new',State={'Running':True,'Status':'running','ExitCode':0})
        calls=[]
        def transport(request):
            calls.append(request)
            return httpx.Response(200,json=[{'Id':'new'}] if request.url.path=='/containers/json' else data)
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport),base_url='http://docker') as client:
            with pytest.raises(ValueError,match='identity changed'):
                await Docker(client).operate(Operation(target='catalog-b',verb='stop',generation='old'))
            assert all(r.method=='GET' for r in calls)
    asyncio.run(exercise())


def test_replacement_during_operation_remains_uncertain():
    async def exercise():
        data=target();data.update(Id='old',State={'Running':True,'Status':'running','ExitCode':0})
        def transport(request):
            if request.method=='POST':
                data['Id']='replacement'
                return httpx.Response(204)
            return httpx.Response(200,json=[{'Id':data['Id']}] if request.url.path=='/containers/json' else copy.deepcopy(data))
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport),base_url='http://docker') as client:
            with pytest.raises(ValueError,match='replaced during'):
                await Docker(client).operate(Operation(target='catalog-b',verb='stop',generation='old'))
    asyncio.run(exercise())


def frame(text):
    data = text.encode()
    return b'\x01\x00\x00\x00' + len(data).to_bytes(4, 'big') + data


def network_docker(inspected, qdisc, calls, created):
    """Fake Docker whose short-lived helpers run tc against a pretend queue discipline."""
    state = {'qdisc': qdisc, 'argv': None}
    def transport(request):
        calls.append(request)
        path = request.url.path
        if path.endswith('/containers/json'):
            return httpx.Response(200, json=[{'Id': inspected['Id']}])
        if path.endswith('/containers/create'):
            body = json.loads(request.content)
            created.append(body)
            state['argv'] = body['Cmd'][1:]
            return httpx.Response(201, json={'Id': 'h' * 64})
        if path.endswith('/start'):
            return httpx.Response(204)
        if path.endswith('/wait'):
            argv = state['argv']
            if argv[:2] == ['qdisc', 'add']:
                state['qdisc'] = 'qdisc netem 8001: root refcnt 2 limit 1000 delay 600ms\n'
            elif argv[:2] == ['qdisc', 'del']:
                state['qdisc'] = 'qdisc noqueue 0: root refcnt 2\n'
            return httpx.Response(200, json={'StatusCode': 0})
        if path.endswith('/logs'):
            return httpx.Response(200, content=frame(state['qdisc'] if state['argv'][:2] == ['qdisc', 'show'] else ''))
        if request.method == 'DELETE':
            return httpx.Response(204)
        return httpx.Response(200, json=copy.deepcopy(inspected))
    return transport, state


def run_network(verb, qdisc):
    async def exercise():
        inspected = target(); inspected.update(Id='a' * 64, State={'Running': True, 'Status': 'running', 'ExitCode': 0})
        calls, created = [], []
        transport, state = network_docker(inspected, qdisc, calls, created)
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport), base_url='http://docker') as client:
            result = await Docker(client).operate(Operation(target='catalog-b', verb=verb, generation='a' * 64))
        return result, state, calls, created
    return asyncio.run(exercise())


def test_delay_adds_a_fixed_netem_queue_through_a_minimal_helper():
    result, state, calls, created = run_network('delay', 'qdisc noqueue 0: root refcnt 2\n')
    assert result['changed'] is True and 'delay 600ms' in state['qdisc']
    assert [c['Cmd'] for c in created] == [['tc', 'qdisc', 'show', 'dev', 'eth0'], ['tc', 'qdisc', 'add', 'dev', 'eth0', 'root', 'netem', 'delay', '600ms']]
    for config in created:
        host = config['HostConfig']
        assert host['NetworkMode'] == 'container:' + 'a' * 64 and host['CapAdd'] == ['NET_ADMIN'] and host['CapDrop'] == ['ALL']
        assert host['ReadonlyRootfs'] and 'Binds' not in host and 'Mounts' not in host and config['Image'] == PROJECT + ':local'
    # Every helper is removed again.
    assert sum(1 for r in calls if r.method == 'DELETE') == len(created)


def test_delay_and_undelay_report_unchanged_when_nothing_to_do():
    assert run_network('delay', 'qdisc netem 8001: root refcnt 2 limit 1000 delay 600ms\n')[0]['changed'] is False
    assert run_network('undelay', 'qdisc noqueue 0: root refcnt 2\n')[0]['changed'] is False


def test_undelay_removes_only_the_fault_queue():
    result, state, _, created = run_network('undelay', 'qdisc netem 8001: root refcnt 2 limit 1000 delay 600ms\n')
    assert result['changed'] is True and state['qdisc'].startswith('qdisc noqueue')
    assert created[-1]['Cmd'] == ['tc', 'qdisc', 'del', 'dev', 'eth0', 'root']


def test_foreign_queue_disciplines_are_never_touched():
    for verb, qdisc in (('delay', 'qdisc fq_codel 0: root refcnt 2\n'), ('undelay', 'qdisc netem 8001: root refcnt 2 delay 50ms\n')):
        with pytest.raises(ValueError):
            run_network(verb, qdisc)


def test_drop_adds_a_fixed_loss_queue_and_undrop_removes_it():
    result, state, _, created = run_network('drop', 'qdisc noqueue 0: root refcnt 2\n')
    assert result['changed'] is True and created[-1]['Cmd'] == ['tc', 'qdisc', 'add', 'dev', 'eth0', 'root', 'netem', 'loss', '50%']
    result, state, _, created = run_network('undrop', 'qdisc netem 8003: root refcnt 2 limit 1000 loss 50% seed 1277\n')
    assert result['changed'] is True and created[-1]['Cmd'] == ['tc', 'qdisc', 'del', 'dev', 'eth0', 'root']


def test_each_network_remedy_leaves_the_other_fault_alone():
    # A wrong remedy is an honest no-op, never an error and never a removal of the other fault's queue.
    for verb, qdisc in (('undelay', 'qdisc netem 8003: root refcnt 2 limit 1000 loss 50% seed 1277\n'),
                        ('undrop', 'qdisc netem 8001: root refcnt 2 limit 1000 delay 600ms\n')):
        result, _, _, created = run_network(verb, qdisc)
        assert result['changed'] is False and len(created) == 1
    assert run_network('drop', 'qdisc netem 8003: root refcnt 2 limit 1000 loss 50% seed 9\n')[0]['changed'] is False
    with pytest.raises(ValueError):
        run_network('drop', 'qdisc netem 8001: root refcnt 2 limit 1000 delay 600ms\n')
    with pytest.raises(ValueError):
        run_network('undrop', 'qdisc netem 8003: root refcnt 2 limit 1000 loss 5%\n')


def run_cap(verb, current):
    async def exercise():
        inspected = target(); inspected.update(Id='a' * 64, State={'Running': True, 'Status': 'running', 'ExitCode': 0})
        inspected['HostConfig']['NanoCpus'] = current
        updates = []
        def transport(request):
            path = request.url.path
            if path.endswith('/containers/json'):
                return httpx.Response(200, json=[{'Id': inspected['Id']}])
            if path.endswith('/update'):
                updates.append(json.loads(request.content))
                inspected['HostConfig']['NanoCpus'] = updates[-1]['NanoCpus']
                return httpx.Response(200, json={'Warnings': []})
            return httpx.Response(200, json=copy.deepcopy(inspected))
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport), base_url='http://docker') as client:
            result = await Docker(client).operate(Operation(target='catalog-b', verb=verb, generation='a' * 64))
        return result, updates
    return asyncio.run(exercise())


def test_power_cap_sets_only_the_two_fixed_cpu_limits():
    result, updates = run_cap('cap', 250_000_000)
    assert result['changed'] is True and updates == [{'NanoCpus': 50_000_000}]
    result, updates = run_cap('uncap', 50_000_000)
    assert result['changed'] is True and updates == [{'NanoCpus': 250_000_000}]
    result, updates = run_cap('cap', 50_000_000)
    assert result['changed'] is False and updates == []  # already capped: nothing sent
    assert run_cap('uncap', 250_000_000)[0]['changed'] is False


PRIO = 'qdisc prio 1: root refcnt 2 bands 3 priomap 1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1\n'
FILTER_SHOWN = ('filter parent 1: protocol ip pref 1 u32 chain 0 \n'
                'filter parent 1: protocol ip pref 1 u32 chain 0 fh 800: ht divisor 1 \n'
                'filter parent 1: protocol ip pref 1 u32 chain 0 fh 800::800 order 2048 key ht 800 bkt 0 terminal flowid not_in_hw \n'
                '  match ac140003/ffffffff at 16\n'
                '\taction order 1: gact action drop\n'
                '\t random type none pass val 0\n'
                '\t index 1 ref 1 bind 1\n')


def partition_docker(gateway, catalogue, qdisc, filters, created, fail_filter=False):
    """Fake Docker for the partition rule: helpers run tc against a pretend root queue and filter list."""
    state = {'qdisc': qdisc, 'filters': filters, 'argv': None}
    def transport(request):
        path = request.url.path
        if path.endswith('/containers/json'):
            service = json.loads(request.url.params['filters'])['label'][1].split('=')[1]
            target_name = gateway['Config']['Labels']['com.docker.compose.service']
            return httpx.Response(200, json=[{'Id': (gateway if service == target_name else catalogue)['Id']}])
        if path.endswith('/containers/create'):
            body = json.loads(request.content)
            created.append(body)
            state['argv'] = body['Cmd'][1:]
            return httpx.Response(201, json={'Id': 'h' * 64})
        if path.endswith('/start') or request.method == 'DELETE':
            return httpx.Response(204)
        if path.endswith('/wait'):
            argv = state['argv']
            if argv[:2] == ['qdisc', 'add']:
                state['qdisc'] = PRIO
            elif argv[:2] == ['filter', 'add']:
                if fail_filter:
                    return httpx.Response(200, json={'StatusCode': 2})
                address = argv[argv.index('dst') + 1].split('/')[0]
                state['filters'] = FILTER_SHOWN.replace('ac140003', '%08x' % int.from_bytes(bytes(map(int, address.split('.'))), 'big'))
            elif argv[:2] == ['qdisc', 'del']:
                state['qdisc'], state['filters'] = 'qdisc noqueue 0: root refcnt 2\n', ''
            return httpx.Response(200, json={'StatusCode': 0})
        if path.endswith('/logs'):
            shown = {'qdisc': state['qdisc'], 'filter': state['filters']}.get(state['argv'][0], '') if state['argv'][1] == 'show' else ''
            return httpx.Response(200, content=frame(shown))
        if path.endswith('/' + catalogue['Id'] + '/json'):
            return httpx.Response(200, json=copy.deepcopy(catalogue))
        return httpx.Response(200, json=copy.deepcopy(gateway))
    return transport, state


def run_partition(verb, qdisc, filters='', target_name='gateway-a', catalogue_ip='172.20.0.3', fail_filter=False):
    async def exercise():
        gateway = target(); gateway.update(Id='a' * 64, State={'Running': True, 'Status': 'running', 'ExitCode': 0})
        gateway['Config']['Labels']['com.docker.compose.service'] = target_name
        catalogue = target(); catalogue.update(Id='c' * 64, State={'Running': True, 'Status': 'running', 'ExitCode': 0})
        catalogue['NetworkSettings']['Networks'][PROJECT + '_fleet'] = {'IPAddress': catalogue_ip}
        created = []
        transport, state = partition_docker(gateway, catalogue, qdisc, filters, created, fail_filter)
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport), base_url='http://docker') as client:
            result = await Docker(client).operate(Operation(target=target_name, verb=verb, generation='a' * 64))
        return result, state, created
    return asyncio.run(exercise())


def test_isolate_drops_only_the_catalogue_address_resolved_by_the_broker():
    result, state, created = run_partition('isolate', 'qdisc noqueue 0: root refcnt 2\n')
    assert result['changed'] is True and state['qdisc'] == PRIO
    assert [c['Cmd'] for c in created] == [['tc', 'qdisc', 'show', 'dev', 'eth0'], ['tc', 'qdisc', 'add', 'dev', 'eth0', 'root', 'handle', '1:', 'prio'],
                                           ['tc', 'filter', 'add', 'dev', 'eth0', 'parent', '1:', 'protocol', 'ip', 'prio', '1', 'u32',
                                            'match', 'ip', 'dst', '172.20.0.3/32', 'action', 'drop']]
    assert all(c['HostConfig']['CapAdd'] == ['NET_ADMIN'] and c['HostConfig']['NetworkMode'] == 'container:' + 'a' * 64 for c in created)
    # Already partitioned from that address: nothing changes.
    assert run_partition('isolate', state['qdisc'], state['filters'])[0]['changed'] is False


def test_route_removes_only_the_partition_rule():
    result, state, created = run_partition('route', PRIO, FILTER_SHOWN)
    assert result['changed'] is True and created[-1]['Cmd'] == ['tc', 'qdisc', 'del', 'dev', 'eth0', 'root'] and state['qdisc'].startswith('qdisc noqueue')
    # No partition, or another fault's netem queue: an honest no-op that removes nothing.
    for qdisc in ('qdisc noqueue 0: root refcnt 2\n', 'qdisc netem 8003: root refcnt 2 limit 1000 loss 50% seed 9\n'):
        result, _, created = run_partition('route', qdisc)
        assert result['changed'] is False and len(created) == 1
    # A prio root with any other filter is not ours to remove.
    with pytest.raises(ValueError, match='Unexpected filters'):
        run_partition('route', PRIO, FILTER_SHOWN.replace('action drop', 'action pass'))


def test_partition_refuses_other_queues_targets_and_addresses():
    with pytest.raises(ValueError, match='Unexpected queue discipline'):
        run_partition('isolate', 'qdisc netem 8001: root refcnt 2 limit 1000 delay 600ms\n')  # never alongside a netem fault
    with pytest.raises(ValueError, match='Only a gateway'):
        run_partition('isolate', 'qdisc noqueue 0: root refcnt 2\n', target_name='catalog-b')
    for address in ('', 'not-an-address', '8.8.8.8'):
        with pytest.raises(ValueError, match='catalogue'):
            run_partition('isolate', 'qdisc noqueue 0: root refcnt 2\n', catalogue_ip=address)
    with pytest.raises(ValueError, match='another address'):
        run_partition('isolate', PRIO, FILTER_SHOWN, catalogue_ip='172.20.0.9')


def test_a_failed_filter_leaves_the_default_queue_rather_than_half_a_rule():
    created = []
    with pytest.raises(ValueError, match='Network change failed'):
        _, _, created = run_partition('isolate', 'qdisc noqueue 0: root refcnt 2\n', fail_filter=True)


def test_netem_remedies_leave_a_partition_alone():
    for verb in ('undelay', 'undrop'):
        assert run_network(verb, PRIO)[0]['changed'] is False
    with pytest.raises(ValueError):
        run_network('drop', PRIO)
