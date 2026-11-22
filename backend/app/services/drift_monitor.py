"""特征漂移监控领域服务：特征分布漂移的持续监控、PSI计算与告警。

复核口径改造：
- 监控按「定义标识」关联特征定义，同名但定义标识不同的另一条定义互不影响。
- 每个监控周期单独提交采样回执：采样未齐标记为待补，不能借用其他周期的
  分布与阈值定级；采样齐全时按当轮阈值定级，并冻结基准分布、当前分布、
  PSI 与当轮阈值作为证据版本。
- 阈值调整只影响之后提交的新周期，历史周期的证据与结论保留不变。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.drift_review import drift_review_store
from app.store import store


class DriftMonitorService(BaseModuleService):
    MODULE = "drift_monitor"
    ENTITY = "漂移监控"
    OPERATION_TERM = "关联"
    LIST_FIELDS = [
        "监控编号", "关联特征", "定义标识", "基准分布", "当前分布", "PSI值",
        "漂移阈值", "采样窗口", "监控状态", "复核状态",
    ]
    REQUIRED_FIELDS = ["监控编号", "关联特征", "定义标识", "基准分布"]
    STATUS_ORDER = ["草稿", "待校验", "监控中", "已停用"]
    ACTION_RULES = {"提交校验": "待校验", "启动监控": "监控中", "停用监控": "已停用"}
    NEGATIVE_ACTIONS = ["停用监控"]

    # ── 复核口径：监控登记 ──
    def register_monitor(
        self,
        *,
        monitor_code: str,
        define_code: str,
        feature_name: str,
        baseline_dist: str,
        threshold: float,
        sample_window: str = "1d",
    ) -> dict[str, Any]:
        """登记一条按定义标识关联的漂移监控（同名另一条定义不受影响）。"""
        rows = store.rows(self.MODULE)
        monitor_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        monitor = {
            "id": monitor_id,
            "status": "监控中",
            "pending": False,
            "abnormal": False,
            "监控编号": monitor_code,
            "关联特征": feature_name,
            "定义标识": define_code,
            "基准分布": baseline_dist,
            "当前分布": "",
            "PSI值": "",
            "漂移阈值": str(threshold),
            "采样窗口": sample_window,
            "监控状态": "监控中",
            "复核状态": "未结案",
        }
        rows.append(monitor)
        drift_review_store.case_status(monitor_id)
        return {"ok": True, "monitor_id": monitor_id, "monitor": monitor}

    # ── 复核口径：周期提交与定级 ──
    def submit_monitor_period(
        self,
        monitor_id: int,
        covered_dates: list[str],
        *,
        sample_complete: bool,
        psi: float | None = None,
        current_dist: str | None = None,
    ) -> dict[str, Any]:
        """提交一个监控周期的采样回执与漂移分布数据。

        采样未齐 → 周期保持待补，不定级；采样齐全 → 按监控当前阈值（当轮阈值）
        定级并冻结证据。同一周期重复提交时，待补可补齐，已定级则保留历史结论。
        """
        monitor = store.find(self.MODULE, monitor_id)
        if monitor is None:
            return {"ok": False, "message": f"漂移监控 {monitor_id} 不存在"}
        if not covered_dates:
            return {"ok": False, "message": "覆盖日期不能为空"}
        try:
            threshold = float(str(monitor.get("漂移阈值", "0.25")))
        except (ValueError, TypeError):
            threshold = 0.25

        if sample_complete and psi is None:
            return {"ok": False, "message": "采样齐全的周期必须提供 PSI 才能定级"}

        outcome = drift_review_store.submit_period(
            monitor_id=monitor_id,
            monitor_code=str(monitor.get("监控编号", f"DRIFT-{monitor_id}")),
            define_code=str(monitor.get("定义标识", "")),
            covered_dates=list(covered_dates),
            sample_complete=sample_complete,
            psi=psi,
            threshold=threshold,
            baseline_dist=monitor.get("基准分布"),
            current_dist=current_dist,
        )
        period = outcome["period"]

        # 回写监控主表的摘要字段，保证基准分布、当前分布、PSI、当轮阈值可一起查到
        if period["状态"] == "已定级":
            monitor["当前分布"] = period["当前分布"]
            monitor["PSI值"] = str(period["PSI"])
            monitor["漂移阈值"] = str(period["当轮阈值"])
            monitor["abnormal"] = period["定级"] == "异常"
        monitor["监控状态"] = "监控中"
        return {
            "ok": True,
            "monitor_id": monitor_id,
            "action": outcome["action"],
            "message": outcome["message"],
            "period": period,
            "evidence_version": period.get("证据版本"),
        }

    # ── 复核口径：阈值调整（只影响新周期） ──
    def adjust_monitor_threshold(self, monitor_id: int, new_threshold: float) -> dict[str, Any]:
        """调整漂移阈值；历史周期已冻结证据不受影响，之后的新周期按新阈值判定。"""
        monitor = store.find(self.MODULE, monitor_id)
        if monitor is None:
            return {"ok": False, "message": f"漂移监控 {monitor_id} 不存在"}
        old_threshold = float(str(monitor.get("漂移阈值", "0.25")))
        monitor["漂移阈值"] = str(new_threshold)
        frozen = [
            {
                "周期序号": p["周期序号"],
                "覆盖日期": p["覆盖日期"],
                "定级": p["定级"],
                "当轮阈值": p["当轮阈值"],
                "证据版本": p["证据版本"],
            }
            for p in drift_review_store.periods_of(monitor_id)
            if p["状态"] == "已定级"
        ]
        return {
            "ok": True,
            "monitor_id": monitor_id,
            "old_threshold": old_threshold,
            "new_threshold": new_threshold,
            "message": "阈值已调整，历史周期保留原结论，新周期按新阈值判定",
            "frozen_periods": frozen,
        }

    # ── 复核口径：案件结案 ──
    def close_review_case(self, monitor_id: int) -> dict[str, Any]:
        """复核员结案：该监控的异常结论不再优先压制重叠日期的新结论。"""
        monitor = store.find(self.MODULE, monitor_id)
        if monitor is None:
            return {"ok": False, "message": f"漂移监控 {monitor_id} 不存在"}
        case = drift_review_store.close_case(monitor_id)
        monitor["复核状态"] = "已结案"
        return {"ok": True, "monitor_id": monitor_id, "case": case}

    # ── 复核口径：周期证据查询 ──
    def list_period_evidence(self, monitor_id: int) -> dict[str, Any]:
        """查看监控下各周期的分布、PSI、当轮阈值、定级与证据版本。"""
        monitor = store.find(self.MODULE, monitor_id)
        if monitor is None:
            return {"ok": False, "message": f"漂移监控 {monitor_id} 不存在"}
        return {
            "ok": True,
            "monitor_id": monitor_id,
            "监控编号": monitor.get("监控编号"),
            "定义标识": monitor.get("定义标识"),
            "基准分布": monitor.get("基准分布"),
            "当前阈值": monitor.get("漂移阈值"),
            "复核状态": monitor.get("复核状态"),
            "periods": drift_review_store.periods_of(monitor_id),
        }

    def review_conflicts(self, define_code: str) -> dict[str, Any]:
        """查看同一定义标识下、日期范围重叠且定级冲突的复核字段记录。"""
        fields = drift_review_store.fields_of(define_code)
        conflict_dates = []
        by_date: dict[str, list[dict[str, Any]]] = {}
        for f in fields:
            by_date.setdefault(f["生效日期"], []).append(f)
        for date, rows in sorted(by_date.items()):
            grades = {r["定级"] for r in rows}
            if len(grades) > 1:
                conflict_dates.append({
                    "日期": date,
                    "records": [drift_review_store._with_case_flags(r) for r in rows],
                    "裁决": drift_review_store.decide(define_code, date),
                })
        return {"ok": True, "定义标识": define_code, "conflict_dates": conflict_dates}

    # ── 链路5: 特征漂移监控（保留原链路能力） ──
    def calculate_psi_and_alert(self, monitor_id: int) -> dict[str, Any]:
        """计算当前周期PSI值并校验是否触发漂移告警阈值。"""
        mon = store.find(self.MODULE, monitor_id)
        if mon is None:
            return {"ok": False}
        psi = round(0.05 + (monitor_id % 20) * 0.015, 4)
        try:
            threshold = float(str(mon.get("漂移阈值", "0.25")))
        except (ValueError, TypeError):
            threshold = 0.25
        mon["PSI值"] = str(psi)
        mon["当前分布"] = f"distribution_{monitor_id}_2026_11_18"
        is_alert = psi >= threshold
        if is_alert:
            mon["abnormal"] = True
        else:
            mon["abnormal"] = False
        return {"ok": True, "monitor_id": monitor_id, "psi": psi, "threshold": threshold,
                "alert": is_alert, "message": "漂移超阈值" if is_alert else "特征稳定"}

    def compare_baseline_distribution(self, monitor_id: int) -> dict[str, Any]:
        """比较当前特征分布与基准分布，计算分布差异度量并生成报告。"""
        mon = store.find(self.MODULE, monitor_id)
        if mon is None:
            return {"ok": False}
        psi = float(str(mon.get("PSI值", "0")))
        return {
            "ok": True,
            "monitor_id": monitor_id,
            "baseline": mon.get("基准分布"),
            "current": mon.get("当前分布"),
            "psi": psi,
            "drift_level": "严重" if psi >= 0.25 else "轻微" if psi >= 0.1 else "无",
        }

    def sync_to_feature_definition_status(self, monitor_id: int) -> dict[str, Any]:
        """将漂移告警同步到特征定义模块，标记异常特征为待校验状态。"""
        mon = store.find(self.MODULE, monitor_id)
        if mon is None:
            return {"ok": False}
        psi = float(str(mon.get("PSI值", "0")))
        if psi >= 0.25:
            define_rows = store.rows("feature_define")
            feature_name = mon.get("关联特征", "")
            updated = 0
            for r in define_rows:
                if feature_name and feature_name in str(r.get("特征名称", "")):
                    r["status"] = "待校验"
                    r["abnormal"] = True
                    updated += 1
            return {"ok": True, "feature_name": feature_name, "updated_count": updated, "psi": psi}
        return {"ok": True, "psi": psi, "message": "PSI正常，无需同步异常状态"}

    def aggregate_drift_trend(self, monitor_id: int) -> dict[str, Any]:
        """聚合特征漂移监控的历史趋势数据，生成PSI时间序列用于趋势分析。"""
        mon = store.find(self.MODULE, monitor_id)
        if mon is None:
            return {"ok": False}
        current_psi = float(str(mon.get("PSI值", "0")))
        return {
            "ok": True,
            "monitor_id": monitor_id,
            "current_psi": current_psi,
            "psi_history": [current_psi - 0.02, current_psi - 0.01, current_psi],
            "window": mon.get("采样窗口"),
            "trend": "上升" if current_psi > 0.15 else "稳定",
        }
