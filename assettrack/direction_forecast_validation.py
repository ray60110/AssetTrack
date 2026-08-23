"""Family-blind direction-forecast scoring.

Callers emit Forecast Records.  This module settles them against a price
panel, purges overlapping label intervals, and returns PASS / FAIL /
UNDERPOWERED.  It does not fetch prices, does not know about options or
sectors, and does not promote a Policy Version.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal, Mapping, Optional, Sequence
import hashlib
import json

import numpy as np

from .market_sessions import NYSESessionCalendar


Direction = Literal["up", "down"]
ScoringMode = Literal["probability", "direction_only"]
HeadlineVerdict = Literal["PASS", "FAIL", "UNDERPOWERED"]

PROTOCOL_VERSION = "direction-forecast-validation-v1"
MINIMUM_IMPROVEMENT_PROFILE_B_V1 = "promotion-minimum-improvement-b-v1"
BRIER_SKILL_FLOOR = 0.0200
SIGNED_RETURN_FLOOR = 0.0020

PricePanel = Mapping[tuple[str, date], float]

_CALENDAR = NYSESessionCalendar()


class ValidationInputError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ForecastRecord:
    policy_version_id: str
    outcome_target: str
    entry_session: date
    horizon_sessions: int
    direction: Direction
    probability_up: Optional[float] = None


@dataclass(frozen=True)
class ValidationSpec:
    horizons: tuple[int, ...]
    primary_horizon: int
    benchmarks: tuple[str, ...]
    cost_bps: float
    min_independent_blocks: int
    min_coverage: float
    bootstrap_samples: int
    seed: int
    confidence: float
    holdouts: tuple = ()
    minimum_improvement_profile: str = MINIMUM_IMPROVEMENT_PROFILE_B_V1

    @classmethod
    def scheme_b(
        cls,
        *,
        horizons: tuple[int, ...] = (1, 5, 10),
        primary_horizon: int = 5,
        benchmarks: tuple[str, ...] = ("QQQ", "SPY"),
        cost_bps: float = 10.0,
        min_independent_blocks: int = 30,
        min_coverage: float = 0.70,
        bootstrap_samples: int = 10_000,
        seed: int = 20260817,
        confidence: float = 0.95,
    ) -> "ValidationSpec":
        return cls(
            horizons=horizons,
            primary_horizon=primary_horizon,
            benchmarks=benchmarks,
            cost_bps=cost_bps,
            min_independent_blocks=min_independent_blocks,
            min_coverage=min_coverage,
            bootstrap_samples=bootstrap_samples,
            seed=seed,
            confidence=confidence,
        )


@dataclass(frozen=True)
class MetricInterval:
    lower: Optional[float]
    upper: Optional[float]


@dataclass(frozen=True)
class MaturedOutcome:
    forecast: ForecastRecord
    settlement_session: Optional[date]
    asset_return: Optional[float]
    benchmark_returns: Mapping[str, float]
    signed_return: Optional[float]
    excess_signed_return: Optional[float]
    cost_adjusted_excess: Optional[float]
    hit: Optional[bool]
    void_reason: Optional[str]


@dataclass(frozen=True)
class SegmentReport:
    name: str
    scoring_mode: ScoringMode
    claim_count: int
    matured_count: int
    void_count: int
    independent_blocks: int
    coverage: Optional[float]
    hit_rate: Optional[float]
    baseline_hit_rate: Optional[float]
    brier_score: Optional[float]
    baseline_brier_score: Optional[float]
    brier_skill: Optional[float]
    brier_skill_ci: MetricInterval
    mean_cost_adjusted_excess: Optional[float]
    excess_ci: MetricInterval
    first_entry_session: Optional[date]
    last_entry_session: Optional[date]


@dataclass(frozen=True)
class GateVerdict:
    passed: bool
    profile: str
    primary_metric: str
    floor: float
    improvement: Optional[float]
    improvement_ci_lower: Optional[float]
    failures: tuple[str, ...]


@dataclass(frozen=True)
class DirectionValidationReport:
    protocol_version: str
    data_hash: str
    policy_version_id: str
    scoring_mode: ScoringMode
    verdict: HeadlineVerdict
    reason: str
    overall: SegmentReport
    gate: GateVerdict
    matured: tuple[MaturedOutcome, ...]
    holdouts: tuple[SegmentReport, ...] = field(default_factory=tuple)

    def __str__(self) -> str:
        return (
            f"{self.verdict}  {self.reason}  "
            f"({self.policy_version_id} {self.overall.name})"
        )


def validate(
    forecasts: Sequence[ForecastRecord],
    prices: PricePanel,
    spec: ValidationSpec,
) -> DirectionValidationReport:
    """Score one Policy Version against exact-session Matured Outcomes."""
    _check_spec(spec)
    rows = [_check_forecast(item, spec) for item in forecasts]
    if not rows:
        raise ValidationInputError("empty_forecasts", "forecasts must not be empty")
    policy_ids = {row.policy_version_id for row in rows}
    if len(policy_ids) != 1:
        raise ValidationInputError("mixed_policy_version", "one Policy Version per call")
    policy_id = next(iter(policy_ids))
    identities = [
        (row.policy_version_id, row.outcome_target, row.entry_session, row.horizon_sessions)
        for row in rows
    ]
    if len(identities) != len(set(identities)):
        raise ValidationInputError("duplicate_forecast", "duplicate Forecast Record identity")
    scoring_mode = _scoring_mode(rows)
    if spec.minimum_improvement_profile != MINIMUM_IMPROVEMENT_PROFILE_B_V1:
        primary_metric = (
            "brier_skill" if scoring_mode == "probability" else "benchmark_adjusted_signed_return"
        )
        floor = BRIER_SKILL_FLOOR if scoring_mode == "probability" else SIGNED_RETURN_FLOOR
        overall = _empty_segment("overall", scoring_mode)
        return DirectionValidationReport(
            protocol_version=PROTOCOL_VERSION,
            data_hash=_hash_inputs(rows, prices, spec),
            policy_version_id=policy_id,
            scoring_mode=scoring_mode,
            verdict="FAIL",
            reason="unknown minimum-improvement profile",
            overall=overall,
            gate=GateVerdict(
                passed=False,
                profile=spec.minimum_improvement_profile,
                primary_metric=primary_metric,
                floor=floor,
                improvement=None,
                improvement_ci_lower=None,
                failures=("minimum_improvement_profile",),
            ),
            matured=(),
        )

    settled = [_settle(row, prices, spec) for row in rows]
    kept = _purge_overlapping(settled)
    primary = [row for row in kept if row.forecast.horizon_sessions == spec.primary_horizon]
    overall = _score_segment("overall", primary, scoring_mode, spec)
    verdict, failures, reason = _judge(overall, scoring_mode, spec)
    primary_metric = (
        "brier_skill" if scoring_mode == "probability" else "benchmark_adjusted_signed_return"
    )
    floor = BRIER_SKILL_FLOOR if scoring_mode == "probability" else SIGNED_RETURN_FLOOR
    improvement = (
        overall.brier_skill if scoring_mode == "probability" else overall.mean_cost_adjusted_excess
    )
    improvement_ci_lower = (
        overall.brier_skill_ci.lower if scoring_mode == "probability" else overall.excess_ci.lower
    )
    return DirectionValidationReport(
        protocol_version=PROTOCOL_VERSION,
        data_hash=_hash_inputs(rows, prices, spec),
        policy_version_id=policy_id,
        scoring_mode=scoring_mode,
        verdict=verdict,
        reason=reason,
        overall=overall,
        gate=GateVerdict(
            passed=verdict == "PASS",
            profile=spec.minimum_improvement_profile,
            primary_metric=primary_metric,
            floor=floor,
            improvement=improvement,
            improvement_ci_lower=improvement_ci_lower,
            failures=failures,
        ),
        matured=tuple(kept),
    )


def _check_spec(spec: ValidationSpec) -> None:
    if not spec.horizons or any(h <= 0 for h in spec.horizons):
        raise ValidationInputError("invalid_horizon", "horizons must be positive")
    if spec.primary_horizon not in spec.horizons:
        raise ValidationInputError("invalid_horizon", "primary_horizon must be in horizons")
    if not spec.benchmarks:
        raise ValidationInputError("invalid_spec", "at least one benchmark is required")
    if spec.cost_bps < 0 or not 0.0 <= spec.min_coverage <= 1.0:
        raise ValidationInputError("invalid_spec", "cost_bps or min_coverage out of range")
    if spec.min_independent_blocks < 1 or spec.bootstrap_samples < 1:
        raise ValidationInputError("invalid_spec", "power / bootstrap knobs must be positive")
    if not 0.0 < spec.confidence < 1.0:
        raise ValidationInputError("invalid_spec", "confidence must be in (0, 1)")


def _check_forecast(row: ForecastRecord, spec: ValidationSpec) -> ForecastRecord:
    if row.direction not in ("up", "down"):
        raise ValidationInputError("invalid_direction", "direction must be up or down")
    if row.horizon_sessions not in spec.horizons:
        raise ValidationInputError("invalid_horizon", "forecast horizon is not in spec.horizons")
    if not _CALENDAR.is_session(row.entry_session):
        raise ValidationInputError("invalid_entry_session", "entry_session is not an NYSE session")
    if row.probability_up is not None and not 0.0 <= row.probability_up <= 1.0:
        raise ValidationInputError("invalid_probability", "probability_up must be in [0, 1]")
    return row


def _scoring_mode(rows: Sequence[ForecastRecord]) -> ScoringMode:
    flags = {row.probability_up is None for row in rows}
    if len(flags) != 1:
        raise ValidationInputError("mixed_scoring_mode", "mix of None and set probability_up")
    return "direction_only" if True in flags else "probability"


def _price(prices: PricePanel, symbol: str, session: date) -> Optional[float]:
    value = prices.get((symbol, session))
    if value is None or value <= 0:
        return None
    return float(value)


def _settle(row: ForecastRecord, prices: PricePanel, spec: ValidationSpec) -> MaturedOutcome:
    try:
        settlement = _CALENDAR.shift(row.entry_session, row.horizon_sessions)
    except ValueError:
        settlement = None
    entry_px = _price(prices, row.outcome_target, row.entry_session)
    settle_px = _price(prices, row.outcome_target, settlement) if settlement else None
    primary = spec.benchmarks[0]
    bench_entry = _price(prices, primary, row.entry_session)
    bench_settle = _price(prices, primary, settlement) if settlement else None
    if entry_px is None:
        return _void(row, settlement, "missing_entry_price")
    if settlement is None or settle_px is None:
        return _void(row, settlement, "missing_settlement_price")
    if bench_entry is None or bench_settle is None:
        return _void(row, settlement, "missing_benchmark_price")
    asset_ret = settle_px / entry_px - 1.0
    bench_ret = bench_settle / bench_entry - 1.0
    sign = 1.0 if row.direction == "up" else -1.0
    signed = sign * asset_ret
    excess = sign * (asset_ret - bench_ret)
    cost = spec.cost_bps / 10_000.0
    hit = asset_ret > 0 if row.direction == "up" else asset_ret < 0
    benchmark_returns = {primary: bench_ret}
    for extra in spec.benchmarks[1:]:
        extra_entry = _price(prices, extra, row.entry_session)
        extra_settle = _price(prices, extra, settlement)
        if extra_entry is not None and extra_settle is not None:
            benchmark_returns[extra] = extra_settle / extra_entry - 1.0
    return MaturedOutcome(
        forecast=row,
        settlement_session=settlement,
        asset_return=asset_ret,
        benchmark_returns=benchmark_returns,
        signed_return=signed,
        excess_signed_return=excess,
        cost_adjusted_excess=excess - cost,
        hit=hit,
        void_reason=None,
    )


def _void(row: ForecastRecord, settlement: Optional[date], reason: str) -> MaturedOutcome:
    return MaturedOutcome(
        forecast=row,
        settlement_session=settlement,
        asset_return=None,
        benchmark_returns={},
        signed_return=None,
        excess_signed_return=None,
        cost_adjusted_excess=None,
        hit=None,
        void_reason=reason,
    )


def _purge_overlapping(rows: Sequence[MaturedOutcome]) -> list[MaturedOutcome]:
    grouped: dict[tuple[str, int], list[MaturedOutcome]] = {}
    for row in rows:
        key = (row.forecast.outcome_target, row.forecast.horizon_sessions)
        grouped.setdefault(key, []).append(row)
    kept: list[MaturedOutcome] = []
    for items in grouped.values():
        last_settlement: Optional[date] = None
        for row in sorted(items, key=lambda item: item.forecast.entry_session):
            if last_settlement is not None and row.forecast.entry_session <= last_settlement:
                continue
            kept.append(row)
            if row.settlement_session is not None and row.void_reason is None:
                last_settlement = row.settlement_session
    return sorted(
        kept,
        key=lambda item: (
            item.forecast.entry_session,
            item.forecast.outcome_target,
            item.forecast.horizon_sessions,
        ),
    )


def _score_segment(
    name: str,
    rows: Sequence[MaturedOutcome],
    scoring_mode: ScoringMode,
    spec: ValidationSpec,
) -> SegmentReport:
    scored = [row for row in rows if row.void_reason is None]
    voids = [row for row in rows if row.void_reason is not None]
    n = len(scored)
    coverage = (n / len(rows)) if rows else None
    hit_rate = (sum(bool(row.hit) for row in scored) / n) if n else None
    baseline_hit_rate = (
        sum((row.asset_return or 0.0) > 0 for row in scored) / n if n else None
    )
    excesses = [row.cost_adjusted_excess for row in scored if row.cost_adjusted_excess is not None]
    mean_excess = float(np.mean(excesses)) if excesses else None
    excess_ci = _bootstrap_ci(excesses, spec)
    brier = baseline_brier = skill = None
    skill_ci = MetricInterval(None, None)
    if scoring_mode == "probability" and scored:
        brier, baseline_brier, skill, skill_ci = _brier_metrics(scored, spec)
    sessions = [row.forecast.entry_session for row in scored]
    return SegmentReport(
        name=name if name != "overall" else f"+{spec.primary_horizon}",
        scoring_mode=scoring_mode,
        claim_count=len(rows),
        matured_count=n,
        void_count=len(voids),
        independent_blocks=n,
        coverage=coverage,
        hit_rate=hit_rate,
        baseline_hit_rate=baseline_hit_rate,
        brier_score=brier,
        baseline_brier_score=baseline_brier,
        brier_skill=skill,
        brier_skill_ci=skill_ci,
        mean_cost_adjusted_excess=mean_excess,
        excess_ci=excess_ci,
        first_entry_session=min(sessions) if sessions else None,
        last_entry_session=max(sessions) if sessions else None,
    )


def _brier_metrics(
    rows: Sequence[MaturedOutcome],
    spec: ValidationSpec,
) -> tuple[float, float, float, MetricInterval]:
    ordered = sorted(rows, key=lambda row: row.forecast.entry_session)
    model_terms: list[float] = []
    base_terms: list[float] = []
    known: list[int] = []
    for row in ordered:
        y = 1.0 if (row.asset_return or 0.0) > 0 else 0.0
        p = float(row.forecast.probability_up or 0.5)
        base = (sum(known) / len(known)) if known else 0.5
        model_terms.append((p - y) ** 2)
        base_terms.append((base - y) ** 2)
        known.append(int(y))
    brier = float(np.mean(model_terms))
    baseline_brier = float(np.mean(base_terms))
    skill = 1.0 - brier / baseline_brier if baseline_brier > 0 else 0.0
    per_row = [1.0 - term / max(baseline_brier, 1e-12) for term in model_terms]
    return brier, baseline_brier, skill, _bootstrap_ci(per_row, spec)


def _bootstrap_ci(values: Sequence[float], spec: ValidationSpec) -> MetricInterval:
    if not values:
        return MetricInterval(None, None)
    rng = np.random.default_rng(spec.seed)
    arr = np.asarray(values, dtype=float)
    draws = rng.choice(arr, size=(spec.bootstrap_samples, len(arr)), replace=True)
    means = draws.mean(axis=1)
    alpha = (1.0 - spec.confidence) / 2.0
    lower, upper = np.quantile(means, [alpha, 1.0 - alpha])
    return MetricInterval(float(lower), float(upper))


def _judge(
    segment: SegmentReport,
    scoring_mode: ScoringMode,
    spec: ValidationSpec,
) -> tuple[HeadlineVerdict, tuple[str, ...], str]:
    if segment.independent_blocks < spec.min_independent_blocks:
        return (
            "UNDERPOWERED",
            ("underpowered",),
            (
                f"purged n={segment.independent_blocks}"
                f"<{spec.min_independent_blocks}"
            ),
        )
    failures: list[str] = []
    if segment.coverage is not None and segment.coverage < spec.min_coverage:
        failures.append("coverage")
    if scoring_mode == "probability":
        metric = segment.brier_skill
        lower = segment.brier_skill_ci.lower
        floor = BRIER_SKILL_FLOOR
        label = "Brier skill"
    else:
        metric = segment.mean_cost_adjusted_excess
        lower = segment.excess_ci.lower
        floor = SIGNED_RETURN_FLOOR
        label = "signed excess"
    if metric is None or lower is None:
        failures.append("metric_undefined")
    elif lower <= floor:
        failures.append("ci_below_floor")
    if failures:
        return (
            "FAIL",
            tuple(failures),
            f"{label} CI lower {lower if lower is not None else 'n/a'} ≤ floor {floor}",
        )
    return (
        "PASS",
        (),
        f"{label} {metric:+.4f}, CI lower {lower:+.4f} > floor {floor}",
    )


def _empty_segment(name: str, scoring_mode: ScoringMode) -> SegmentReport:
    return SegmentReport(
        name=name,
        scoring_mode=scoring_mode,
        claim_count=0,
        matured_count=0,
        void_count=0,
        independent_blocks=0,
        coverage=None,
        hit_rate=None,
        baseline_hit_rate=None,
        brier_score=None,
        baseline_brier_score=None,
        brier_skill=None,
        brier_skill_ci=MetricInterval(None, None),
        mean_cost_adjusted_excess=None,
        excess_ci=MetricInterval(None, None),
        first_entry_session=None,
        last_entry_session=None,
    )


def _hash_inputs(
    rows: Sequence[ForecastRecord],
    prices: PricePanel,
    spec: ValidationSpec,
) -> str:
    payload = {
        "forecasts": [
            {
                "policy": row.policy_version_id,
                "target": row.outcome_target,
                "entry": row.entry_session.isoformat(),
                "h": row.horizon_sessions,
                "dir": row.direction,
                "p": row.probability_up,
            }
            for row in rows
        ],
        "prices": [
            [symbol, session.isoformat(), close]
            for (symbol, session), close in sorted(
                prices.items(), key=lambda item: (item[0][0], item[0][1].isoformat())
            )
        ],
        "spec": {
            "horizons": list(spec.horizons),
            "primary": spec.primary_horizon,
            "benchmarks": list(spec.benchmarks),
            "cost_bps": spec.cost_bps,
            "min_n": spec.min_independent_blocks,
            "profile": spec.minimum_improvement_profile,
        },
    }
    blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()
