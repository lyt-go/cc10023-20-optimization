"""特征计算任务领域服务：批量计算的编排、分区管理与执行追踪。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.drift_review import drift_review_store
from app.store import store


class FeatureComputeService(BaseModuleService):
    MODULE = "feature_compute"
    ENTITY = "计算任务"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["任务编号", "关联特征", "数据切片", "计算引擎", "输入分区", "输出分区", "并行度", "任务状态"]
    REQUIRED_FIELDS = ["任务编号", "关联特征", "数据切片"]
    STATUS_ORDER = ["草稿", "等待中", "计算中", "已完成", "已失败"]
    ACTION_RULES = {"提交执行": "草稿", "停止计算": "计算中", "确认完成": "已完成"}
    NEGATIVE_ACTIONS = []

    # ── 漂移复核口径：按请求日期逐日判定，获准日期与证据版本写入任务 ──
    def apply_compute(
        self,
        define_id: int,
        request_dates: list[str],
        *,
        data_slice: str = "default_slice_001",
        compute_engine: str = "spark",
    ) -> dict[str, Any]:
        """申请计算：对请求的每个日期逐日做漂移准入判定。

        获准日期连同当时采用的证据版本写入新计算任务；被拒日期逐条返回理由。
        关联按定义标识（特征编号），同名另一条定义的复核结论不参与判定。
        """
        define_entry = store.find("feature_define", define_id)
        if define_entry is None:
            return {"ok": False, "message": f"特征定义 {define_id} 不存在"}
        define_code = str(define_entry.get("特征编号", ""))
        feature_name = str(define_entry.get("特征名称", ""))

        allowed = []
        rejected = []
        for date in sorted(set(request_dates)):
            verdict = drift_review_store.decide(define_code, date)
            if verdict["decision"] == "允许":
                basis = verdict["basis"] or {}
                allowed.append({
                    "日期": date,
                    "准入结论": verdict["准入结论"],
                    "证据版本": basis.get("证据版本"),
                    "监控编号": basis.get("监控编号"),
                    "周期序号": basis.get("周期序号"),
                    "PSI": basis.get("PSI"),
                    "当轮阈值": basis.get("当轮阈值"),
                    "说明": verdict["reason"],
                })
            else:
                rejected.append({
                    "日期": date,
                    "准入结论": verdict["准入结论"],
                    "理由": verdict["reason"],
                    "裁决依据": verdict["basis"],
                    "待补周期": verdict["pending_periods"],
                })

        if not allowed:
            return {
                "ok": False,
                "define_id": define_id,
                "定义标识": define_code,
                "enqueued": False,
                "allowed_dates": [],
                "rejected_dates": rejected,
                "message": "全部请求日期均未通过漂移准入，未创建计算任务",
            }

        rows = store.rows(self.MODULE)
        task_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        bound_evidence = [
            {
                "日期": item["日期"],
                "证据版本": item["证据版本"],
                "监控编号": item["监控编号"],
                "周期序号": item["周期序号"],
                "PSI": item["PSI"],
                "当轮阈值": item["当轮阈值"],
            }
            for item in allowed
        ]
        task = {
            "id": task_id,
            "status": "等待中",
            "pending": True,
            "abnormal": False,
            "任务编号": f"COMP-{task_id:04d}",
            "关联特征": feature_name,
            "定义标识": define_code,
            "数据切片": data_slice,
            "计算引擎": compute_engine,
            "输入分区": "、".join(f"dt={item['日期']}" for item in allowed),
            "输出分区": f"output/feat_{task_id}",
            "并行度": "8",
            "任务状态": "等待中",
            "申请日期": [item["日期"] for item in allowed],
            "获准日期": [item["日期"] for item in allowed],
            "拒绝日期": [item["日期"] for item in rejected],
            "拒绝明细": rejected,
            "证据绑定": bound_evidence,
        }
        rows.append(task)
        return {
            "ok": True,
            "enqueued": True,
            "task_id": task_id,
            "任务编号": task["任务编号"],
            "define_id": define_id,
            "定义标识": define_code,
            "allowed_dates": allowed,
            "rejected_dates": rejected,
            "message": (
                f"获准 {len(allowed)} 个日期已入队（任务 {task['任务编号']}），"
                f"拒绝 {len(rejected)} 个日期"
            ),
        }

    def query_compute_progress(self, task_id: int) -> dict[str, Any]:
        """查询计算任务进度；进度始终从任务绑定读取原输入和原证据。

        新任务返回申请时冻结的输入分区与证据版本；兼容历史任务（无证据绑定），
        历史任务继续读取其登记时的原输入分区，不被后续阈值或结论变化影响。
        """
        task = store.find(self.MODULE, task_id)
        if task is None:
            return {"ok": False}
        result = {
            "ok": True,
            "task_id": task_id,
            "任务编号": task.get("任务编号"),
            "status": task.get("status"),
            "engine": task.get("计算引擎"),
            "parallelism": task.get("并行度"),
            "input": task.get("输入分区"),
            "output": task.get("输出分区"),
        }
        if "证据绑定" in task:
            result["bound_inputs"] = [
                {"日期": d, "输入分区": f"dt={d}"} for d in task.get("获准日期", [])
            ]
            result["bound_evidence"] = task.get("证据绑定")
            result["rejected_dates"] = task.get("拒绝日期", [])
            result["evidence_note"] = "证据版本在入队时冻结，后续阈值调整不影响本任务"
        else:
            result["bound_inputs"] = [{"输入分区": task.get("输入分区")}]
            result["bound_evidence"] = []
            result["evidence_note"] = "历史任务：按任务登记的原输入分区执行，无漂移证据绑定"
        return result


    # ── 链路1: 特征计算 → 特征快照 ──
    # ── 链路5: 特征计算 → 相关性矩阵 → 特征漂移监控 ──
    def execute_and_snapshot(self, task_id: int) -> dict[str, Any]:
        """执行特征计算任务并在完成后自动生成特征快照存储到snapshots目录。"""
        task = store.find(self.MODULE, task_id)
        if task is None:
            return {"ok": False, "message": f"计算任务 {task_id} 不存在"}
        task["status"] = "已完成"
        task["pending"] = False
        snapshot_rows = store.rows("feature_snapshot")
        next_id = max((int(r.get("id", 0)) for r in snapshot_rows), default=0) + 1
        feats = task.get("关联特征", "unknown")
        snapshot_rows.append({
            "id": next_id,
            "status": "已完成",
            "pending": False,
            "abnormal": False,
            "快照编号": f"SNAP-{next_id:04d}",
            "特征集": feats,
            "快照日期": "2026-11-18",
            "特征数量": 12,
            "数据行数": "100000",
            "存储路径": f"snapshots/compute_{task_id}_{next_id}.json",
            "保留天数": "30",
            "快照状态": "已完成",
        })
        return {
            "ok": True,
            "task_id": task_id,
            "snapshot_id": next_id,
            "message": f"计算任务 {task_id} 已完成，生成特征快照 {next_id}",
        }

    def compute_correlation_matrix(self, task_id: int) -> dict[str, Any]:
        """基于特征计算结果计算特征间相关性矩阵并写入存储。"""
        task = store.find(self.MODULE, task_id)
        if task is None:
            return {"ok": False}
        corr_rows = store.rows("feature_corr")
        next_id = max((int(r.get("id", 0)) for r in corr_rows), default=0) + 1
        feats = task.get("关联特征", "")
        corr_rows.append({
            "id": next_id,
            "status": "已完成",
            "pending": False,
            "abnormal": False,
            "矩阵编号": f"CORR-{next_id:04d}",
            "特征集": feats,
            "计算方法": "pearson",
            "阈值筛选": "0.7",
            "可视化配置": "heatmap_full",
            "计算时间": "2026-11-18T10:00:00",
            "矩阵版本": "v1.0.0",
            "结果存储": f"corr_matrix/compute_{task_id}_v1.json",
        })
        return {"ok": True, "corr_id": next_id, "task_id": task_id}

    def trigger_drift_monitoring(self, task_id: int) -> dict[str, Any]:
        """基于特征计算结果触发特征漂移监控，关联基准分布并启动PSI计算。"""
        task = store.find(self.MODULE, task_id)
        if task is None:
            return {"ok": False}
        drift_rows = store.rows("drift_monitor")
        next_id = max((int(r.get("id", 0)) for r in drift_rows), default=0) + 1
        feats = task.get("关联特征", "")
        drift_rows.append({
            "id": next_id,
            "status": "监控中",
            "pending": False,
            "abnormal": False,
            "监控编号": f"DRIFT-{next_id:04d}",
            "关联特征": feats,
            "基准分布": f"baseline_compute_{task_id}",
            "当前分布": f"current_{task_id}_2026_11_18",
            "PSI值": "0.015",
            "漂移阈值": "0.25",
            "采样窗口": "30d",
            "监控状态": "监控中",
        })
        return {"ok": True, "drift_id": next_id, "task_id": task_id, "psi_value": 0.015}

    def retry_failed_tasks(self, task_ids: list[int]) -> dict[str, Any]:
        """重试失败的计算任务，将状态回退到草稿并重新调度采集。"""
        retried = []
        failed = []
        for tid in task_ids:
            task = store.find(self.MODULE, tid)
            if task is None:
                failed.append(tid)
                continue
            if task.get("status") != "已失败":
                failed.append(tid)
                continue
            task["status"] = "草稿"
            task["pending"] = True
            task["abnormal"] = False
            retried.append(tid)
        return {"ok": len(retried) > 0, "retried": retried, "failed_skipped": failed}