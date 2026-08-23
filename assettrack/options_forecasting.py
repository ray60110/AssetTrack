"""Point-in-time option forecasts, proper-score backtests, and user guidance.

This module is the external seam for deciding whether an options signal may be
shown as an actionable forecast.  Callers provide the raw point-in-time verdict
and its walk-forward report; the implementation keeps probability calibration,
validation, failure classification, and remediation advice in one place.

The raw verdict remains observable even when validation fails.  Only
``actionable_direction`` may be shown as an actionable forecast.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable, Optional


FORECAST_HORIZON = 5
PROBABILITY_THRESHOLD = 0.60
PRIOR_STRENGTH = 10.0


@dataclass(frozen=True)
class OptionForecastAssessment:
    raw_direction: Optional[str]
    actionable_direction: Optional[str]
    horizon: Optional[int]
    probability: Optional[float]
    baseline_probability: Optional[float]
    brier_skill: Optional[float]
    sample_n: int
    raw_sample_n: int
    status: str
    diagnosis: str
    modification_guidance: str
    summary: str

    @property
    def validated(self) -> bool:
        return self.actionable_direction in ("多", "空")


def purge_overlapping_records(records: Iterable[dict]) -> list[dict]:
    """Keep non-overlapping label intervals per symbol and horizon.

    A +5-session return starting before the previous +5-session outcome has
    matured is not a new independent experiment.  Keeping it would inflate the
    apparent sample size and make both hit-rate and p-value look more precise
    than they are.
    """
    grouped: dict[tuple[str, int], list[dict]] = {}
    passthrough: list[dict] = []
    for record in records:
        try:
            key = (str(record.get("underlying") or ""), int(record["h"]))
            int(record["entry_index"])
            int(record["outcome_index"])
        except (KeyError, TypeError, ValueError):
            passthrough.append(dict(record))
            continue
        grouped.setdefault(key, []).append(dict(record))

    selected: list[dict] = []
    for rows in grouped.values():
        last_outcome = -1
        for record in sorted(
            rows,
            key=lambda item: (int(item["entry_index"]), str(item.get("date") or "")),
        ):
            entry = int(record["entry_index"])
            if entry <= last_outcome:
                continue
            selected.append(record)
            last_outcome = int(record["outcome_index"])
    return sorted(
        selected + passthrough,
        key=lambda item: (
            str(item.get("date") or ""),
            str(item.get("underlying") or ""),
            int(item.get("h") or 0),
        ),
    )


def _known_before(prior: dict, current: dict) -> bool:
    if prior.get("outcome_date") and current.get("date"):
        return str(prior["outcome_date"]) < str(current["date"])
    try:
        return int(prior["outcome_index"]) < int(current["entry_index"])
    except (KeyError, TypeError, ValueError):
        return str(prior.get("date") or "") < str(current.get("date") or "")


def _smoothed_rate(hits: int, n: int, centre: float, strength: float) -> float:
    return (hits + strength * centre) / (n + strength)


def _clip_probability(value: float) -> float:
    return max(1e-6, min(1.0 - 1e-6, value))


def _direction_hit(record: dict, direction: str) -> bool:
    if "hit" in record:
        return bool(record["hit"])
    up = bool(record.get("outcome_up"))
    return up if direction == "up" else not up


def build_probability_backtest(
    records: Iterable[dict],
    baseline_records: Iterable[dict],
    *,
    min_samples: int = 20,
    prior_strength: float = PRIOR_STRENGTH,
) -> dict:
    """Build expanding, prequential probability and proper-score metrics.

    Every probability is computed using outcomes that had matured strictly
    before that forecast session.  The signal probability is shrunk toward the
    then-known market base rate, so a three-for-three start cannot masquerade as
    a 100% forecast.  Inputs are purged again defensively; the operation is
    idempotent for already-purged records.
    """
    raw_signal_rows = [dict(row) for row in records]
    raw_base_rows = [dict(row) for row in baseline_records]
    signal_rows = purge_overlapping_records(raw_signal_rows)
    base_rows = purge_overlapping_records(raw_base_rows)
    horizons = sorted({int(row["h"]) for row in signal_rows if row.get("h") is not None})
    by_horizon: dict[int, dict] = {}

    for horizon in horizons:
        by_horizon[horizon] = {}
        horizon_base = [row for row in base_rows if int(row.get("h", -1)) == horizon]
        for direction in ("up", "down"):
            direction_rows = [
                row for row in signal_rows
                if int(row.get("h", -1)) == horizon and row.get("dir") == direction
            ]
            scored: list[tuple[float, float, bool]] = []
            for current in direction_rows:
                known_base = [row for row in horizon_base if _known_before(row, current)]
                base_up_hits = sum(bool(row.get("outcome_up")) for row in known_base)
                base_up = _smoothed_rate(base_up_hits, len(known_base), 0.5, prior_strength)
                base_hit = base_up if direction == "up" else 1.0 - base_up

                known_signals = [row for row in direction_rows if _known_before(row, current)]
                signal_hits = sum(_direction_hit(row, direction) for row in known_signals)
                probability = _smoothed_rate(
                    signal_hits,
                    len(known_signals),
                    base_hit,
                    prior_strength,
                )
                scored.append((probability, base_hit, _direction_hit(current, direction)))

            all_base_up_hits = sum(bool(row.get("outcome_up")) for row in horizon_base)
            next_base_up = _smoothed_rate(
                all_base_up_hits,
                len(horizon_base),
                0.5,
                prior_strength,
            )
            next_base = next_base_up if direction == "up" else 1.0 - next_base_up
            all_signal_hits = sum(_direction_hit(row, direction) for row in direction_rows)
            next_probability = (
                _smoothed_rate(
                    all_signal_hits,
                    len(direction_rows),
                    next_base,
                    prior_strength,
                )
                if direction_rows
                else None
            )

            if scored:
                brier = sum((prob - float(hit)) ** 2 for prob, _, hit in scored) / len(scored)
                base_brier = sum(
                    (base_prob - float(hit)) ** 2 for _, base_prob, hit in scored
                ) / len(scored)
                brier_skill = 1.0 - brier / base_brier if base_brier > 0 else None
                log_loss = -sum(
                    float(hit) * math.log(_clip_probability(prob))
                    + (1.0 - float(hit)) * math.log(_clip_probability(1.0 - prob))
                    for prob, _, hit in scored
                ) / len(scored)
                hit_rate = sum(hit for _, _, hit in scored) / len(scored)
                baseline_hit_rate = sum(base for _, base, _ in scored) / len(scored)
            else:
                brier = base_brier = brier_skill = log_loss = hit_rate = None
                baseline_hit_rate = None

            by_horizon[horizon][direction] = {
                "n": len(direction_rows),
                "raw_n": len([
                    row for row in raw_signal_rows
                    if int(row.get("h", -1)) == horizon and row.get("dir") == direction
                ]),
                "ready": len(direction_rows) >= min_samples,
                "next_probability": next_probability,
                "baseline_probability": next_base,
                "hit_rate": hit_rate,
                "baseline_hit_rate": baseline_hit_rate,
                "brier_score": brier,
                "benchmark_brier_score": base_brier,
                "brier_skill": brier_skill,
                "log_loss": log_loss,
            }

    return {
        "method": "expanding empirical-Bayes probability; purged walk-forward",
        "prior_strength": prior_strength,
        "min_samples": min_samples,
        "by_horizon": by_horizon,
    }


def _metric_for(report: dict, horizon: int, direction: str) -> Optional[dict]:
    by_horizon = (report.get("probability_backtest") or {}).get("by_horizon") or {}
    row = by_horizon.get(horizon)
    if row is None:
        row = by_horizon.get(str(horizon))
    return (row or {}).get(direction)


def _nearest_horizon(report: dict, direction: str, preferred_horizon: int) -> Optional[int]:
    by_horizon = (report.get("probability_backtest") or {}).get("by_horizon") or {}
    usable = []
    for key, row in by_horizon.items():
        try:
            horizon = int(key)
        except (TypeError, ValueError):
            continue
        if (row or {}).get(direction) is not None:
            usable.append(horizon)
    if not usable:
        return None
    return min(usable, key=lambda value: (abs(value - preferred_horizon), -value))


def _tightened_bias(params: Optional[dict]) -> tuple[float, float]:
    current = float((params or {}).get("bias_min_pct", 0.03))
    return current, min(0.15, round(current + 0.02, 4))


def assess_option_forecast(
    raw_direction: Optional[str],
    backtest: Optional[dict],
    *,
    verdict_params: Optional[dict] = None,
    preferred_horizon: int = FORECAST_HORIZON,
) -> OptionForecastAssessment:
    """Return one forecast decision with validation and concrete remediation."""
    report = backtest or {}
    if raw_direction not in ("多", "空"):
        return OptionForecastAssessment(
            raw_direction=None,
            actionable_direction=None,
            horizon=None,
            probability=None,
            baseline_probability=None,
            brier_skill=None,
            sample_n=0,
            raw_sample_n=0,
            status="no_signal",
            diagnosis="目前期權特徵沒有一致方向。",
            modification_guidance=(
                "不要為了產生訊號而放寬門檻；維持觀望並等待新 session。"
            ),
            summary="目前無一致的未來方向訊號，正式建議維持觀望。",
        )

    direction_key = "up" if raw_direction == "多" else "down"
    horizon = _nearest_horizon(report, direction_key, preferred_horizon)
    metric = _metric_for(report, horizon, direction_key) if horizon is not None else None
    target_word = "上漲" if raw_direction == "多" else "下跌"
    current_bias, tightened_bias = _tightened_bias(verdict_params)

    if metric is None:
        return OptionForecastAssessment(
            raw_direction=raw_direction,
            actionable_direction=None,
            horizon=horizon,
            probability=None,
            baseline_probability=None,
            brier_skill=None,
            sample_n=0,
            raw_sample_n=0,
            status="collecting",
            diagnosis="尚無不重疊且已成熟的前瞻樣本，無法校準預測機率。",
            modification_guidance=(
                "不要調整參數；每日收盤後持續累積真實快照，"
                "等 outcome 到期後再評估。"
            ),
            summary=(
                f"模型原始訊號偏{raw_direction}，但尚無成熟回測；"
                "正式建議維持觀望，"
                "期權頁會顯示資料需求與修改方式。"
            ),
        )

    probability = metric.get("next_probability")
    baseline_probability = metric.get("baseline_probability")
    brier_skill = metric.get("brier_skill")
    sample_n = int(metric.get("n") or 0)
    raw_sample_n = int(metric.get("raw_n") or sample_n)
    min_samples = int((report.get("probability_backtest") or {}).get("min_samples") or 20)
    hit_rate = metric.get("hit_rate")
    baseline_hit_rate = metric.get("baseline_hit_rate")

    by_horizon = report.get("by_horizon") or {}
    horizon_stats = by_horizon.get(horizon, by_horizon.get(str(horizon), {}))
    significance = ((horizon_stats or {}).get("significance") or {}).get(direction_key) or {}
    stability = report.get("stability") or {}
    health = report.get("model_health") or {}
    branch_health = (health.get("by_direction") or {}).get(direction_key, health)

    failures: list[tuple[str, str]] = []
    if sample_n < min_samples:
        failures.append(("collecting", f"不重疊成熟樣本 n={sample_n}<{min_samples}"))
    if brier_skill is None:
        failures.append(("collecting", "Brier skill 尚無法計算"))
    elif brier_skill <= 0:
        failures.append((
            "negative_skill",
            f"Brier skill {brier_skill:+.3f}，未優於基準機率",
        ))
    if hit_rate is not None and baseline_hit_rate is not None and hit_rate <= baseline_hit_rate:
        failures.append(("negative_edge", "方向命中率未優於同期間基準"))
    if not significance or not significance.get("significant_adj"):
        failures.append(("not_significant", "未通過多重比較後的顯著性檢定"))
    if stability.get("consistent") is not True:
        stability_reason = (
            "前後子區間表現不一致"
            if stability.get("consistent") is False
            else "尚無足夠樣本確認前後子區間穩定性"
        )
        failures.append(("unstable", stability_reason))
    if probability is None or probability <= PROBABILITY_THRESHOLD:
        failures.append(("low_probability", "校準機率未嚴格超過 60%"))
    if branch_health.get("status") == "degraded":
        failures.append((
            "degraded",
            branch_health.get("reason") or "近期成熟預測連續失配",
        ))

    if not failures:
        pct = probability * 100.0 if probability is not None else 0.0
        skill = brier_skill if brier_skill is not None else 0.0
        return OptionForecastAssessment(
            raw_direction=raw_direction,
            actionable_direction=raw_direction,
            horizon=horizon,
            probability=probability,
            baseline_probability=baseline_probability,
            brier_skill=brier_skill,
            sample_n=sample_n,
            raw_sample_n=raw_sample_n,
            status="validated",
            diagnosis=(
                "purged walk-forward、proper score、edge、穩定性與顯著性皆通過。"
            ),
            modification_guidance=(
                "維持目前參數；只在新增成熟 outcome 後重新評估，"
                "不追逐單次結果。"
            ),
            summary=(
                f"預測未來 +{horizon} 個市場 session {target_word}，"
                f"校準機率 {pct:.0f}%（purged n={sample_n}，Brier skill {skill:+.3f}）。"
            ),
        )

    priority = (
        "collecting",
        "degraded",
        "negative_skill",
        "negative_edge",
        "unstable",
        "not_significant",
        "low_probability",
    )
    status = next(
        (name for name in priority if any(code == name for code, _ in failures)),
        failures[0][0],
    )
    diagnosis = "；".join(dict.fromkeys(message for _, message in failures))
    missing = max(0, min_samples - sample_n)
    if status == "collecting":
        guidance = (
            f"不要調參；先再累積 {missing} 個不重疊成熟樣本。"
            f"+{horizon} session 預測只有到期後才可計分。"
        )
    elif status in {"degraded", "negative_skill", "negative_edge", "unstable", "not_significant"}:
        guidance = (
            f"在 QuantTrade 建立候選，把 bias_min_pct 由 {current_bias:.2f}% 提高至 "
            f"{tightened_bias:.2f}% 以濾除弱殘差，再用相同 +{horizon} horizon 做 "
            "purged walk-forward；只有 Brier skill、edge 與穩定性都改善才可升級。"
            "若仍未改善，停用此方向，不要繼續在同一份樣本上調門檻。"
        )
    else:
        guidance = (
            "維持觀望；不要為了跨過 60% 而調參，"
            "等新增成熟 outcome 再重新評估。"
        )

    pct_text = f"{probability * 100:.0f}%" if probability is not None else "尚無法估計"
    if status == "collecting":
        short_reason = f"purged n={sample_n}<{min_samples}"
    elif status == "negative_skill":
        short_reason = f"Brier skill {brier_skill:+.3f} 未優於基準"
    elif status == "negative_edge":
        short_reason = "方向 edge 不為正"
    elif status == "degraded":
        short_reason = "近期成熟預測持續失配"
    elif status == "unstable":
        short_reason = "前後區間不穩定"
    elif status == "not_significant":
        short_reason = "顯著性未通過"
    else:
        short_reason = "校準機率未超過 60%"
    return OptionForecastAssessment(
        raw_direction=raw_direction,
        actionable_direction=None,
        horizon=horizon,
        probability=probability,
        baseline_probability=baseline_probability,
        brier_skill=brier_skill,
        sample_n=sample_n,
        raw_sample_n=raw_sample_n,
        status=status,
        diagnosis=diagnosis,
        modification_guidance=guidance,
        summary=(
            f"模型原始預測 +{horizon} 個市場 session {target_word}"
            f"（校準機率 {pct_text}），但回測未通過（{short_reason}），"
            "正式建議維持觀望；期權頁有修改方式。"
        ),
    )
