# Validation boundaries

The lab has several distinct validation layers. Passing one does not establish the guarantees of another.

| Layer | What runs | What it establishes | What it does not establish |
| --- | --- | --- | --- |
| Backend regression suite | Controller, SQLite, broker contract tests and explicit HTTP test doubles | Workflow transitions, challenge response filtering, scoped evidence, retry semantics, retention, ownership exclusion and broker rejection rules | Compatibility with an actual Docker daemon |
| Real service-process test | Two owned Python services on temporary loopback ports | Actual dependency HTTP failure when the catalogue exits, and successful requests after restart | Container limits or isolation |
| UI logic tests | Pure JavaScript decisions and event-wiring syntax | Timer calculations, control eligibility, request ordering and evidence labels | Browser layout or end-to-end behavior |
| Browser archive review | Headless Chromium, real controller routes, isolated labelled archive fixtures | Desktop/mobile layout, exact citation lookup and expiry, escaped notes, polling focus, keyboard dismissal, connection timeout and reconnection | A running injected fault |
| Browser workflow review | Headless Chromium, real controller, two owned service processes, test-only process-control adapter | Investigation, diagnosis, guidance, wrong/correct recovery, challenge UI and browser-independent lease cleanup | Production broker behavior, Docker isolation or production latency |
| Browser simulator walkthrough | Headless Chromium against the static `site/` build and `preview/sim.js` | The published preview loads, stays labelled as a simulator, and completes practice/challenge, recovery, diagnosis, archive, export and expiry-continuation flows without page errors | Anything about real services, Docker or measured latency |
| Accessibility audit | axe-core 4.10.2 in Chromium against the static `site/` build (`tests/accessibility_audit.py`) | No serious or critical automated violations on the welcome dialog, working page, active investigation, shortcuts dialog, incident report and phone layout | Screen-reader usability; automated rules find only part of what matters |
| Soak run | `scripts/soak.py --run --hours N` against the production Compose lab | Repeated random investigations without failure, and bounded memory and storage over hours | Behaviour over days, or under other workloads on the host |
| Docker walkthrough | Production Compose deployment and `scripts/validate_f1.py --run` | Full controller/broker/container integration, repeatable measured recovery | Physical hardware repair experience |

## Latest evidence

20 October 2026: **80 backend tests** and **21 pure UI logic tests** pass. Both browser reviews pass. Desktop/mobile archive screenshots and the active expiry prompt have been visually inspected.

The production Docker walkthrough now passes all three rounds. Detection took 5.59–5.67 seconds; verified recovery took 7.36–7.41 seconds. Effective fleet cgroup/privilege checks pass via `scripts/verify_runtime.py`. The real Docker browser challenge/restart/recovery walkthrough also passes via `scripts/validate_browser.py --run`, with no JavaScript page errors. Reports and screenshots are retained under ignored `state/validation/`.

A controller restart during an active investigation is validated.

Two further production gates passed on 20 October 2026 via `scripts/validate_gates.py`. **Restart during an in-flight mutation:** the controller was killed 0.15 s after a recovery start was sent; after restart the action was recorded as unknown with the reconciliation note, Docker logged exactly one start (the broker completed it independently), nothing was replayed, and recovery was credited only after twelve fresh healthy observations. **Unaccelerated lease:** with no browser attached, the fifteen-minute timer expired, the sixty-second grace elapsed and the verified automatic reset ended the investigation 69 s after the deadline, citing twelve baseline observations, with both nodes healthy. Over that 16-minute run the controller used 45–49 MiB and at most 2% CPU, the broker about 16 MiB, the gateway about 19 MiB; host available memory stayed between 2.8 and 3.3 GiB, swap use was flat at about 1.4 GiB and the database grew from 3.0 to 4.1 MiB. Storage-pressure exhaustion and multi-hour runs remain untested.

**Scenario matrix (20 October 2026, `scripts/validate_scenarios.py --run`).** On real Docker, every fault was run on both services in practice mode, then challenge rounds continued until every fault type and both targets had appeared (six rounds), then a reset without recovery. Each round identified the fault from inspections alone, received guidance citing that evidence, confirmed that wrong remedies (wrong node, and wrong action on a running target) reported *unchanged* and were not credited, verified recovery from twelve fresh observations, and reset to a healthy baseline. Every challenge diagnosis matched the revealed scenario, and exports withheld the fault until reveal. Detection took 5.0–14.1 s (CPU starvation is slowest because its slowness is statistical) and verified recovery 4.1–8.4 s. The real Docker browser challenge walkthrough also passed with a hidden CPU-starvation fault found from evidence and fixed through the UI. Backend tests: 100 pass. The simulator walkthrough covers all three faults, the report replay, the tour and the live map; the older archive and workflow browser reviews pass against the updated harness. Do not describe the entire roadmap as complete.

**Config drift and new tools (21 October 2026).** With the fourth fault added, the scenario matrix passed again on real Docker: all four faults on both services in practice, challenge rounds until every fault and target had appeared (four rounds), and a reset without recovery. Config drift was detected in 6.1 s and verified recovered in 5.1 s on either service; the full matrix ranged 5.1–9.1 s to detection and 5.1–8.5 s to verified recovery. Diagnoses now also read the running config, and wrong remedies (restore on the healthy node, start on a running target) reported *unchanged*. The matrix now judges each node's health by its most common reading over five cycles, because one fast sample during CPU starvation had made a single snapshot read healthy. The real Docker browser walkthrough (a hidden disk-full fault this run), the archive and workflow browser reviews, and the simulator walkthrough (which now drives config drift, the trace waterfall, the config table, keyboard shortcuts, request packets and the recovery celebration) all pass with no page errors. Backend tests: 108 pass.

**Third node, network delay, AI investigator and hardening (21 October 2026).** The fleet now has three services: gateway-a and gateway-c both call catalog-b, so a fault that reaches both gateways points at what they share. The fifth fault adds a fixed 600 ms netem delay on one service's network interface (long enough to be clearly slower than the 300 ms threshold, short enough that requests still succeed inside their timeouts); it is applied and removed by a short-lived broker helper with only `NET_ADMIN`, and the new Network path check shows the TCP connect times and the queue settings. The scenario matrix passed on real Docker across all five faults on all three services (fifteen practice rounds), seven challenge rounds until every fault and target had appeared, and a reset without recovery: detection 5.1–9.1 s, verified recovery 5.1–7.6 s; network delay was detected in 6.1–8.1 s and verified recovered in 5.2–6.8 s. The real Docker browser challenge passed with a hidden network delay on catalog-b found and removed through the UI, and survived a controller restart. The archive and workflow browser reviews pass (the workflow review now also asks the investigator, using a labelled harness stand-in, never a model); the archive review caught a 12 px horizontal overflow at 320 px in the scenario picker, now fixed. The simulator walkthrough passes in Chromium and Firefox, and the axe-core audit reports no serious or critical violations after contrast, landmark and focus fixes. A storage-pressure test fills the evidence budget (configurable with `AEGIS_STORAGE_LIMIT_MIB`) with a real SQLite database: collection and checks then stop with an explicit HTTP 507 telling the operator to export and run retention, retention frees space, and collection resumes on the next cycle. Backend tests: 127 pass, 1 skipped; UI logic tests: 24 pass.

**Soak (20–21 October 2026, `scripts/soak.py`).** Two runs on real Docker totalled **118 passing rounds over about 91 minutes**: 78 practice and 40 challenge, covering all five faults (20–31 rounds each) on all three services. Detection took 5.1–16.2 s (the slowest were CPU starvation) and verified recovery 5.0–7.7 s. Controller memory stayed between 94 and 103 MiB with no upward trend. Broker memory rose from 49 to 72 MiB during the first run, then held flat at 72 MiB throughout the second. The write-ahead log never exceeded 4 MiB. The evidence database grew by about 0.1 MiB per round, as expected while evidence is kept for 24 hours. There were no collector errors.

The first run stopped after 64 rounds on a validator assertion, not a lab fault. During CPU starvation on catalog-b, the diagnosis was correct (the catalogue's CPU was at its limit), but the catalogue's own probe read slow in only one of five cycles while the gateways calling it read slow in four. The check now judges CPU starvation across every service the fault reaches; a starved gateway is still checked on its own. The second run was stopped on purpose after 42 minutes, because the host had to sleep; its in-progress round was reset and a healthy baseline verified. A full uninterrupted multi-hour run remains to be done.

**AI investigator with a real model (21 October 2026).** Beyond the fake-provider tests, `gemini-3.5-flash-lite` was asked about a real network delay on catalog-b in the Docker lab. Both answers compared the two gateways, ruled out CPU from the resources check, identified the netem queue from the network check and recommended *undelay on catalog-b*, citing 8 and 6 real observations, in about 2 s each. A test on synthetic evidence showed the model sometimes groups citations as `[id1, id2]`, which the parser had dropped; it now reads every id in a bracket. `gemini-2.5-flash-lite`, the original default, is closed to new keys, so the default is now `gemini-3.5-flash-lite`. This is a handful of answers, not an evaluation: answers can still be wrong, which is why every claim is cited.

## Reproduce browser reviews

Use a development Python environment with `requirements.lock`, Playwright 1.64.0 and its Chromium headless shell:

```bash
python -m pip install playwright==1.64.0
python -m playwright install chromium --only-shell
python tests/browser_review.py
python tests/browser_workflow.py
```

Run these commands sequentially; both require an unused local port 8787. Each runner refuses to reuse another server, creates a temporary state database, starts its own controller and cleans up its owned processes. Reports and screenshots are under ignored `state/ui-review/` and `state/ui-workflow/`.

The workflow harness is only in `tests/browser_harness.py`. It replaces broker operations with control of its own child processes. Service observations still travel over real HTTP. Its collector runs every 250 ms to keep the review short, and a test-only endpoint expires leases without waiting fifteen minutes. These accelerated timings must not be reported as production detection or recovery performance. The production image does not copy the test harness.

The workflow checks:

- Healthy baseline admission and a known catalogue interruption.
- Gateway liveness with failed business requests and an unavailable catalogue.
- Evidence staying in place while open during polling.
- A saved diagnosis with a selected citation.
- Optional explanations and actionable guidance, including a cited service check that has left the recent polling window.
- No verified recovery from starting the unaffected gateway.
- Verified recovery after starting the catalogue, followed by separately verified reset.
- Challenge target hidden in the ordinary UI.
- Expiry continuation and automatic reset after closing Chromium.

## Remaining deployment gates

1. Screen-reader review and Safari/WebKit coverage.
2. A full uninterrupted multi-hour soak (so far 118 rounds over about 91 minutes in two runs), and a systematic evaluation of real AI investigator answers across every fault.

Manual investigation remains fully usable without a model key.
