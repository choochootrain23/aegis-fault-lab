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


def test_failing_gateway_with_healthy_catalogue_points_at_gateway_config():
    latest={('gateway-a','liveness'):observation('gateway-a','liveness',duration_ms=3),
            ('gateway-a','business'):observation('gateway-a','business',False,result={'error':'ConnectError'}),
            ('catalog-b','business'):observation('catalog-b','business',duration_ms=6)}
    result=explain(latest,'evidence',now=101)
    assert result['suggested_check']=={'node':'gateway-a','kind':'config'}
    assert 'catalog-b-business' in result['evidence_ids']


def test_catalogue_500_points_at_catalogue_config():
    latest={('gateway-a','liveness'):observation('gateway-a','liveness'),
            ('gateway-a','business'):observation('gateway-a','business',False,result={'upstream_status':500})}
    assert explain(latest,'evidence',now=101)['suggested_check']=={'node':'catalog-b','kind':'config'}


def test_drifted_config_supports_restore():
    config=observation('gateway-a','config',result={'drifted':['upstream'],'settings':[
        {'key':'upstream','value':'http://catalog-b:8080','deployed':'http://catalog-b:8000','drifted':True}]})
    result=explain({('gateway-a','config'):config},'action',now=101)
    assert result['evidence_ids']==[config['id']]
    assert 'restoring the deployed config' in result['message'] and ':8080' in result['message']


def test_matching_config_supports_no_action():
    config=observation('gateway-a','config',result={'drifted':[],'settings':[]})
    assert 'No fresh inspection' in explain({('gateway-a','config'):config},'action',now=101)['message']


def test_other_gateway_failing_too_points_at_the_shared_catalogue():
    latest={('gateway-a','liveness'):observation('gateway-a','liveness'),
            ('gateway-a','business'):observation('gateway-a','business',False),
            ('gateway-c','liveness'):observation('gateway-c','liveness'),
            ('gateway-c','business'):observation('gateway-c','business',False)}
    result=explain(latest,'evidence',now=101)
    assert 'gateway-c is affected too' in result['message'] and 'gateway-c-business' in result['evidence_ids']


def test_healthy_other_gateway_marks_the_fault_as_local():
    latest={('gateway-a','liveness'):observation('gateway-a','liveness'),
            ('gateway-a','business'):observation('gateway-a','business'),
            ('gateway-c','liveness'):observation('gateway-c','liveness',False),
            ('gateway-c','business'):observation('gateway-c','business',False)}
    result=explain(latest,'evidence',now=101)
    assert result['suggested_check']=={'node':'gateway-c','kind':'service'}
    assert 'gateway-a is healthy at the same time' in result['message'] and 'local to gateway-c' in result['message']


def test_slow_with_normal_cpu_points_at_the_network_path():
    latest={('gateway-a','liveness'):observation('gateway-a','liveness',duration_ms=4),
            ('gateway-a','business'):observation('gateway-a','business',duration_ms=700,slow=True),
            ('catalog-b','resources'):observation('catalog-b','resources',cpu_pct_of_limit=6.0,cpu_limit=0.25)}
    result=explain(latest,'evidence',now=101)
    assert result['suggested_check']=={'node':'catalog-b','kind':'network'} and 'network path' in result['message']


def test_gateway_timeouts_behind_a_slow_catalogue_point_at_the_catalogue():
    latest={('gateway-a','liveness'):observation('gateway-a','liveness'),
            ('gateway-a','business'):observation('gateway-a','business',False,result={'error':'ReadTimeout'}),
            ('catalog-b','business'):observation('catalog-b','business',duration_ms=640,slow=True)}
    result=explain(latest,'evidence',now=101)
    assert result['suggested_check']=={'node':'catalog-b','kind':'resources'} and 'answers slowly (640 ms)' in result['message']


def test_netem_queue_supports_removing_the_delay():
    network=observation('catalog-b','network',median_connect_ms=601.2,result={'qdiscs':[{'kind':'netem','dev':'eth0','delay_ms':600.0}]})
    result=explain({('catalog-b','network'):network},'action',now=101)
    assert result['evidence_ids']==[network['id']] and 'removing the traffic delay' in result['message'] and '600 ms' in result['message']


def test_simulator_debriefs_match_the_controller():
    """The browser simulator keeps its own copy of the debriefs; it must not drift from what the real lab says."""
    from pathlib import Path
    import json
    from aegis.controller import DEBRIEFS, DELAY_MS, LOSS_PCT, SLOW_MS
    source = (Path(__file__).resolve().parents[1] / 'preview' / 'sim.js').read_text()
    block = source.split('const DEBRIEFS = ', 1)[1].split('\n};', 1)[0].rstrip().rstrip(',') + '\n}'  # JS allows a trailing comma
    simulated = json.loads(block)
    expected = {k: v.replace('{slow}', str(SLOW_MS)).replace('{delay}', str(DELAY_MS)).replace('{loss}', str(LOSS_PCT)) for k, v in DEBRIEFS.items()}
    assert simulated == expected


def test_upstream_429_points_at_catalogue_traffic():
    latest={('gateway-a','liveness'):observation('gateway-a','liveness'),
            ('gateway-a','business'):observation('gateway-a','business',False,result={'upstream_status':429,'upstream_error':'QuotaExceeded'})}
    result=explain(latest,'hint',now=101)
    assert result['suggested_check']=={'node':'catalog-b','kind':'traffic'} and 'HTTP 429' in result['message'] and 'different caller' in result['message']


def test_timeout_with_a_fast_catalogue_points_at_the_network():
    latest={('gateway-a','liveness'):observation('gateway-a','liveness'),
            ('gateway-a','business'):observation('gateway-a','business',False,result={'error':'ReadTimeout'}),
            ('catalog-b','business'):observation('catalog-b','business',duration_ms=6)}
    result=explain(latest,'evidence',now=101)
    assert result['suggested_check']=={'node':'catalog-b','kind':'network'} and 'catalog-b-business' in result['evidence_ids']
    assert 'packets went missing' in result['message']


def test_timeout_without_catalogue_evidence_does_not_claim_packet_loss():
    latest={('gateway-a','liveness'):observation('gateway-a','liveness'),
            ('gateway-a','business'):observation('gateway-a','business',False,result={'error':'ReadTimeout'})}
    assert 'packets went missing' not in explain(latest,'evidence',now=101)['message']


def test_netem_loss_supports_removing_packet_loss():
    network=observation('catalog-b','network',median_connect_ms=1.2,result={'qdiscs':[{'kind':'netem','dev':'eth0','delay_ms':None,'loss_pct':40.0}],'tcp':{'retrans_pct':24.6}})
    result=explain({('catalog-b','network'):network},'action',now=101)
    assert result['evidence_ids']==[network['id']] and 'removing the packet loss' in result['message'] and '40%' in result['message'] and '24.6%' in result['message']
    assert 'removing the traffic delay' not in result['message']


def test_zero_quota_supports_resetting_client_quotas():
    traffic=observation('catalog-b','traffic',result={'quotas':[{'client':'gateway-a','limit_rps':0,'deployed_rps':20},{'client':'gateway-c','limit_rps':0,'deployed_rps':20}],
                                                     'clients':[{'client':'gateway-a','rejected':29},{'client':'gateway-c','rejected':31}],'quota_source':{'pushed_by':'quota-manager'}})
    result=explain({('catalog-b','traffic'):traffic},'action',now=101)
    assert result['suggested_check']=={'node':'catalog-b','kind':'traffic'} and 'resetting the client quotas' in result['message']
    assert '60 requests were rejected' in result['message'] and 'quota-manager' in result['message']


def test_healthy_traffic_supports_no_quota_action():
    traffic=observation('catalog-b','traffic',result={'quotas':[{'client':'gateway-a','limit_rps':20,'deployed_rps':20}],'clients':[]})
    assert 'No fresh inspection yet supports' in explain({('catalog-b','traffic'):traffic},'action',now=101)['message']
