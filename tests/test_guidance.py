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
    traffic=observation('catalog-b','traffic',result={'quotas':[{'client':'gateway-a','limit_rps':0,'deployed_rps':100},{'client':'gateway-c','limit_rps':0,'deployed_rps':100}],
                                                     'clients':[{'client':'gateway-a','rejected':29},{'client':'gateway-c','rejected':31}],'quota_source':{'pushed_by':'quota-manager'}})
    result=explain({('catalog-b','traffic'):traffic},'action',now=101)
    assert result['suggested_check']=={'node':'catalog-b','kind':'traffic'} and 'resetting the client quotas' in result['message']
    assert '60 requests were rejected' in result['message'] and 'quota-manager' in result['message']


def test_healthy_traffic_supports_no_quota_action():
    traffic=observation('catalog-b','traffic',result={'quotas':[{'client':'gateway-a','limit_rps':100,'deployed_rps':100}],'clients':[]})
    assert 'No fresh inspection yet supports' in explain({('catalog-b','traffic'):traffic},'action',now=101)['message']


def test_a_gateways_own_500_points_at_its_release_history():
    latest={('gateway-c','liveness'):observation('gateway-c','liveness'),
            ('gateway-c','business'):observation('gateway-c','business',False,http_status=500,result={'error':'TypeError','version':'v1.5.0'})}
    result=explain(latest,'hint',now=101)
    assert result['suggested_check']=={'node':'gateway-c','kind':'releases'} and 'v1.5.0' in result['message']


def test_a_catalogue_code_error_points_at_releases_not_config():
    latest={('gateway-a','liveness'):observation('gateway-a','liveness'),
            ('gateway-a','business'):observation('gateway-a','business',False,result={'upstream_status':500,'upstream_error':'TypeError'})}
    assert explain(latest,'evidence',now=101)['suggested_check']=={'node':'catalog-b','kind':'releases'}


def test_an_out_of_memory_stop_does_not_recommend_starting():
    service=observation('catalog-b','service',running=False,oom_killed=True,exit_code=137)
    result=explain({('catalog-b','service'):service},'action',now=101)
    assert 'OOMKilled' in result['message'] and 'You can try starting' not in result['message']
    assert result['suggested_check']=={'node':'catalog-b','kind':'memory'}


def test_a_growing_memory_trend_supports_rolling_back():
    memory=observation('catalog-b','memory',running=True,oom_killed=False,result={'growth_mib_s':11.8,'current_mib':130.2,'limit_mib':192.0,'version':'v1.5.1'})
    result=explain({('catalog-b','memory'):memory},'action',now=101)
    assert 'rolling back the release' in result['message'] and '11.8 MiB a second' in result['message'] and 'v1.5.1' in result['message']
    steady=observation('catalog-b','memory',running=True,oom_killed=False,result={'growth_mib_s':0.1,'current_mib':45.0,'limit_mib':192.0})
    assert 'No fresh inspection yet supports' in explain({('catalog-b','memory'):steady},'action',now=101)['message']


def test_a_release_after_boot_supports_rolling_back():
    releases=observation('gateway-a','releases',result={'running':'v1.5.0','history':[{'version':'v1.4.2','by':'deployment'},{'version':'v1.5.0','by':'release-bot'}]})
    result=explain({('gateway-a','releases'):releases},'action',now=101)
    assert 'rolling back the release' in result['message'] and 'release-bot' in result['message']
    unchanged=observation('gateway-a','releases',result={'running':'v1.4.2','history':[{'version':'v1.4.2','by':'deployment'}]})
    assert 'No fresh inspection yet supports' in explain({('gateway-a','releases'):unchanged},'action',now=101)['message']


def test_cpu_at_the_limit_just_after_a_restart_is_not_called_starvation():
    from datetime import datetime, timezone
    started=datetime.fromtimestamp(95,timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.123456789Z')
    booting=observation('gateway-a','resources',cpu_pct_of_limit=100.0,cpu_limit=0.25,started_at=started)
    result=explain({('gateway-a','resources'):booting},'action',now=101)
    assert 'stopping the runaway job' not in result['message'] and 'boots' in result['message']
    long_running=observation('gateway-a','resources',cpu_pct_of_limit=100.0,cpu_limit=0.25,started_at='0001-01-01T00:00:00Z')
    assert 'stopping the runaway job' in explain({('gateway-a','resources'):long_running},'action',now=101)['message']


def test_a_heavy_caller_without_a_quota_supports_rate_limiting():
    traffic=observation('catalog-b','traffic',result={'clients':[{'client':'report-exporter','requests':21000,'rps':350.0,'recent_rps':355.0},{'client':'gateway-a','requests':30,'rps':0.5}],
                                                     'quotas':[{'client':'gateway-a','limit_rps':100,'deployed_rps':100}]})
    result=explain({('catalog-b','traffic'):traffic},'action',now=101)
    assert 'rate-limiting the top client' in result['message'] and 'report-exporter' in result['message']
    limited=observation('catalog-b','traffic',result={'clients':[{'client':'report-exporter','requests':400,'rps':6.7}],
                                                     'quotas':[{'client':'report-exporter','limit_rps':2,'deployed_rps':None}]})
    assert 'No fresh inspection yet supports' in explain({('catalog-b','traffic'):limited},'action',now=101)['message']


def test_cpu_at_the_limit_defers_to_a_flood_and_otherwise_names_both_causes():
    cpu=observation('catalog-b','resources',cpu_pct_of_limit=99.0,cpu_limit=0.25)
    alone=explain({('catalog-b','resources'):cpu},'action',now=101)
    assert 'stopping the runaway job' in alone['message'] and 'rate-limit that client' in alone['message'] and alone['suggested_check']['kind']=='traffic'
    traffic=observation('catalog-b','traffic',result={'clients':[{'client':'report-exporter','requests':21000,'rps':350.0,'recent_rps':355.0}],'quotas':[]})
    both=explain({('catalog-b','resources'):cpu,('catalog-b','traffic'):traffic},'action',now=101)
    assert both['evidence_ids']==[traffic['id']] and 'runaway job' not in both['message']


def test_a_caller_that_flooded_a_minute_ago_but_is_quiet_now_is_not_flagged():
    traffic=observation('catalog-b','traffic',result={'clients':[{'client':'report-exporter','requests':21000,'rps':350.0,'recent_rps':0.0}],'quotas':[]})
    assert 'No fresh inspection yet supports' in explain({('catalog-b','traffic'):traffic},'action',now=101)['message']


def test_mostly_retries_means_a_retry_storm_and_supports_load_shedding():
    traffic=observation('gateway-a','traffic',result={'clients':[{'client':'mobile-app','requests':9000,'rps':150.0,'recent_rps':160.0,'retries':6600}],'quotas':[]})
    result=explain({('gateway-a','traffic'):traffic},'action',now=101)
    assert 'enabling load shedding on gateway-a' in result['message'] and '73%' in result['message']
    assert 'rate-limiting' not in result['message']
    flood=observation('catalog-b','traffic',result={'clients':[{'client':'report-exporter','requests':21000,'rps':350.0,'recent_rps':355.0,'retries':0}],'quotas':[]})
    assert 'rate-limiting the top client' in explain({('catalog-b','traffic'):flood},'action',now=101)['message']


def test_an_open_breaker_supports_resetting_it_not_starting_services():
    facility=observation('gateway-a','facility',simulated=True,pdu='PDU-A',rack='R1',breaker='open',fed_services=['catalog-b','gateway-a'])
    stopped=observation('gateway-a','service',running=False,at=100.5)
    result=explain({('gateway-a','facility'):facility,('gateway-a','service'):stopped},'action',now=101)
    assert 'resetting the breaker' in result['message'] and 'catalog-b and gateway-a' in result['message'] and 'You can try starting' not in result['message']
    closed=observation('gateway-c','facility',simulated=True,pdu='PDU-B',rack='R2',breaker='closed',fed_services=['gateway-c'])
    assert 'No fresh inspection yet supports' in explain({('gateway-c','facility'):closed},'action',now=101)['message']


def test_a_lowered_cpu_limit_points_at_the_facility_and_the_cap_lifts_only_after_cooling():
    capped=observation('catalog-b','resources',cpu_pct_of_limit=100.0,cpu_limit=0.05)
    result=explain({('catalog-b','resources'):capped},'action',now=101)
    assert 'below the 0.25' in result['message'] and result['suggested_check']=={'node':'catalog-b','kind':'facility'} and 'runaway job' not in result['message']
    hot=observation('catalog-b','facility',simulated=True,power_cap={'active':True,'cpu_limit':0.05},cooling={'crac_2':'failed'},inlet_c=38.4)
    waiting=explain({('catalog-b','facility'):hot},'action',now=101)
    assert 'no safe fix yet' in waiting['message'] and 'lifting the power cap' not in waiting['message']
    cooled=observation('catalog-b','facility',simulated=True,power_cap={'active':True,'cpu_limit':0.05},cooling={'crac_2':'running'},inlet_c=24.8)
    assert 'lifting the power cap' in explain({('catalog-b','facility'):cooled},'action',now=101)['message']


def test_a_gateway_slow_even_to_liveness_points_at_its_own_resources():
    latest={('gateway-a','liveness'):observation('gateway-a','liveness',duration_ms=420.0,slow=True),
            ('gateway-a','business'):observation('gateway-a','business',False,result={'error':'ReadTimeout'})}
    result=explain(latest,'evidence',now=101)
    assert result['suggested_check']=={'node':'gateway-a','kind':'resources'} and 'slow even to liveness' in result['message']


def test_a_backed_up_gateway_points_at_its_own_resources_not_the_network():
    for result in ({'error':'UpstreamBusy'},{'error':'HTTPStatusError','upstream_status':504,'upstream_error':'DeadlineExceeded'}):
        latest={('gateway-c','liveness'):observation('gateway-c','liveness'),('gateway-c','business'):observation('gateway-c','business',False,result=result),
                ('catalog-b','business'):observation('catalog-b','business',duration_ms=4)}
        explained=explain(latest,'evidence',now=101)
        assert explained['suggested_check']=={'node':'gateway-c','kind':'resources'} and 'backed up' in explained['message'] and 'packets' not in explained['message']


def test_heavy_users_at_a_gateway_are_not_called_a_flood():
    traffic=observation('gateway-a','traffic',result={'clients':[{'client':'mobile-app','requests':2400,'rps':40.0,'recent_rps':60.0,'retries':300}],'quotas':[]})
    assert 'rate-limiting' not in explain({('gateway-a','traffic'):traffic},'action',now=101)['message']


def test_a_storm_is_judged_by_its_recent_retries_not_the_minute_total():
    early=observation('gateway-a','traffic',result={'clients':[{'client':'mobile-app','requests':4435,'retries':2025,'rps':74.0,'recent_rps':238.8,'recent_retries':1700}],'quotas':[]})
    assert 'enabling load shedding' in explain({('gateway-a','traffic'):early},'action',now=101)['message']
    cpu=observation('gateway-a','resources',cpu_pct_of_limit=99.0,cpu_limit=0.25)
    message=explain({('gateway-a','resources'):cpu},'action',now=101)['message']
    assert 'load shedding' in message and 'rate-limit' not in message


def test_a_name_that_does_not_resolve_points_at_the_gateways_network_path():
    latest={('gateway-c','liveness'):observation('gateway-c','liveness'),
            ('gateway-c','business'):observation('gateway-c','business',False,result={'error':'NameResolutionError','detail':'catalog-b: connection refused by nameserver 127.0.0.53'})}
    unknown=explain(latest,'evidence',now=101)  # a stopped catalogue's name stops resolving too, so check it first
    assert unknown['suggested_check']=={'node':'catalog-b','kind':'service'} and 'drops the name of a stopped container' in unknown['message']
    latest[('catalog-b','business')]=observation('catalog-b','business',duration_ms=3)
    result=explain(latest,'evidence',now=101)
    assert result['suggested_check']=={'node':'gateway-c','kind':'network'}
    assert 'could not resolve' in result['message'] and '127.0.0.53' in result['message']


def test_a_failed_lookup_supports_restoring_the_resolver_but_a_working_one_does_not():
    dns={'name':'catalog-b','ok':False,'error':'catalog-b: connection refused by nameserver 127.0.0.53','nameserver':'127.0.0.53','deployed_nameserver':'127.0.0.11',
         'changed':True,'pushed_by':'node-settings-sync','last_good_address':'172.18.0.4','connect_last_good_ms':0.4}
    failing=observation('gateway-a','network',result={'qdiscs':[],'dns':dns})
    result=explain({('gateway-a','network'):failing},'action',now=101)
    assert result['evidence_ids']==[failing['id']] and 'restoring the resolver' in result['message']
    assert 'node-settings-sync' in result['message'] and 'took 0.4 ms' in result['message']
    gone=observation('gateway-a','network',result={'qdiscs':[],'dns':{**dns,'error':'catalog-b: SERVFAIL from nameserver 127.0.0.11','nameserver':'127.0.0.11','changed':False,
                                                                     'connect_last_good_ms':None,'connect_last_good_error':'TimeoutError'}})
    assert 'No fresh inspection' in explain({('gateway-a','network'):gone},'action',now=101)['message']  # a stopped catalogue, not the resolver
    working=observation('gateway-a','network',result={'qdiscs':[],'dns':{**dns,'ok':True,'error':None,'changed':False}})
    assert 'No fresh inspection' in explain({('gateway-a','network'):working},'action',now=101)['message']


def test_a_sagging_feed_supports_moving_to_the_redundant_feed_instead_of_starting():
    facility=observation('gateway-c','facility',simulated=True,pdu='PDU-B',rack='R2',breaker='closed',fed_services=['gateway-c'],active_feed='A',psu_min_v=200,
                         feeds={'A':{'voltage_v':186,'status':'sagging'},'B':{'voltage_v':231,'status':'normal'}},
                         events=[{'at':90,'event':'utility feed A sagging to 186 V'},{'at':91,'event':'power supply dropout'},{'at':99,'event':'power supply dropout'}])
    stopped=observation('gateway-c','service',at=99,running=False)
    result=explain({('gateway-c','facility'):facility,('gateway-c','service'):stopped},'action',now=101)
    assert result['evidence_ids']==[facility['id']] and 'moving the rack to the redundant feed' in result['message'] and '2 power-supply dropouts' in result['message']
    normal={**facility,'body':{**facility['body'],'feeds':{'A':{'voltage_v':230,'status':'normal'},'B':{'voltage_v':231,'status':'normal'}}}}
    assert 'starting that selected service' in explain({('gateway-c','facility'):normal,('gateway-c','service'):stopped},'action',now=101)['message']


def test_a_gateway_that_alone_cannot_connect_is_pointed_at_its_own_network_path():
    latest={('gateway-a','liveness'):observation('gateway-a','liveness'),
            ('gateway-a','business'):observation('gateway-a','business',False,result={'error':'ConnectTimeout'}),
            ('catalog-b','business'):observation('catalog-b','business',duration_ms=3),
            ('gateway-c','liveness'):observation('gateway-c','liveness'),('gateway-c','business'):observation('gateway-c','business',duration_ms=5)}
    result=explain(latest,'evidence',now=101)
    assert result['suggested_check']=={'node':'gateway-a','kind':'network'} and 'Only gateway-a is affected' in result['message']


def test_a_drop_filter_supports_restoring_the_route():
    network=observation('gateway-c','network',median_connect_ms=0.5,result={'qdiscs':[{'kind':'prio','root':True}],'filters':[{'kind':'u32','dst':'172.20.0.3/32','action':'drop'}],
                        'upstream_connect':{'name':'catalog-b','address':'172.20.0.3','ok':False,'error':'TimeoutError'},'dns':{'name':'catalog-b','ok':True}})
    result=explain({('gateway-c','network'):network},'action',now=101)
    assert result['evidence_ids']==[network['id']] and 'restoring the route' in result['message'] and '172.20.0.3/32' in result['message']


def test_an_expired_signature_points_at_the_gateways_clock_not_its_quota():
    latest={('gateway-a','liveness'):observation('gateway-a','liveness'),
            ('gateway-a','business'):observation('gateway-a','business',False,result={'upstream_status':401,'upstream_error':'RequestExpired',
                                                                                      'upstream_detail':'timestamp +300.0 s from the catalogue clock; allowed ±30 s'})}
    result=explain(latest,'evidence',now=101)
    assert result['suggested_check']=={'node':'gateway-a','kind':'network'}
    assert 'HTTP 401' in result['message'] and '+300.0 s' in result['message'] and 'clock' in result['message']


def test_a_measured_clock_offset_supports_resync_and_a_small_one_does_not():
    skewed=observation('gateway-a','network',clock_offset_s=300.02,result={'qdiscs':[],'clock':{'source':'time daemon, peer 10.20.0.7 (falseticker)'}})
    result=explain({('gateway-a','network'):skewed},'action',now=101)
    assert result['evidence_ids']==[skewed['id']] and 'resynchronising its clock' in result['message'] and 'falseticker' in result['message']
    steady=observation('gateway-a','network',clock_offset_s=0.4,result={'qdiscs':[]})
    assert 'No fresh inspection' in explain({('gateway-a','network'):steady},'action',now=101)['message']
