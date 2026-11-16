"""异常重跑业务规则：状态流转、字段校验与筛选口径都收在这里。

异常重跑是运行实例之外的另一个操作入口：重跑动作可能在迁移期间更新历史运行
记录。任何重跑状态流转都在同一事务内连带执行「类型化快照迁移 + 基线重算」，
让基线管理清单同步拿到重算后的运行实例结果，且转换与基线消费要么同时可见、
要么整体回滚。
"""
from __future__ import annotations

from typing import Any

from app.store import store
from app.services.runtime_snapshot import migrate_runtime_snapshots
from app.services.runtime_stats import recalculate_baselines

MODULE = "rerun"
REQUIRED_FIELDS = ["任务编号", "原始运行编号", "重跑区间开始"]
STATUS_ORDER = ["草稿", "重跑审批中", "执行中", "已完成", "已取消"]
ACTION_RULES = {"提交重跑审批": "重跑审批中", "启动重跑": "执行中", "取消重跑": "已取消"}
NEGATIVE_ACTIONS = []


def _refresh_typed_views() -> None:
    migrate_runtime_snapshots()
    recalculate_baselines()


class RerunService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("任务编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        with store.transaction():
            rows = store.rows(MODULE)
            entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
            entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
            entry["status"] = STATUS_ORDER[0]
            entry["pending"] = True
            entry["abnormal"] = False
            rows.append(entry)
            _refresh_typed_views()
            return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于异常重跑可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        with store.transaction():
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"重跑任务 {entry_id} 不存在或已归档"
            entry["status"] = target
            entry["pending"] = target != STATUS_ORDER[-1]
            entry["abnormal"] = action in NEGATIVE_ACTIONS
            # 其它操作入口：同步重算运行实例快照与基线管理清单。
            _refresh_typed_views()
            return entry, f"重跑任务已{action}"
