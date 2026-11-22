"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import drift_review, resource

# 漂移复核专用路由含 /api/drift-reviews/* 固定前缀，必须先于通用资源路由
# /api/{module} 注册，否则固定路径会被动态参数抢先匹配。
ROUTERS = [drift_review, resource]
