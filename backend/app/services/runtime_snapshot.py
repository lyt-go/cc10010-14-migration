"""历史运行实例文本记录 -> 带版本的类型化运行数据快照。

设计要点（对应迁移需求）：

1. 旧库字段保持原样不动：转换只读 ``runtime`` 表的中文文本字段，快照写到独立的
   ``runtime_snapshot`` 内部表，运行实例清单继续延续旧有记录。
2. 回填保留原运行编号（``runtime_id`` / ``record_code``）与来源版本
   （``source_version``），结构版本固定走配置中的 ``runtime_snapshot_version``，
   示例数据、本地开发、上线读取始终是同一版本。
3. 历史记录中的缺失结束时间、非法吞吐、旧状态枚举逐字段解析，无法解析的字段用
   兼容标记（``compatibility_markers``）标注，而不是静默吞掉。旧文本值与类型化值
   冲突时同时保留两份值并打 ``conflict`` 标记。
4. 兼容标记直接决定基线计算口径（见 ``BASELINE_MARKER_POLICY``），选哪种标记会
   改变基线结果，因此标记与策略集中在本模块，运行实例清单、基线管理与跨业务面
   统计全部复用同一套口径。
5. 同一份历史快照（源行内容指纹不变）重复迁移只生成一个数据版本；源行被重跑更新
   （指纹变化）才追加新版本，旧版本保留可审计。
6. 迁移转换与基线重算由调用方放进同一个 ``store.transaction()``，要么同时可见，
   要么整体回滚。
"""
from __future__ import annotations

import copy
import hashlib
from datetime import datetime
from typing import Any

from app.config import settings
from app.store import SNAPSHOT_TABLE, store

# 类型化状态枚举：迁移后的唯一合法执行状态口径。
STATUS_DRAFT = "草稿"
STATUS_RUNNING = "运行中"
STATUS_SUCCESS = "成功"
STATUS_FAILED = "失败"
STATUS_TERMINATED = "已终止"
CANONICAL_STATUSES = (
    STATUS_DRAFT,
    STATUS_RUNNING,
    STATUS_SUCCESS,
    STATUS_FAILED,
    STATUS_TERMINATED,
)

# 旧状态枚举 -> 类型化状态。历史系统曾混用中文与英文枚举。
LEGACY_STATUS_MAP = {
    "草稿": STATUS_DRAFT,
    "运行中": STATUS_RUNNING,
    "成功": STATUS_SUCCESS,
    "失败": STATUS_FAILED,
    "已终止": STATUS_TERMINATED,
    # 旧版本/外部系统遗留枚举
    "FINISHED": STATUS_SUCCESS,
    "SUCCESS": STATUS_SUCCESS,
    "SUCCEEDED": STATUS_SUCCESS,
    "FAILED": STATUS_FAILED,
    "FAILURE": STATUS_FAILED,
    "RUNNING": STATUS_RUNNING,
    "PENDING": STATUS_DRAFT,
    "KILLED": STATUS_TERMINATED,
    "TERMINATED": STATUS_TERMINATED,
    "CANCELED": STATUS_TERMINATED,
    "CANCELLED": STATUS_TERMINATED,
}

# 兼容标记取值。
MARKER_OK = "ok"          # 文本可正常解析，且与类型化值一致
MARKER_MIGRATED = "migrated"  # 文本可解析但形态为旧格式（如英文枚举、带千分位）
MARKER_MISSING = "missing"    # 历史记录缺失该字段（如缺失结束时间）
MARKER_INVALID = "invalid"    # 文本不可解析（如非法吞吐、不可解析时间）
MARKER_CONFLICT = "conflict"  # 旧文本值与类型化值/主状态冲突

# 每种兼容标记在基线计算中的处理口径。标记选择会改变基线结果，策略在此显式声明，
# 基线管理与其它业务面统计必须按同一策略消费快照。
BASELINE_MARKER_POLICY: dict[str, str] = {
    MARKER_OK: "类型化值直接参与基线计算",
    MARKER_MIGRATED: "按映射后的类型化值参与基线计算，原始文本仅留痕",
    MARKER_MISSING: "字段缺失：不参与对应指标计算，计入质量缺失数，不算破线",
    MARKER_INVALID: "字段非法：排除出数值类指标，计入异常记录与数据质量问题数",
    MARKER_CONFLICT: "以类型化值为唯一口径，冲突记录计入质量冲突数并标记异常",
}

_DATETIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d",
)

# 快照依赖的旧字段，指纹只覆盖这些业务字段，忽略 pending/abnormal 等展示标记。
LEGACY_FINGERPRINT_FIELDS = (
    "记录编号",
    "作业名称",
    "开始时间",
    "结束时间",
    "执行状态",
    "读取行数",
    "写入行数",
    "错误信息",
    "status",
)


def parse_datetime(raw: Any) -> tuple[datetime | None, str]:
    """把历史时间文本解析成类型化时间，返回 (值, 兼容标记)。"""
    text = str(raw or "").strip()
    if not text:
        return None, MARKER_MISSING
    for fmt in _DATETIME_FORMATS:
        try:
            return datetime.strptime(text, fmt), MARKER_OK
        except ValueError:
            continue
    return None, MARKER_INVALID


def parse_throughput(raw: Any) -> tuple[int | None, str]:
    """把历史吞吐文本解析成非负整数行数，返回 (值, 兼容标记)。

    支持千分位与带单位的历史写法（如 ``"86,400"``、``"12,000 行"``）；
    空值记缺失，负数或无法解析记非法。
    """
    text = str(raw or "").strip()
    if not text:
        return None, MARKER_MISSING
    migrated = any(ch in text for ch in (",", "，", "行")) or " " in text
    normalized = text.replace(",", "").replace("，", "").strip()
    if normalized.endswith("行"):
        normalized = normalized[:-1].strip()
    # 剥离千分位与单位后，剩余内容必须整体是一个带符号整数；
    # 像「运行实例样例2」这种夹带中文的文本直接判非法，不能抽出数字凑数。
    if not normalized or any(not (ch.isdigit() or ch == "-") for ch in normalized):
        return None, MARKER_INVALID
    try:
        value = int(normalized)
    except (TypeError, ValueError):
        return None, MARKER_INVALID
    if value < 0:
        return None, MARKER_INVALID
    return value, MARKER_MIGRATED if migrated else MARKER_OK


def parse_status(raw: Any) -> tuple[str | None, str]:
    """把历史执行状态文本映射为类型化状态枚举。"""
    text = str(raw or "").strip()
    if not text:
        return None, MARKER_MISSING
    if text in CANONICAL_STATUSES:
        return text, MARKER_OK
    mapped = LEGACY_STATUS_MAP.get(text)
    if mapped is not None:
        return mapped, MARKER_MIGRATED
    return None, MARKER_INVALID


def legacy_fingerprint(row: dict[str, Any]) -> str:
    """对历史源行的业务内容计算指纹：指纹不变即同一份历史快照。"""
    payload = {field: str(row.get(field, "")) for field in LEGACY_FINGERPRINT_FIELDS}
    encoded = repr(sorted(payload.items())).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def build_snapshot(row: dict[str, Any], *, data_version: int) -> dict[str, Any]:
    """把一条历史文本记录转换成一条带版本的类型化快照（纯函数，不落库）。"""
    start_time, start_marker = parse_datetime(row.get("开始时间"))
    end_time, end_marker = parse_datetime(row.get("结束时间"))
    read_rows, read_marker = parse_throughput(row.get("读取行数"))
    write_rows, write_marker = parse_throughput(row.get("写入行数"))
    exec_status, exec_marker = parse_status(row.get("执行状态"))

    primary_status = str(row.get("status") or "").strip() or None
    status_marker = exec_marker
    typed_status = exec_status
    # 主状态与执行状态文本冲突：以类型化执行状态为唯一口径，同时保留冲突痕迹。
    if (
        typed_status is not None
        and primary_status in CANONICAL_STATUSES
        and primary_status != typed_status
    ):
        status_marker = MARKER_CONFLICT

    duration_seconds: int | None = None
    duration_marker = MARKER_MISSING
    if start_time is not None and end_time is not None:
        duration_seconds = int((end_time - start_time).total_seconds())
        duration_marker = MARKER_OK if duration_seconds >= 0 else MARKER_INVALID
        if duration_seconds < 0:
            duration_seconds = None
    elif start_time is not None or end_time is not None:
        duration_marker = MARKER_MISSING

    markers = {
        "开始时间": start_marker,
        "结束时间": end_marker,
        "执行状态": status_marker,
        "读取行数": read_marker,
        "写入行数": write_marker,
        "运行时长": duration_marker,
    }
    issue_counts = {
        MARKER_MISSING: sum(1 for m in markers.values() if m == MARKER_MISSING),
        MARKER_INVALID: sum(1 for m in markers.values() if m == MARKER_INVALID),
        MARKER_CONFLICT: sum(1 for m in markers.values() if m == MARKER_CONFLICT),
        MARKER_MIGRATED: sum(1 for m in markers.values() if m == MARKER_MIGRATED),
    }

    return {
        # 回填必须保留原运行编号与来源版本。
        "runtime_id": int(row["id"]),
        "record_code": str(row.get("记录编号", "")),
        "job_name": str(row.get("作业名称", "")),
        "snapshot_version": settings.runtime_snapshot_version,
        "source_version": settings.runtime_legacy_source_version,
        "data_version": data_version,
        "source_fingerprint": legacy_fingerprint(row),
        # 类型化字段
        "start_time": start_time.isoformat() if start_time else None,
        "end_time": end_time.isoformat() if end_time else None,
        "duration_seconds": duration_seconds,
        "status": typed_status,
        "read_rows": read_rows,
        "write_rows": write_rows,
        # 兼容标记与基线口径
        "compatibility_markers": markers,
        "baseline_policy": dict(BASELINE_MARKER_POLICY),
        "quality_summary": {
            "missing_fields": issue_counts[MARKER_MISSING],
            "invalid_fields": issue_counts[MARKER_INVALID],
            "conflict_fields": issue_counts[MARKER_CONFLICT],
            "migrated_fields": issue_counts[MARKER_MIGRATED],
            "has_issue": any(
                issue_counts[m] > 0
                for m in (MARKER_MISSING, MARKER_INVALID, MARKER_CONFLICT)
            ),
        },
        # 原始文本原样留痕，便于审计旧值与新值冲突。
        "raw_legacy": {field: row.get(field) for field in LEGACY_FINGERPRINT_FIELDS},
        "migrated_at": datetime.now().isoformat(timespec="seconds"),
    }


def _all_snapshots() -> list[dict[str, Any]]:
    return store.rows(SNAPSHOT_TABLE)


def latest_snapshots() -> list[dict[str, Any]]:
    """返回每个运行实例当前生效（数据版本最高）的类型化快照。"""
    latest: dict[int, dict[str, Any]] = {}
    for snap in _all_snapshots():
        current = latest.get(snap["runtime_id"])
        if current is None or snap["data_version"] > current["data_version"]:
            latest[snap["runtime_id"]] = snap
    return [latest[key] for key in sorted(latest)]


def latest_snapshot_by_id(runtime_id: int) -> dict[str, Any] | None:
    for snap in reversed(latest_snapshots()):
        if snap["runtime_id"] == runtime_id:
            return snap
    return None


def migrate_runtime_snapshots() -> dict[str, Any]:
    """把当前历史运行实例表迁移为类型化快照，返回迁移摘要。

    幂等规则：源行业务指纹未变时复用既有数据版本，不产生新版本；
    指纹变化（例如迁移期间被重跑更新）才追加下一个数据版本。
    本函数只负责转换与版本落库，调用方需在 ``store.transaction()`` 内
    紧接着执行基线重算，保证转换与基线消费同一事务可见。
    """
    legacy_rows = store.rows("runtime")
    snapshots = _all_snapshots()

    created = 0
    versioned = 0
    unchanged = 0
    for row in legacy_rows:
        runtime_id = int(row["id"])
        fingerprint = legacy_fingerprint(row)
        existing = [s for s in snapshots if s["runtime_id"] == runtime_id]
        same = next((s for s in existing if s["source_fingerprint"] == fingerprint), None)
        if same is not None:
            # 同一份历史快照重复迁移：只保留原版本，不再生成新版本。
            unchanged += 1
            continue
        next_version = max((s["data_version"] for s in existing), default=0) + 1
        snapshots.append(build_snapshot(row, data_version=next_version))
        if existing:
            versioned += 1
        else:
            created += 1

    return {
        "ok": True,
        "snapshot_version": settings.runtime_snapshot_version,
        "source_version": settings.runtime_legacy_source_version,
        "legacy_total": len(legacy_rows),
        "snapshot_total": len(snapshots),
        "created": created,
        "new_versions": versioned,
        "unchanged": unchanged,
    }


def snapshot_view(snap: dict[str, Any]) -> dict[str, Any]:
    """快照的只读视图（去掉内部原始大字段时保留标记，供接口返回）。"""
    view = copy.deepcopy(snap)
    return view
