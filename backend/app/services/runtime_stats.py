"""基于类型化运行数据快照的统一统计口径。

运行实例清单、基线管理以及其它业务面（例如运营概览的跨模块统计）都通过本模块
读取快照结果，保证状态、时间与吞吐在各处完全一致。所有指标只消费
``__internal__runtime_snapshot`` 内部表的最新数据版本，并严格按照兼容标记的
基线策略处理缺失/非法/冲突字段。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.config import settings
from app.store import BASELINE_RESULT_TABLE, store
from app.services.runtime_snapshot import (
    BASELINE_MARKER_POLICY,
    MARKER_CONFLICT,
    MARKER_INVALID,
    MARKER_MISSING,
    MARKER_MIGRATED,
    MARKER_OK,
    latest_snapshots,
)

# 数值类指标只接受这两类标记的字段值；缺失与非法值按策略排除，不参与求和/均值。
NUMERIC_ACCEPTED_MARKERS = (MARKER_OK, MARKER_MIGRATED, MARKER_CONFLICT)
# 完成判定只接受状态字段可确定的快照。
STATUS_ACCEPTED_MARKERS = (MARKER_OK, MARKER_MIGRATED, MARKER_CONFLICT)


def _marker(snap: dict[str, Any], field: str) -> str:
    return str(snap["compatibility_markers"][field])


def compute_runtime_stats() -> dict[str, Any]:
    """跨业务面共享的运行统计：状态、时间、吞吐同一口径。"""
    snaps = latest_snapshots()
    status_counts = {status: 0 for status in ("草稿", "运行中", "成功", "失败", "已终止")}
    read_total = 0
    write_total = 0
    duration_total = 0
    duration_count = 0
    missing_end = 0
    invalid_throughput = 0
    quality_conflicts = 0
    quality_invalid = 0
    quality_missing = 0
    today = datetime.now().date().isoformat()
    today_runs = 0

    for snap in snaps:
        marker = snap["compatibility_markers"]
        if snap.get("status") and marker["执行状态"] in STATUS_ACCEPTED_MARKERS:
            status_counts[snap["status"]] += 1
        if snap.get("start_time") and str(snap["start_time"]).startswith(today):
            today_runs += 1
        if marker["结束时间"] == MARKER_MISSING:
            missing_end += 1
        if marker["读取行数"] == MARKER_INVALID or marker["写入行数"] == MARKER_INVALID:
            invalid_throughput += 1
        if marker["读取行数"] in NUMERIC_ACCEPTED_MARKERS and snap.get("read_rows") is not None:
            read_total += int(snap["read_rows"])
        if marker["写入行数"] in NUMERIC_ACCEPTED_MARKERS and snap.get("write_rows") is not None:
            write_total += int(snap["write_rows"])
        if snap.get("duration_seconds") is not None and marker["运行时长"] == MARKER_OK:
            duration_total += int(snap["duration_seconds"])
            duration_count += 1
        summary = snap["quality_summary"]
        quality_conflicts += int(summary["conflict_fields"] > 0)
        quality_invalid += int(summary["invalid_fields"] > 0)
        quality_missing += int(summary["missing_fields"] > 0)

    return {
        "snapshot_version": settings.runtime_snapshot_version,
        "source_version": settings.runtime_legacy_source_version,
        "total": len(snaps),
        "status_counts": status_counts,
        "today_runs": today_runs,
        "read_rows_total": read_total,
        "write_rows_total": write_total,
        "avg_duration_seconds": round(duration_total / duration_count, 1) if duration_count else None,
        "missing_end_time": missing_end,
        "invalid_throughput": invalid_throughput,
        "quality": {
            "conflict_records": quality_conflicts,
            "invalid_records": quality_invalid,
            "missing_records": quality_missing,
        },
        "marker_policy": dict(BASELINE_MARKER_POLICY),
    }


def _evaluate_baseline(snaps: list[dict[str, Any]], deadline_text: str | None) -> dict[str, Any]:
    """按兼容标记策略重算单条基线的运行实例结果。"""
    deadline = None
    deadline_marker = MARKER_MISSING
    if deadline_text:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                deadline = datetime.strptime(deadline_text, fmt)
                deadline_marker = MARKER_OK
                break
            except ValueError:
                continue
        if deadline is None:
            deadline_marker = MARKER_INVALID

    related_total = len(snaps)
    excluded = 0
    finished = 0
    on_time = 0
    breached = 0
    read_total = 0
    write_total = 0
    missing_end = 0
    invalid_throughput = 0
    conflicts = 0
    runtime_ids: list[int] = []

    for snap in snaps:
        runtime_ids.append(snap["runtime_id"])
        markers = snap["compatibility_markers"]
        if any(m == MARKER_INVALID for m in markers.values()):
            excluded += 1
        if snap["quality_summary"]["conflict_fields"]:
            conflicts += 1
        if markers["结束时间"] == MARKER_MISSING:
            missing_end += 1
        if MARKER_INVALID in (markers["读取行数"], markers["写入行数"]):
            invalid_throughput += 1
        if markers["读取行数"] in NUMERIC_ACCEPTED_MARKERS and snap.get("read_rows") is not None:
            read_total += int(snap["read_rows"])
        if markers["写入行数"] in NUMERIC_ACCEPTED_MARKERS and snap.get("write_rows") is not None:
            write_total += int(snap["write_rows"])

        status_marker = markers["执行状态"]
        if status_marker not in STATUS_ACCEPTED_MARKERS or not snap.get("status"):
            continue
        if snap["status"] != "成功":
            # 非成功实例不计入完成/准时口径，但保留在总量中。
            continue
        finished += 1
        end_marker = markers["结束时间"]
        if end_marker in (MARKER_OK, MARKER_MIGRATED) and snap.get("end_time") and deadline:
            end_at = datetime.fromisoformat(snap["end_time"])
            if end_at <= deadline:
                on_time += 1
            else:
                breached += 1
        elif end_marker == MARKER_MISSING:
            # 缺失结束时间不算破线，但无法计入准时。
            pass
        else:
            excluded += 1

    completion_rate = round(finished / related_total, 4) if related_total else None
    on_time_rate = round(on_time / finished, 4) if finished else None

    return {
        "runtime_ids": runtime_ids,
        "related_total": related_total,
        "finished_total": finished,
        "on_time_total": on_time,
        "breached_total": breached,
        "excluded_total": excluded,
        "completion_rate": completion_rate,
        "on_time_rate": on_time_rate,
        "read_rows_total": read_total,
        "write_rows_total": write_total,
        "missing_end_time": missing_end,
        "invalid_throughput": invalid_throughput,
        "conflict_records": conflicts,
        "deadline_marker": deadline_marker,
        "marker_policy": dict(BASELINE_MARKER_POLICY),
    }


def recalculate_baselines() -> dict[str, Any]:
    """加载最新类型化快照，重算全部基线定义的运行实例结果。

    结果写入内部表 ``__internal__baseline_result``，同时把关键结果同步回基线
    清单行（旧字段原样保留），让其它操作入口读到的基线管理清单也是同一份口径。
    必须在 ``store.transaction()`` 内与迁移转换成对调用。
    """
    snaps = latest_snapshots()
    results = store.rows(BASELINE_RESULT_TABLE)
    results.clear()
    baseline_rows = store.rows("baseline")

    for baseline in baseline_rows:
        detail = _evaluate_baseline(snaps, str(baseline.get("承诺完成时间") or ""))
        result = {
            "id": int(baseline["id"]),
            "baseline_code": baseline.get("基线编号"),
            "baseline_name": baseline.get("基线名称"),
            "snapshot_version": settings.runtime_snapshot_version,
            "source_version": settings.runtime_legacy_source_version,
            "recalculated_at": datetime.now().isoformat(timespec="seconds"),
            **detail,
        }
        results.append(result)
        # 同步回基线管理清单：新增类型化结果字段，旧中文文本字段不动。
        baseline["运行实例结果"] = {
            "关联运行数": detail["related_total"],
            "成功完成数": detail["finished_total"],
            "准时完成数": detail["on_time_total"],
            "破线数": detail["breached_total"],
            "排除记录数": detail["excluded_total"],
            "完成率": detail["completion_rate"],
            "准时率": detail["on_time_rate"],
            "读取行数合计": detail["read_rows_total"],
            "写入行数合计": detail["write_rows_total"],
            "缺失结束时间数": detail["missing_end_time"],
            "非法吞吐数": detail["invalid_throughput"],
            "状态冲突数": detail["conflict_records"],
            "快照版本": settings.runtime_snapshot_version,
        }

    return {
        "ok": True,
        "baseline_total": len(baseline_rows),
        "snapshot_total": len(snaps),
        "snapshot_version": settings.runtime_snapshot_version,
    }


def baseline_results() -> list[dict[str, Any]]:
    return store.rows(BASELINE_RESULT_TABLE)
