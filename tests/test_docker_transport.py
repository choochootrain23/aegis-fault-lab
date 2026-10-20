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
