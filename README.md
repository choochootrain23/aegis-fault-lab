# AegisRack

An independent, local fleet investigation lab. Practise Linux service and dependency troubleshooting using real processes, measured evidence and human-controlled recovery. Data Center Technician learning comes first, with reliable integration, persistence and a planned read-only AI investigator supporting the FDE story.

**Current status: Docker validation milestone, not a completed portfolio.** The production Compose deployment now runs successfully. Three real Docker fault/recovery/reset rounds and effective fleet resource/privilege checks pass. Earlier tests also cover real subprocess HTTP and Chromium UI workflows. No mock measurements are substituted in the application.

## Try it in your browser

**[Open the browser simulator →](https://choochootrain23.github.io/aegis-fault-lab/)**

The preview runs the real web UI against a JavaScript simulator of the controller, broker and two services. It follows the same workflow rules (baseline admission, three-cycle verified recovery, hidden-target challenges, timer expiry), but **every service and measurement in it is generated in your browser tab**. No containers run and state resets on reload. A purple *SIMULATOR* banner stays on screen throughout. For real processes, real Docker faults and measured evidence, run the full lab below.

Try this: start a practice investigation on the shared catalogue, compare the gateway's liveness and business checks, inspect the catalogue's container state, try starting the *wrong* node first, then start the right one and watch the three verification cycles.

## Run the full lab locally

Prerequisites: Linux with Docker Engine and Compose, existing authorized daemon access, at least 2 GiB available RAM and 10 GiB free disk. Python 3.14 is used in the image and development environment.

Clone the repository, then run everything from its root:

```bash
git clone https://github.com/choochootrain23/aegis-fault-lab.git
cd aegis-fault-lab
python3 scripts/preflight.py
python3 scripts/init_env.py
docker compose build
docker compose up -d
```

Open **http://localhost:8787**. The web port binds only to loopback. The fleet and broker have no published ports. For Windows access, use an existing authenticated SSH tunnel to VM port 8787; do not expose the Docker socket or change the VM network for this app.

The shared image is built once by the controller service. The controller joins a dedicated operator bridge for localhost publishing as well as the private fleet/control networks. Only the controller joins that operator bridge; it permits controller outbound connectivity, but the application currently makes no model calls.

`init_env.py` creates a mode-0600 `.env` broker credential and refuses to overwrite an existing file. Do not paste credentials into chat or commit `.env`. The controller needs no model key in this milestone. Exact installed Python dependencies are captured in `requirements.lock`; Docker uses that lock.

If Docker permission is denied, stop and use a terminal/account with existing authorized access. Do not make the socket world-writable. Changing host permissions is not part of setup automation.

## First investigation

1. Wait for five healthy baseline cycles. The UI labels absent or stale observations **Unknown**.
2. Choose practice or challenge and start. Practice lets you choose the catalogue dependency or gateway; challenge randomly stops one of the two services and withholds the selected target.
3. Select a topology node. Run liveness, business, service-state and dependency-log checks in any order. Open evidence records and select citations. Reading or selecting pauses the evidence list so live polling cannot move your place; explicitly resume to see new observations.
4. Optionally request explanations, a hint or one suggested action. This guidance is deterministic, uses fresh observations and cites its supporting evidence. Its suggested check is optional. It cannot read the hidden fault ledger and remains separate from the future AI assistant.
5. Try starting the selected service. A control success is not recovery: three healthy fleet/business cycles must follow. Starting the wrong node is allowed and remains in the history.
6. Save an optional diagnosis, export evidence, reveal the exercise or end/reset. Reset preserves the investigation and does not count as targeted recovery. If recovery was already verified, that result is preserved separately from reset/closure. Use **Review** in history to browse older evidence, open exact note/help citations, review actions and export a specific investigation. Recovery and reset each retain their own supporting observations.

The timer starts at 15 minutes. Extend it explicitly, or respond to the expiry prompt within its 60-second grace period. The controller enforces expiry even when the browser closes. A stopped controller cannot enforce cleanup until it resumes; use the manual reset below if needed.

Challenge isolation is a learning boundary: the operator who controls Docker/the local database can discover the fault. The application hides it from normal challenge responses, exports and guidance until reveal/end. It is not a tamper-proof examination platform.

## Reset and stop

The UI resets only the two owned fleet containers and verifies their responses. If the controller is unavailable:

```bash
docker compose start catalog-b gateway-a
docker compose up -d controller broker
```

Reopen the UI, inspect the current state, and use **End & reset** to reconcile and verify the baseline. Manual Docker actions do not fabricate an application success record.

```bash
docker compose down
```

The dedicated state volume is retained. No global prune or unrelated service cleanup is used. Do not add `-v` unless you intentionally want to delete investigation history.

## Development and validation

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pytest -q
AEGIS_PROCESS_TESTS=1 .venv/bin/python -m pytest -q
```

The optional process test starts two temporary project-owned Python services on loopback, stops/restarts its catalogue process, checks real HTTP effects and cleans both processes up. It does **not** prove Docker/network isolation. Workflow tests use explicit test doubles; the shipped runtime has no fake fleet mode.

You can inspect the UI's unavailable-state behaviour without Docker:

```bash
.venv/bin/python -m uvicorn aegis.controller:app --host 127.0.0.1 --port 8787 --no-access-log
```

Nodes will correctly remain unavailable/unknown and fault injection is blocked without a healthy baseline and reachable broker. This is not a substitute for a running fleet demonstration.

Run one controller worker per state database. A local file lock rejects competing controllers; it releases automatically when the owner exits. Restart restores recorded action results without replaying mutations and requires fresh verification checks. If reset was interrupted, inspect current state and explicitly retry reset.

## Docker walkthrough (once daemon access is available)

With the project running and no active investigation:

```bash
python3 scripts/validate_f1.py --run
```

This deliberately interrupts the project catalogue, captures independent evidence, tries an unhelpful gateway recovery, verifies catalogue recovery, and resets the lab, three times. It uses the controller's bounded operator API and writes a timestamped report in ignored `state/validation/`. It refuses to interrupt an existing investigation. All three rounds passed on 20 October 2026: detection took 5.59–5.67 seconds and verified recovery took 7.36–7.41 seconds on this VM.

After ending any investigation, verify the fleet's actual limits and broker inspection contract:

```bash
.venv/bin/python scripts/verify_runtime.py
```

With Playwright/Chromium installed, `python scripts/validate_browser.py --run` exercises challenge secrecy, controller restart during investigation, fresh diagnosis and browser-driven recovery/reset against the production Docker deployment. It deliberately interrupts project services and refuses an existing active investigation.

Rebuild and check the browser simulator (it is published by `.github/workflows/pages.yml` on every push to `main`):

```bash
python3 scripts/build_preview.py            # writes the ignored site/ folder
python3 -m http.server -d site 8000         # optional local look at http://localhost:8000
python tests/preview_walkthrough.py         # Chromium walkthrough; needs Playwright
```

The build copies `web/` unchanged except for relabelling every real-measurement claim, and fails if an expected label is missing. The walkthrough validates the simulator only, never Docker or the real controller.

Optional pure UI-logic checks, with Node.js available:

```bash
node tests/view-model.mjs
```

These tests do not render a browser or establish visual quality.

Optional browser review requires Playwright (validated with version 1.64.0) and Chromium, installed separately from runtime dependencies:

```bash
python -m pip install playwright==1.64.0
python -m playwright install chromium --only-shell
python tests/browser_review.py
```

Use a development environment containing `requirements.lock`. This review refuses an occupied port 8787, starts its own controller with an isolated temporary database, and cleans up its process. Seeded archive records are explicitly labelled as test fixtures; no Docker fault is injected. Screenshots and a report are written under ignored `state/ui-review/`. It checks desktop, 390px and 320px layouts, archive access, expired citations, hidden controls, polling focus and keyboard dismissal.

Run `python tests/browser_workflow.py` separately to exercise the active browser workflow. It starts two owned service processes and a test-only control adapter, then checks real HTTP failure, diagnosis, optional guidance, wrong/correct recovery, challenge mode and expiry cleanup after browser closure. Cadence and lease timing are accelerated for testing. This is not a Docker validation or a performance benchmark. See [validation boundaries](docs/VALIDATION.md).

## Evidence and storage

Detailed records are retained for 24 hours; active-investigation evidence is protected. Ended summaries and their actions remain for 30 days after closure. Archive pages flag expired citations, and exports prioritise cited observations plus recent detail within 8 MiB, reporting omitted records. **Run retention cleanup** only removes records eligible under this policy; it does not erase active work. Freed database pages are reused without requiring a full vacuum. Cleanup also releases checkpointed journal allocation and retries when an external reader temporarily holds it. If a storage guard pauses collection, do not treat the remaining displayed observations as fresh; inspect the error and export relevant evidence.

## Current limitations and next gates

- The unaccelerated fifteen-minute expiry and a controller kill during an in-flight Docker action now pass in production (see [validation](docs/VALIDATION.md)). Storage-pressure exhaustion and multi-hour runs still need deployment validation.
- Desktop/mobile screenshots, archive interactions, connection loss/recovery and a real-process active investigation have been reviewed in headless Chromium. The real Docker browser challenge/restart/recovery walkthrough now also passes. Broader accessibility auditing and other browsers remain outstanding.
- Only service-unavailability scenarios are implemented. Network delay, filesystem exhaustion and the third node remain deferred until the first scenario is polished and validated.
- No AI calls are implemented. Gemini free tier/lightweight model is the agreed later direction; no subscription or paid billing is configured.
- Retention and archive/export behaviour have regression coverage, but sustained runtime storage-pressure recovery and detailed metric charts remain work ahead. At a hard persistence failure the application cannot safely promise automated cleanup; the documented project-only manual reset remains available.
- Containers share a kernel. Synthetic business content and deliberately injected faults are labelled; no physical repair or hardware telemetry claims are made.

See [implementation checkpoint](docs/IMPLEMENTATION_STATUS.md), [vision](docs/vision.md), [architecture](docs/architecture.md) and [roadmap](docs/roadmap.md).

Fonts in `web/fonts/` (Archivo, Inter, Geist Mono, Instrument Serif) are self-hosted under the SIL Open Font License; see `web/fonts/LICENSES.txt`.
