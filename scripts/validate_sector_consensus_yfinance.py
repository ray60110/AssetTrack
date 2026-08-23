#!/usr/bin/env python3
"""Validate the frozen sector breadth-consensus rule on external yfinance history.

This is a research tool.  It deliberately does not read AssetTrack's accumulated
sector snapshots and does not write anything back to production caches.

The tested rule is the current ``sector_analysis.detect_broad_flow`` shape:

* daily breadth = (advancers - decliners) / rated members;
* a broad-up day needs breadth >= 0.5 and basket return > 0.1%;
* a broad-down day is the mirror;
* at least 3 of the most recent 5 eligible sessions must agree.

Historical point-in-time market capitalisation is not reliably available from
yfinance, so the primary validation uses equal-weight basket returns.  Applying
today's market caps to old observations would introduce look-ahead bias.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable, Optional

import numpy as np
import pandas as pd


DEFAULT_START = "2016-01-01"
DEFAULT_HORIZONS = (1, 5, 10)
DEFAULT_BENCHMARKS = ("QQQ", "SPY")
DEFAULT_LOOKBACK = 5
DEFAULT_MIN_DAYS = 3
DEFAULT_BREADTH_THRESHOLD = 0.5
DEFAULT_RETURN_THRESHOLD = 0.1
DEFAULT_MIN_COVERAGE = 0.70
DEFAULT_COST_BPS = 10.0
DEFAULT_BOOTSTRAP_SAMPLES = 10_000
DEFAULT_SEED = 20260815
NON_US_SUFFIXES = (
    ".KS", ".KQ", ".T", ".HK", ".L", ".SS", ".SZ", ".DE", ".PA",
    ".AS", ".MI", ".MC", ".BR", ".VI", ".HE", ".LS", ".SW", ".ST",
    ".OL", ".CO", ".TO", ".V", ".AX", ".NS", ".BO",
)


@dataclass(frozen=True)
class ValidationSpec:
    start: str
    end_exclusive: str
    horizons: tuple[int, ...]
    benchmarks: tuple[str, ...]
    lookback: int
    min_days: int
    breadth_threshold: float
    return_threshold_pct: float
    min_coverage: float
    cost_bps: float
    bootstrap_samples: int
    seed: int
    repair: bool


def parse_int_tuple(value: str) -> tuple[int, ...]:
    values = tuple(sorted({int(part.strip()) for part in value.split(",") if part.strip()}))
    if not values or any(v <= 0 for v in values):
        raise argparse.ArgumentTypeError("horizons must be positive integers")
    return values


def parse_str_tuple(value: str) -> tuple[str, ...]:
    values = tuple(dict.fromkeys(part.strip().upper() for part in value.split(",") if part.strip()))
    if not values:
        raise argparse.ArgumentTypeError("at least one benchmark is required")
    return values


def load_groups(path: Path) -> dict[str, list[str]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    source = raw.get("groups", raw) if isinstance(raw, dict) else {}
    groups: dict[str, list[str]] = {}
    for name, members in source.items():
        if not isinstance(members, list):
            continue
        clean = list(
            dict.fromkeys(
                str(symbol).strip().upper()
                for symbol in members
                if str(symbol).strip()
                and not str(symbol).strip().upper().endswith((".TW", ".TWO"))
            )
        )
        if clean:
            groups[str(name)] = clean
    if not groups:
        raise ValueError(f"no usable groups in {path}")
    return groups


def _normalise_close_frame(raw: pd.DataFrame, symbols: list[str]) -> pd.DataFrame:
    if raw.empty:
        return pd.DataFrame()
    if isinstance(raw.columns, pd.MultiIndex):
        first = set(str(v) for v in raw.columns.get_level_values(0))
        last = set(str(v) for v in raw.columns.get_level_values(-1))
        if "Close" in first:
            close = raw["Close"].copy()
        elif "Close" in last:
            close = raw.xs("Close", axis=1, level=-1).copy()
        else:
            raise ValueError("yfinance response has no Close field")
    else:
        if "Close" not in raw.columns:
            raise ValueError("yfinance response has no Close field")
        close = raw[["Close"]].copy()
        close.columns = symbols[:1]
    close.columns = [str(column).upper() for column in close.columns]
    close.index = pd.to_datetime(close.index).tz_localize(None).normalize()
    close = close[~close.index.duplicated(keep="last")].sort_index()
    return close.reindex(columns=symbols)


def download_adjusted_closes(
    symbols: Iterable[str], start: str, end_exclusive: str, *, repair: bool = False
) -> tuple[pd.DataFrame, str]:
    import yfinance as yf

    ordered = list(dict.fromkeys(str(symbol).upper() for symbol in symbols))
    raw = yf.download(
        ordered,
        start=start,
        end=end_exclusive,
        interval="1d",
        auto_adjust=True,
        actions=True,
        repair=repair,
        keepna=True,
        prepost=False,
        ignore_tz=True,
        progress=False,
        threads=False,
        group_by="column",
        multi_level_index=True,
    )
    closes = _normalise_close_frame(raw, ordered)
    digest_payload = closes.to_csv(date_format="%Y-%m-%d", float_format="%.10g").encode()
    return closes, hashlib.sha256(digest_payload).hexdigest()


def close_frame_hash(closes: pd.DataFrame) -> str:
    payload = closes.to_csv(date_format="%Y-%m-%d", float_format="%.10g").encode()
    return hashlib.sha256(payload).hexdigest()


def data_quality(closes: pd.DataFrame, symbols: Iterable[str]) -> list[dict]:
    rows = []
    for symbol in symbols:
        series = closes.get(symbol)
        clean = series.dropna() if series is not None else pd.Series(dtype=float)
        rows.append(
            {
                "symbol": symbol,
                "rows": int(len(clean)),
                "first_date": clean.index[0].date().isoformat() if len(clean) else None,
                "last_date": clean.index[-1].date().isoformat() if len(clean) else None,
            }
        )
    return rows


def build_group_panel(
    closes: pd.DataFrame,
    members: list[str],
    benchmark_sessions: pd.DatetimeIndex,
    *,
    lookback: int,
    min_days: int,
    breadth_threshold: float,
    return_threshold_pct: float,
    min_coverage: float,
) -> pd.DataFrame:
    prices = closes.reindex(benchmark_sessions).reindex(columns=members)
    member_returns = prices.pct_change(fill_method=None) * 100.0
    rated = member_returns.notna().sum(axis=1)
    minimum_rated = max(3, math.ceil(len(members) * min_coverage))
    eligible = rated >= minimum_rated
    n_up = (member_returns > 0).sum(axis=1)
    n_down = (member_returns < 0).sum(axis=1)
    breadth = (n_up - n_down).div(rated.where(rated > 0))
    basket_return = member_returns.mean(axis=1, skipna=True).where(eligible)
    breadth = breadth.where(eligible)

    daily_direction = pd.Series("none", index=benchmark_sessions, dtype="object")
    daily_direction.loc[
        eligible
        & (breadth >= breadth_threshold)
        & (basket_return > return_threshold_pct)
    ] = "up"
    daily_direction.loc[
        eligible
        & (breadth <= -breadth_threshold)
        & (basket_return < -return_threshold_pct)
    ] = "down"

    # Persistence is defined over the most recent eligible observations.  A date
    # that fails the coverage gate neither emits a signal nor consumes one of the
    # five history slots.
    eligible_direction = daily_direction.loc[eligible]
    eligible_up = (eligible_direction == "up").astype(int)
    eligible_down = (eligible_direction == "down").astype(int)
    eligible_evaluated = pd.Series(
        np.minimum(np.arange(1, len(eligible_direction) + 1), lookback),
        index=eligible_direction.index,
        dtype=float,
    )
    evaluated = eligible_evaluated.reindex(benchmark_sessions)
    up_days = eligible_up.rolling(lookback, min_periods=1).sum().reindex(benchmark_sessions)
    down_days = eligible_down.rolling(lookback, min_periods=1).sum().reindex(benchmark_sessions)
    direction = pd.Series("none", index=benchmark_sessions, dtype="object")
    ready = evaluated >= min_days
    direction.loc[ready & (up_days >= min_days) & (up_days >= down_days)] = "up"
    direction.loc[ready & (direction == "none") & (down_days >= min_days)] = "down"

    return pd.DataFrame(
        {
            "basket_return_pct": basket_return,
            "rated": rated,
            "minimum_rated": minimum_rated,
            "coverage": rated / len(members),
            "breadth": breadth,
            "daily_direction": daily_direction,
            "evaluated": evaluated,
            "up_days": up_days,
            "down_days": down_days,
            "direction": direction,
        },
        index=benchmark_sessions,
    )


def exact_forward_return(daily_return_pct: pd.Series, horizon: int) -> pd.Series:
    gross = 1.0 + daily_return_pct / 100.0
    compounded = gross.rolling(horizon, min_periods=horizon).apply(np.prod, raw=True)
    return (compounded.shift(-horizon) - 1.0) * 100.0


def _bootstrap_ci(
    values: list[float], samples: int, seed: int, confidence: float = 0.95
) -> tuple[Optional[float], Optional[float]]:
    clean = np.asarray([value for value in values if np.isfinite(value)], dtype=float)
    if not len(clean):
        return None, None
    rng = np.random.default_rng(seed)
    draws = np.empty(samples, dtype=float)
    chunk_size = 1_000
    for start in range(0, samples, chunk_size):
        stop = min(samples, start + chunk_size)
        draws[start:stop] = rng.choice(
            clean, size=(stop - start, len(clean)), replace=True
        ).mean(axis=1)
    alpha = (1.0 - confidence) / 2.0
    return float(np.quantile(draws, alpha)), float(np.quantile(draws, 1.0 - alpha))


def _poisson_binomial_sf(k: int, probabilities: list[float]) -> float:
    """Exact P(X >= k) for independent Bernoulli trials with heterogeneous p.

    Each date cluster can contain a different sector basket and therefore a
    different unconditional baseline.  Treating their mean as one binomial p is
    only an approximation; this dynamic program keeps the null matched to every
    evaluated cluster.
    """
    if not probabilities:
        return 1.0
    distribution = np.array([1.0], dtype=float)
    for raw_probability in probabilities:
        probability = min(1.0, max(0.0, float(raw_probability)))
        updated = np.zeros(len(distribution) + 1, dtype=float)
        updated[:-1] += distribution * (1.0 - probability)
        updated[1:] += distribution * probability
        distribution = updated
    return float(distribution[max(0, k):].sum())


def _wilson_interval(hits: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n <= 0:
        return 0.0, 1.0
    p = hits / n
    z2 = z * z
    denominator = 1.0 + z2 / n
    centre = (p + z2 / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denominator
    return max(0.0, centre - half), min(1.0, centre + half)


def _purge_overlapping(clusters: list[dict], horizon: int) -> list[dict]:
    selected = []
    last_position: Optional[int] = None
    for cluster in sorted(clusters, key=lambda row: row["session_position"]):
        position = int(cluster["session_position"])
        if last_position is None or position >= last_position + horizon:
            selected.append(cluster)
            last_position = position
    return selected


def _half_stability(blocks: list[dict], benchmark: str) -> dict:
    if len(blocks) < 4:
        return {
            "stable": None,
            "reason": "fewer than four non-overlapping date clusters",
        }
    midpoint = len(blocks) // 2
    halves = (blocks[:midpoint], blocks[midpoint:])
    result = []
    for rows in halves:
        result.append(
            {
                "n": len(rows),
                "hit_edge": float(np.mean([row["hit_edge"] for row in rows])),
                "mean_signed_excess_net_pct": float(
                    np.mean([row[f"signed_excess_{benchmark}_net_pct"] for row in rows])
                ),
                "first_date": rows[0]["date"],
                "last_date": rows[-1]["date"],
            }
        )
    return {
        "stable": all(
            half["hit_edge"] > 0 and half["mean_signed_excess_net_pct"] > 0
            for half in result
        ),
        "early": result[0],
        "late": result[1],
    }


def collect_signal_records(
    panels: dict[str, pd.DataFrame],
    benchmark_returns: dict[str, pd.Series],
    horizons: tuple[int, ...],
    lookback: int = DEFAULT_LOOKBACK,
) -> tuple[list[dict], dict[tuple[str, int, str], float]]:
    records: list[dict] = []
    unconditional: dict[tuple[str, int, str], float] = {}
    for group, panel in panels.items():
        previous_direction = panel["direction"].shift(1, fill_value="none")
        is_episode = (
            panel["direction"].isin(("up", "down"))
            & (panel["direction"] != previous_direction)
        )
        trailing_gross = (
            (1.0 + panel["basket_return_pct"] / 100.0)
            .rolling(lookback, min_periods=lookback)
            .apply(np.prod, raw=True)
        )
        trailing_return = (trailing_gross - 1.0) * 100.0
        for horizon in horizons:
            group_forward = exact_forward_return(panel["basket_return_pct"], horizon)
            benchmark_forward = {
                symbol: exact_forward_return(series, horizon)
                for symbol, series in benchmark_returns.items()
            }
            valid = group_forward.dropna()
            unconditional[(group, horizon, "up")] = float((valid > 0).mean())
            unconditional[(group, horizon, "down")] = float((valid < 0).mean())
            for timestamp, direction in panel["direction"].items():
                if direction not in ("up", "down"):
                    continue
                group_result = group_forward.get(timestamp)
                if group_result is None or not np.isfinite(group_result):
                    continue
                benchmark_values = {
                    symbol: forward.get(timestamp)
                    for symbol, forward in benchmark_forward.items()
                }
                if any(value is None or not np.isfinite(value) for value in benchmark_values.values()):
                    continue
                sign = 1.0 if direction == "up" else -1.0
                momentum_value = trailing_return.get(timestamp)
                momentum_sign = (
                    1.0 if momentum_value is not None and momentum_value > 0
                    else -1.0 if momentum_value is not None and momentum_value < 0
                    else 0.0
                )
                record = {
                    "group": group,
                    "date": timestamp.date().isoformat(),
                    "session_position": int(panel.index.get_loc(timestamp)),
                    "horizon": horizon,
                    "direction": direction,
                    "episode": bool(is_episode.get(timestamp, False)),
                    "group_forward_pct": float(group_result),
                    "signed_return_pct": float(sign * group_result),
                    "momentum_direction": (
                        "up" if momentum_sign > 0 else "down" if momentum_sign < 0 else "none"
                    ),
                    "momentum_signed_return_pct": float(momentum_sign * group_result),
                    "signal_vs_momentum_pct": float(
                        (sign - momentum_sign) * group_result
                    ),
                    "hit": bool(sign * group_result > 0),
                    "baseline_rate": unconditional[(group, horizon, direction)],
                }
                for symbol, value in benchmark_values.items():
                    record[f"benchmark_{symbol}_forward_pct"] = float(value)
                    record[f"signed_excess_{symbol}_pct"] = float(sign * (group_result - value))
                records.append(record)
    return records, unconditional


def _cluster_records(
    records: list[dict], benchmark_symbols: tuple[str, ...], cost_bps: float
) -> list[dict]:
    by_key: dict[tuple[str, int, str], list[dict]] = {}
    for record in records:
        key = (record["date"], record["horizon"], record["direction"])
        by_key.setdefault(key, []).append(record)

    clusters = []
    cost_pct = cost_bps / 100.0
    for (date_value, horizon, direction), rows in sorted(by_key.items()):
        signed_return = float(np.mean([row["signed_return_pct"] for row in rows]))
        baseline_rate = float(np.mean([row["baseline_rate"] for row in rows]))
        cluster = {
            "date": date_value,
            "session_position": rows[0]["session_position"],
            "horizon": horizon,
            "direction": direction,
            "groups": sorted(row["group"] for row in rows),
            "group_count": len(rows),
            "hit": bool(signed_return > 0),
            "baseline_rate": baseline_rate,
            "hit_edge": (1.0 if signed_return > 0 else 0.0) - baseline_rate,
            "signed_return_pct": signed_return,
            "signed_return_net_pct": signed_return - cost_pct,
            "signal_vs_momentum_pct": float(
                np.mean([row["signal_vs_momentum_pct"] for row in rows])
            ),
        }
        for symbol in benchmark_symbols:
            signed_excess = float(
                np.mean([row[f"signed_excess_{symbol}_pct"] for row in rows])
            )
            cluster[f"signed_excess_{symbol}_pct"] = signed_excess
            cluster[f"signed_excess_{symbol}_net_pct"] = signed_excess - cost_pct
        clusters.append(cluster)
    return clusters


def _primary_episode_blocks(
    records: list[dict], spec: ValidationSpec, start: str, end: Optional[str] = None,
    excluded_group: Optional[str] = None,
) -> list[dict]:
    primary_horizon = 5
    selected = [
        row for row in records
        if row["episode"]
        and row["horizon"] == primary_horizon
        and row["date"] >= start
        and (end is None or row["date"] <= end)
        and (excluded_group is None or row["group"] != excluded_group)
    ]
    by_date: dict[str, list[dict]] = {}
    for row in selected:
        by_date.setdefault(row["date"], []).append(row)
    cost_pct = spec.cost_bps / 100.0
    clusters = []
    for date_value, rows in sorted(by_date.items()):
        clusters.append(
            {
                "date": date_value,
                "session_position": rows[0]["session_position"],
                "groups": sorted(row["group"] for row in rows),
                "up_n": sum(row["direction"] == "up" for row in rows),
                "down_n": sum(row["direction"] == "down" for row in rows),
                "signed_return_net_pct": float(
                    np.mean([row["signed_return_pct"] for row in rows]) - cost_pct
                ),
                # Both policies make one directional decision, so equal assumed
                # costs cancel in the paired improvement.
                "signal_vs_momentum_pct": float(
                    np.mean([row["signal_vs_momentum_pct"] for row in rows])
                ),
            }
        )
    return _purge_overlapping(clusters, primary_horizon)


def _summarise_primary_blocks(
    blocks: list[dict], spec: ValidationSpec, seed_offset: int = 0
) -> dict:
    signed = [row["signed_return_net_pct"] for row in blocks]
    paired = [row["signal_vs_momentum_pct"] for row in blocks]
    signed_ci = _bootstrap_ci(
        signed, spec.bootstrap_samples, spec.seed + seed_offset, confidence=0.95
    )
    paired_ci = _bootstrap_ci(
        paired, spec.bootstrap_samples, spec.seed + seed_offset + 1, confidence=0.95
    )
    stress_cost_delta_pct = (25.0 - spec.cost_bps) / 100.0
    mean_signed = float(np.mean(signed)) if signed else None
    mean_paired = float(np.mean(paired)) if paired else None
    stress_mean = mean_signed - stress_cost_delta_pct if mean_signed is not None else None
    n = len(blocks)
    return {
        "purged_episode_blocks": n,
        "up_episode_votes": sum(row["up_n"] for row in blocks),
        "down_episode_votes": sum(row["down_n"] for row in blocks),
        "mean_signed_return_net_pct": mean_signed,
        "signed_return_bootstrap_ci": signed_ci,
        "mean_paired_improvement_vs_momentum_pct": mean_paired,
        "paired_improvement_bootstrap_ci": paired_ci,
        "mean_signed_return_stress_25bps_pct": stress_mean,
        "first_date": blocks[0]["date"] if blocks else None,
        "last_date": blocks[-1]["date"] if blocks else None,
        "passed": bool(
            n >= 50
            and signed_ci[0] is not None and signed_ci[0] > 0
            and paired_ci[0] is not None and paired_ci[0] > 0
            and stress_mean is not None and stress_mean > 0
        ),
    }


def analyse_primary_episodes(records: list[dict], spec: ValidationSpec) -> dict:
    periods = {
        "development": ("2016-01-01", "2020-12-31"),
        "validation": ("2021-01-01", "2023-12-31"),
        "final_holdout": ("2024-01-01", None),
    }
    segments = {}
    for offset, (name, (start, end)) in enumerate(periods.items()):
        blocks = _primary_episode_blocks(records, spec, start, end)
        segments[name] = _summarise_primary_blocks(blocks, spec, seed_offset=5000 + offset * 10)

    holdout_blocks = _primary_episode_blocks(records, spec, "2024-01-01")
    leave_one_out = {}
    groups = sorted({row["group"] for row in records})
    for offset, group in enumerate(groups):
        blocks = _primary_episode_blocks(
            records, spec, "2024-01-01", excluded_group=group
        )
        leave_one_out[group] = _summarise_primary_blocks(
            blocks, spec, seed_offset=6000 + offset * 10
        )
    holdout = segments["final_holdout"]
    all_segments_positive = all(
        segment["mean_signed_return_net_pct"] is not None
        and segment["mean_signed_return_net_pct"] > 0
        for segment in segments.values()
    )
    leave_one_out_positive = all(
        result["mean_signed_return_net_pct"] is not None
        and result["mean_signed_return_net_pct"] > 0
        for result in leave_one_out.values()
    )
    return {
        "hypothesis": (
            "5-session sector-flow episodes have positive signed return and positive "
            "paired improvement over a simple five-session momentum direction"
        ),
        "segments": segments,
        "leave_one_sector_out": leave_one_out,
        "all_segments_positive": all_segments_positive,
        "leave_one_sector_out_positive": leave_one_out_positive,
        "passed": bool(
            holdout["passed"] and all_segments_positive and leave_one_out_positive
        ),
        "holdout_block_dates": [row["date"] for row in holdout_blocks],
    }


def analyse_records(
    records: list[dict],
    spec: ValidationSpec,
) -> list[dict]:
    clusters = _cluster_records(records, spec.benchmarks, spec.cost_bps)
    num_tests = len(spec.horizons) * 2
    analyses = []
    for horizon in spec.horizons:
        for direction in ("up", "down"):
            raw = [
                cluster
                for cluster in clusters
                if cluster["horizon"] == horizon and cluster["direction"] == direction
            ]
            blocks = _purge_overlapping(raw, horizon)
            n = len(blocks)
            hits = sum(bool(block["hit"]) for block in blocks)
            hit_rate = hits / n if n else None
            baseline_rate = float(np.mean([block["baseline_rate"] for block in blocks])) if n else None
            hit_edge = hit_rate - baseline_rate if n else None
            hit_ci = _wilson_interval(hits, n) if n else (None, None)
            familywise_confidence = 1.0 - 0.05 / num_tests
            hit_edge_ci = _bootstrap_ci(
                [block["hit_edge"] for block in blocks],
                spec.bootstrap_samples,
                spec.seed + horizon + (0 if direction == "up" else 1000),
                confidence=familywise_confidence,
            )
            p_value = _poisson_binomial_sf(
                hits, [block["baseline_rate"] for block in blocks]
            ) if n else None
            alpha_adjusted = 0.05 / num_tests
            primary = spec.benchmarks[0]
            primary_values = [
                block[f"signed_excess_{primary}_net_pct"] for block in blocks
            ]
            primary_ci = _bootstrap_ci(
                primary_values,
                spec.bootstrap_samples,
                spec.seed + horizon + (2000 if direction == "up" else 3000),
                confidence=familywise_confidence,
            )
            stability = _half_stability(blocks, primary)
            sufficient = n >= 30
            directional_pass = bool(
                sufficient
                and hit_edge_ci[0] is not None
                and hit_edge_ci[0] > 0
                and p_value is not None
                and p_value < alpha_adjusted
            )
            economic_pass = bool(
                sufficient
                and primary_ci[0] is not None
                and primary_ci[0] > 0
            )
            stable = stability.get("stable") is True
            result = {
                "horizon_sessions": horizon,
                "direction": direction,
                "raw_cluster_n": len(raw),
                "purged_cluster_n": n,
                "raw_group_signal_n": sum(cluster["group_count"] for cluster in raw),
                "hit_rate": hit_rate,
                "hit_rate_ci": hit_ci,
                "baseline_rate": baseline_rate,
                "hit_edge": hit_edge,
                "hit_edge_bootstrap_ci": hit_edge_ci,
                "p_value": p_value,
                "alpha_adjusted": alpha_adjusted,
                "familywise_confidence": familywise_confidence,
                "significant_adjusted": bool(p_value is not None and p_value < alpha_adjusted),
                "mean_signed_return_net_pct": (
                    float(np.mean([block["signed_return_net_pct"] for block in blocks]))
                    if blocks else None
                ),
                "stability": stability,
                "sufficient_independent_blocks": sufficient,
                "directional_pass": directional_pass,
                "economic_pass_primary": economic_pass,
                "validated": bool(directional_pass and economic_pass and stable),
                "first_date": blocks[0]["date"] if blocks else None,
                "last_date": blocks[-1]["date"] if blocks else None,
            }
            for benchmark in spec.benchmarks:
                values = [
                    block[f"signed_excess_{benchmark}_net_pct"] for block in blocks
                ]
                result[f"mean_signed_excess_{benchmark}_net_pct"] = (
                    float(np.mean(values)) if values else None
                )
                result[f"signed_excess_{benchmark}_bootstrap_ci"] = _bootstrap_ci(
                    values,
                    spec.bootstrap_samples,
                    spec.seed + horizon + sum(ord(char) for char in benchmark)
                    + (4000 if direction == "down" else 0),
                    confidence=familywise_confidence,
                )
            analyses.append(result)
    return analyses


def group_diagnostics(records: list[dict], benchmarks: tuple[str, ...]) -> list[dict]:
    keys = sorted({(row["group"], row["horizon"], row["direction"]) for row in records})
    diagnostics = []
    for group, horizon, direction in keys:
        rows = [
            row for row in records
            if (row["group"], row["horizon"], row["direction"])
            == (group, horizon, direction)
        ]
        item = {
            "group": group,
            "horizon_sessions": horizon,
            "direction": direction,
            "n": len(rows),
            "hit_rate": float(np.mean([row["hit"] for row in rows])),
            "baseline_rate": float(np.mean([row["baseline_rate"] for row in rows])),
            "mean_signed_return_pct": float(
                np.mean([row["signed_return_pct"] for row in rows])
            ),
        }
        for benchmark in benchmarks:
            item[f"mean_signed_excess_{benchmark}_pct"] = float(
                np.mean([row[f"signed_excess_{benchmark}_pct"] for row in rows])
            )
        diagnostics.append(item)
    return diagnostics


def _fmt_pct(value: Optional[float], decimals: int = 1) -> str:
    return "—" if value is None else f"{value * 100:.{decimals}f}%"


def _fmt_return(value: Optional[float], decimals: int = 3) -> str:
    return "—" if value is None else f"{value:+.{decimals}f}%"


def render_markdown(report: dict) -> str:
    spec = report["spec"]
    lines = [
        "# 類股共識 yfinance 歷史驗證結果",
        "",
        f"執行時間：{report['run_date']}  ",
        f"資料範圍：{report['data_first_date']}～{report['data_last_date']}  ",
        f"資料雜湊：`{report['data_sha256']}`  ",
        "",
        "## 凍結假說",
        "",
        (
            f"以目前固定規則（lookback={spec['lookback']}、min_days={spec['min_days']}、"
            f"breadth_threshold={spec['breadth_threshold']}、等權日報酬門檻 "
            f"{spec['return_threshold_pct']}%）判斷普漲／普跌；不使用本機 sector snapshots，"
            "不在看到結果後調參。"
        ),
        "",
        f"yfinance `repair={spec['repair']}`；下載參數與資料 SHA-256 均固定於 JSON 報告。",
        "",
        f"至少 {spec['min_coverage'] * 100:.0f}% 成分股有當日報酬才評估。"
        f"前瞻期為精確第 {', '.join(str(v) for v in spec['horizons'])} 個交易日；"
        f"主基準為 {spec['benchmarks'][0]}，交易成本假設 {spec['cost_bps']:.0f} bps。",
        "",
        "## 主要結果",
        "",
        "### Primary：2024 年後的 5-session 訊號 episodes",
        "",
    ]
    primary_result = report["primary_episode_analysis"]
    holdout = primary_result["segments"]["final_holdout"]
    lines += [
        f"- 去重疊 episode blocks：{holdout['purged_episode_blocks']}",
        (
            "- 扣 10 bps 後 signed return："
            f"{_fmt_return(holdout['mean_signed_return_net_pct'])} "
            f"[95% CI {_fmt_return(holdout['signed_return_bootstrap_ci'][0])}, "
            f"{_fmt_return(holdout['signed_return_bootstrap_ci'][1])}]"
        ),
        (
            "- 相對簡單五日動能的 paired improvement："
            f"{_fmt_return(holdout['mean_paired_improvement_vs_momentum_pct'])} "
            f"[95% CI {_fmt_return(holdout['paired_improvement_bootstrap_ci'][0])}, "
            f"{_fmt_return(holdout['paired_improvement_bootstrap_ci'][1])}]"
        ),
        f"- 三段同號：{'是' if primary_result['all_segments_positive'] else '否'}；"
        f"leave-one-sector-out 皆為正：{'是' if primary_result['leave_one_sector_out_positive'] else '否'}",
        f"- Primary 判定：{'✅ 通過' if primary_result['passed'] else '❌ 未通過'}",
        "",
        "| 區段 | 去重疊 episodes | 淨 signed return | 動能 paired improvement |",
        "|---|---:|---:|---:|",
    ]
    for name, segment in primary_result["segments"].items():
        lines.append(
            f"| {name} | {segment['purged_episode_blocks']} | "
            f"{_fmt_return(segment['mean_signed_return_net_pct'])} | "
            f"{_fmt_return(segment['mean_paired_improvement_vs_momentum_pct'])} |"
        )

    lines += [
        "",
        "### Secondary：每日持續狀態的方向與相對市場表現",
        "",
        "| 方向 | 前瞻 | 原始日期簇 | 去重疊 blocks | 命中率 | 無條件基準 | edge | 校正 p | 主基準淨超額（家族 CI） | 穩定 | 判定 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    primary = spec["benchmarks"][0]
    for row in report["analysis"]:
        stability = row["stability"].get("stable")
        stable_text = "是" if stability is True else "否" if stability is False else "不足"
        verdict = "✅ 通過" if row["validated"] else "❌ 未通過"
        lines.append(
            "| {direction} | +{horizon_sessions} | {raw_cluster_n} | {purged_cluster_n} | "
            "{hit} | {base} | {edge} | {p} | {economic} | {stable} | {verdict} |".format(
                **row,
                hit=_fmt_pct(row["hit_rate"]),
                base=_fmt_pct(row["baseline_rate"]),
                edge=_fmt_pct(row["hit_edge"]),
                p="—" if row["p_value"] is None else f"{row['p_value']:.4g}",
                economic=(
                    _fmt_return(row[f"mean_signed_excess_{primary}_net_pct"])
                    + " ["
                    + _fmt_return(row[f"signed_excess_{primary}_bootstrap_ci"][0])
                    + ", "
                    + _fmt_return(row[f"signed_excess_{primary}_bootstrap_ci"][1])
                    + "]"
                ),
                stable=stable_text,
                verdict=verdict,
            )
        )

    passed = [row for row in report["analysis"] if row["validated"]]
    lines += [
        "",
        "## 判讀",
        "",
    ]
    if passed:
        lines.append(
            "下列分支同時通過獨立樣本數、方向 edge、Bonferroni、扣成本後主基準超額與前後期穩定性："
        )
        lines.extend(
            f"- {row['direction']} / +{row['horizon_sessions']} 交易日"
            for row in passed
        )
    else:
        lines.append(
            "沒有任何方向／前瞻期通過全部守門；依本次外部歷史資料，不能把現行類股共識稱為已驗證的前瞻預測。"
        )

    lines += [
        "",
        "## 資料品質",
        "",
        "| Symbol | 日線筆數 | 起日 | 迄日 |",
        "|---|---:|---|---|",
    ]
    for row in report["data_quality"]:
        lines.append(
            f"| {row['symbol']} | {row['rows']} | {row['first_date'] or '—'} | {row['last_date'] or '—'} |"
        )

    lines += [
        "",
        "## 限制",
        "",
        "- 這是目前成分清單的固定籃子回測，不是 point-in-time 歷史成分；仍有存活者與成分選擇偏誤。",
        "- yfinance 不提供可靠的歷史逐日市值，因此採等權；結果驗證的是無前視偏誤的廣度＋等權報酬版本。",
        "- 同日跨板塊先合成一個日期簇，並依前瞻期 purge 重疊視窗；這比直接把每個板塊日當獨立樣本保守。",
        "- 命中檢定與經濟超額區間都以 6 個方向／前瞻組合做 family-wise 多重比較控制。",
        "- Primary 只在方向首次形成／翻轉時建立 episode，並以 2024 年後資料作 final holdout；仍未涵蓋 next-open 可成交性與 repair=True 敏感度。",
        "- 本報告只驗證預先凍結的現行參數。若根據結果調參，必須另開 train/validation/test walk-forward，不能重用本報告作樣本外證據。",
        "",
    ]
    return "\n".join(lines)


def run_validation(
    groups: dict[str, list[str]], closes: pd.DataFrame, data_hash: str, spec: ValidationSpec
) -> dict:
    benchmark_sessions = closes[spec.benchmarks[0]].dropna().index
    benchmark_returns = {
        symbol: closes[symbol].reindex(benchmark_sessions).pct_change(fill_method=None) * 100.0
        for symbol in spec.benchmarks
    }
    panels = {
        group: build_group_panel(
            closes,
            members,
            benchmark_sessions,
            lookback=spec.lookback,
            min_days=spec.min_days,
            breadth_threshold=spec.breadth_threshold,
            return_threshold_pct=spec.return_threshold_pct,
            min_coverage=spec.min_coverage,
        )
        for group, members in groups.items()
    }
    records, _ = collect_signal_records(
        panels, benchmark_returns, spec.horizons, lookback=spec.lookback
    )
    all_symbols = list(dict.fromkeys(symbol for members in groups.values() for symbol in members))
    all_symbols.extend(symbol for symbol in spec.benchmarks if symbol not in all_symbols)
    usable = closes.dropna(how="all")
    return {
        "run_date": date.today().isoformat(),
        "spec": asdict(spec),
        "groups": groups,
        "data_sha256": data_hash,
        "data_first_date": usable.index[0].date().isoformat() if len(usable) else None,
        "data_last_date": usable.index[-1].date().isoformat() if len(usable) else None,
        "data_quality": data_quality(closes, all_symbols),
        "analysis": analyse_records(records, spec),
        "primary_episode_analysis": analyse_primary_episodes(records, spec),
        "group_diagnostics": group_diagnostics(records, spec.benchmarks),
        "signal_record_n": len(records),
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--groups-file",
        type=Path,
        default=Path("data/sector_cache/sector_groups.json"),
    )
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument(
        "--end-exclusive",
        default=(date.today() + timedelta(days=1)).isoformat(),
        help="yfinance end date is exclusive",
    )
    parser.add_argument("--horizons", type=parse_int_tuple, default=DEFAULT_HORIZONS)
    parser.add_argument("--benchmarks", type=parse_str_tuple, default=DEFAULT_BENCHMARKS)
    parser.add_argument("--lookback", type=int, default=DEFAULT_LOOKBACK)
    parser.add_argument("--min-days", type=int, default=DEFAULT_MIN_DAYS)
    parser.add_argument("--breadth-threshold", type=float, default=DEFAULT_BREADTH_THRESHOLD)
    parser.add_argument("--return-threshold-pct", type=float, default=DEFAULT_RETURN_THRESHOLD)
    parser.add_argument("--min-coverage", type=float, default=DEFAULT_MIN_COVERAGE)
    parser.add_argument("--cost-bps", type=float, default=DEFAULT_COST_BPS)
    parser.add_argument("--bootstrap-samples", type=int, default=DEFAULT_BOOTSTRAP_SAMPLES)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument(
        "--price-cache",
        type=Path,
        default=Path("data/research/sector_consensus_yfinance_closes.csv.gz"),
        help="ignored local cache for reproducibility; production caches are untouched",
    )
    parser.add_argument(
        "--reuse-price-cache",
        action="store_true",
        help="read --price-cache instead of downloading (for an exact offline rerun)",
    )
    parser.add_argument(
        "--us-listed-only",
        action="store_true",
        help="robustness run excluding symbols with known non-US exchange suffixes",
    )
    parser.add_argument(
        "--repair",
        action="store_true",
        help="yfinance repair=True data-sensitivity run; never replaces the primary cache",
    )
    parser.add_argument(
        "--output-md",
        type=Path,
        default=Path("docs/sector_consensus_yfinance_validation_results.md"),
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        default=Path("docs/sector_consensus_yfinance_validation_results.json"),
    )
    args = parser.parse_args(argv)
    if not 0 < args.min_coverage <= 1:
        parser.error("--min-coverage must be in (0, 1]")
    if args.min_days <= 0 or args.lookback < args.min_days:
        parser.error("lookback must be >= min-days > 0")

    groups = load_groups(args.groups_file)
    if args.us_listed_only:
        groups = {
            name: [symbol for symbol in members if not symbol.endswith(NON_US_SUFFIXES)]
            for name, members in groups.items()
        }
        groups = {name: members for name, members in groups.items() if len(members) >= 3}
    symbols = list(dict.fromkeys(symbol for members in groups.values() for symbol in members))
    symbols.extend(symbol for symbol in args.benchmarks if symbol not in symbols)
    if args.reuse_price_cache:
        print(f"Reading external-history cache {args.price_cache}...", flush=True)
        closes = pd.read_csv(args.price_cache, index_col=0, parse_dates=True)
        closes.columns = [str(column).upper() for column in closes.columns]
        closes = closes.reindex(columns=symbols)
        data_hash = close_frame_hash(closes)
    else:
        print(
            f"Downloading {len(symbols)} symbols from {args.start} to {args.end_exclusive} "
            "with auto-adjusted daily closes...",
            flush=True,
        )
        closes, data_hash = download_adjusted_closes(
            symbols, args.start, args.end_exclusive, repair=args.repair
        )
    if closes.empty or all(closes.get(symbol, pd.Series(dtype=float)).dropna().empty for symbol in args.benchmarks):
        print("No benchmark history downloaded.", file=sys.stderr)
        return 2
    if not args.reuse_price_cache:
        args.price_cache.parent.mkdir(parents=True, exist_ok=True)
        closes.to_csv(args.price_cache, compression="gzip", date_format="%Y-%m-%d")
        print(f"Wrote ignored research cache {args.price_cache}")

    spec = ValidationSpec(
        start=args.start,
        end_exclusive=args.end_exclusive,
        horizons=args.horizons,
        benchmarks=args.benchmarks,
        lookback=args.lookback,
        min_days=args.min_days,
        breadth_threshold=args.breadth_threshold,
        return_threshold_pct=args.return_threshold_pct,
        min_coverage=args.min_coverage,
        cost_bps=args.cost_bps,
        bootstrap_samples=args.bootstrap_samples,
        seed=args.seed,
        repair=args.repair,
    )
    report = run_validation(groups, closes, data_hash, spec)
    args.output_md.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_md.write_text(render_markdown(report), encoding="utf-8")
    args.output_json.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Wrote {args.output_md}")
    print(f"Wrote {args.output_json}")
    for row in report["analysis"]:
        verdict = "PASS" if row["validated"] else "FAIL"
        print(
            f"{row['direction']:>4} +{row['horizon_sessions']:>2} sessions: "
            f"blocks={row['purged_cluster_n']:>3} "
            f"hit={_fmt_pct(row['hit_rate'])} edge={_fmt_pct(row['hit_edge'])} "
            f"{args.benchmarks[0]} net={_fmt_return(row[f'mean_signed_excess_{args.benchmarks[0]}_net_pct'])} "
            f"p={row['p_value'] if row['p_value'] is not None else '—'} {verdict}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
