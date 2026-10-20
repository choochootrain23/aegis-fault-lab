# Roadmap and acceptance gates

Status: proposed, not implemented. Work proceeds over multiple sessions with no one-day deadline. Review [open questions](open-questions.md) before implementation. Each stage leaves a reproducible result and a short record of commands, observations and limitations.

## 0. Design review and runtime preflight

Confirm shared understanding from the design review. Recheck environment from the intended VM terminal: Docker daemon accessibility/version, running workloads, cgroup CPU/memory/PID enforcement, networking and available RAM. Confirm project name, resources and isolation checks cannot select the sibling project. No global daemon reconfiguration is assumed.

Acceptance: reviewed architecture, working runtime access, known resource limits and a plan to verify them. Blocked access remains an explicit blocker. No need to enable nested virtualisation.

## 1. Two-node vertical slice

Build A → B, collector, minimal broker and SQLite evidence/action state. Use a thin command/API workflow first. Include explicit start, stop, baseline and project reset instructions. Establish simple liveness, dependency-aware readiness and real synthetic business requests. Keep AI and visual polish out of this gate.

Acceptance experiment:

1. Observe five successful cycles and a business response; record baseline and container identities.
2. Stop only B through the fixed action. Within 10 seconds, observe three failed dependency checks on A, A liveness still succeeding, and B stopped/unreachable. Capture timestamps and error records, not just coloured status.
3. With the action ledger hidden, write a diagnosis linking at least two independent observations. Explain why A itself being down is contradicted; leave the reason B stopped unknown unless separate diagnostic evidence establishes it.
4. Start B. Mark `verifying` until three consecutive checks pass on B and A's business path. Target recovery confirmation within 15 seconds of B becoming ready; record actual times.
5. Reset and repeat three times. Assert unrelated container identities/states were not targeted. Verify fault/recovery evidence survives controller restart.

This is the smallest proof: two real nodes, one real fault, measured downstream effect, defensible diagnosis and verified recovery. Failing thresholds lead to investigation or threshold review, not fabricated results.

## 2. Polish one interactive scenario

Accepted visual direction: muted background, colourful interactions and actionable topology/node panels. Use labels/icons as well as colour, finite gentle attention cues for requested evidence assistance, and reduced-motion support. Keep the AI panel collapsible and unaided exploration free from solution hints. Diagnosis recording is optional and does not gate recovery; post-challenge debrief supports comparison rather than AI-only scoring.

Build a compact topology view with fresh/unknown status, measured dependency edges, incident timeline, evidence drawer, action history and verification state. Make injection and recovery explicit local operator controls. Display configured topology separately from observed traffic. Show action ground truth in a separate panel accessible in practice or after challenge reveal/end; never expose it to the AI.

Acceptance: a reviewer can complete the demo without a terminal, inspect each claim's evidence, distinguish stale telemetry from service failure and follow recovery attempts to verification. Keyboard operation and clear labels accompany colour. Refreshing the browser restores the persisted incident. No browser route accepts shell commands.

Complete practice/challenge modes, optional progressive guidance, recovery/reset distinction and timer controls for F1 before adding other fault families. A second gateway can then deepen shared-dependency investigation during stage 3.

UI/UX acceptance: visually review the full investigation flow, including colourful labelled action buttons/options, selected/hover/focus/disabled states, loading feedback, empty/error/stale states and recovery results. Keep a consistent colour vocabulary and readable contrast on the muted background. Make important actions discoverable without excessive animation; respect reduced motion and keyboard navigation. Verify that an operator can select a node, run a check, inspect evidence, request help and attempt recovery without searching through unrelated controls. Polish this experience before expanding the catalogue.

## 3. Broader diagnostics and bounded faults

Review decision: polish the initial three fault families before expanding the catalogue. Evaluate new faults throughout development for distinct learning value, real observable effects, resource fit and reliable reset. Keep DC learning primary and preserve meaningful FDE integration/AI work. Challenge outcomes separately record diagnosis, verified recovery and assistance; no initial numerical score or AI-only correctness judgment.

Add C → B to demonstrate shared-dependency symptoms versus an A-only fault. Add storage-full and network-delay scenarios after their isolation prerequisites pass. Add container CPU/memory trends, bounded logs and error-rate/latency comparisons; defer broad host exporters.

Acceptance: each enabled catalogue scenario has a healthy baseline, expected discriminating evidence, timed reset, repeated recovery checks and a three-run repeatability record. Distinguish stopped service, dependency latency and full filesystem using observations with the action labels hidden. Unknown/timeout states must not be silently classified. Fault lease expiry, invalid targets and duplicate requests are exercised. Test one fault at a time.

## 4. Reliability gate before AI

Persistence and timeouts began in stage 1; this stage deepens them. Add restart reconciliation, cancellation, retention enforcement, tool error records, idempotency and explicit degraded-storage behaviour.

Acceptance: restart controller during collection and recovery; record ambiguous operations and reconcile without automatic duplicate mutation. Exercise broker outage, missing target, disk-write failure in an isolated test resource, stale samples, truncated logs and interrupted resets. Confirm caps and retention over an accelerated 24-hour-equivalent workload and a 30-minute ordinary run. Record actual RAM, disk growth, probe latency and swap activity. Disabling persistence must never yield a false recovered status. No host disk-filling test.

## 5. Bounded AI investigator (required for finished portfolio)

Implement the read-only contract only after deterministic scenarios and reliability gates pass. Start with the accepted Gemini free-tier direction, verify eligibility and evaluate a lightweight model, keeping a provider adapter small and all secrets outside version control. Store report revisions, tool provenance and uncertainty. Deterministic manual reporting stays available.

Acceptance: no-key mode remains fully usable; rejected action requests cannot reach broker; forged instructions in logs cannot expand privileges; missing evidence and provider/tool failure produce honest partial reports. Run at least 12 held-out incident cases spanning faults, healthy and ambiguous states, with action ledger hidden. Review citation correctness, unsupported-cause claims, diagnostic usefulness, latency and actual usage. Aim for zero fabricated evidence IDs and zero unauthorized mutations; flag unsupported claims rather than inventing an accuracy statistic. Compare manual and assisted report completion on the same evidence using counterbalanced cases; report the small sample and reviewer bias. Revise the assistant if usefulness is not demonstrated before declaring the finished portfolio complete.

Review update: AI may be postponed while the deterministic milestones are built, but cannot be omitted from the finished portfolio. If usefulness is not demonstrated, revise the assistant before declaring portfolio completion. Its runtime use remains optional and no-key operation must work.

## 6. Portfolio packaging

Write clean-checkout setup/run/reset instructions, architecture narrative, representative incident exports and a 2–3 minute recording following [vision](vision.md). Verify that a new synthetic run works using documented steps and pinned dependencies. Describe container/physical-hardware limits explicitly. Keep an interview walkthrough explaining one fault, privilege tradeoff, restart failure and AI uncertainty.

Acceptance: reproducible demonstration, reviewable evidence and honest measured resource results; no employer material or secrets. Stop services after the demo.

## Deferred scope

Fourth node, concurrent faults, full VMs/systemd lab, true block-device I/O impairment, hardware-sensor simulation, elaborate dashboards, local models, multi-user auth/hosting, production deployment and advanced networking. Add a deferred item only when a specific learning goal or observed limitation justifies its cost. Synthetic hardware illustration is optional and may add less value than one well-explained real incident.
