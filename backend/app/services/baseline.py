"""基线管理业务规则：状态流转、字段校验与筛选口径都收在这里。

基线清单的「运行实例结果」完全来自类型化运行数据快照的重算结果
（见 ``runtime_stats.recalculate_baselines``）。本模块只负责基线定义自身的
状态流转，并在任何变更后与快照迁移放在同一事务内重新重算，保证其它操作入口
读到的基线管理清单始终与运行实例快照同版本、同口径。
"""
from __future__ import annotations

from typing import Any

from app.store import store
from app.services.runtime_snapshot import migrate_runtime_snapshots
from app.services.runtime_stats import baseline_results, recalculate_baselines

MODULE = "baseline"
REQUIRED_FIELDS = ["基线编号", "基线名称", "承诺完成时间"]
STATUS_ORDER = ["草稿", "基线评审中", "已启用", "已停用"]
ACTION_RULES = {"提交基线评审": "基线评审中", "启用基线": "已启用", "停用基线": "已停用"}
NEGATIVE_ACTIONS = ["停用基线"]


def _refresh_typed_views() -> None:
    """同一事务内先迁移快照、再重算基线，转换与基线消费同时可见。"""
    migrate_runtime_snapshots()
    recalculate_baselines()


class BaselineService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = [dict(row) for row in store.rows(MODULE)]
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("基线编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        return dict(entry) if entry is not None else None

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
            return dict(entry), []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于基线管理可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        with store.transaction():
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"基线定义 {entry_id} 不存在或已归档"
            entry["status"] = target
            entry["pending"] = target != STATUS_ORDER[-1]
            entry["abnormal"] = action in NEGATIVE_ACTIONS
            _refresh_typed_views()
            return dict(entry), f"基线定义已{action}"

    def recalculate(self) -> dict[str, Any]:
        """显式触发「加载最新类型化快照并重算运行实例结果」。"""
        with store.transaction():
            migration = migrate_runtime_snapshots()
            recalc = recalculate_baselines()
        return {**recalc, "migration": migration}

    def results(self) -> list[dict[str, Any]]:
        """返回带版本的基线重算明细（内部表视图）。"""
        return [dict(row) for row in baseline_results()]
