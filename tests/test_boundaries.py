import copy
import json
import time

import pytest
import asyncio
import httpx
from aegis.controller import Lab

from aegis.broker import PROJECT, validate_target, Operation
from aegis.controller import app
from aegis.store import Store


def target():
    return {'Config': {'Labels': {'com.docker.compose.project': PROJECT, 'com.docker.compose.service': 'catalog-b'}, 'User': '10001:10001'},
            'HostConfig': {'ReadonlyRootfs': True, 'Memory': 192*1024*1024, 'NanoCpus': 250_000_000, 'PidsLimit': 64, 'CapDrop':['ALL'], 'SecurityOpt':['no-new-privileges:true'],
                           'MemorySwap':192*1024*1024, 'ShmSize':8*1024*1024, 'RestartPolicy':{'Name':'no'},
                           'IpcMode':'private', 'Tmpfs':{'/tmp':'size=8m,mode=1777'}},
            'NetworkSettings': {'Networks': {PROJECT + '_fleet': {}}}, 'Mounts': []}


def test_broker_accepts_only_owned_isolated_target():
    validate_target(target(), 'catalog-b')


@pytest.mark.parametrize('mutation', ['project', 'service', 'privileged', 'network', 'mount', 'ports', 'memory', 'cpu', 'pid', 'user', 'writable', 'capability', 'cap-drop', 'security-opt', 'swap', 'shm', 'restart', 'devices', 'ipc', 'tmpfs'])
def test_broker_rejects_target_drift(mutation):
    data = copy.deepcopy(target())
    if mutation == 'project': data['Config']['Labels']['com.docker.compose.project'] = 'other-project'
    if mutation == 'service': data['Config']['Labels']['com.docker.compose.service'] = 'controller'
    if mutation == 'privileged': data['HostConfig']['Privileged'] = True
    if mutation == 'network': data['NetworkSettings']['Networks']['host'] = {}
    if mutation == 'mount': data['Mounts'] = [{'Source': '/'}]
    if mutation == 'ports': data['HostConfig']['PortBindings'] = {'8000/tcp': []}
    if mutation == 'memory': data['HostConfig']['Memory'] = 0
    if mutation == 'cpu': data['HostConfig']['NanoCpus'] = 0
    if mutation == 'pid': data['HostConfig']['PidsLimit'] = 0
    if mutation == 'user': data['Config']['User'] = 'root'
    if mutation == 'writable': data['HostConfig']['ReadonlyRootfs'] = False
    if mutation == 'capability': data['HostConfig']['CapAdd'] = ['SYS_ADMIN']
    if mutation == 'cap-drop': data['HostConfig']['CapDrop'] = []
    if mutation == 'security-opt': data['HostConfig']['SecurityOpt'] = []
    if mutation == 'swap': data['HostConfig']['MemorySwap'] = -1
    if mutation == 'shm': data['HostConfig']['ShmSize'] = 1024*1024*1024
    if mutation == 'restart': data['HostConfig']['RestartPolicy']['Name'] = 'always'
    if mutation == 'devices': data['HostConfig']['Devices'] = [{'PathOnHost':'/dev/sda'}]
    if mutation == 'ipc': data['HostConfig']['IpcMode'] = 'host'
    if mutation == 'tmpfs': data['HostConfig']['Tmpfs']['/tmp'] = 'size=2g'
    with pytest.raises(ValueError): validate_target(data, 'catalog-b')


def test_broker_schema_rejects_exec_or_extra_arguments():
    with pytest.raises(ValueError): Operation(target='catalog-b', verb='exec')
    with pytest.raises(ValueError): Operation(target='controller', verb='stop')
    with pytest.raises(ValueError): Operation(target='catalog-b', verb='inspect', command='echo test')


def test_retention_keeps_summary_but_removes_old_detail(tmp_path):
    store = Store(tmp_path / 'lab.sqlite')
    e = store.record('s', 'catalog-b', 'business', {'ok': True})
    store.db.execute('UPDATE evidence SET at=? WHERE id=?', (time.time()-90000, e['id']))
    store.db.commit()
    store.save({'id': 's', 'status': 'ended', 'started_at': time.time()-90000})
    store.prune()
    assert not store.evidence()
    assert store.sessions()[0]['id'] == 's'
    store.db.close()


def test_evidence_payload_is_bounded(tmp_path):
    store = Store(tmp_path / 'lab.sqlite')
    e = store.record(None, 'catalog-b', 'logs', {'message': 'x'*40000})
    assert e['body']['truncated']
    assert len(json.dumps(e['body'])) < 32768
    store.db.close()


def test_api_origin_token_and_host_boundaries(tmp_path, monkeypatch):
    async def exercise():
        store = Store(tmp_path / 'api.sqlite')
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503))) as upstream:
            app.state.lab = Lab(store, upstream, resource_check=False)
            app.state.csrf = 'test-session-token'
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost:8787') as client:
                state = await client.get('/api/state')
                assert state.status_code == 200
                token = state.json()['token']
                assert (await client.post('/api/check', json={'node':'catalog-b','kind':'service'})).status_code == 403
                assert (await client.post('/api/check', json={'node':'catalog-b','kind':'service'}, headers={'Origin':'http://evil.example','X-Aegis-Token':token})).status_code == 403
                assert (await client.get('/api/state', headers={'Host':'evil.example'})).status_code == 403
                valid = {'Origin':'http://localhost:8787','X-Aegis-Token':token}
                assert (await client.post('/api/check', json={'node':'controller','kind':'service'}, headers=valid)).status_code == 422
                assert (await client.post('/api/check', json={'node':'catalog-b','kind':'exec'}, headers=valid)).status_code == 422
                assert (await client.get('/')).status_code == 200
                assert 'frame-ancestors' in (await client.get('/')).headers['content-security-policy']
        store.db.close()
    asyncio.run(exercise())


def test_active_investigation_evidence_is_not_silently_pruned(tmp_path):
    store = Store(tmp_path / 'lab.sqlite')
    e = store.record('active', 'catalog-b', 'business', {'ok': False})
    store.db.execute('UPDATE evidence SET at=? WHERE id=?', (time.time()-90000, e['id']))
    store.db.commit()
    store.save({'id': 'active', 'status': 'investigating', 'started_at': time.time()-90000})
    store.prune()
    assert store.evidence('active')[0]['id'] == e['id']
    store.db.close()


def test_operator_notes_extensions_and_reveal_boundaries(tmp_path):
    async def exercise():
        store = Store(tmp_path / 'api.sqlite')
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503))) as upstream:
            lab = Lab(store, upstream, resource_check=False)
            app.state.lab, app.state.csrf = lab, 'test-session-token'
            lab.session = {'id':'s1','mode':'challenge','status':'investigating','started_at':time.time(),
                           'deadline':time.time()+900,'fault_target':'catalog-b','fault_kind':'service-unavailable',
                           'revealed':False,'notes':[],'help':[],'recovery_attempts':[], 'verification':'pending'}
            store.save(lab.session)
            evidence = store.record('s1','catalog-b','service',{'ok':True,'running':False})
            other = store.record('s2','catalog-b','service',{'ok':True})
            headers={'Origin':'http://localhost:8787','X-Aegis-Token':'test-session-token'}
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://localhost:8787',headers=headers) as client:
                bad = await client.post('/api/note', json={'session_id':'s1','explanation':'Stopped process','evidence_ids':[other['id']]})
                assert bad.status_code == 422
                good = await client.post('/api/note', json={'session_id':'s1','explanation':'Stopped process','uncertainty':'Original cause unknown','evidence_ids':[evidence['id']]})
                assert good.status_code == 200
                original = lab.session['deadline']
                for _ in range(2):
                    assert (await client.post('/api/extend',json={'session_id':'s1','key':'extend-001'})).status_code == 200
                assert lab.session['deadline'] == original+900
                export = (await client.get('/api/export')).json()
                assert 'scenario' not in export['session']
                assert other['id'] not in {e['id'] for e in export['evidence']}
                assert (await client.post('/api/reveal',json={'session_id':'s1','key':'reveal-001'})).status_code == 200
                assert (await client.get('/api/export')).json()['session']['scenario']['target'] == 'catalog-b'
        store.db.close()
    asyncio.run(exercise())
