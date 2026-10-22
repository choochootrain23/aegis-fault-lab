# Implementation checkpoint

20 October 2026 (Singapore), fifth implementation checkpoint. Implementation authorized after design review. This file distinguishes implemented behaviour from the broader agreed design.

## Implemented

- Independent `gateway-a` and `catalog-b` FastAPI service processes; real HTTP dependency requests, correlation IDs, measured response durations and bounded dependency logs.
- Compose definition with project-specific isolated networks, resource caps, non-root/read-only fleet nodes, no fleet ports, loopback-only UI and a private Docker broker.
- Broker accepts only fixed inspect/start/stop operations on two named project services. It checks ownership, generation, networking, mounts, user, resource limits and privilege drift. No arbitrary shell or Docker proxy route.
- SQLite evidence, session, action and diagnosis persistence. Intent precedes mutation; ambiguous interrupted actions remain unknown and visible after restart. New startup observations are required; stale data is not healthy.
- Five-cycle baseline admission; one controlled service-unavailability scenario at a time; three-cycle verified recovery. Idempotency protects mutations and reset verification from duplicate retries.
- Practice and hidden-target challenge modes. Fault ledger excluded from challenge state/exports until reveal/end. Optional deterministic explanations/hints/actions; no AI inference is claimed.
- Fifteen-minute timer, explicit extension, sixty-second continuation grace, browser-independent expiry reset, reset verification and preserved history.
- First UI: muted canvas, colourful diagnostic controls, two-node topology, fresh evidence inspection/selection, optional notes, action timeline, investigation history and clear unknown/error states. Evidence reading pauses on open/select; practice can target either node; loading/disconnection states disable inappropriate actions.
- Archived investigations have stable paginated evidence, notes, expired-citation warnings and per-investigation exports. Exports prioritise cited observations, report omissions and stay within an 8 MiB budget.
- Opt-in deterministic guidance uses fresh observations, names supporting evidence IDs and suggests a read-only next check. It has no access to the hidden fault ledger.
- Session-bound requests reject stale browser actions after acquiring the workflow lock. Grace-expired extensions are rejected server-side; reset cannot erase an earlier verified recovery outcome.
- Local resource preflight, secret initialization, dependency lock, bounded evidence/log payloads and batched retention/storage-budget guards. Active evidence is retained. Reusable SQLite pages no longer permanently block admission after retention. Maintenance can run even when collection is paused.
- Broker contract now also checks dropped capabilities, no-new-privileges, swap/shared-memory limits, private namespaces, temporary filesystem size, restart policy and absence of device/host-resource access.
- Restart reconciliation restores both ambiguous and completed action results when a session update was interrupted. Explicit retry can finish an interrupted reset without replaying already recorded operations. Reset intent/automatic closure is durable before mutation.
- An OS file lock enforces one controller per database. Another worker fails clearly instead of competing for timers or mutations. Ownership releases on exit.
- Recovery and reset each retain twelve supporting observations from their three verification cycles; archives expose those citations and exports prioritise them.
- Summary retention starts at closure and preserves associated action history. Cleanup truncates checkpointed WAL allocation without waiting on external readers, then retries busy checkpoints. Baseline evidence excludes archived investigations.
- Archive notes, help and actions are reviewable; citation buttons open exact retained observations or report expiry. Polling preserves history-button focus and rejects older responses. Narrow phone layouts stack topology cards; hidden controls stay hidden.
- Evidence labels distinguish a completed inspection from a running service, and summarize failures in retained dependency logs. Requested guidance highlights exact supporting records even after they leave the latest polling window; reduced-motion users retain a static highlight.
- Guidance treats newer successful probes as potentially contradicting an older stopped-state observation and requests fresh inspection. No hidden fault ledger is consulted.
- Browser reads time out after five seconds; mutation requests time out after forty-five seconds with an explicit uncertain-completion message. Disconnection marks measurements unknown and disables diagnostic/maintenance/continuation actions until polling reconnects.

## Validation evidence

- Latest suite includes workflow, broker rejection, retention, API origin/token/host checks, evidence-scope validation, extension retries, challenge exports, cross-process ownership and real service-process integration. **80 backend tests pass**, including the real-process HTTP test. **21 pure UI-logic tests pass** for timers, freshness, request ordering, evidence labels and enabled controls; event-wiring JavaScript also parses.
- Real-process test starts a catalogue and gateway on temporary loopback ports, verifies a business request, stops the catalogue, observes gateway liveness plus business failure, restarts the catalogue and checks three successful requests. Processes are cleaned up. This is not a Docker isolation test.
- `docker compose config --quiet` passes with a non-secret validation placeholder.
- Headless Chromium review passes with a real controller and isolated, explicitly labelled archive test records. Desktop (1440px), 390px and 320px screenshots were inspected. Citation lookup/expiry, escaped note content, keyboard dismissal, focus across polling, hidden controls and horizontal overflow are checked. Screenshots/report live under ignored `state/ui-review/`; rerun `tests/browser_review.py` to reproduce. This does not validate active Docker investigations.
- The archive review also verifies a stalled controller response times out, disables actions and recovers after reconnection.
- `tests/browser_workflow.py` passes against Chromium, the real controller and two real service subprocesses. It covers stable open evidence, saved diagnosis, guided citations outside the recent window, ineffective wrong-node recovery, verified correct recovery/reset, challenge UI, expiry extension and browser-independent automatic reset. Only process control is adapted; collector cadence/lease expiry are accelerated. Reports are under ignored `state/ui-workflow/`. The expiry screenshot was inspected. See [validation boundaries](VALIDATION.md).
- Tests require execution outside the current sandbox because its thread wakeup restrictions hang framework file-serving tests. All test Docker operations use explicit test doubles. No unrelated containers or services were modified.

## Production Docker validation

Docker access is restored through the refreshed login's existing group membership. No socket permissions were changed. The shared-image build race was fixed by building only the controller's shared image. A controller-only operator bridge makes the loopback UI port reachable; fleet and broker networks remain internal.

Three production rounds passed on 20 October 2026: detection **5.59–5.67 s**, verified recovery **7.36–7.41 s**, with wrong-node attempts rejected as recovery and baseline restored each round. `scripts/verify_runtime.py` passed the broker inspection contract and actual cgroup checks: 192 MiB RAM, zero swap, 0.25 CPU, 64 PIDs, UID 10001, zero effective capabilities and no-new-privileges on both fleet nodes.

The real Docker Chromium challenge/restart walkthrough passed. State/export responses withheld the target, the active investigation survived controller restart, fresh inspections identified the stopped gateway, and browser recovery/reset succeeded with no JavaScript errors. The desktop challenge screenshot was inspected. An initial validation-client disconnect error was fixed and its owned investigation reset before the successful rerun; three transport regression tests cover that failure.

Reports are under ignored `state/validation/`. The app is running at http://localhost:8787 with both fleet services healthy and no active investigation at this checkpoint.

## Sixth checkpoint (20 October 2026)

- Published to https://github.com/choochootrain23/aegis-fault-lab with a GitHub Pages browser simulator. The simulator reuses the real UI with a JavaScript stand-in controller; every measurement in it is generated and permanently labelled. `tests/preview_walkthrough.py` passes.
- Production restart during an in-flight mutation and the unaccelerated fifteen-minute lease both pass, with resource samples. See [validation](VALIDATION.md).
- Both the app and the simulator now follow an editorial visual style inspired by getartcraft.com, with self-hosted OFL fonts and colour reserved for state and diagnostics. The real Docker browser walkthrough passes on the restyled UI.
- Design developed further: progress tracker, state-driven suggested-next card with cycle meters (`healthy_streak` added to `/api/state`), per-check last results, animated request wire and signal colours. Backend tests, the simulator walkthrough and the real Docker browser walkthrough pass.
- Interactive live map (`web/topology.js`) with draggable controller, broker and service nodes, state-coloured edges, keyboard movement and remembered layout; section-aware navigation; vivid Google-style palette. The simulator walkthrough covers dragging, click-vs-drag, keyboard movement, persistence, reset and the control-plane toggle; the real Docker browser walkthrough passes.

## Seventh checkpoint (20 October 2026)

- Three fault types on either service: stopped service, CPU starvation from a bounded runaway job, and a full 8 MiB `/tmp` causing real `ENOSPC`/HTTP 507 write failures. Challenge mode hides both the fault type and the target.
- New checks: Resource use (Docker stats: CPU against the container limit, kernel throttling, memory) and Disk usage (statvfs and largest files). Slow requests above 300 ms count as degraded.
- Recovery toolbox: Start service, Stop runaway job, Clear temp files. Actions that find nothing to change report *unchanged*.
- Incident report with separate measures (no score) and a replayable latency chart, also in every archived investigation; optional guided tour.
- Broker gained only a read-only `resources` operation. Disk and job faults use token-protected service maintenance endpoints; `init_env.py` now adds missing credentials on upgrade.
- Validation: 100 backend tests; production scenario matrix across all faults and targets; real Docker browser challenge; simulator walkthrough; older browser reviews. See [validation](VALIDATION.md).

## Eighth checkpoint (20 October 2026)

- **Fourth fault: config drift.** A bad runtime reload overrides one setting in memory: the gateway's upstream moves to a port nothing listens on (looks exactly like a stopped catalogue from the gateway's side), or the catalogue's data source moves to a dataset that was never deployed (HTTP 500). Liveness, container state, CPU and disk stay normal. Remedy: **Restore config**. Reset reverses it; a container restart also reloads the deployed values.
- **Two new diagnostic tools.** **Running config** lists each setting beside its deployed value and flags drift. **Request trace** times each step of one business request (upstream call, catalogue load, receipt write) and the controller adds the time spent outside the handler, shown as a waterfall with the slowest step highlighted, so slowness and failures can be located rather than guessed.
- Guidance gained two rules: a catalogue answering HTTP 500 points at its running config, and a failing gateway beside a healthy, fast catalogue points at the gateway's own path and config. Action guidance cites a drifted config check.
- Reliability: the evidence-browsing API now accepts every check kind (resource, disk, config and trace records could not be filtered before); polling backs off to 16 s while the controller is unreachable, announces reconnection, slows to 10 s in a hidden tab and refreshes immediately on return; a scenario that needs the fleet token is refused up front for every maintenance-based fault, not only disk-full.
- Interface: request packets travel the live map (green and quick when healthy, amber and slow when degraded, red and stopping halfway when requests fail), remedy tags name the verb and the path it actually took, check buttons show a scanning bar while running, toasts carry a tone, verified recovery gets a brief celebration (a plain message under reduced motion), and keyboard shortcuts (**?** lists them) cover reading and checking but never fleet changes.

## Ninth checkpoint (21 October 2026)

- **Third node.** gateway-c is a second gateway that also calls catalog-b. Comparing the two gateways is now part of the diagnosis: a fault reaching both points at the shared catalogue, one reaching a single gateway is local. Guidance, the live map, the incident report (three series), keyboard shortcuts (A/B/C) and every validator cover all three services.
- **Fifth fault: network delay.** The broker adds a fixed 600 ms netem delay to one service's network interface through a short-lived helper container that joins only that container's network namespace, with `NET_ADMIN` as its only capability, a read-only root, no new privileges and tight memory and process limits; it refuses to replace a queue it did not create. A new **Network path** check times TCP connects from the controller and lists the service's queue settings. Remedy: **Remove traffic delay**. Guidance points slow requests with normal CPU at the network path.
- **Optional AI investigator.** A read-only panel asks a model (Gemini by default, or Claude through the Anthropic SDK) about the evidence on screen. It receives only summarised public observations and node health, never the fault record, cannot act, cites observations by id (citations are checked against what was sent) and is limited to 30 questions per investigation; use is recorded as help. Nothing is sent anywhere without `AEGIS_AI_API_KEY` in `.env`.
- **Hardening.** Storage budget configurable with `AEGIS_STORAGE_LIMIT_MIB` and tested under pressure; `scripts/soak.py` for multi-hour runs (a full two-hour run passed 157 of 157 rounds with flat controller memory); an axe-core accessibility audit (contrast, landmark and focus fixes made); simulator walkthrough in Firefox as well as Chromium; a 320 px overflow in the scenario picker fixed.
- Validation: see [validation](VALIDATION.md). In a 30-round hidden-fault evaluation on real Docker, the AI investigator named the right remedy and service in every round once all inspections had been run, and gave one premature wrong guess in 30 rounds when only business checks had been run.
- AI hardening: service-supplied text is clipped before it reaches the model, the prompt treats evidence as data, and a test shows a fully hijacked model still cannot act, reveal the fault or fake citations.
- Simulator fidelity: the network-delay model is calibrated against about 4,400 real checks from the soak (no failures; about 610 ms through a delayed catalogue and 1,210 ms on a delayed gateway). The debrief text was corrected to match, and a test keeps the simulator's debriefs identical to the controller's.
- Clean checkout: a fresh clone set up from the README alone builds, starts, shows a healthy fleet with the AI off, and passes the three-round walkthrough. It found a preflight bug, now fixed: free disk was measured in the project folder rather than in Docker's storage.
- Portfolio: [interview walkthrough](interview-walkthrough.md), two [example incident exports](examples/) from real challenge runs (one with real AI answers), and `scripts/record_demo.py`, which records a captioned simulator demo.
- Interface motion: the suggested-next card breathes in its tone colour, with a light running down its bar and a comet circling it when an action is suggested; Start investigation carries a rotating ring of the signature colours; failing services ping on the live map; the AI shows a flowing line while it thinks. Everything stops under reduced motion.

## Tenth checkpoint (22 October 2026)

- **Sixth fault: packet loss.** The broker's helper applies a fixed `netem loss 50%` (exaggerated from real-world 1–2% so it shows within a minute). Requests are intermittently late or time out while nothing is down. **Network path** now reports the loss setting and each service's TCP retransmissions since the previous check, and records a connection attempt that outlasts its 3-second limit as a measurement instead of failing the check (found on the real lab: at 50% loss, the check that reveals the fault was itself failing over half the time). Each network remedy leaves the other fault's queue alone and reports *unchanged*. Remedy: **Remove packet loss**.
- **Seventh fault: quota set to zero**, inspired by public reports of the December 2020 Google authentication outage. The catalogue enforces per-caller quotas, which come from a quota system rather than its config; the fault pushes 0 for both gateways. The controller's probes are not limited, so the catalogue stays green while every gateway request gets HTTP 429. A new **Traffic & quotas** check shows requests, rejections and quotas per caller. This fault only targets the catalogue, and the scenario picker enforces that. Remedy: **Reset client quotas**.
- **Detection and verification for intermittent faults.** An investigation opens when three of the last five cycles fail (it needed three in a row). Once failures have come and gone twice, recovery needs ten consecutive healthy cycles instead of three, shown on the verification meter, so a lucky healthy run cannot pass a wrong fix.
- Guidance: HTTP 429 from the catalogue points at its traffic and quotas; a timeout beside a fast catalogue points at the network path; action guidance cites a loss queue or a zero quota. The AI investigator's prompt and evidence summaries include both faults.
- Simulator: packet loss is calibrated from real measurements (about 280 checks per path at 50% loss), and the quota fault, traffic counts and flapping rules mirror the controller.
- Interface: the evidence notebook shows a **Live** or **Paused** state, a **Cite** column and the list of cited records in the diagnosis; help answers say which kind they are and offer the next check inline; the AI investigator is a card.
- The planned scenarios are in the [scenario roadmap](scenario-roadmap.md).

## Eleventh checkpoint (22 October 2026)

- **Eighth fault: bad release.** A rollout deploys v1.5.0 in place of the stable v1.4.2; its new code path fails about half of business requests with a `TypeError`. A new **Release history** check shows the running version and each deploy. Remedy: **Roll back release**; restoring config changes nothing.
- **Ninth fault: memory leak.** A rollout of v1.5.1 leaks about 12 MiB a second until the kernel kills the process at the container's 192 MiB limit. The controller acts as the rollout's deploy controller, restarting the crashed service after three missed cycles and re-applying the release, so the service crash-loops until it is rolled back. A new **Memory** check shows the last exit (with Docker's out-of-memory flag) and the memory trend. The broker now reports `OOMKilled` and start and finish times.
- **Verification refinement.** Ten healthy cycles are required once a failure has come and gone on its own; the first recovery after a fix no longer counts, so steady faults keep three cycles and a crash loop restarted by hand cannot pass on a short run.
- Guidance: a service's own HTTP 500 `TypeError` points at its release history; a catalogue code error points at releases rather than config; an out-of-memory stop advises checking memory instead of starting; a growing memory trend or a post-boot release supports rolling back. The simulator models both faults and the deploy controller from real measurements.

## Twelfth checkpoint (22 October 2026)

- **Tenth fault: traffic flood.** A new `reports` service (hardened like the others, internal network only, never touched by the broker) runs a report export job that floods the catalogue over 160 keep-alive connections. The catalogue's CPU sits at its limit with no job to stop. The traffic check now reports each caller's current rate, and **Rate-limit top client** limits heavy callers without a quota, answering them with HTTP 429 and `Retry-After`; the exporter backs off with jitter. Traffic accounting moved to one-second buckets so a flood cannot push other callers out of the window.
- Robustness found while prototyping a retry storm: the traffic counter no longer cancels in-flight requests (it had leaked every pooled connection under overload), and every container allows 65,536 open files instead of 1,024.
- Guidance: CPU at the limit names both possible causes (a job, or one heavy caller) and defers to the traffic check when it shows a flood.

## Thirteenth checkpoint (22 October 2026)

- **Overload safety for every fault.** Each request carries a deadline, which services honour (answering 504 without doing the work) and gateways propagate to the catalogue. Gateways bound their upstream calls with a bulkhead below the HTTP client's pool.
- **Eleventh fault: retry storm.** App users from the reports service, 60 a second through one gateway, retry every failure at once. An 8-second catalogue compaction tips that gateway into an overload that outlives its cause. Gateways count retries per caller, and **Enable load shedding** caps a gateway's in-flight requests, turning the rest away at once. It only takes effect on a gateway that is actually queuing. The catalogue's per-gateway quota is now 300 a second, room for user traffic.
- Guidance: a caller whose requests are mostly retries means a retry storm, and supports shedding load rather than rate-limiting.

## Fourteenth checkpoint (23 October 2026)

- **A simulated facility layer.** Racks, PDUs, breakers, cooling and inlet temperature are modelled in the controller and every record says `simulated`; their effects act through the broker's ordinary operations. A new **Facility** check shows them.
- **Twelfth fault: rack power loss.** PDU-A's breaker trips and the catalogue and gateway-a stop together; starting either reports no power. Remedy: **Reset breaker**, which starts the rack, catalogue first.
- **Thirteenth fault: thermal throttling.** A cooling failure brings a real power cap (every container's CPU set to exactly 0.05 through a new fixed broker operation) under real daytime users. The gateways fail first while the catalogue stays fast. **Lift power cap** is refused until the simulated cooling recovers.
- Guidance: a lowered CPU limit points at the facility; a gateway slow even to liveness points at its own resources; an open breaker or an active cap is explained before any service-level fix.
- Validation: all 30 practice rounds and 32 challenge rounds on real Docker passed, covering every fault on every service it can target. A retry storm is now judged by the share of retries in the last ten seconds, after a storm caught early was taken for CPU starvation.
- Interface: the fixes moved under the live map in a grid and each check became one compact row, halving the height of the inspector (2,038 to 1,007 pixels at desktop width).
- AI investigator: the prompt now says what each fix does. With full evidence, its answers went from 22 to 30 correct out of 30 in challenge rounds across all thirteen faults.

## Next work

1. AI evaluation with other models and question wordings; more samples of the rarer faults.
2. Compound incidents (for example a heatwave that tips into a retry storm), DNS failure, clock skew and partial partitions from the roadmap.
3. Screen-reader review; runs measured in days.

## Boundaries preserved

Only `aegis-rack-diagnostics/` is changed. The sibling project is untouched. Runtime has no fake-fleet fallback. Virtual containers are not physical repair experience. The design documents remain the intended final scope; this checkpoint does not claim all roadmap stages are complete.
