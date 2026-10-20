# Proposed fault catalogue

Status: F1, F3 and F4 are implemented and validated on real Docker (see [validation](VALIDATION.md)); F2 remains a design specification. No fault is enabled before its isolation and reset checks pass.

## Implementation notes (20 October 2026)

- **F1 stopped service** is implemented as specified, on either service. Starting or stopping a container already in that state now reports *unchanged* (Docker's own 304 answer) instead of an unconfirmed result.
- **F4 CPU starvation (new)** was first designed as the broker lowering the target's CPU limit. Measured on the lab, even Docker's minimum limit (0.01 CPU) did not slow these light requests, so it would have taught nothing. The implemented fault starts a bounded runaway background job (three threads, self-stopping after 25 minutes) inside the target's own process, still capped by its 0.25-CPU limit. Measured effect on the catalogue: median latency 390–600 ms against a 5–15 ms baseline, gateway liveness unchanged at ~8 ms. Evidence: slow probes (above 300 ms), Docker stats showing CPU at the container limit with kernel throttling, and a log entry recording the job start. Remedy: stop the job. The broker gained only a read-only `resources` operation; the earlier throttle/restore idea was dropped, so no new mutation privilege was added.
- **F3 disk full** uses each service's existing 8 MiB `/tmp` tmpfs rather than a new 32 MiB data mount. Every business request writes a new small receipt file (rotating at 40), so a full filesystem fails real writes with `ENOSPC`, surfaced as HTTP 507; reads and liveness keep working. The fill routine refuses any scratch space larger than 64 MiB and never writes more than 16 MiB. Remedy: delete only the known filler file. Restarting the container also empties the tmpfs.
- Injection and remedy for F3/F4 go through token-protected maintenance endpoints on the internal fleet network; the token is separate from the broker credential.
- Reset starts every service, then reverses a non-stop fault on its target. Challenge picks the fault type as well as the target, and hides both.

## Common scenario contract

Review decision: the initial catalogue prioritises polish and repeatability, not a permanently fixed scope. Add scenarios only after evaluating their distinct learning value, evidence quality, isolation and reset reliability. Represent diagnosis, verified recovery and assistance as separate outcomes; a successful reset or a lucky recovery is not proof of understanding.

Require a healthy five-cycle baseline, sufficient resource headroom, expected project/container identities and one active scenario maximum. Record scenario intent in a restricted action ledger; the diagnostic evidence store contains observations without injected fault labels. Injection parameters are fixed enums/bounded values, not commands. The initial lease is 15 minutes with a visible timer, resource-checked extension button and continuation prompt at expiry; at expiry offer Continue for 15 minutes or End and reset, with a visible 60-second grace period before automatic reset if unanswered. Browser closure does not stop deadline enforcement, and cleanup must be verified. Broker records partial failures and reconciles them on restart. Focus on deep, polished scenarios rather than simultaneous injected faults.

Recovery means changing the faulted condition. Reset additionally removes scenario artefacts and returns the synthetic fixture/topology to baseline. Verification means three consecutive successful affected service/business checks after recovery; it is never inferred from a successful control API response. After failed reset, block another scenario and retain the evidence. If the broker is unavailable, the future documented manual reset must operate only on the named project and re-run baseline checks. Never clean up unrelated containers or global networks.

## F1 — Shared dependency process unavailable (first milestone)

- **Target/injection:** B (`catalog-b`), or A for a later local-failure comparison. Broker performs a bounded Docker stop on an exact allow-listed container. Configure fleet restart policy so it does not immediately undo this intentional stop. This tests service unavailability, not crash mechanics or boot/systemd repair.
- **Expected effect:** stopping B causes A (and later C) dependency/business failures while their own liveness succeeds. Direct B probes fail; container state records stopped. Distinguish connection refusal/timeout as actually observed, without demanding a fabricated errno.
- **Diagnosis:** B unavailability explains shared downstream impact. Container stopped plus dependency errors supports this; it does not establish an unexpected underlying crash cause. Stopping A instead should leave B and C healthy.
- **Recovery:** start the same expected container after identity validation; inspect failures explicitly.
- **Reset:** start every expected fleet node, confirm fixture presence, clear active lease and verify baseline. Do not erase incident history. A tmpfs fixture may need deterministic reseeding on B startup.
- **Acceptance:** three repeats, evidence appears within 10 seconds, unaffected liveness remains healthy, and verification succeeds within 15 seconds of B readiness.

## F2 — Dependency response delay through isolated network impairment

- **Target/injection:** B's dedicated network namespace. A short-lived helper with only required `NET_ADMIN` attaches to that exact container namespace and applies a fixed 1,500 ms egress delay to its validated interface using netem. A/B HTTP timeout is 1,000 ms. No host interface, default VM route or host namespace may be accepted. No packet-loss randomness initially.
- **Prerequisites:** verify kernel/netem availability and actual namespace identity; helper has no Docker socket or host mounts. If rootless networking or kernel limitations prevent this, defer this scenario. An application sleep may be a separately labelled alternative, never represented as network impairment.
- **Expected effect:** real traffic to/from B experiences delayed responses; A/C dependency timeouts rise, B process remains running, container CPU need not rise, and direct B probes can time out too. A/C liveness remains normal. A bounded diagnostic probe with a longer 3-second timeout may observe B responding slowly; keep this distinct from the normal collector timeout.
- **Diagnosis:** reachable-but-slow dependency path is supported; observations alone may not isolate network delay versus application delay. Report that ambiguity unless independent transport measurements resolve it. Do not expose the injected qdisc configuration as the AI's answer key.
- **Recovery:** fixed removal of the scenario-owned qdisc from the same namespace; do not delete pre-existing unrelated qdiscs. Refuse injection when an unexpected qdisc is present.
- **Reset:** verify scenario qdisc absent and baseline latency restored. If target recreation changed namespace identity, discard stale handles and reconcile the new target; never apply cleanup to a guessed interface.
- **Acceptance:** record latency before/during/after, real timeout evidence, helper lease cleanup and unchanged primary VM connectivity. A local namespace fault has real effects but does not demonstrate physical-switch repair.

## F3 — Bounded writable filesystem full

- **Target/injection:** B's dedicated 32 MiB data tmpfs, containing only regenerable synthetic fixture data and scenario files. A fixed routine creates a filler file in bounded chunks until a reserved cap or `ENOSPC`; permit enough room for initial fixture, then exhaust only this mount. Read-only root filesystem and memory cap remain enforced. Never fill a host bind mount, Docker overlay, controller database or `/`.
- **Expected effect:** B liveness and existing reads can remain healthy while a synthetic receipt-write request fails with recorded `ENOSPC`; free bytes on the scoped mount approach zero. Gateway business path must include this write for the scenario so its failure is real and observable. Compare healthy read path with failing write path to avoid diagnosing total service outage.
- **Diagnosis:** the scoped filesystem is full and writes fail. This is real filesystem-capacity troubleshooting, not physical disk failure, SMART data, storage latency or block-device I/O degradation.
- **Recovery:** remove only the known filler file, then issue a fresh bounded synthetic write/readback and verify gateway business requests.
- **Reset:** remove scenario receipts/filler from the dedicated mount, reseed the fixture if needed, verify expected mount size/free capacity and baseline. Restarting B clears tmpfs, so record that reset mechanism if used rather than claiming file deletion succeeded.
- **Acceptance:** repeat three times without OOM, show actual write error and scoped usage, demonstrate successful write/readback after reset, and show no growth beyond bounded logs/state on the VM filesystem. If memory enforcement or mount identity cannot be verified, disable this scenario.

## Deferred storage I/O extension

True block-device I/O throttling/contention is deliberately deferred: it introduces storage-driver and device-accounting assumptions and could contend with unrelated VM work. No `fio` against host devices, loopback filesystem setup or host disk-pressure workload is proposed now. F3 satisfies the storage-impairment objective with a safer, bounded real failure. A later extension needs its own resource isolation argument and measurable distinction between application delay and actual I/O latency.

## Cross-scenario evidence checklist

Capture baseline, affected node/generation, liveness/readiness/business outcomes, timestamps, relevant logs, scoped metrics, diagnosis with alternatives, human recovery action, post-recovery checks and reset outcome. Retain failures and truncation markers. The separate action ledger supports evaluator ground truth and auditing; it cannot replace diagnostic evidence. Compare F1-on-B with F1-on-A to demonstrate shared versus local effects before adding more fault types.
