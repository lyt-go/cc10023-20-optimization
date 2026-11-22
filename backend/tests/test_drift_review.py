"""漂移复核口径集成测试：周期校验、阈值版本、冲突裁决与计算入队留痕。

直接以脚本方式运行（仓库未引入 pytest）：
    .venv/bin/python tests/test_drift_review.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.main import app  # noqa: E402
from app.services import service_for  # noqa: E402
from app.services.drift_review import drift_review_store  # noqa: E402
from app.store import store  # noqa: E402

D1, D2, D3, D4, D5, D6 = (
    "2026-11-01", "2026-11-02", "2026-11-03",
    "2026-11-04", "2026-11-05", "2026-11-06",
)
PSI = 0.18


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def setup_monitors():
    drift_review_store.reset()
    svc_define = service_for("feature_define")
    svc_drift = service_for("drift_monitor")

    # 两条同名但定义标识不同的特征定义，验证复核关联按定义标识隔离
    entry_a, missing_a = svc_define.create_entry({
        "特征编号": "FEAT-DRIFTA", "特征名称": "同名漂移特征", "特征类型": "数值型",
        "计算逻辑": "a*2", "数据来源": "src_a",
    })
    entry_b, missing_b = svc_define.create_entry({
        "特征编号": "FEAT-DRIFTB", "特征名称": "同名漂移特征", "特征类型": "数值型",
        "计算逻辑": "b*2", "数据来源": "src_b",
    })
    check(not missing_a and not missing_b, "测试特征定义应登记成功")
    id_a, id_b = entry_a["id"], entry_b["id"]

    old = svc_drift.register_monitor(
        monitor_code="DRIFT-OLD", define_code="FEAT-DRIFTA",
        feature_name="同名漂移特征", baseline_dist="baseline_old_v1", threshold=0.10,
    )
    new = svc_drift.register_monitor(
        monitor_code="DRIFT-NEW", define_code="FEAT-DRIFTA",
        feature_name="同名漂移特征", baseline_dist="baseline_new_v2", threshold=0.25,
    )
    third = svc_drift.register_monitor(
        monitor_code="DRIFT-THIRD", define_code="FEAT-DRIFTA",
        feature_name="同名漂移特征", baseline_dist="baseline_third", threshold=0.20,
    )
    return id_a, id_b, old["monitor_id"], new["monitor_id"], third["monitor_id"]


def test_pending_repeat_and_conflict():
    id_a, id_b, m_old, m_new, m_third = setup_monitors()
    svc_drift = service_for("drift_monitor")

    # 1) 旧监控覆盖 d1、d2，PSI 0.18，阈值 0.10 → 异常，证据冻结 EV-0001
    r1 = svc_drift.submit_monitor_period(
        m_old, [D1, D2], sample_complete=True, psi=PSI, current_dist="current_old_d2",
    )
    check(r1["ok"] and r1["period"]["定级"] == "异常", "旧监控 0.18/0.10 应定级异常")
    check(r1["evidence_version"] == "EV-0001", "首轮证据版本应为 EV-0001")
    p_old = r1["period"]
    check(p_old["当轮阈值"] == 0.10 and p_old["PSI"] == 0.18, "旧周期应冻结 PSI 与当轮阈值")
    check(p_old["基准分布"] == "baseline_old_v1", "基准分布应随证据一起冻结")

    # 2) 新监控覆盖 d2、d3，采样未齐 → 待补；重复提交缺样数据仍为待补
    r2 = svc_drift.submit_monitor_period(m_new, [D2, D3], sample_complete=False)
    check(r2["period"]["状态"] == "待补", "缺样周期应标记为待补")
    r2b = svc_drift.submit_monitor_period(m_new, [D2, D3], sample_complete=False)
    check(r2b["action"] == "still_pending" and r2b["period"]["状态"] == "待补",
          "重复提交缺样监控数据，该周期仍应为待补")
    check(r2b["period"]["提交次数"] == 2, "重复提交应累计提交次数")

    # 待补周期不能借已有周期的分布与阈值定级：d3 此时没有任何复核字段
    verdict_d3_pending = drift_review_store.decide("FEAT-DRIFTA", D3)
    check(verdict_d3_pending["decision"] == "拒绝" and verdict_d3_pending["pending_periods"],
          "待补周期覆盖的日期应拒绝并注明待补，不得借用其他周期定级")

    # 3) 补齐迟到的采样回执，PSI 同为 0.18，阈值 0.25 → 正常，证据 EV-0002
    r3 = svc_drift.submit_monitor_period(
        m_new, [D2, D3], sample_complete=True, psi=PSI, current_dist="current_new_d3",
    )
    check(r3["action"] == "graded" and r3["period"]["定级"] == "正常",
          "补齐后应按当轮阈值 0.25 定级为正常")
    check(r3["evidence_version"] == "EV-0002", "新周期证据版本应为 EV-0002")

    # 4) d2 两轮定级冲突：旧异常案件未结案，复核员仍需处理旧异常 → 采用旧结论
    conflicts = svc_drift.review_conflicts("FEAT-DRIFTA")
    d2_conflict = next(c for c in conflicts["conflict_dates"] if c["日期"] == D2)
    check({x["定级"] for x in d2_conflict["records"]} == {"异常", "正常"},
          "d2 应能查到异常/正常两条冲突复核字段")
    verdict_d2 = drift_review_store.decide("FEAT-DRIFTA", D2)
    check(verdict_d2["decision"] == "拒绝", "旧异常未结案时 d2 应被限制")
    check(verdict_d2["basis"]["监控编号"] == "DRIFT-OLD"
          and verdict_d2["basis"]["证据版本"] == "EV-0001",
          "d2 实际裁决依据应是旧监控 EV-0001")
    check(any(c["governing"] and c["冲突说明"] for c in verdict_d2["candidates"]),
          "候选字段应标明实际采用的裁决依据与冲突说明")

    # 5) 两个周期的基准分布、当前分布、PSI、当轮阈值可一起查到
    ev_old = svc_drift.list_period_evidence(m_old)
    ev_new = svc_drift.list_period_evidence(m_new)
    po = ev_old["periods"][0]
    pn = ev_new["periods"][0]
    check((po["基准分布"], po["当前分布"], po["PSI"], po["当轮阈值"])
          == ("baseline_old_v1", "current_old_d2", 0.18, 0.10),
          "旧周期应能同时查到基准/当前分布、PSI 与当轮阈值")
    check((pn["基准分布"], pn["当前分布"], pn["PSI"], pn["当轮阈值"])
          == ("baseline_new_v2", "current_new_d3", 0.18, 0.25),
          "新周期应能同时查到基准/当前分布、PSI 与当轮阈值")

    # 5b) 已定级周期即使再次提交（哪怕带新数据）也保持历史结论与证据不变
    re_submit = svc_drift.submit_monitor_period(
        m_old, [D1, D2], sample_complete=True, psi=0.01, current_dist="should_be_ignored",
    )
    check(re_submit["action"] == "frozen", "已定级周期重复提交应走冻结分支")
    po2 = svc_drift.list_period_evidence(m_old)["periods"][0]
    check(po2["定级"] == "异常" and po2["PSI"] == 0.18
          and po2["当轮阈值"] == 0.10 and po2["当前分布"] == "current_old_d2"
          and po2["证据版本"] == "EV-0001",
          "历史周期的定级、PSI、阈值、分布与证据版本均不得被重复提交改写")

    return id_a, id_b, m_old, m_new, m_third


def test_threshold_versions_and_legacy_task(id_a, m_old, m_third):
    svc_drift = service_for("drift_monitor")
    svc_compute = service_for("feature_compute")

    # 6) 第三条监控先在阈值调整前受理一条计算任务（d5，阈值 0.20，0.18 正常）
    r5 = svc_drift.submit_monitor_period(
        m_third, [D5], sample_complete=True, psi=PSI, current_dist="current_third_d5",
    )
    ev_d5 = r5["period"]["证据版本"]
    check(r5["period"]["定级"] == "正常" and r5["period"]["当轮阈值"] == 0.20,
          "0.18 < 0.20 应定级正常并冻结阈值 0.20")
    app5 = svc_compute.apply_compute(id_a, [D5])
    check(app5["ok"] and app5["allowed_dates"][0]["日期"] == D5, "d5 调整前申请应获准")
    task_legacy_id = app5["task_id"]
    check(app5["allowed_dates"][0]["证据版本"] == ev_d5, "入队应写入当时采用的证据版本")

    # 7) 阈值调整 0.20 → 0.15：历史周期保留原结论，新周期按新阈值判定为异常
    adj = svc_drift.adjust_monitor_threshold(m_third, 0.15)
    check(adj["frozen_periods"][0]["当轮阈值"] == 0.20
          and adj["frozen_periods"][0]["定级"] == "正常",
          "调整接口应回传冻结周期，且历史周期结论必须保留为正常")
    r6 = svc_drift.submit_monitor_period(
        m_third, [D6], sample_complete=True, psi=PSI, current_dist="current_third_d6",
    )
    check(r6["period"]["定级"] == "异常" and r6["period"]["当轮阈值"] == 0.15,
          "新周期应按新阈值 0.15 定级异常")

    # 8) 调整前已受理的任务：进度仍从任务绑定读取原输入和原证据
    progress = svc_compute.query_compute_progress(task_legacy_id)
    check(progress["input"] == f"dt={D5}", "历史受理任务应保留原输入分区")
    check(progress["bound_evidence"][0]["证据版本"] == ev_d5
          and progress["bound_evidence"][0]["当轮阈值"] == 0.20,
          "历史受理任务应保留原证据版本与原阈值")
    # 新申请 d6 被新阈值异常拒绝；d5 仍按冻结的正常证据获准
    app6 = svc_compute.apply_compute(id_a, [D6])
    check(not app6["ok"] and app6["rejected_dates"][0]["日期"] == D6,
          "新周期 d6 异常应拒绝入队")
    app5b = svc_compute.apply_compute(id_a, [D5])
    check(app5b["ok"] and app5b["allowed_dates"][0]["证据版本"] == ev_d5,
          "d5 新申请仍应采用冻结的历史证据")

    # 9) 旧监控阈值 0.10 → 0.25：d1/d2 历史结论不变，d4 新周期按新阈值判正常
    svc_drift.adjust_monitor_threshold(m_old, 0.25)
    r4 = svc_drift.submit_monitor_period(
        m_old, [D4], sample_complete=True, psi=PSI, current_dist="current_old_d4",
    )
    check(r4["period"]["定级"] == "正常" and r4["period"]["当轮阈值"] == 0.25,
          "阈值调整后的新周期应按 0.25 判正常")
    ev_old = svc_drift.list_period_evidence(m_old)
    first = next(p for p in ev_old["periods"] if D1 in p["覆盖日期"])
    check(first["定级"] == "异常" and first["当轮阈值"] == 0.10,
          "历史周期必须保留异常原结论与 0.10 原阈值")


def test_catalog_application_and_isolation(id_a, id_b, m_old):
    svc_define = service_for("feature_define")
    svc_compute = service_for("feature_compute")
    svc_drift = service_for("drift_monitor")

    # 10) 以监控定级结果刷新特征定义的计算准入规则，目录可按日期查限制与依据
    refresh = svc_define.refresh_compute_admission(id_a)
    check(refresh["ok"] and set(refresh["rule_dates"]) == {D1, D2, D3, D4, D5, D6},
          "准入规则应按全部生效日期刷新")
    cat_d2 = svc_define.read_drift_review_catalog(id_a, D2)
    item_d2 = cat_d2["items"][0]
    check(item_d2["准入结论"] == "限制计算"
          and item_d2["裁决依据"]["监控编号"] == "DRIFT-OLD",
          "目录 d2 应给出限制结论与旧监控依据")

    # 11) 新申请逐日判定：d1 受旧期限制，d3 可入队，d2 冲突按旧结论拒绝，d4 正常
    application = svc_compute.apply_compute(id_a, [D1, D2, D3, D4])
    check(application["ok"], "存在获准日期时应创建计算任务")
    allowed = {x["日期"]: x for x in application["allowed_dates"]}
    rejected = {x["日期"]: x for x in application["rejected_dates"]}
    check(sorted(allowed) == [D3, D4], "仅 d3、d4 应获准入队")
    check(sorted(rejected) == [D1, D2], "d1、d2 应被拒绝")
    check(allowed[D3]["证据版本"] == "EV-0002", "d3 应绑定新监控证据 EV-0002")
    check("未结案" in rejected[D2]["理由"], "d2 拒绝理由应解释旧异常未结案的实际依据")
    task = store.find("feature_compute", application["task_id"])
    check(task["输入分区"] == f"dt={D3}、dt={D4}", "任务输入分区应只包含获准日期")
    check(len(task["证据绑定"]) == 2 and task["证据绑定"][0]["证据版本"],
          "任务应逐日写入获准日期采用的证据版本")

    # 12) 同名另一条定义不受影响：没有任何复核证据，申请被拒且目录为空
    cat_b = svc_define.read_drift_review_catalog(id_b)
    check(cat_b["items"] == [], "同名另一条定义不应读到 A 的复核字段")
    app_b = svc_compute.apply_compute(id_b, [D3])
    check(not app_b["ok"] and "缺少漂移复核证据" in app_b["rejected_dates"][0]["理由"],
          "同名另一条定义的申请应因缺少自身证据被拒")

    # 13) 兼容已有计算任务：种子任务无证据绑定，进度读取原输入
    legacy = svc_compute.query_compute_progress(1)
    check(legacy["ok"] and legacy["bound_evidence"] == []
          and "历史任务" in legacy["evidence_note"],
          "历史计算任务应继续读取登记时的原输入，不被复核口径破坏")

    # 14) 复核员结案旧异常后，d2 改由最新证据（新监控正常）接管，但历史结论保留
    svc_drift.close_review_case(m_old)
    verdict = drift_review_store.decide("FEAT-DRIFTA", D2)
    check(verdict["decision"] == "允许"
          and verdict["basis"]["监控编号"] == "DRIFT-NEW"
          and verdict["basis"]["证据版本"] == "EV-0002",
          "旧异常结案后 d2 应采用最新正常证据")
    svc_define.refresh_compute_admission(id_a)
    cat_after = svc_define.read_drift_review_catalog(id_a, D2)["items"][0]
    check(cat_after["准入结论"] == "允许计算" and cat_after["裁决依据"]["证据版本"] == "EV-0002",
          "目录应能解释结案后的实际入队依据")
    first = svc_drift.list_period_evidence(m_old)["periods"][0]
    check(first["定级"] == "异常", "结案不改变历史周期的异常原结论")

    # 结案后新申请 d2 可入队，目录结论与实际入队一致
    app_after = svc_compute.apply_compute(id_a, [D2])
    check(app_after["ok"] and app_after["allowed_dates"][0]["证据版本"] == "EV-0002",
          "结案后 d2 实际入队应采用 EV-0002")


def test_http_routes_registered():
    # FastAPI 0.163 以惰性 _IncludedRouter 收纳子路由，路径以 OpenAPI schema 为准
    paths = set(app.openapi()["paths"].keys())
    expected = {
        "/api/drift-reviews/monitors",
        "/api/drift-reviews/monitors/{monitor_id}/periods",
        "/api/drift-reviews/monitors/{monitor_id}/threshold",
        "/api/drift-reviews/monitors/{monitor_id}/close",
        "/api/drift-reviews/monitors/{monitor_id}/evidence",
        "/api/drift-reviews/defines/{define_id}/catalog",
        "/api/drift-reviews/defines/{define_id}/refresh-admission",
        "/api/drift-reviews/defines/{define_id}/compute-applications",
        "/api/drift-reviews/compute-tasks/{task_id}/progress",
        "/api/drift-reviews/conflicts",
    }
    missing = expected - paths
    check(not missing, f"漂移复核路由缺失: {missing}")
    # 通用资源路由仍可正常解析，且未被专用路由前缀抢走
    check("/api/{module}" in paths and "/api/{module}/{entry_id}" in paths,
          "通用资源路由应保持注册")


if __name__ == "__main__":
    id_a, id_b, m_old, m_new, m_third = test_pending_repeat_and_conflict()
    test_threshold_versions_and_legacy_task(id_a, m_old, m_third)
    test_catalog_application_and_isolation(id_a, id_b, m_old)
    test_http_routes_registered()
    print("[PASS] test_pending_repeat_and_conflict（待补/重复提交/补齐定级/d2冲突/证据联查）")
    print("[PASS] test_threshold_versions_and_legacy_task（阈值版本/调整前受理任务留痕）")
    print("[PASS] test_catalog_application_and_isolation（目录依据/逐日入队/同名隔离/结案接管）")
    print("[PASS] test_http_routes_registered（复核路由全部注册）")
    print("\n漂移复核口径全部测试通过")
