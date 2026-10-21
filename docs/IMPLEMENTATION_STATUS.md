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
- **Hardening.** Storage budget configurable with `AEGIS_STORAGE_LIMIT_MIB` and tested under pressure; `scripts/soak.py` for multi-hour runs (118 passing rounds over about 91 minutes so far, memory flat); an axe-core accessibility audit (contrast, landmark and focus fixes made); simulator walkthrough in Firefox as well as Chromium; a 320 px overflow in the scenario picker fixed.
- Validation: see [validation](VALIDATION.md). A real Gemini model diagnosed a live network delay correctly in the lab; real answers have not been evaluated systematically.

## Next work

1. Screen-reader review and Safari/WebKit coverage.
2. A full uninterrupted multi-hour soak, and a systematic evaluation of the AI investigator's answers across every fault.

## Boundaries preserved

Only `aegis-rack-diagnostics/` is changed. The sibling project is untouched. Runtime has no fake-fleet fallback. Virtual containers are not physical repair experience. The design documents remain the intended final scope; this checkpoint does not claim all roadmap stages are complete.
