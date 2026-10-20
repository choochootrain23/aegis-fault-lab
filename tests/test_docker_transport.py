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
