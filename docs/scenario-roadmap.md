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
| 5 | **Bad release**: a new version fails about half of requests | Config drift | Roll back release | **Done** (F8) |
| 4 | **Memory leak and repeated OOM kills**: the service crash-loops as the kernel kills it and the deploy controller restarts it | Stopped service ("start" seems to work, then it fails again) | Roll back release | **Done** (F9) |
| 1 | **Traffic flood from one client**: high CPU from serving requests | CPU starvation (no runaway job to stop) | Rate-limit top client | **Done** (F10) |
| 2 | **Retry storm**: a brief catalogue slowdown is kept alive by app users retrying without backoff, overloading one gateway | A traffic flood; a broken gateway | Enable load shedding | **Done** (F11), after adding request deadlines and a bulkhead (see [validation](VALIDATION.md)) |
| 6 | **Thermal throttling / power capping**: the gateways fail under a power cap while the catalogue stays fast | CPU starvation | Lift power cap, once cooling recovers | **Done** (F13), with steady user load, since a cap alone barely slows light requests |
| 7 | **Rack power loss**: two services on one PDU stop together | Two separate stopped services | Reset breaker | **Done** (F12), with a simulated facility layer |
| 8 | **Brownout and restart loops**: feed A to rack R2 sags, so gateway-c drops out and reboots again and again | Memory leak (memory flat here); stopped service | Move to the redundant feed | **Done** (F15), with power feeds in the facility layer |
| 9 | **Clock skew** on one gateway: its time daemon steps the clock 300 s ahead, and the catalogue refuses its signed requests as stale | Bad release, config drift; zero quota (401, not 429) | Resync clock | **Done** (F17), after adding request signing |
| 10 | **DNS resolution failure** on one gateway: its resolver points at a nameserver that is not there | Stopped catalogue (it is fine, and reachable at its last known address); config drift | Restore resolver | **Done** (F14), with real DNS queries from the gateways |
| 11 | **Partial partition**: one gateway cannot reach the catalogue | Stopped catalogue; packet loss; DNS failure | Restore route | **Done** (F16), with a new fixed helper rule reviewed in [architecture](architecture.md) |
| 14 | **Heatwave compound incident**: a power cap sets off a retry storm on one gateway | Thermal throttling; a retry storm | Lift power cap, then enable load shedding | **Done** (F18), with multi-step recovery |
| 13, 15, 16 | Gray failure, read-only disk, path MTU black hole | | | Later |

Inspirations, all public: the Google SRE book chapters on cascading failures and load shedding; Google Cloud incident reports for the 2022 europe-west2 cooling failure, the 2015 St. Ghislain lightning strikes and the 2023 europe-west9 water leak and fire; and the December 2020 authentication outage attributed to the quota system.
