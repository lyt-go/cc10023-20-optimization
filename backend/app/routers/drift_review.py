"""漂移复核路由：监控周期校验、复核字段目录、计算准入与入队证据查询。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services import service_for
from app.store import store

router = APIRouter(prefix="/api/drift-reviews", tags=["漂移复核"])


class RegisterMonitorPayload(BaseModel):
    define_id: int
    monitor_code: str
    baseline_dist: str
    threshold: float
    sample_window: str = "1d"


class PeriodPayload(BaseModel):
    covered_dates: list[str] = Field(default_factory=list)
    sample_complete: bool
    psi: float | None = None
    current_dist: str | None = None


class ThresholdPayload(BaseModel):
    new_threshold: float


class ComputeApplicationPayload(BaseModel):
    request_dates: list[str] = Field(default_factory=list)
    data_slice: str = "default_slice_001"
    compute_engine: str = "spark"


def _drift_service():
    return service_for("drift_monitor")


def _define_service():
    return service_for("feature_define")


def _compute_service():
    return service_for("feature_compute")


def _require_define(define_id: int) -> dict[str, Any]:
    entry = store.find("feature_define", define_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"特征定义 {define_id} 不存在")
    return entry


@router.post("/monitors")
def register_monitor(payload: RegisterMonitorPayload) -> dict[str, Any]:
    entry = _require_define(payload.define_id)
    result = _drift_service().register_monitor(
        monitor_code=payload.monitor_code,
        define_code=str(entry.get("特征编号", "")),
        feature_name=str(entry.get("特征名称", "")),
        baseline_dist=payload.baseline_dist,
        threshold=payload.threshold,
        sample_window=payload.sample_window,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("message", "登记失败"))
    return result


@router.post("/monitors/{monitor_id}/periods")
def submit_period(monitor_id: int, payload: PeriodPayload) -> dict[str, Any]:
    result = _drift_service().submit_monitor_period(
        monitor_id,
        payload.covered_dates,
        sample_complete=payload.sample_complete,
        psi=payload.psi,
        current_dist=payload.current_dist,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("message", "周期提交失败"))
    return result


@router.post("/monitors/{monitor_id}/threshold")
def adjust_threshold(monitor_id: int, payload: ThresholdPayload) -> dict[str, Any]:
    result = _drift_service().adjust_monitor_threshold(monitor_id, payload.new_threshold)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("message", "监控不存在"))
    return result


@router.post("/monitors/{monitor_id}/close")
def close_case(monitor_id: int) -> dict[str, Any]:
    result = _drift_service().close_review_case(monitor_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("message", "监控不存在"))
    return result


@router.get("/monitors/{monitor_id}/evidence")
def period_evidence(monitor_id: int) -> dict[str, Any]:
    result = _drift_service().list_period_evidence(monitor_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("message", "监控不存在"))
    return result


@router.get("/defines/{define_id}/catalog")
def review_catalog(define_id: int, date: str | None = None) -> dict[str, Any]:
    result = _define_service().read_drift_review_catalog(define_id, date)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("message", "特征定义不存在"))
    return result


@router.post("/defines/{define_id}/refresh-admission")
def refresh_admission(define_id: int) -> dict[str, Any]:
    result = _define_service().refresh_compute_admission(define_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("message", "特征定义不存在"))
    return result


@router.post("/defines/{define_id}/compute-applications")
def apply_compute(define_id: int, payload: ComputeApplicationPayload) -> dict[str, Any]:
    return _compute_service().apply_compute(
        define_id,
        payload.request_dates,
        data_slice=payload.data_slice,
        compute_engine=payload.compute_engine,
    )


@router.get("/compute-tasks/{task_id}/progress")
def compute_progress(task_id: int) -> dict[str, Any]:
    result = _compute_service().query_compute_progress(task_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=f"计算任务 {task_id} 不存在")
    return result


@router.get("/conflicts")
def review_conflicts(define_code: str) -> dict[str, Any]:
    return _drift_service().review_conflicts(define_code)
