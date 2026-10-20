# AegisRack: project vision

Status: proposed design for review, 20 October 2026. Documentation only; nothing described here is implemented or validated. Working title may change. Review this set with `/grill-with-docs` before implementation.

## Problem and user

A service can be alive while its dependency is broken. An operator needs to establish what failed, which other nodes are affected, what evidence supports a diagnosis, and whether a recovery actually worked. AegisRack is a small, local virtual fleet that makes that investigation visible and repeatable.

The primary user is a single learner/operator investigating synthetic incidents. A secondary audience is an interviewer watching a short demonstration and inspecting reproducible evidence. This is an independent personal project: use new code, synthetic records and project-owned infrastructure only, with no employer assets.

## Product promise

Final design-review emphasis: UI/UX quality is a core deliverable. Use a polished muted canvas with colourful, clearly labelled buttons and options, discoverable diagnostic actions and immediate feedback. The interface should invite exploration while keeping primary actions, current selection, progress and results easy to understand. Colour and animation support meaning rather than replace labels or reveal hidden answers.

Review decision, 20 October 2026: prioritise Data Center Technician learning and demonstration when tradeoffs arise, while retaining a substantial Forward Deployed Engineer component. AegisRack remains a separate project from Server Emergency Room. Investigations must involve meaningful operator interaction and useful diagnostic or recovery actions, rather than merely displaying an automatically supplied answer. The exact interaction model remains under review.

Run real Linux container processes with real HTTP dependencies, introduce bounded faults, collect observations, and record a human-led diagnosis and recovery. Show topology, health freshness, incidents, evidence and outcomes. After this works deterministically, add an optional assistant that reduces the work of assembling an evidence-backed incident report.

The diagnostic platform works without an LLM, an API key or internet access after dependencies/images are available. The finished portfolio nevertheless includes a useful AI assistant, as agreed in review; offline operation remains a supported mode. The assistant cannot recover services. Its role is to choose approved diagnostic queries, compare evidence, state uncertainty and draft a report for the operator. The operator can explore diagnostic actions freely rather than follow a prescribed sequence; the precise action boundary remains under review.

## Internship relevance and limits

| Audience | Demonstrable work | What it does not establish |
| --- | --- | --- |
| Data Center Technician Intern | Linux process/service inspection, HTTP and network troubleshooting, filesystem exhaustion, dependency fault isolation, comparison across nodes, repeatable recovery and verification | Physical server repair, component replacement, cabling, firmware diagnosis, genuine SMART/ECC/sensor analysis, production data-centre experience |
| Forward Deployed Engineer Intern | Integration of services and tools, persisted workflows, evidence provenance, failure handling, operator-oriented interface, bounded AI investigation and measured usefulness | Production scale, customer deployment experience, autonomous incident response, guaranteed root-cause inference |

These are portfolio mappings to the user's stated role interests, not a claim about verified current Google job requirements or hiring outcomes. Container nodes share the VM kernel; they are not independent physical machines or full operating-system VMs.

## Smallest convincing milestone

Review update: the finished interface offers free exploration with optional next-step hints and a solution walkthrough. Practice mode lets the operator select a fault; challenge mode hides the selected fault until reveal/end. Both use the same real fault mechanisms, and neither exposes fault ground truth to the AI. The AI can choose read-only checks autonomously within limits and can be interrupted or redirected. Prefer a lightweight model if measured diagnostic quality is sufficient. Detailed mode design is delegated to the implementer, within these boundaries.

Two fleet nodes: `gateway-a` serves a synthetic order-summary endpoint and calls `catalog-b`, which serves a tiny synthetic catalogue. The control plane probes both. Stop `catalog-b` through an allow-listed operation. Observe that `gateway-a` remains live while its readiness/business request fails; capture the dependency error and independently confirm `catalog-b` is stopped/unreachable. Record a diagnosis supported by those evidence IDs. Start `catalog-b`; require repeated successful dependency and end-to-end checks before marking recovered.

The fault action is audit evidence, not diagnostic proof. An operator must reach a supported diagnosis with the action ledger hidden. This milestone needs only a thin API/CLI workflow and persisted evidence, not a polished UI or AI.

## Proposed 2–3 minute demo

| Time | Operator action and visible result |
| --- | --- |
| 0:00–0:25 | Show three-node topology: gateways A and C depend on catalogue B. Explain that these are containers. Display fresh healthy probes and one successful synthetic business request. |
| 0:25–0:45 | In a separate control panel, stop B. Hide the action ledger from the investigation panel. B becomes unavailable; A and C become degraded while their liveness remains healthy. Edges display measured failed calls. |
| 0:45–1:20 | Compare timestamps, dependency errors, B's process/container state and A/C liveness. Attach evidence to the hypothesis that a shared dependency outage explains both symptoms. Distinguish that conclusion from the unknown reason B stopped. |
| 1:20–1:50 | Optional, only once implemented: assistant queries the same evidence and drafts an explanation with citations. Show an uncertainty statement and tool history. Without AI, show the operator's evidence-backed report instead. |
| 1:50–2:20 | Operator requests B's recovery. Show the pending verification state, then three consecutive successful probes and business requests. A successful start action alone never turns the incident green. |
| 2:20–2:45 | Show persisted incident timeline, before/after evidence and verified recovery. Briefly show reset status and the project's limitations. |

Real: service processes, request failures, dependency calls, timestamps, logs, bounded filesystem exhaustion, and recovery measurements. Synthetic: catalogue/order content and scenario selection. Network latency is deliberately injected into real traffic. A full tmpfs is a real full filesystem but does not emulate physical disk failure or physical disk throughput. No hardware telemetry is planned initially. Any future hardware illustration must carry `SIMULATED` on every view and export and be excluded from measured root-cause claims.

## Completion standard

### UI reference supplied by the user

[ArtCraft apps](https://getartcraft.com/apps) is a candidate UI reference. Its page content was reviewed on 20 October 2026; rendered visual appearance and interactions could not be inspected because no browser was available. Revisit visually during UI design before making specific claims about its palette, layout or animation.

Potential adaptations from its structured app catalogue: clearly named scenario cards with short purpose descriptions, explicit availability/status labels and obvious entry actions. Adapt these to scenario selection; retain the topology-centred workspace for active investigation. The agreed muted background, colourful actionable controls and progressive help remain AegisRack's own requirements. Use the reference as inspiration, not a requirement to reproduce its branding or page structure.

Assistance is opt-in: begin with unaided exploration, then offer evidence highlights with brief explanations, then hints, then one suggested solution action at a time. These are levels of support, not a prescribed order of diagnostic checks. Multiple investigation paths can be valid. Record help usage; explicitly revealing the injected fault ends the blinded attempt. Lab reset separately ends the attempt, restores and verifies baseline, and preserves evidence and unsuccessful recovery attempts. It does not count as a successful diagnosis or targeted recovery.

A clean checkout can eventually reproduce baseline, fault, investigation, recovery and reset using documented commands. A reviewer can follow a claim back to evidence, reproduce at least three fault classes and see honest handling of missing observations. The system survives a controller restart without pretending an interrupted action succeeded. AI must add measurable investigation/reporting value before the finished portfolio is considered complete; it can be deferred beyond the initial deterministic milestones. Broader OS and hardware exercises are desirable later additions where feasible, not prerequisites for the container-based core.

See [architecture](architecture.md), [roadmap](roadmap.md), [fault catalogue](fault-catalogue.md) and [review questions](open-questions.md).
