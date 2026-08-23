# AssetTrack Portfolio Performance

AssetTrack records a user's complete investable portfolio and compares its cash-flow-adjusted performance with market benchmarks.

## Language

**Performance Tracking**:
An opt-in history that starts from a declared valuation baseline and remains comparable only while every external cash flow is recorded.
_Avoid_: Profit tracking, P&L tracking

**Tracking Baseline**:
The complete portfolio value and benchmark closing prices captured when Performance Tracking becomes measurable.
_Avoid_: Starting balance, cost basis

**Tracking Gap**:
A period before or between enabled tracking intervals for which AssetTrack cannot make a continuous performance claim.
_Avoid_: Missing data, backfill

**External Cash Flow**:
Money entering or leaving the tracked portfolio from outside; it changes investable capital and must be mirrored into every benchmark comparison.
_Avoid_: Trade, position change

**Position Reallocation**:
A buy, sale, or transfer between cash and securities inside the tracked portfolio; it changes composition but not total tracked capital.
_Avoid_: Deposit, withdrawal

**Valuation Snapshot**:
The complete portfolio value at a point in time, including every security position and cash balance.
_Avoid_: Position snapshot

**Shadow Benchmark Portfolio**:
A hypothetical benchmark holding initialized from the Tracking Baseline and adjusted by the same External Cash Flows as the user's portfolio.
_Avoid_: Benchmark return

**Performance Gap**:
The percentage by which the current portfolio value is above or below a Shadow Benchmark Portfolio.
_Avoid_: Alpha

## Recommendation Experiments

These terms describe the QuantTrade policy lab spun out of AssetTrack on 2026-08-06. They stay here so shared vocabulary remains aligned; this package does not implement the experiment engine.

**Recommendation Policy**:
An immutable set of rules, features, parameters, and outcome definitions that emits investment conclusions from point-in-time evidence.
_Avoid_: Backtest model, current logic

**Policy Version**:
A uniquely identified Recommendation Policy that cannot change after it has emitted a conclusion.
_Avoid_: Updated settings, latest model

**Forecast Record**:
An immutable, time-stamped conclusion actually emitted by a Policy Version for one Outcome Target and horizon.
_Avoid_: Recomputed signal, historical guess

**Entry Session**:
The market session whose adjusted close is frozen as the Forecast Record's outcome starting point; it is recorded explicitly rather than inferred later from the emission timestamp.
_Avoid_: Signal date, assumed close

**Outcome Target**:
The security, index, portfolio, or strategy result that a Forecast Record claims to predict.
_Avoid_: Market direction, actual trend

**Matured Outcome**:
The settled real-world result of a Forecast Record after its full horizon has elapsed.
_Avoid_: Current divergence, early miss

**Settlement Grace**:
A fixed number of market sessions during which a matured Forecast Record waits for delayed local truth before missing data becomes a VOID outcome.
_Avoid_: Retry until success, moving horizon

**Champion**:
The Policy Version currently authorized to emit formal investment conclusions.
_Avoid_: Best model, production winner

**Policy Assignment**:
An immutable, per-user state transition that activates, retires, or rolls back the Champion for one Policy Family; current state is resolved from the latest transition rather than by updating history.
_Avoid_: Mutable active flag, global user setting

**Policy Event**:
The immutable audit event written atomically with a Policy Assignment, recording its previous and next Policy Version, actor, reason, and occurrence time.
_Avoid_: Application log, editable status

**Experiment Cycle Lease**:
A short-lived, per-user SQLite lock that serializes one named settle/capture/evaluate cycle; expired leases may be replaced, but a stale owner can never release the replacement.
_Avoid_: Permanent job status, model lock

**Challenger**:
A Policy Version evaluated alongside the Champion without affecting formal investment conclusions.
_Avoid_: Automatic replacement, experimental setting

**Shadow Run**:
A forward-only comparison in which a Challenger records conclusions and outcomes without controlling formal recommendations.
_Avoid_: Backfill, simulation

**Promotion Proposal**:
An evidence-backed request for a Challenger to replace the Champion, requiring explicit review.
_Avoid_: Automatic calibration, self-modification

**Promotion Gate**:
A versioned set of practical-improvement, paired Replay, negative-control, live Shadow, coverage, risk, slice, and data-quality guardrails that must all pass before a Promotion Proposal may exist; it never applies the proposal.
_Avoid_: Accuracy threshold, automatic promotion

**Minimum Improvement Profile**:
The immutable practical-value floor used by a Promotion Gate for one primary metric; the active Scheme B profile requires `0.0200` Brier skill improvement for probability policies and `0.0020` benchmark-adjusted signed-return improvement for direction-only policies.
_Avoid_: Family tuning knob, confidence level, transaction fee

**Rollback Proposal**:
An immutable request to restore a Policy Version that was previously active for the same user and family; it requires a separate immutable approve/reject/request-more-data decision before the Registry can append a rollback event.
_Avoid_: Direct registry rollback, deleting a bad version

**Model Health**:
The current evidence-based operating state of a Policy Version, distinct from whether it is the Champion.
_Avoid_: Accuracy, confidence

**Degraded**:
A Model Health state in which recent matured evidence has crossed a predeclared limitation boundary; under Warning Mode the direction remains visible with prominent evidence limits, and the state never authorizes a Challenger.
_Avoid_: Broken model, automatic replacement, automatic abstain

**Warning Mode**:
The operating policy for a Degraded model that preserves an otherwise eligible direction while requiring the recent failure evidence and a not-for-standalone-decision limitation to appear with it.
_Avoid_: Healthy output, silent continuation

**Candidate**:
A new immutable Policy Version proposed from a Failure Diagnostic under an approved parameter whitelist and mutation budget, requiring separate Replay and Shadow evidence.
_Avoid_: Automatic fix, edited Champion

**Sector Predictive Forecast**:
One accepted probability claim for a specific sector-group member and one future market-session horizon; +1, +2, and +3 sessions are separate Forecast Records and are never collapsed into Sector Flow.
_Avoid_: Sector conclusion, combined short-term outlook

**Trained Model Snapshot**:
The canonical serialized probability table whose content hash is frozen into a Sector Predictive Policy Version, so a later daily rebuild creates a new version rather than changing older forecasts.
_Avoid_: Latest cache, current model

**Evidence Pack**:
A reproducible evaluation result for exactly one Policy Version and mode, identified by the evaluation protocol and immutable Forecast/Outcome data hash; it contains direction, probability, economic, coverage, and fixed-slice metrics.
_Avoid_: Accuracy report, latest backtest

**Failure Diagnostic**:
A read-only classification derived from an Evidence Pack that separates data quality, insufficient power, negative edge, and probability miscalibration before any Candidate is allowed to change.
_Avoid_: Automatic fix, failed model

**Evaluation Ledger**:
An append-only, per-user history of reproducible Evidence Packs and versioned Failure Diagnostics; a new run is checkpointed only when its matured Outcome count advances, repeated writes are idempotent, and conflicting rewrites are rejected.
_Avoid_: Current metrics cache, editable report

**Clustered Edge Validation**:
A versioned inference result that first collapses same-session cross-sectional observations, then applies horizon-length moving-block bootstrap and block sign-flip null controls; it may support an edge claim but never authorizes Promotion by itself.
_Avoid_: Raw-sample confidence interval, promotion decision

**Point-in-Time Replay Dataset**:
A canonical immutable set of Replay cases whose evidence, eligible target universe, label interval, and outcome were all frozen as they were knowable at the signal session.
_Avoid_: Current universe replay, mutable historical frame

**Purged Walk-Forward Fold**:
One expanding training and later validation split that removes fixed purge sessions, an embargo gap, and every training label whose interval crosses into validation.
_Avoid_: Random train/test split, ordinary time-series fold

**Replay Comparison Report**:
A deterministic paired Champion–Challenger result over eligible validation cases, including clustered improvement, horizon-adjusted significance, and time-shift, random-policy, and universe-permutation negative controls.
_Avoid_: Legacy aggregate backtest, best-horizon report

**Forecast Correction**:
An append-only annotation or pre-settlement data invalidation attached to a Forecast Record; it may document truth/data-quality facts but can never rewrite the original target, direction, probability, or horizon.
_Avoid_: Editing a forecast, correcting a wrong prediction
