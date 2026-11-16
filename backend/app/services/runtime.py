"""运行实例业务规则：状态流转、字段校验与筛选口径都收在这里。

迁移后运行实例清单继续延续 ``runtime`` 旧表记录（旧文本字段原样保留），
同时把最新类型化快照的状态、时间、吞吐回填到同一条清单记录上，使业务侧
在清单与基线计算中看到的快照口径完全一致。任何会改动运行实例的动作都会在
同一事务内完成「快照迁移 + 基线重算」，保证转换与基线消费同时可见。
"""
from __future__ import annotations

from typing import Any

from app.store import store
from app.services.runtime_snapshot import latest_snapshot_by_id, migrate_runtime_snapshots
from app.services.runtime_stats import recalculate_baselines

MODULE = "runtime"
REQUIRED_FIELDS = ["记录编号", "作业名称", "开始时间"]
STATUS_ORDER = ["草稿", "运行中", "成功", "失败", "已终止"]
ACTION_RULES = {"启动作业": "运行中", "终止作业": "已终止", "标记成功": "成功"}
NEGATIVE_ACTIONS = []

# 回填到清单行的类型化字段（旧文本字段同名保留，类型化值使用独立键名）。
TYPED_FIELD_KEYS = (
    "typed_status",
    "typed_start_time",
    "typed_end_time",
    "typed_read_rows",
    "typed_write_rows",
    "duration_seconds",
    "snapshot_version",
    "source_version",
    "data_version",
    "compatibility_markers",
    "quality_summary",
)


def _merge_snapshot(entry: dict[str, Any]) -> dict[str, Any]:
    """把最新类型化快照回填到旧清单行，旧文本字段与编号原样保留。"""
    snap = latest_snapshot_by_id(int(entry["id"]))
    if snap is None:
        return entry
    entry["typed_status"] = snap["status"]
    entry["typed_start_time"] = snap["start_time"]
    entry["typed_end_time"] = snap["end_time"]
    entry["typed_read_rows"] = snap["read_rows"]
    entry["typed_write_rows"] = snap["write_rows"]
    entry["duration_seconds"] = snap["duration_seconds"]
    entry["snapshot_version"] = snap["snapshot_version"]
    entry["source_version"] = snap["source_version"]
    entry["data_version"] = snap["data_version"]
    entry["compatibility_markers"] = snap["compatibility_markers"]
    entry["quality_summary"] = snap["quality_summary"]
    return entry


def _refresh_typed_views() -> None:
    """动作落库后，在同一事务内迁移快照并重算基线。"""
    migrate_runtime_snapshots()
    recalculate_baselines()


class RuntimeService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = [dict(row) for row in store.rows(MODULE)]
        for row in rows:
            _merge_snapshot(row)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("记录编号", ""))]
        if status:
            # 状态筛选统一走类型化快照口径；尚无快照时回落到旧主状态。
            rows = [
                row for row in rows
                if str(row.get("typed_status") or row.get("status")) == status
            ]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        return _merge_snapshot(dict(entry))

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        with store.transaction():
            rows = store.rows(MODULE)
            entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
            entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
            # 新登记记录补齐历史表惯用字段，保持旧数据库字段结构不变。
            entry.setdefault("结束时间", "")
            entry.setdefault("执行状态", STATUS_ORDER[0])
            entry.setdefault("读取行数", "")
            entry.setdefault("写入行数", "")
            entry.setdefault("错误信息", "")
            entry["status"] = STATUS_ORDER[0]
            entry["pending"] = True
            entry["abnormal"] = False
            rows.append(entry)
            _refresh_typed_views()
            return _merge_snapshot(dict(entry)), []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于运行实例可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        # 状态流转、快照迁移与基线重算放进同一事务：外部不会看到半迁移状态。
        with store.transaction():
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"运行记录 {entry_id} 不存在或已归档"
            entry["status"] = target
            # 旧文本执行状态同步更新，保证重跑后下一次迁移产生新数据版本。
            entry["执行状态"] = target
            entry["pending"] = target != STATUS_ORDER[-1]
            entry["abnormal"] = action in NEGATIVE_ACTIONS
            _refresh_typed_views()
            return _merge_snapshot(dict(entry)), f"运行记录已{action}"
