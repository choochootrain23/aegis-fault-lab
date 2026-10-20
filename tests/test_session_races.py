"""Stale browser tabs and requests queued across investigation transitions."""
import asyncio
import time

import httpx
import pytest

from aegis.controller import Action, Lab, Start, app, recover
from aegis.store import Store


def active_session(sid='current', status='investigating'):
    return {'id':sid,'status':status,'mode':'practice','started_at':time.time(),'deadline':time.time()+900,
            'fault_target':'catalog-b','fault_kind':'service-unavailable','revealed':False,'notes':[],
            'help':[],'recovery_attempts':[],'verification':'pending','outcome':None}


def test_stale_tab_cannot_mutate_or_annotate_new_investigation(tmp_path):
    async def exercise():
        store=Store(tmp_path/'test.sqlite')
        calls=[]
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:calls.append(r) or httpx.Response(503))) as upstream:
            lab=Lab(store,upstream,resource_check=False)
            lab.session=active_session()
            store.save(lab.session)
            app.state.lab,app.state.csrf=lab,'token'
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://localhost:8787',
                                         headers={'Origin':'http://localhost:8787','X-Aegis-Token':'token'}) as client:
                for route,body in [('recover',{'key':'stale-key'}),('reset',{'key':'stale-key'}),
                                   ('extend',{'key':'stale-key'}),('reveal',{'key':'stale-key'}),
                                   ('note',{'explanation':'Old diagnosis'}),('help',{'level':'hint'})]:
                    response=await client.post('/api/'+route,json={'session_id':'old',**body})
                    assert response.status_code==409, (route,response.text)
            assert calls==[]
            assert lab.session['notes']==[] and lab.session['help']==[]
        store.db.close()
    asyncio.run(exercise())


def test_queued_request_rechecks_session_after_acquiring_lock(tmp_path):
    async def exercise():
        store=Store(tmp_path/'test.sqlite')
        calls=[]
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:calls.append(r) or httpx.Response(503))) as upstream:
            lab=Lab(store,upstream,resource_check=False)
            lab.session=active_session()
            app.state.lab=lab
            await lab.lock.acquire()
            task=asyncio.create_task(recover(Action(session_id='current',key='queued-action')))
            await asyncio.sleep(0)
            lab.session=active_session('new')
            lab.lock.release()
            with pytest.raises(Exception) as exc: await task
            assert exc.value.status_code==409
            assert calls==[]
        store.db.close()
    asyncio.run(exercise())


def test_old_idempotency_key_cannot_be_used_in_new_session(tmp_path):
    async def exercise():
        store=Store(tmp_path/'test.sqlite')
        store.save_action('old-key','old','{"node": "catalog-b", "verb": "start"}',{'status':'succeeded'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(503))) as upstream:
            lab=Lab(store,upstream,resource_check=False)
            lab.session=active_session('new')
            with pytest.raises(Exception) as exc: await lab.mutate('old-key','catalog-b','start')
            assert exc.value.status_code==409
        store.db.close()
    asyncio.run(exercise())


def test_expired_grace_cannot_be_extended_before_collector_catches_up(tmp_path):
    async def exercise():
        store=Store(tmp_path/'test.sqlite')
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(503))) as upstream:
            lab=Lab(store,upstream,resource_check=False)
            lab.session=active_session()
            lab.session['deadline']=time.time()-61
            app.state.lab,app.state.csrf=lab,'token'
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://localhost:8787',
                                         headers={'Origin':'http://localhost:8787','X-Aegis-Token':'token'}) as client:
                response=await client.post('/api/extend',json={'session_id':'current','key':'too-late'})
                assert response.status_code==409
        store.db.close()
    asyncio.run(exercise())


def test_preflight_rejects_stopped_target_even_with_recent_healthy_probes(tmp_path):
    async def exercise():
        store=Store(tmp_path/'test.sqlite')
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'running':False}))) as upstream:
            lab=Lab(store,upstream,resource_check=False)
            lab.baseline=5
            lab.last_cycle=time.time()
            with pytest.raises(Exception) as exc: await lab.start(Start(mode='practice',key='stale-baseline'))
            assert exc.value.status_code==409
            assert lab.session is None
            assert store.actions(None)==[]
        store.db.close()
    asyncio.run(exercise())
