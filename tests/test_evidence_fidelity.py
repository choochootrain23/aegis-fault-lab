import asyncio
import json
import time

import httpx

from aegis import fleet
from aegis.controller import Lab
from aegis.store import Store


def test_log_endpoint_is_bounded_and_retains_newest_results():
    original=list(fleet.LOGS)
    try:
        for index in range(200):
            fleet.LOGS.append({'at':time.time(),'ok':False,'error':'Synthetic dependency error '*8,'index':index})
        result=asyncio.run(fleet.logs())
        assert result['truncated'] is True
        assert result['entries'][-1]['index']==199
        assert result['omitted_retained_entries']>0
        assert len(json.dumps(result).encode())<32768
    finally:
        fleet.LOGS.clear()
        fleet.LOGS.extend(original)


def test_revealed_uncertain_injection_is_not_claimed_as_completed(tmp_path):
    async def exercise():
        store=Store(tmp_path/'db.sqlite')
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(503))) as client:
            lab=Lab(store,client,resource_check=False)
            lab.session={'id':'s','mode':'challenge','status':'investigating','fault_target':'catalog-b',
                         'fault_kind':'service-unavailable','revealed':True,'fault_action':{'status':'unknown'}}
            debrief=lab.public_session()['debrief']
            assert 'requested stopping' in debrief and 'unknown' in debrief
            assert 'scenario stopped' not in debrief
        store.db.close()
    asyncio.run(exercise())


def test_process_and_container_identity_are_distinguished(tmp_path):
    async def exercise():
        store=Store(tmp_path/'db.sqlite')
        def transport(request):
            if request.url.host=='broker':
                return httpx.Response(200,json={'generation':'container-id','running':True})
            return httpx.Response(200,json={'generation':'process-boot-id','live':True})
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            lab=Lab(store,client,resource_check=False)
            process=await lab.check('catalog-b','liveness')
            container=await lab.check('catalog-b','service')
            assert process['body']['generation_scope']=='process'
            assert process['body']['process_generation']=='process-boot-id'
            assert container['body']['generation_scope']=='container'
            assert container['body']['generation']=='container-id'
            assert process['body']['origin']=='operator'
        store.db.close()
    asyncio.run(exercise())
