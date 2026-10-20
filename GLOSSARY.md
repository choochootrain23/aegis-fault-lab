# AegisRack

A local fleet troubleshooting lab for evidence-backed operator investigations.

## Language

**Diagnosis**:
An evidence-supported explanation of a failure mechanism and its affected scope, with remaining uncertainty stated explicitly. It need not establish the original cause when the available evidence cannot support that conclusion.
_Avoid_: Root cause (unless the evidence establishes it)

**Practice mode**:
An investigation mode in which the operator selects a known fault and explores its effects, with optional guidance.

**Challenge mode**:
An investigation mode in which the selected fault is hidden from the operator until the challenge is ended or revealed. The investigator must reason from observed effects.

**Diagnostic check**:
A bounded, read-only examination of a fleet node or dependency that returns observations relevant to an investigation.
_Avoid_: Recovery action (for a read-only check)

**Recovery attempt**:
An operator action intended to restore an affected service or dependency, whose success must be checked against observed outcomes.
_Avoid_: Reset

**Lab reset**:
An operation that ends the current investigation attempt and restores a verified baseline while preserving its history. It does not establish that the fault was diagnosed or resolved through targeted recovery.
_Avoid_: Recovery attempt
