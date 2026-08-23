---
status: accepted
---

# Use one minimum-improvement floor per Promotion metric

On 2026-08-02, AssetTrack chose Scheme B for D-06. Every probability-policy
Promotion must exceed `0.0200` Brier skill improvement, and every direction-only
Promotion must exceed `0.0020` benchmark-adjusted signed-return improvement.
Both historical Replay and forward Shadow confidence-interval lower bounds must
clear the same floor; passing it still does not bypass negative controls, risk,
coverage, data-quality, slice, or manual-approval gates.

The accepted configuration is versioned as
`promotion-minimum-improvement-b-v1`. A review that supplies a different value
fails closed with `minimum_improvement_profile`. The previously proposed
family-specific Scheme A is retained only as a possible future recommendation:
adopting any part of it requires a new profile/protocol and fresh Replay and
Shadow evidence, never an in-place change to this decision.
