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
| Docker walkthrough | Production Compose deployment and `scripts/validate_f1.py --run` | Full controller/broker/container integration, repeatable measured recovery | Physical hardware repair experience |

## Latest evidence

20 October 2026: **80 backend tests** and **21 pure UI logic tests** pass. Both browser reviews pass. Desktop/mobile archive screenshots and the active expiry prompt have been visually inspected.

The production Docker walkthrough now passes all three rounds. Detection took 5.59–5.67 seconds; verified recovery took 7.36–7.41 seconds. Effective fleet cgroup/privilege checks pass via `scripts/verify_runtime.py`. The real Docker browser challenge/restart/recovery walkthrough also passes via `scripts/validate_browser.py --run`, with no JavaScript page errors. Reports and screenshots are retained under ignored `state/validation/`.

A controller restart during an active investigation is validated.

Two further production gates passed on 20 October 2026 via `scripts/validate_gates.py`. **Restart during an in-flight mutation:** the controller was killed 0.15 s after a recovery start was sent; after restart the action was recorded as unknown with the reconciliation note, Docker logged exactly one start (the broker completed it independently), nothing was replayed, and recovery was credited only after twelve fresh healthy observations. **Unaccelerated lease:** with no browser attached, the fifteen-minute timer expired, the sixty-second grace elapsed and the verified automatic reset ended the investigation 69 s after the deadline, citing twelve baseline observations, with both nodes healthy. Over that 16-minute run the controller used 45–49 MiB and at most 2% CPU, the broker about 16 MiB, the gateway about 19 MiB; host available memory stayed between 2.8 and 3.3 GiB, swap use was flat at about 1.4 GiB and the database grew from 3.0 to 4.1 MiB. Storage-pressure exhaustion and multi-hour runs remain untested. Do not describe the entire roadmap as complete.

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

1. Exercise the full fifteen-minute production lease and interruption during a broker mutation. The completed production restart check occurred during investigation.
2. Measure sustained memory, CPU, disk/WAL growth, and detection/recovery latency beyond the three smoke-test rounds.
3. Broaden accessibility and browser coverage.

Add the third node, additional fault mechanisms and the model-backed investigator after the first production scenario passes these gates. Manual investigation remains usable without a model key.
