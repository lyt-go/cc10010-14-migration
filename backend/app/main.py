"""数据仓库ETL任务编排平台 后端服务入口。

启动：uvicorn app.main:app --host 127.0.0.1 --port 8000
健康检查：GET /api/health
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routers import ROUTERS
from app.services.runtime_snapshot import migrate_runtime_snapshots
from app.services.runtime_stats import recalculate_baselines
from app.store import store


@asynccontextmanager
async def lifespan(_: FastAPI):
    """启动即迁移：历史文本记录 -> 带版本类型化快照，并在同一事务内重算基线。

    迁移幂等，重复启动不会产生重复数据版本；示例数据、本地开发与上线读取
    都经过这同一条迁移路径，保证快照版本与口径一致。
    """
    with store.transaction():
        migrate_runtime_snapshots()
        recalculate_baselines()
    yield


app = FastAPI(title="数据仓库ETL任务编排平台", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for module in ROUTERS:
    app.include_router(module.router)


@app.get("/api/health")
def health() -> dict[str, object]:
    """健康检查：确认服务已经监听、示例数据已经就绪。"""
    return {"ok": True, "app": settings.app_name, "modules": len(store.module_names())}


@app.get("/api/overview")
def overview() -> dict[str, object]:
    """运营概览：把各业务模块的待处理量汇总成看板卡片。"""
    return store.overview()
