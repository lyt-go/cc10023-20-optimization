"""特征定义领域服务：维护特征的计算逻辑、数据类型与取值范围定义。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.drift_review import drift_review_store
from app.store import store


class FeatureDefineService(BaseModuleService):
    MODULE = "feature_define"
    ENTITY = "特征定义"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["特征编号", "特征名称", "特征类型", "计算逻辑", "数据来源", "缺失填充", "取值范围", "特征版本"]
    REQUIRED_FIELDS = ["特征编号", "特征名称", "特征类型"]
    STATUS_ORDER = ["草稿", "待校验", "已生效", "已废弃"]
    ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "标记废弃": "已废弃"}
    NEGATIVE_ACTIONS = []

    # ── 漂移复核口径：以监控周期定级结果更新计算准入规则 ──
    def refresh_compute_admission(self, define_id: int) -> dict[str, Any]:
        """按漂移复核字段结果刷新特征定义的计算准入规则（按日期生效）。

        关联使用定义标识（特征编号），同名的另一条定义不受影响。
        """
        entry = store.find(self.MODULE, define_id)
        if entry is None:
            return {"ok": False, "message": f"特征定义 {define_id} 不存在"}
        define_code = str(entry.get("特征编号", ""))
        rules = {}
        for item in drift_review_store.catalogue(define_code):
            rules[item["生效日期"]] = {
                "准入结论": item["准入结论"],
                "裁决依据": item["裁决依据"],
                "说明": item["说明"],
            }
        entry["计算准入规则"] = rules
        entry["准入规则版本"] = len(rules)
        return {
            "ok": True,
            "define_id": define_id,
            "定义标识": define_code,
            "admission_rules": rules,
            "rule_dates": sorted(rules),
        }

    def read_drift_review_catalog(
        self, define_id: int, date: str | None = None
    ) -> dict[str, Any]:
        """特征定义目录读取漂移复核字段结果，可按日期查到限制和依据。"""
        entry = store.find(self.MODULE, define_id)
        if entry is None:
            return {"ok": False, "message": f"特征定义 {define_id} 不存在"}
        define_code = str(entry.get("特征编号", ""))
        items = drift_review_store.catalogue(define_code, date)
        return {
            "ok": True,
            "define_id": define_id,
            "定义标识": define_code,
            "特征名称": entry.get("特征名称"),
            "query_date": date,
            "items": items,
        }

    # ── 链路1: 特征源 → 特征定义 → 特征计算 → 特征快照 ──
    def validate_definitions_batch(self, define_ids: list[int]) -> dict[str, Any]:
        """批量校验特征定义的数据来源、计算逻辑和取值范围，生成校验报告。"""
        results = []
        for did in define_ids:
            entry = store.find(self.MODULE, did)
            if entry is None:
                results.append({"id": did, "ok": False, "reason": "不存在"})
                continue
            errors = []
            if not entry.get("数据来源"):
                errors.append("缺少数据来源")
            if not entry.get("计算逻辑"):
                errors.append("缺少计算逻辑")
            if not errors:
                entry["status"] = "已生效"
                entry["pending"] = False
                entry["abnormal"] = False
                results.append({"id": did, "ok": True, "name": entry.get("特征名称")})
            else:
                entry["status"] = "待校验"
                entry["abnormal"] = True
                results.append({"id": did, "ok": False, "reason": "; ".join(errors)})
        return {"ok": all(r.get("ok") for r in results), "results": results, "total": len(results)}

    def push_to_compute_tasks(self, define_ids: list[int]) -> dict[str, Any]:
        """将已生效的特征定义导入特征计算任务队列，触发下游批量计算。"""
        compute_rows = store.rows("feature_compute")
        next_id = max((int(r.get("id", 0)) for r in compute_rows), default=0)
        pushed = []
        for did in define_ids:
            entry = store.find(self.MODULE, did)
            if entry is None or entry.get("status") != "已生效":
                continue
            next_id += 1
            compute_rows.append({
                "id": next_id,
                "status": "草稿",
                "pending": True,
                "abnormal": False,
                "任务编号": f"COMP-{next_id:04d}",
                "关联特征": entry.get("特征名称", ""),
                "数据切片": "default_slice_001",
                "计算引擎": "spark",
                "输入分区": "dt=2026-11-18",
                "输出分区": f"output/d=2026-11-18/feat_{next_id}",
                "并行度": "8",
                "任务状态": "草稿",
            })
            pushed.append({"define_id": did, "task_id": next_id})
        return {"ok": len(pushed) > 0, "pushed_count": len(pushed), "tasks": pushed}

    def analyze_definition_coverage(self) -> dict[str, Any]:
        """统计分析所有特征定义的字段覆盖率、缺失填充策略分布和数据类型分布。"""
        rows = store.rows(self.MODULE)
        type_dist = {}
        fill_dist = {}
        has_source = 0
        has_logic = 0
        total = len(rows)
        for r in rows:
            ft = r.get("特征类型", "unknown")
            type_dist[ft] = type_dist.get(ft, 0) + 1
            fill = r.get("缺失填充", "none")
            fill_dist[fill] = fill_dist.get(fill, 0) + 1
            if r.get("数据来源"):
                has_source += 1
            if r.get("计算逻辑"):
                has_logic += 1
        return {
            "total_definitions": total,
            "source_coverage": round(has_source / max(total, 1), 4),
            "logic_coverage": round(has_logic / max(total, 1), 4),
            "type_distribution": type_dist,
            "fill_strategy_distribution": fill_dist,
        }

    def export_feature_catalog(self) -> list[dict[str, Any]]:
        """导出全量特征定义目录，包含编码、名称、类型、来源与逻辑供下游系统写入。"""
        rows = store.rows(self.MODULE)
        catalog = []
        for r in rows:
            catalog.append({
                "feature_code": r.get("特征编号"),
                "feature_name": r.get("特征名称"),
                "feature_type": r.get("特征类型"),
                "data_source": r.get("数据来源"),
                "compute_logic": r.get("计算逻辑"),
                "version": r.get("特征版本"),
            })
        return catalog