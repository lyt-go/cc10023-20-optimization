"""漂移复核领域：周期证据冻结、按特征和日期生效的复核字段、计算准入裁决。

设计要点：
- 监控周期按 (监控id, 覆盖日期) 唯一识别，支持重复提交；采样未齐只能是「待补」，
  任何其他周期的分布与阈值都不能拿来给它定级。
- 采样齐全的周期在定级瞬间冻结基准分布、当前分布、PSI 与当轮阈值，分配证据版本；
  阈值后续调整只影响之后的新周期，历史周期保留原结论。
- 复核字段按 (定义标识, 生效日期) 生效，同名但定义标识不同的特征定义互不影响。
- 日期范围重叠导致多轮结论冲突时，未结案的异常定级优先（取最早一轮未结案异常），
  全部结案后才由最新证据版本接管；裁决依据随准入结论一起留痕。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

# 定级 → 准入结论
GRADE_ADMISSION = {"异常": "限制计算", "正常": "允许计算"}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class DriftReviewStore:
    """漂移复核的内存台账：周期证据、复核字段、案件结案状态与证据版本序列。"""

    def __init__(self) -> None:
        # (monitor_id, period_key) -> 周期记录
        self._periods: dict[tuple[int, tuple[str, ...]], dict[str, Any]] = {}
        # 复核字段结果（按特征+日期生效，允许同一日期存在多轮冲突记录）
        self._fields: list[dict[str, Any]] = []
        # 监控案件状态：monitor_id -> {"closed": bool, "closed_at": str|None}
        self._cases: dict[int, dict[str, Any]] = {}
        self._evidence_seq = 0
        self._field_seq = 0

    def reset(self) -> None:
        self._periods.clear()
        self._fields.clear()
        self._cases.clear()
        self._evidence_seq = 0
        self._field_seq = 0

    # ── 监控案件 ──
    def case_status(self, monitor_id: int) -> dict[str, Any]:
        return self._cases.setdefault(monitor_id, {"closed": False, "closed_at": None})

    def is_case_closed(self, monitor_id: int) -> bool:
        return bool(self.case_status(monitor_id)["closed"])

    def close_case(self, monitor_id: int) -> dict[str, Any]:
        case = self.case_status(monitor_id)
        if not case["closed"]:
            case["closed"] = True
            case["closed_at"] = _now()
        return case

    # ── 监控周期 ──
    @staticmethod
    def _period_key(covered_dates: list[str]) -> tuple[str, ...]:
        return tuple(sorted(covered_dates))

    def get_period(self, monitor_id: int, covered_dates: list[str]) -> dict[str, Any] | None:
        return self._periods.get((monitor_id, self._period_key(covered_dates)))

    def periods_of(self, monitor_id: int) -> list[dict[str, Any]]:
        rows = [p for p in self._periods.values() if p["monitor_id"] == monitor_id]
        return sorted(rows, key=lambda p: p["周期序号"])

    def pending_periods(self, define_code: str, date: str | None = None) -> list[dict[str, Any]]:
        rows = [
            p for p in self._periods.values()
            if p["定义标识"] == define_code and p["状态"] == "待补"
        ]
        if date is not None:
            rows = [p for p in rows if date in p["覆盖日期"]]
        return sorted(rows, key=lambda p: (p["monitor_id"], p["周期序号"]))

    def submit_period(
        self,
        *,
        monitor_id: int,
        monitor_code: str,
        define_code: str,
        covered_dates: list[str],
        sample_complete: bool,
        psi: float | None,
        threshold: float,
        baseline_dist: str | None,
        current_dist: str | None,
    ) -> dict[str, Any]:
        """登记或重复提交一个监控周期，返回周期记录与本次处理动作说明。"""
        key = self._period_key(covered_dates)
        existing = self._periods.get((monitor_id, key))

        if existing is not None:
            existing["提交次数"] += 1
            # 已定级周期属于历史结论：无论再次提交什么数据都保持原结论不变
            if existing["状态"] == "已定级":
                existing["最近提交时间"] = _now()
                return {
                    "period": existing,
                    "action": "frozen",
                    "message": "该周期已按历史阈值定级，重复提交不改变原结论",
                }
            # 仍是待补：继续缺样则继续待补，不能借其他周期的数据定级
            if not sample_complete:
                existing["最近提交时间"] = _now()
                return {
                    "period": existing,
                    "action": "still_pending",
                    "message": "采样回执仍未齐全，周期保持待补",
                }
            # 待补 → 补齐，使用当轮阈值一次性定级并冻结证据
            return self._grade_period(
                existing, psi=psi, threshold=threshold,
                baseline_dist=baseline_dist, current_dist=current_dist,
            )

        seq = len(self.periods_of(monitor_id)) + 1
        period = {
            "monitor_id": monitor_id,
            "监控编号": monitor_code,
            "定义标识": define_code,
            "period_key": key,
            "周期序号": seq,
            "覆盖日期": list(key),
            "采样齐全": bool(sample_complete),
            "提交次数": 1,
            "状态": "待补",
            "定级": None,
            "基准分布": None,
            "当前分布": None,
            "PSI": None,
            "当轮阈值": None,
            "证据版本": None,
            "首次提交时间": _now(),
            "最近提交时间": _now(),
            "定级时间": None,
        }
        self._periods[(monitor_id, key)] = period
        if not sample_complete:
            return {
                "period": period,
                "action": "pending_created",
                "message": "采样回执未齐全，周期标记为待补，暂不定级",
            }
        return self._grade_period(
            period, psi=psi, threshold=threshold,
            baseline_dist=baseline_dist, current_dist=current_dist,
        )

    def _grade_period(
        self,
        period: dict[str, Any],
        *,
        psi: float | None,
        threshold: float,
        baseline_dist: str | None,
        current_dist: str | None,
    ) -> dict[str, Any]:
        if psi is None:
            raise ValueError("采样齐全的周期必须提供 PSI 才能定级")
        self._evidence_seq += 1
        evidence_version = f"EV-{self._evidence_seq:04d}"
        grade = "异常" if psi >= threshold else "正常"
        period.update({
            "采样齐全": True,
            "状态": "已定级",
            "定级": grade,
            "基准分布": baseline_dist,
            "当前分布": current_dist,
            "PSI": round(float(psi), 4),
            "当轮阈值": round(float(threshold), 4),
            "证据版本": evidence_version,
            "定级时间": _now(),
        })
        # 案件随首轮定级打开，等待复核员结案
        self.case_status(period["monitor_id"])
        new_fields = []
        for date in period["覆盖日期"]:
            self._field_seq += 1
            field = {
                "id": self._field_seq,
                "定义标识": period["定义标识"],
                "生效日期": date,
                "监控id": period["monitor_id"],
                "监控编号": period["监控编号"],
                "周期序号": period["周期序号"],
                "定级": grade,
                "准入结论": GRADE_ADMISSION[grade],
                "基准分布": baseline_dist,
                "当前分布": current_dist,
                "PSI": period["PSI"],
                "当轮阈值": period["当轮阈值"],
                "证据版本": evidence_version,
                "定级时间": period["定级时间"],
            }
            self._fields.append(field)
            new_fields.append(field)
        return {
            "period": period,
            "action": "graded",
            "message": f"采样已齐，按当轮阈值 {threshold:g} 定级为「{grade}」，证据已冻结",
            "evidence_version": evidence_version,
            "new_fields": new_fields,
        }

    # ── 复核字段 ──
    def fields_of(self, define_code: str, date: str | None = None) -> list[dict[str, Any]]:
        rows = [f for f in self._fields if f["定义标识"] == define_code]
        if date is not None:
            rows = [f for f in rows if f["生效日期"] == date]
        return sorted(rows, key=lambda f: (f["生效日期"], f["监控id"], f["周期序号"], f["id"]))

    def _with_case_flags(self, field: dict[str, Any]) -> dict[str, Any]:
        view = dict(field)
        view["案件结案"] = self.is_case_closed(field["监控id"])
        view["结案状态"] = "已结案" if view["案件结案"] else "未结案"
        return view

    # ── 按日裁决 ──
    def decide(self, define_code: str, date: str) -> dict[str, Any]:
        """对某个定义标识的某一天做出计算准入裁决，返回全部候选证据与裁决依据。"""
        candidates = [self._with_case_flags(f) for f in self.fields_of(define_code, date)]
        pending = [dict(p) for p in self.pending_periods(define_code, date)]

        if not candidates:
            if pending:
                names = "、".join(
                    f"{p['监控编号']}第{p['周期序号']}周期" for p in pending
                )
                reason = f"覆盖该日期的监控周期采样未齐（{names}），按待补处理，暂无可定级证据"
            else:
                reason = "该日期没有已定级的漂移监控周期覆盖，缺少漂移复核证据"
            return {
                "定义标识": define_code,
                "日期": date,
                "decision": "拒绝",
                "准入结论": "拒绝计算",
                "basis": None,
                "candidates": [],
                "pending_periods": pending,
                "reason": reason,
            }

        open_abnormal = [
            f for f in candidates
            if f["定级"] == "异常" and not f["案件结案"]
        ]
        if open_abnormal:
            # 未结案异常优先；多轮都未结案时最早一轮（监控id、周期序号最小）负责
            basis = min(open_abnormal, key=lambda f: (f["监控id"], f["周期序号"]))
            reason = (
                f"{basis['监控编号']}第{basis['周期序号']}周期定级异常且复核案件尚未结案"
                f"（PSI {basis['PSI']} ≥ 当轮阈值 {basis['当轮阈值']}，证据版本 {basis['证据版本']}），"
                "异常结论仍限制计算"
            )
            decision = "拒绝"
        else:
            # 案件均已结案，由最新证据版本接管
            basis = max(candidates, key=lambda f: f["证据版本"])
            if basis["定级"] == "正常":
                decision = "允许"
                reason = (
                    f"采用最新证据 {basis['证据版本']}（{basis['监控编号']}第{basis['周期序号']}周期，"
                    f"PSI {basis['PSI']} < 当轮阈值 {basis['当轮阈值']}），定级正常"
                )
            else:
                decision = "拒绝"
                reason = (
                    f"最新证据 {basis['证据版本']} 定级为异常"
                    f"（PSI {basis['PSI']} ≥ 当轮阈值 {basis['当轮阈值']}），限制计算"
                )

        for f in candidates:
            f["governing"] = (
                f["监控id"] == basis["监控id"]
                and f["周期序号"] == basis["周期序号"]
                and f["证据版本"] == basis["证据版本"]
            )
            if f["governing"]:
                f["冲突说明"] = "实际采用的裁决依据"
            elif f["定级"] != basis["定级"]:
                f["冲突说明"] = (
                    f"与裁决依据结论冲突：本记录为「{f['定级']}」，"
                    f"因{'案件未结案的更早异常优先' if decision == '拒绝' else '最新证据版本优先'}未被采用"
                )
            else:
                f["冲突说明"] = "同结论历史证据，未作为本轮裁决依据"

        return {
            "定义标识": define_code,
            "日期": date,
            "decision": decision,
            "准入结论": "允许计算" if decision == "允许" else "限制计算",
            "basis": basis,
            "candidates": candidates,
            "pending_periods": pending,
            "reason": reason,
        }

    def catalogue(self, define_code: str, date: str | None = None) -> list[dict[str, Any]]:
        """生成特征定义目录可读取的漂移复核字段结果（可按日期过滤）。"""
        dated = sorted({f["生效日期"] for f in self.fields_of(define_code)}
                       | {d for p in self.pending_periods(define_code) for d in p["覆盖日期"]})
        if date is not None:
            dated = [d for d in dated if d == date]
        result = []
        for d in dated:
            verdict = self.decide(define_code, d)
            basis = verdict["basis"]
            result.append({
                "定义标识": define_code,
                "生效日期": d,
                "准入结论": verdict["准入结论"],
                "裁决依据": None if basis is None else {
                    "监控编号": basis["监控编号"],
                    "周期序号": basis["周期序号"],
                    "定级": basis["定级"],
                    "PSI": basis["PSI"],
                    "当轮阈值": basis["当轮阈值"],
                    "证据版本": basis["证据版本"],
                    "结案状态": basis["结案状态"],
                },
                "说明": verdict["reason"],
                "候选复核字段": verdict["candidates"],
                "待补周期": verdict["pending_periods"],
            })
        return result


# 全局单例：与内存 store 同生命周期
drift_review_store = DriftReviewStore()
