# Example incident exports

Two real investigation exports from the Docker lab on 21 October 2026, saved exactly as **Export evidence** produces them (only re-indented for reading). Every measurement in them is real; nothing was edited.

They come from automated validation runs, not from a person. The notes and guidance requests were made by the validation scripts, and the AI answers are real `gemini-3.5-flash-lite` output from `scripts/evaluate_ai.py`. Both are challenge rounds: the fault was hidden until the investigation ended, then revealed in `session.scenario` and `session.debrief`.

## `challenge-network-delay-with-ai.json`

A hidden 600 ms network delay on catalog-b, the catalogue both gateways depend on. Detected after 7.3 s; recovery verified 33.3 s after the start.

- **AI answers** in `session.help`, three of them, each listing the observation ids it cited.
  - After business checks only, it held back: `Action: none yet`.
  - After every inspection on every service, it named the fault: `Action: undelay on catalog-b`.
  - After recovery was verified, it correctly recommended nothing more: `Action: none yet`.
- **Recovery** in `session.recovery_attempts`: one action, *undelay* on catalog-b. That was the model's own recommendation, applied by the script.
- **Evidence:** 120 observations. Look for the `network` records: catalog-b's shows a `netem` queue with a 600 ms delay. Compare the `business` durations of both gateways (about 610 ms) with their fast `liveness`.

## `challenge-config-drift-local.json`

A hidden configuration drift on gateway-c, a local fault: its upstream setting pointed at a port nothing listens on. Detected after 5.4 s; recovery verified 33.7 s after the start.

- **Wrong remedies, honestly recorded.** *restore* on gateway-a and *start* on gateway-c both came back `unchanged` and were not credited. Then *restore* on gateway-c succeeded. The validator deliberately tries wrong remedies first.
- **Diagnosis note and guidance:** `session.notes` and `session.help` show what the evidence supported and the guidance that cited the drifted `config` record.
- **Evidence:** 136 observations. gateway-c's business requests fail, while gateway-a and catalog-b stay healthy, which is what makes it local.

## Reading an export

| Field | What it holds |
| --- | --- |
| `session` | Timeline, mode, outcome, recovery attempts, notes, help requests and, once ended, the revealed scenario and debrief |
| `report` | The incident report's measures: time to detect, first check, checks by kind, help used, ineffective attempts |
| `evidence` | The observations themselves, cited ones first, each with node, kind, time and the raw result |
| `evidence_summary`, `omitted_evidence_count`, `missing_citations` | What was kept, what was left out by the 8 MiB bound, and citations that had already expired (none here) |
