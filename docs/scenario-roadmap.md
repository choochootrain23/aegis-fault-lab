# Scenario roadmap

New faults modelled on the kinds of incident that public data-centre reports describe: power, cooling, traffic floods, bad rollouts, quota systems and network faults. They are inspired by published reports and the Google SRE books, not copies of them, and make no claim about anyone's internal systems.

## Design rules

1. **Every fault has a look-alike.** The lesson is telling the two apart, and the look-alike's fix must change nothing.
2. **Real effects, honest simulation.** Containers have no power supply or inlet temperature. Where a story is physical, a simulated facility-telemetry service would report it, with every record marked `simulated`; the effects on the services (throttling, stopped containers) stay real.
3. **The same safety model.** The broker keeps a fixed list of operations, any helper gets one capability briefly, traffic stays on the internal network, and every rate and size is bounded. Each new broker operation gets its own review.
4. **Verification must survive intermittent faults.** Detection counts failing cycles in a window, and a flapping investigation needs a longer healthy run (see [architecture](architecture.md)).

## Status

| # | Scenario | Look-alike | Fix | Status |
| --- | --- | --- | --- | --- |
| 3 | **Packet loss**: 50% of a service's packets dropped; requests intermittently late or timed out | Network delay (every request evenly late) | Remove packet loss | **Done** (F6) |
| 12 | **Quota set to zero**: the catalogue refuses both gateways with HTTP 429 while its own probes stay green | Config drift; traffic flood | Reset client quotas | **Done** (F7) |
| 5 | **Bad release on one gateway**: a new version fails 30% of requests | Config drift | Roll back release | Planned: needs a **Release history** check |
| 4 | **Memory leak and repeated OOM kills**: the service flaps as Docker kills and restarts it | Stopped service ("start" seems to work, then it fails again) | Roll back the leaking build | Planned: needs a **Memory & restarts** check |
| 1 | **Traffic flood from one client**: high CPU from serving requests, rejections | CPU starvation (no runaway job to stop) | Rate-limit the top client | Planned: reuses **Traffic & quotas** plus a bounded load-generator helper |
| 2 | **Retry storm**: a brief catalogue failure is kept alive by gateways retrying without backoff | A catalogue that is still broken | Enable retry backoff on the gateways | Planned |
| 6 | **Thermal throttling / power capping**: everything slower at once, CPU throttled but usage low | CPU starvation on every node | Shed load, then lift the cap once cooling recovers | Planned: needs facility telemetry and a reversible "power cap" broker operation |
| 7 | **Rack power loss**: two services on one PDU stop together | Two separate faults | Reset the breaker, start in dependency order | Planned: facility telemetry |
| 8 | **Brownout and restart loops** | Memory leak (memory flat here) | Move to the redundant feed | Planned |
| 9 | **Clock skew** on one gateway: signed requests rejected | Bad release, config drift | Resync clock | Planned: needs request signing |
| 10 | **DNS resolution failure** on one gateway | Stopped catalogue (it is fine, and reachable by IP) | Restore resolver | Planned |
| 11 | **Partial partition**: one gateway cannot reach the catalogue | Stopped catalogue | Restore route | Planned: new fixed helper rule, needs review |
| 13–16 | Gray failure, heatwave compound incident, read-only disk, path MTU black hole | | | Later |

Inspirations, all public: the Google SRE book chapters on cascading failures and load shedding; Google Cloud incident reports for the 2022 europe-west2 cooling failure, the 2015 St. Ghislain lightning strikes and the 2023 europe-west9 water leak and fire; and the December 2020 authentication outage attributed to the quota system.
