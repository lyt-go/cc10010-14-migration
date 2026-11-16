"""类型化运行数据快照接口：迁移触发、快照查询与跨业务面统一统计。"""
from __future__ import annotations

from fastapi import APIRouter

from app.schemas import ActionResult, PageResult
from app.services.runtime_snapshot import (
    BASELINE_MARKER_POLICY,
    latest_snapshots,
    migrate_runtime_snapshots,
    snapshot_view,
)
from app.services.runtime_stats import compute_runtime_stats, recalculate_baselines
from app.store import SNAPSHOT_TABLE, store

router = APIRouter(prefix="/api/runtime-snapshots", tags=["运行实例快照"])


@router.get("", response_model=PageResult[dict])
def list_snapshots(page: int = 1, size: int = 20, include_versions: bool = False) -> PageResult[dict]:
    """读取带版本的类型化运行数据快照。

    默认只返回每个运行实例当前生效的最新版本；``include_versions=true`` 时
    返回全部历史数据版本（含被重跑更新前的旧版本），便于审计版本演进。
    """
    if include_versions:
        items = [
            snapshot_view(snap)
            for snap in sorted(
                store.rows(SNAPSHOT_TABLE),
                key=lambda snap: (snap["runtime_id"], snap["data_version"]),
            )
        ]
    else:
        items = [snapshot_view(snap) for snap in latest_snapshots()]
    total = len(items)
    start = max(page - 1, 0) * size
    return PageResult(items=items[start:start + size], total=total, page=page, size=size)


@router.post("/migrate", response_model=ActionResult)
def migrate() -> ActionResult:
    """执行历史文本记录迁移并在同一事务内重算基线；重复迁移幂等。"""
    with store.transaction():
        summary = migrate_runtime_snapshots()
        recalculate_baselines()
    return ActionResult(ok=True, message="类型化运行数据快照迁移完成", entry=summary)


@router.get("/stats", response_model=dict)
def stats() -> dict:
    """另一业务面使用的统计结果：与运行实例清单、基线计算同一类型口径。"""
    return compute_runtime_stats()


@router.get("/marker-policy", response_model=dict)
def marker_policy() -> dict:
    """兼容标记及其对基线计算结果的影响口径，供业务侧核对。"""
    return {"marker_policy": BASELINE_MARKER_POLICY}
