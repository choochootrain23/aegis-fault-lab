from aegis.guidance import explain


def observation(node,kind,ok=True,at=100,**body):
    return {'id':node+'-'+kind,'node':node,'kind':kind,'at':at,'body':{'ok':ok,**body}}


def test_dependency_hint_uses_live_gateway_and_failed_business_evidence():
    latest={('gateway-a','liveness'):observation('gateway-a','liveness'),
            ('gateway-a','business'):observation('gateway-a','business',False)}
    result=explain(latest,'evidence',now=101)
    assert len(result['evidence_ids'])==2
    assert result['suggested_check']=={'node':'catalog-b','kind':'service'}
    assert 'process is reachable' in result['message']


def test_unavailable_gateway_has_different_next_check():
    latest={('gateway-a','liveness'):observation('gateway-a','liveness',False),
            ('gateway-a','business'):observation('gateway-a','business',False)}
    result=explain(latest,'hint',now=101)
    assert result['suggested_check']['node']=='gateway-a'
    assert 'does not by itself distinguish' in result['message']


def test_stale_or_failed_service_check_never_justifies_start():
    for entry in [observation('catalog-b','service',at=50,running=False),
                  observation('catalog-b','service',False,running=False)]:
        result=explain({('catalog-b','service'):entry},'action',now=101)
        assert 'No fresh inspection yet supports a specific recovery action' in result['message']
        assert result['evidence_ids']==[]


def test_start_suggestion_requires_observed_stopped_container():
    e=observation('catalog-b','service',running=False)
    result=explain({('catalog-b','service'):e},'action',now=101)
    assert result['evidence_ids']==[e['id']]
    assert 'does not establish why' in result['message']


def test_healthy_evidence_does_not_invent_incident():
    latest={('gateway-a','liveness'):observation('gateway-a','liveness'),
            ('gateway-a','business'):observation('gateway-a','business')}
    assert 'do not establish an active failure' in explain(latest,'evidence',now=101)['message']


def test_later_success_supersedes_stopped_inspection():
    stopped = observation('catalog-b', 'service', running=False, at=100)
    live = observation('catalog-b', 'liveness', at=102)
    result = explain({('catalog-b', 'service'): stopped, ('catalog-b', 'liveness'): live},
                     'action', now=103)
    assert result['evidence_ids'] == [stopped['id'], live['id']]
    assert 'state may have changed' in result['message']
    assert 'try starting' not in result['message']


def test_slow_gateway_requests_point_to_catalogue_resources():
    latest={('gateway-a','liveness'):observation('gateway-a','liveness',duration_ms=4),
            ('gateway-a','business'):observation('gateway-a','business',duration_ms=820,slow=True)}
    result=explain(latest,'evidence',now=101)
    assert result['suggested_check']=={'node':'catalog-b','kind':'resources'}
    assert 'slowly' in result['message']


def test_storage_failures_point_to_the_full_node():
    upstream={('gateway-a','liveness'):observation('gateway-a','liveness'),
              ('gateway-a','business'):observation('gateway-a','business',False,http_status=503,result={'upstream_status':507})}
    assert explain(upstream,'evidence',now=101)['suggested_check']=={'node':'catalog-b','kind':'disk'}
    local={('gateway-a','liveness'):observation('gateway-a','liveness'),
           ('gateway-a','business'):observation('gateway-a','business',False,http_status=507,result={'errno':'ENOSPC'})}
    assert explain(local,'evidence',now=101)['suggested_check']=={'node':'gateway-a','kind':'disk'}


def test_cpu_at_limit_supports_stopping_runaway_job():
    e=observation('catalog-b','resources',cpu_limit=0.25,cpu_pct_of_limit=99.0,throttled_periods=40)
    result=explain({('catalog-b','resources'):e},'action',now=101)
    assert result['evidence_ids']==[e['id']]
    assert 'stopping the runaway job' in result['message']


def test_full_disk_supports_clearing():
    e=observation('gateway-a','disk',result={'free_bytes':0,'largest':[{'path':'aegis-fill.bin','bytes':8_000_000}]})
    result=explain({('gateway-a','disk'):e},'action',now=101)
    assert result['evidence_ids']==[e['id']]
    assert 'clearing its temporary files' in result['message'] and 'aegis-fill.bin' in result['message']


def test_healthy_inspections_do_not_suggest_a_remedy():
    latest={('catalog-b','resources'):observation('catalog-b','resources',cpu_limit=0.25,cpu_pct_of_limit=6.0),
            ('catalog-b','disk'):observation('catalog-b','disk',result={'free_bytes':8_000_000,'largest':[]})}
    assert explain(latest,'action',now=101)['evidence_ids']==[]


def test_older_success_does_not_override_new_stopped_inspection():
    stopped = observation('catalog-b', 'service', running=False, at=102)
    live = observation('catalog-b', 'liveness', at=100)
    result = explain({('catalog-b', 'service'): stopped, ('catalog-b', 'liveness'): live},
                     'action', now=103)
    assert result['evidence_ids'] == [stopped['id']]
    assert 'try starting' in result['message']
