"""Archive integrity, stable evidence paging and bounded retention."""
import asyncio
import json
import sqlite3
import time

import httpx
import pytest

from aegis.controller import Lab, app
from aegis.store import Store


def session(sid='old', status='ended', mode='challenge'):
    return {'id': sid, 'status': status, 'mode': mode, 'started_at': time.time()-100,
            'fault_target': 'catalog-b', 'fault_kind': 'service-unavailable', 'revealed': False,
            'notes': [], 'help': [], 'recovery_attempts': [], 'verification': 'Baseline verified', 'outcome': 'reset'}


def test_page_cursor_stays_stable_when_new_evidence_arrives(tmp_path):
    store = Store(tmp_path/'test.sqlite')
    store.save(session())
    for _ in range(8): store.record('old', 'catalog-b', 'business', {'ok': True})
    first = store.evidence_page('old', 3)
    store.record('old', 'gateway-a', 'business', {'ok': True})
    second = store.evidence_page('old', 3, first['next_cursor'])
    third = store.evidence_page('old', 3, second['next_cursor'])
    ids = [e['id'] for page in (first,second,third) for e in page['items']]
    assert len(ids) == len(set(ids)) == 8
    assert third['next_cursor'] is None
    store.db.close()


def test_page_rejects_other_session_cursor_and_filters(tmp_path):
    store = Store(tmp_path/'test.sqlite')
    good=store.record('old','catalog-b','logs',{'ok':True})
    other=store.record('new','catalog-b','logs',{'ok':True})
    store.record('old','gateway-a','business',{'ok':True})
    assert store.evidence_page('old',node='catalog-b',kind='logs')['items'][0]['id']==good['id']
    with pytest.raises(ValueError): store.evidence_page('old', before=other['id'])
    store.db.close()


def test_retention_is_batched_and_freed_pages_are_reusable(tmp_path):
    store=Store(tmp_path/'test.sqlite')
    for _ in range(12): store.record(None,'catalog-b','logs',{'ok':True,'content':'x'*10000})
    before=store.storage()
    store.db.execute('UPDATE evidence SET at=?',(time.time()-90000,))
    store.db.commit()
    result=store.prune(batch_size=5)
    assert result['evidence_removed']==5 and result['batch_full']
    store.prune(batch_size=20)
    after=store.storage()
    assert after['live_bytes'] < before['live_bytes']
    assert after['allocated_bytes'] >= after['live_bytes']
    store.db.close()


def test_truncated_unicode_and_escapes_remain_bounded(tmp_path):
    store=Store(tmp_path/'test.sqlite')
    for text in ['\\"'*40000, '\u2603'*40000]:
        e=store.record(None,'catalog-b','logs',{'ok':True,'content':text})
        assert e['body']['truncated']
        assert len(json.dumps(e['body']).encode()) <= 32768
        assert e['body']['ok'] is True
    store.db.close()


def test_summary_retention_starts_at_closure_and_preserves_action_history(tmp_path):
    store = Store(tmp_path/'test.sqlite')
    now = time.time()
    for sid, status, ended in [('active', 'investigating', None),
                               ('recent', 'ended', now-60),
                               ('expired', 'ended', now-31*86400)]:
        s = session(sid, status)
        s.update(started_at=now-40*86400, ended_at=ended)
        store.save(s)
        store.save_action(sid, sid, '{}', {'status': 'unknown'})
    store.db.execute('UPDATE actions SET at=?', (now-40*86400,))
    store.db.commit()
    result = store.prune(now)
    assert result['summaries_removed'] == 1
    assert store.session('expired') is None and store.action('expired') is None
    for sid in ('active', 'recent'):
        assert store.session(sid) and store.action(sid)
    store.db.close()


def test_cleanup_releases_wal_after_reader_finishes(tmp_path):
    store = Store(tmp_path/'test.sqlite')
    store.record(None, 'gateway-a', 'logs', {'ok': True})
    reader = sqlite3.connect(store.path)
    try:
        reader.execute('BEGIN')
        reader.execute('SELECT * FROM evidence').fetchall()
        store.record(None, 'gateway-a', 'logs', {'ok': True, 'content': 'x'*20000})
        result = store.prune()
        assert result['checkpoint_busy'] is True
        assert result['storage']['wal_bytes'] > 0
        assert store.db.execute('PRAGMA busy_timeout').fetchone()[0] == 3000
        reader.rollback()
        result = store.prune()
        assert result['checkpoint_busy'] is False
        assert result['storage']['wal_bytes'] == 0
        assert len(store.evidence()) == 2
    finally:
        reader.close()
        store.db.close()


def test_archived_export_includes_old_citations_and_reports_expiry(tmp_path):
    async def exercise():
        store=Store(tmp_path/'test.sqlite')
        s=session()
        cited=store.record(s['id'],'catalog-b','service',{'ok':True,'running':False})
        s['notes']=[{'explanation':'Stopped service','uncertainty':'Cause unknown','at':time.time(),'evidence_ids':[cited['id'],'expired-id']}]
        store.save(s)
        for _ in range(510): store.record(s['id'],'catalog-b','liveness',{'ok':True})
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(503))) as upstream:
            app.state.lab=Lab(store,upstream,resource_check=False)
            app.state.csrf='test-token'
            assert app.state.lab.session is None
            assert app.state.lab.state()['evidence'] == []
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://localhost:8787') as client:
                detail=(await client.get('/api/investigations/old')).json()
                assert detail['missing_citations']==['expired-id']
                exported=(await client.get('/api/export?session_id=old')).json()
                assert cited['id'] in {e['id'] for e in exported['evidence']}
                assert len(exported['evidence'])==501
                assert exported['omitted_evidence_count']==10
                assert (await client.get('/api/investigations/missing')).status_code==404
                assert (await client.get('/api/investigations/old/evidence?limit=101')).status_code==422
                observation = await client.get('/api/investigations/old/evidence/'+cited['id'])
                assert observation.status_code == 200
                assert observation.json()['body']['running'] is False
                assert (await client.get('/api/investigations/old/evidence/expired-id')).status_code == 404
                other = store.record('another-session', 'catalog-b', 'service', {'ok': True})
                assert (await client.get('/api/investigations/old/evidence/'+other['id'])).status_code == 404
        store.db.close()
    asyncio.run(exercise())


def test_active_challenge_archive_does_not_reveal_ground_truth(tmp_path):
    async def exercise():
        store=Store(tmp_path/'test.sqlite')
        s=session(status='investigating')
        store.save(s)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(503))) as upstream:
            app.state.lab=Lab(store,upstream,resource_check=False)
            app.state.csrf='test-token'
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://localhost:8787') as client:
                for path in ['/api/investigations/old','/api/export?session_id=old']:
                    public=(await client.get(path)).json()['session']
                    assert 'scenario' not in public and 'fault_target' not in public and 'debrief' not in public
        store.db.close()
    asyncio.run(exercise())


def test_storage_limit_returns_actionable_error_without_mutation(tmp_path,monkeypatch):
    async def exercise():
        store=Store(tmp_path/'test.sqlite')
        calls=[]
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:calls.append(r) or httpx.Response(503))) as client:
            lab=Lab(store,client,resource_check=False)
            monkeypatch.setattr(store,'storage',lambda:{'live_bytes':256*1024**2,'live_limit_bytes':256*1024**2,'wal_bytes':0,'wal_limit_bytes':128*1024**2})
            with pytest.raises(Exception) as exc:
                await lab.mutate('key-001','catalog-b','start')
            assert exc.value.status_code==507
            assert calls==[] and store.action('key-001') is None
        store.db.close()
    asyncio.run(exercise())
