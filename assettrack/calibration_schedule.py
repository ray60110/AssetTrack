"""
assettrack/calibration_schedule.py — 每雙週/每週自動重算校準（提案需使用者確認才套用）

bug#00095（使用者需求：「具備每雙週/每週修正投資建議設定的能力」；決策：「自動排程重算
並顯示，參數改動需你確認」）。

設計要點：
  • 「重算＋顯示」是持續的（每次背景刷新，回測本就重算並顯示，成本低且有快取）。
  • 「提出需你確認的參數調整」是排程事件，預設每雙週（14 天）——因為回測前瞻期最長
    10 天，一週的新訊號多半尚未結算，且有效樣本受自相關拖累，每週重調多屬雜訊；雙週
    看到的已結算證據約翻倍，提案才有意義。可改每週（7）。
  • 每個提案一律用 bug#00094 的統計驗證把關：只有在「樣本充足且未顯著優於基準」時才
    建議收緊門檻；「已顯著有效且門檻高於預設」時才建議放寬回預設；證據不足一律不動。
  • AI/系統只會把調整放進 pending，**永不自行套用**。
  • bug#00129 起本模組為**唯讀**：狀態、歷史與建議照常顯示，但不再寫入
    active_params。參數變更一律走 Experiment 的 Candidate → Replay → Shadow
    → Promotion Proposal，避免兩個控制器搶同一個旋鈕（設計文件 §4）。

狀態檔：data/{user}_calibration.json
  { active_params, last_calibrated, cadence_days, pending, history }

純離線、零網路。只讀寫本機 JSON。
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

CADENCE_WEEKLY = 7
CADENCE_BIWEEKLY = 14
DEFAULT_CADENCE_DAYS = CADENCE_BIWEEKLY

# 可調參數規格：每個都是既有推薦函式已接受的 kwarg，tighten 指「更嚴格/更保守」的方向。
PARAM_SPEC: dict[str, dict[str, dict]] = {
    "etf": {
        "consensus_threshold": {"default": 0.5, "min": 0.5, "max": 0.8, "step": 0.05,
                                "tighten": "up", "label": "ETF 多數性一致門檻"},
        "min_etfs_evaluated": {"default": 4, "min": 3, "max": 8, "step": 1,
                               "tighten": "up", "label": "ETF 最少評估檔數"},
    },
    "sector": {
        "breadth_threshold": {"default": 0.5, "min": 0.5, "max": 0.8, "step": 0.05,
                              "tighten": "up", "label": "類股廣度門檻"},
        "min_days": {"default": 3, "min": 3, "max": 5, "step": 1,
                     "tighten": "up", "label": "類股持續天數"},
    },
    "options": {
        # 百分比單位：0.03 代表現價的 0.03%。模型失效時只允許提高門檻、先減少
        # 弱訊號；新模型／放寬仍須樣本外驗證與使用者確認。
        "bias_min_pct": {
            "default": 0.03,
            "min": 0.03,
            "max": 0.15,
            "step": 0.02,
            "tighten": "up",
            "label": "期權 IV 重定價殘差門檻（現價%）",
        },
    },
}

# 每族群「主要」選擇性參數（收緊時優先動它）。
_PRIMARY_PARAM = {
    "etf": "consensus_threshold",
    "sector": "breadth_threshold",
    "options": "bias_min_pct",
}

_READY_MIN_N = 20  # 樣本充足門檻（與各回測 min_signals 一致），未達不提案


def default_params() -> dict:
    return {fam: {p: spec["default"] for p, spec in params.items()}
            for fam, params in PARAM_SPEC.items()}


def _round_param(fam: str, param: str, value: float):
    step = PARAM_SPEC[fam][param]["step"]
    # 整數步進參數回整數，浮點步進參數對齊到步長避免累積誤差
    if float(step).is_integer() and float(PARAM_SPEC[fam][param]["default"]).is_integer():
        return int(round(value))
    return round(value, 4)


# ── State persistence ─────────────────────────────────────────────────────────

def _state_path(user: str) -> Path:
    from .storage import get_data_dir
    safe = (user or "default").replace("/", "_")
    return get_data_dir() / f"{safe}_calibration.json"


def ensure_state(user: str) -> dict:
    """Load the user's calibration state, initialising with defaults on first run."""
    p = _state_path(user)
    if p.exists():
        try:
            state = json.loads(p.read_text())
            if isinstance(state, dict) and "active_params" in state:
                # backfill any newly-added params without clobbering user-applied values
                dp = default_params()
                for fam, params in dp.items():
                    state["active_params"].setdefault(fam, {})
                    for k, v in params.items():
                        state["active_params"][fam].setdefault(k, v)
                state.setdefault("cadence_days", DEFAULT_CADENCE_DAYS)
                state.setdefault("pending", None)
                state.setdefault("history", [])
                state.setdefault("last_calibrated", None)
                state.setdefault("last_health_intervention", {})
                return state
        except Exception:
            pass
    state = {
        "active_params": default_params(),
        "last_calibrated": None,
        "cadence_days": DEFAULT_CADENCE_DAYS,
        "pending": None,
        "history": [],
        "last_health_intervention": {},
    }
    save_calibration_state(user, state)
    return state


def save_calibration_state(user: str, state: dict) -> None:
    try:
        _state_path(user).write_text(json.dumps(state, ensure_ascii=False, indent=2))
    except Exception:
        pass


def set_cadence(user: str, cadence_days: int) -> dict:
    state = ensure_state(user)
    state["cadence_days"] = int(cadence_days)
    save_calibration_state(user, state)
    return state


# ── Scheduling ────────────────────────────────────────────────────────────────

def _parse_date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def due_for_recalibration(state: dict, today: date) -> bool:
    last = _parse_date(state.get("last_calibrated"))
    if last is None:
        return True
    return (today - last).days >= int(state.get("cadence_days", DEFAULT_CADENCE_DAYS))


def days_until_next(state: dict, today: date) -> Optional[int]:
    last = _parse_date(state.get("last_calibrated"))
    if last is None:
        return 0
    nxt = last + timedelta(days=int(state.get("cadence_days", DEFAULT_CADENCE_DAYS)))
    return max(0, (nxt - today).days)


# ── Proposal logic (significance-gated) ───────────────────────────────────────

def _best_direction_sig(report: dict) -> Optional[dict]:
    """Pick the (direction, horizon) significance dict with the most samples."""
    best = None
    for st in (report or {}).get("by_horizon", {}).values():
        for d in ("up", "down"):
            sig = (st.get("significance") or {}).get(d)
            if sig and (best is None or sig["n"] > best["n"]):
                best = sig
    return best


def _clamp(fam: str, param: str, value):
    spec = PARAM_SPEC[fam][param]
    return max(spec["min"], min(spec["max"], value))


def propose_adjustments(active_params: dict, backtests: dict) -> list[dict]:
    """Given current params + each family's backtest report (with significance
    attached), return a conservative, significance-gated list of proposed changes.
    Empty list = nothing warrants changing this cycle (the honest common case)."""
    proposals: list[dict] = []
    for fam in PARAM_SPEC:
        report = backtests.get(fam)
        health = (report or {}).get("model_health") or {}
        primary = _PRIMARY_PARAM[fam]
        cur = active_params.get(fam, {}).get(primary, PARAM_SPEC[fam][primary]["default"])
        spec = PARAM_SPEC[fam][primary]

        # 已成熟的近期結果出現連續失配時，立即開始「安全修正」：只建立待確認的收緊
        # 提案，不自動把候選模型升為正式模型。這條介入不必等雙週顯著性報告，但
        # degraded 本身已要求達到 health protocol 的最少獨立 sessions 且連續失配。
        if health.get("status") == "degraded":
            new = _round_param(fam, primary, _clamp(fam, primary, cur + spec["step"]))
            if new != cur:
                proposals.append({
                    "family": fam,
                    "param": primary,
                    "label": spec["label"],
                    "from": cur,
                    "to": new,
                    "action": "tighten",
                    "rationale": (
                        f"模型健康度已降為 degraded：{health.get('reason', '近期預測失配')}。"
                        "先提高門檻抑制弱訊號；需你確認後才套用。"
                    ),
                    "evidence": {
                        "model_health": health,
                        "recent_n": health.get("recent_n"),
                        "recent_hit_rate": health.get("recent_hit_rate"),
                    },
                })
            continue

        sig = _best_direction_sig(report) if report else None
        if sig is None or sig["n"] < _READY_MIN_N:
            continue  # 樣本不足，維持現行設定（不提案）

        evidence = {
            "n": sig["n"], "hit_rate": round(sig["hit_rate"], 3),
            "baseline_rate": round(sig["baseline_rate"], 3),
            "p_value": round(sig["p_value"], 4),
            "significant_adj": sig["significant_adj"],
            "ci": [round(sig["ci_lo"], 3), round(sig["ci_hi"], 3)],
        }
        stab = (report or {}).get("stability") or {}

        if not sig["significant_adj"]:
            # 樣本充足但未顯著優於基準 → 收緊門檻更嚴格篩選
            new = _clamp(fam, primary, cur + spec["step"])
            new = _round_param(fam, primary, new)
            if new != cur:
                proposals.append({
                    "family": fam, "param": primary, "label": spec["label"],
                    "from": cur, "to": new, "action": "tighten",
                    "rationale": (f"近期回測 n={sig['n']}、命中率 {sig['hit_rate']*100:.0f}% "
                                  f"未顯著優於基準 {sig['baseline_rate']*100:.0f}%"
                                  f"（p={sig['p_value']:.3f}），建議提高門檻更嚴格篩選訊號。"),
                    "evidence": evidence,
                })
        else:
            # 已顯著有效：若前後子區間不一致，仍偏保守收緊；否則若門檻高於預設，放寬一步回預設
            if stab.get("consistent") is False:
                new = _clamp(fam, primary, cur + spec["step"])
                new = _round_param(fam, primary, new)
                if new != cur:
                    proposals.append({
                        "family": fam, "param": primary, "label": spec["label"],
                        "from": cur, "to": new, "action": "tighten",
                        "rationale": (f"訊號整體顯著（p={sig['p_value']:.3f}）但前後子區間不一致"
                                      f"（{stab.get('early_rate',0)*100:.0f}% vs "
                                      f"{stab.get('late_rate',0)*100:.0f}%），保守起見提高門檻。"),
                        "evidence": {**evidence, "stability": stab},
                    })
            elif cur > spec["default"]:
                new = _clamp(fam, primary, cur - spec["step"])
                new = _round_param(fam, primary, new)
                if new != cur:
                    proposals.append({
                        "family": fam, "param": primary, "label": spec["label"],
                        "from": cur, "to": new, "action": "loosen",
                        "rationale": (f"訊號顯著優於基準（命中率 {sig['hit_rate']*100:.0f}% "
                                      f"vs 基準 {sig['baseline_rate']*100:.0f}%，p={sig['p_value']:.3f}）"
                                      f"且前後穩定，建議放寬門檻回接近預設以擷取更多機會。"),
                        "evidence": {**evidence, "stability": stab},
                    })
        # 顯著有效且門檻已在預設 → 不動（維持現行）。
    return proposals


def run_recalibration(user: str, backtests: dict, today: date,
                      force: bool = False) -> dict:
    """If due (or forced), recompute a proposal and store it as pending (NOT applied),
    stamping last_calibrated. Returns the updated state. Idempotent within a cycle."""
    state = ensure_state(user)
    health_fingerprints = {}
    for family, report in (backtests or {}).items():
        health = (report or {}).get("model_health") or {}
        if health.get("status") != "degraded":
            continue
        # A new intervention requires new settled evidence, not merely another
        # 30-minute dashboard refresh with the same failed sample.
        fingerprint_payload = {
            "horizon": health.get("horizon"),
            "recent_n": health.get("recent_n"),
            "recent_hit_rate": health.get("recent_hit_rate"),
            "miss_streak": health.get("miss_streak"),
            "by_direction": health.get("by_direction"),
        }
        health_fingerprints[family] = json.dumps(
            fingerprint_payload,
            ensure_ascii=False,
            sort_keys=True,
        )
    prior_health = state.setdefault("last_health_intervention", {})
    new_degraded_evidence = any(
        prior_health.get(family) != fingerprint
        for family, fingerprint in health_fingerprints.items()
    )
    scheduled = force or due_for_recalibration(state, today)
    if not scheduled and not new_degraded_evidence:
        return state
    proposal_backtests = (
        backtests
        if scheduled
        else {
            family: backtests.get(family)
            for family, fingerprint in health_fingerprints.items()
            if prior_health.get(family) != fingerprint
        }
    )
    proposals = propose_adjustments(state["active_params"], proposal_backtests)
    if not scheduled and (state.get("pending") or {}).get("changes"):
        # Immediate health intervention must not erase a regular proposal the
        # user has not reviewed yet. Replace only the same family/parameter.
        merged = {
            (change["family"], change["param"]): change
            for change in state["pending"]["changes"]
        }
        merged.update({
            (change["family"], change["param"]): change
            for change in proposals
        })
        proposals = list(merged.values())
    state["pending"] = {
        "computed_at": today.strftime("%Y-%m-%d"),
        "changes": proposals,
    } if proposals else None
    if scheduled:
        state["last_calibrated"] = today.strftime("%Y-%m-%d")
    for family, fingerprint in health_fingerprints.items():
        prior_health[family] = fingerprint
    if not proposals:
        state["history"].append({"at": today.strftime("%Y-%m-%d"),
                                 "event": "recalibrated", "result": "no_change"})
    save_calibration_state(user, state)
    return state


class CalibrationReadOnlyError(RuntimeError):
    """The legacy calibration track no longer writes active parameters."""


def apply_pending(user: str, today: Optional[date] = None) -> dict:
    """Refuse: parameter changes now belong to the Experiment feedback loop.

    Two independent controllers used to be able to move the same knobs.  This
    one tuned `consensus_threshold`, `breadth_threshold` and `min_days` from the
    legacy backtest reports with a significance check but no Replay, Shadow or
    negative controls; the Experiment track tunes the same three through
    Candidate -> Replay -> Shadow -> Promotion Gate.  Neither could see the
    other's cooldown, so they could push a threshold back and forth, and every
    legacy apply minted a new Policy Version that reset the Forecast Ledger's
    sample count for that family.

    Per §14.4 the legacy track therefore becomes read-only: its state, history
    and suggestions stay visible, but only the Experiment track may change what
    the recommendation policies actually use.  Raising rather than silently
    doing nothing is deliberate — a caller that still expects to write should
    fail loudly instead of appearing to succeed.
    """
    raise CalibrationReadOnlyError(
        "legacy calibration is read-only; parameter changes go through the "
        "Experiment Promotion Proposal flow"
    )


def _apply_pending_disabled(user: str, today: Optional[date] = None) -> dict:
    state = ensure_state(user)
    pending = state.get("pending")
    if not pending or not pending.get("changes"):
        return state
    stamp = (today.strftime("%Y-%m-%d") if today else pending.get("computed_at"))
    for ch in pending["changes"]:
        fam, param = ch["family"], ch["param"]
        state["active_params"].setdefault(fam, {})[param] = ch["to"]
    state["history"].append({"at": stamp, "event": "applied",
                             "changes": pending["changes"]})
    state["pending"] = None
    save_calibration_state(user, state)
    return state


def dismiss_pending(user: str, today: Optional[date] = None) -> dict:
    """User rejected the pending proposal: clear it and log the dismissal."""
    state = ensure_state(user)
    pending = state.get("pending")
    if pending:
        stamp = (today.strftime("%Y-%m-%d") if today else pending.get("computed_at"))
        state["history"].append({"at": stamp, "event": "dismissed",
                                 "changes": pending.get("changes", [])})
    state["pending"] = None
    save_calibration_state(user, state)
    return state


# ── Display helpers ───────────────────────────────────────────────────────────

def cadence_label(days: int) -> str:
    return {CADENCE_WEEKLY: "每週", CADENCE_BIWEEKLY: "每雙週"}.get(int(days), f"每 {days} 天")


def format_status(state: dict, today: date) -> str:
    last = state.get("last_calibrated") or "尚未校準"
    cad = cadence_label(state.get("cadence_days", DEFAULT_CADENCE_DAYS))
    left = days_until_next(state, today)
    nxt = f"下次 {left} 天後" if not due_for_recalibration(state, today) else "本次已到期"
    n_pending = len((state.get("pending") or {}).get("changes", []))
    tail = f"｜待你確認 {n_pending} 項調整" if n_pending else "｜目前無待確認調整"
    return f"校準（{cad}）：上次 {last}｜{nxt}{tail}"


def format_proposal(state: dict) -> list[str]:
    """Human-readable lines describing the pending proposal (for the confirm UI)."""
    pending = state.get("pending") or {}
    changes = pending.get("changes", [])
    if not changes:
        return []
    lines = [f"📅 {pending.get('computed_at','')} 校準建議（需你確認才會套用）："]
    for ch in changes:
        arrow = "🔼 收緊" if ch["action"] == "tighten" else "🔽 放寬"
        lines.append(f"  {arrow} {ch['label']}：{ch['from']} → {ch['to']}")
        lines.append(f"      理由：{ch['rationale']}")
    return lines
