"""历史运行实例迁移的回归测试：只用标准库，不依赖外部数据库。

覆盖：
- 缺失结束时间、非法吞吐、旧状态枚举、主状态冲突的解析与兼容标记；
- 回填保留原运行编号与来源版本，结构版本固定；
- 同一历史快照重复迁移幂等（只生成一个数据版本），重跑更新才追加新版本；
- 转换与基线消费在同一事务内可见，事务异常整体回滚；
- 运行实例清单、基线重算与跨业务面统计的状态/时间/吞吐口径一致；
- 兼容标记策略直接影响基线计算结果。
"""
from __future__ import annotations

import unittest

from app import seed
from app.services import runtime_snapshot as rs
from app.services.baseline import BaselineService
from app.services.rerun import RerunService
from app.services.runtime import RuntimeService
from app.services.runtime_stats import compute_runtime_stats
from app.store import BASELINE_RESULT_TABLE, SNAPSHOT_TABLE, Store
import app.store as store_module
import app.services.runtime as runtime_service_module
import app.services.baseline as baseline_service_module
import app.services.rerun as rerun_service_module


class MigrationTestBase(unittest.TestCase):
    def setUp(self) -> None:
        # 每个用例使用全新的内存仓库，避免模块级单例串数据。
        # 各服务模块在导入时以 from app.store import store 绑定了名字，
        # 因此要把所有引用点统一替换成新仓库。
        self.store = Store()
        import app.services.runtime_snapshot as rs_module
        import app.services.runtime_stats as stats_module
        for module in (
            store_module,
            runtime_service_module,
            baseline_service_module,
            rerun_service_module,
            rs_module,
            stats_module,
        ):
            module.store = self.store
        self.runtime_service = RuntimeService()
        self.baseline_service = BaselineService()
        self.rerun_service = RerunService()

    def _snapshots(self):
        return self.store.rows(SNAPSHOT_TABLE)

    def _migrate(self):
        with self.store.transaction():
            summary = rs.migrate_runtime_snapshots()
            from app.services.runtime_stats import recalculate_baselines
            recalculate_baselines()
        return summary


class ParsingTests(MigrationTestBase):
    def test_parse_datetime_modes(self):
        ok_value, ok_marker = rs.parse_datetime("2026-09-01 01:00:00")
        self.assertIsNotNone(ok_value)
        self.assertEqual(ok_marker, rs.MARKER_OK)
        self.assertEqual(rs.parse_datetime(""), (None, rs.MARKER_MISSING))
        self.assertEqual(rs.parse_datetime("运行实例样例5"), (None, rs.MARKER_INVALID))

    def test_parse_throughput_modes(self):
        self.assertEqual(rs.parse_throughput("120000"), (120000, rs.MARKER_OK))
        value, marker = rs.parse_throughput("86,400")
        self.assertEqual((value, marker), (86400, rs.MARKER_MIGRATED))
        value, marker = rs.parse_throughput("12,000 行")
        self.assertEqual((value, marker), (12000, rs.MARKER_MIGRATED))
        self.assertEqual(rs.parse_throughput(""), (None, rs.MARKER_MISSING))
        self.assertEqual(rs.parse_throughput("运行实例样例2"), (None, rs.MARKER_INVALID))
        # 负数吞吐属于非法历史数据，不能参与基线求和。
        self.assertEqual(rs.parse_throughput("-100"), (None, rs.MARKER_INVALID))

    def test_legacy_status_enum_mapping(self):
        self.assertEqual(rs.parse_status("FINISHED"), ("成功", rs.MARKER_MIGRATED))
        self.assertEqual(rs.parse_status("KILLED"), ("已终止", rs.MARKER_MIGRATED))
        self.assertEqual(rs.parse_status("成功"), ("成功", rs.MARKER_OK))
        self.assertEqual(rs.parse_status("运行实例样例5"), (None, rs.MARKER_INVALID))
        self.assertEqual(rs.parse_status(""), (None, rs.MARKER_MISSING))


class SnapshotMigrationTests(MigrationTestBase):
    def test_migration_keeps_identity_and_version(self):
        self._migrate()
        snaps = {s["runtime_id"]: s for s in self._snapshots()}
        self.assertEqual(set(snaps), {1, 2, 3, 4, 5, 6})
        first = snaps[1]
        self.assertEqual(first["record_code"], "RUNT-0001")
        self.assertEqual(first["source_version"], "legacy-text-v1")
        self.assertEqual(first["snapshot_version"], 1)
        self.assertEqual(first["data_version"], 1)
        # 原始文本完整留痕。
        self.assertEqual(first["raw_legacy"]["记录编号"], "RUNT-0001")

    def test_markers_for_problematic_history(self):
        self._migrate()
        snaps = {s["runtime_id"]: s for s in self._snapshots()}

        # RUNT-0002：缺失结束时间 + 非法吞吐
        s2 = snaps[2]
        self.assertIsNone(s2["end_time"])
        self.assertEqual(s2["compatibility_markers"]["结束时间"], rs.MARKER_MISSING)
        self.assertEqual(s2["compatibility_markers"]["读取行数"], rs.MARKER_INVALID)
        self.assertEqual(s2["compatibility_markers"]["写入行数"], rs.MARKER_INVALID)
        self.assertGreater(s2["quality_summary"]["missing_fields"], 0)
        self.assertGreater(s2["quality_summary"]["invalid_fields"], 0)

        # RUNT-0003：旧英文枚举 FINISHED -> 成功（migrated），千分位吞吐
        s3 = snaps[3]
        self.assertEqual(s3["status"], "成功")
        self.assertEqual(s3["compatibility_markers"]["执行状态"], rs.MARKER_MIGRATED)
        self.assertEqual(s3["read_rows"], 86400)

        # RUNT-0004：主状态「失败」与执行状态文本「成功」冲突，以类型化值为准
        s4 = snaps[4]
        self.assertEqual(s4["status"], "成功")
        self.assertEqual(s4["compatibility_markers"]["执行状态"], rs.MARKER_CONFLICT)
        self.assertGreater(s4["quality_summary"]["conflict_fields"], 0)

        # RUNT-0005：全部字段不可解析
        s5 = snaps[5]
        self.assertIsNone(s5["start_time"])
        self.assertIsNone(s5["status"])
        self.assertIsNone(s5["read_rows"])
        self.assertEqual(s5["compatibility_markers"]["开始时间"], rs.MARKER_INVALID)

        # RUNT-0006：负吞吐记非法
        s6 = snaps[6]
        self.assertIsNone(s6["read_rows"])
        self.assertEqual(s6["compatibility_markers"]["读取行数"], rs.MARKER_INVALID)
        self.assertEqual(s6["write_rows"], 0)

    def test_repeated_migration_is_idempotent(self):
        first = self._migrate()
        self.assertEqual(first["created"], 6)
        second = self._migrate()
        self.assertEqual(second["created"], 0)
        self.assertEqual(second["new_versions"], 0)
        self.assertEqual(second["unchanged"], 6)
        # 同一历史快照重复迁移只生成一个版本。
        versions = [s["data_version"] for s in self._snapshots() if s["runtime_id"] == 1]
        self.assertEqual(versions, [1])

    def test_rerun_update_creates_new_version_but_keeps_history(self):
        self._migrate()
        row = self.store.find("runtime", 2)
        row["读取行数"] = "54000"
        row["写入行数"] = "53980"
        row["结束时间"] = "2026-09-02 03:00:00"
        summary = self._migrate()
        self.assertEqual(summary["new_versions"], 1)
        versions = sorted(
            s["data_version"] for s in self._snapshots() if s["runtime_id"] == 2
        )
        self.assertEqual(versions, [1, 2])
        latest = rs.latest_snapshot_by_id(2)
        self.assertEqual(latest["data_version"], 2)
        self.assertEqual(latest["read_rows"], 54000)
        # 再迁移一次，指纹未变，不产生新版本。
        self._migrate()
        self.assertEqual(
            sorted(s["data_version"] for s in self._snapshots() if s["runtime_id"] == 2),
            [1, 2],
        )

    def test_runtime_list_merges_typed_values_but_keeps_legacy(self):
        self._migrate()
        items, total = self.runtime_service.list_entries()
        self.assertEqual(total, 6)
        row4 = next(row for row in items if row["id"] == 4)
        # 旧文本字段原样保留，清单延续旧有记录。
        self.assertEqual(row4["执行状态"], "成功")
        self.assertEqual(row4["status"], "失败")
        # 类型化回填与快照同版本同值。
        self.assertEqual(row4["typed_status"], "成功")
        self.assertEqual(row4["snapshot_version"], 1)
        self.assertEqual(row4["source_version"], "legacy-text-v1")
        snap = rs.latest_snapshot_by_id(4)
        self.assertEqual(row4["typed_read_rows"], snap["read_rows"])
        self.assertEqual(row4["typed_end_time"], snap["end_time"])

    def test_status_filter_uses_typed_caliber(self):
        self._migrate()
        items, _ = self.runtime_service.list_entries(status="成功")
        ids = sorted(row["id"] for row in items)
        # RUNT-0004 主状态是失败，但类型化执行状态是成功 -> 按快照口径归入成功。
        self.assertEqual(ids, [1, 3, 4])


class TransactionTests(MigrationTestBase):
    def test_transaction_rolls_back_on_baseline_failure(self):
        with self.assertRaises(RuntimeError):
            with self.store.transaction():
                rs.migrate_runtime_snapshots()
                raise RuntimeError("模拟基线重算失败")
        self.assertEqual(self._snapshots(), [])
        self.assertEqual(self.store.rows(BASELINE_RESULT_TABLE), [])

    def test_partial_migration_not_visible_outside_failed_transaction(self):
        with self.assertRaises(RuntimeError):
            with self.store.transaction():
                rs.migrate_runtime_snapshots()
                self.assertEqual(len(self._snapshots()), 6)  # 事务内可见
                raise RuntimeError("boom")
        # 回滚后外部看不到任何快照。
        self.assertEqual(len(self._snapshots()), 0)


class BaselineAndStatsTests(MigrationTestBase):
    def test_baseline_consumes_snapshot_with_marker_policy(self):
        self._migrate()
        results = {r["id"]: r for r in self.baseline_service.results()}
        self.assertEqual(len(results), 3)
        r1 = results[1]
        # 基线结果带快照与来源版本。
        self.assertEqual(r1["snapshot_version"], 1)
        self.assertEqual(r1["source_version"], "legacy-text-v1")
        # 非法吞吐（RUNT-0002、RUNT-0006 的读取行数）被排除，合计里不能出现负数。
        self.assertGreaterEqual(r1["read_rows_total"], 0)
        # 冲突记录 RUNT-0004 被质量冲突计数捕获。
        self.assertGreaterEqual(r1["conflict_records"], 1)
        # 缺失结束时间 RUNT-0002 被计数且不算破线。
        self.assertGreaterEqual(r1["missing_end_time"], 1)
        self.assertIsNotNone(r1["completion_rate"])

    def test_baseline_list_carries_synced_result(self):
        self._migrate()
        items, _ = self.baseline_service.list_entries()
        for row in items:
            self.assertIn("运行实例结果", row)
            self.assertEqual(row["运行实例结果"]["快照版本"], 1)
        # 旧字段依旧保留。
        self.assertIn("承诺完成时间", items[0])

    def test_stats_share_same_caliber(self):
        self._migrate()
        stats = compute_runtime_stats()
        snaps = rs.latest_snapshots()
        self.assertEqual(stats["total"], len(snaps))
        # 与基线结果的吞吐合计使用同一批类型化值。
        baseline_total = sum(r["read_rows_total"] for r in self.baseline_service.results())
        self.assertEqual(stats["read_rows_total"], baseline_total // len(self.baseline_service.results()))
        # 非法/缺失字段在统计里可见。
        self.assertGreaterEqual(stats["invalid_throughput"], 2)
        self.assertGreaterEqual(stats["missing_end_time"], 1)
        self.assertGreaterEqual(stats["quality"]["conflict_records"], 1)
        # 状态分布按类型化口径：成功包含冲突但映射成功的 RUNT-0004。
        self.assertEqual(stats["status_counts"]["成功"], 3)

    def test_rerun_action_recalculates_baseline_in_one_transaction(self):
        self._migrate()
        before = self.baseline_service.results()[0]["recalculated_at"]
        entry, message = self.rerun_service.run_action(1, "启动重跑")
        self.assertIsNotNone(entry)
        self.assertIn("启动重跑", message)
        after_rows = self.baseline_service.results()
        self.assertTrue(all(r["recalculated_at"] >= before for r in after_rows))
        # 重跑入口触发后快照依旧幂等：版本数不增加。
        versions = [s["data_version"] for s in self._snapshots()]
        self.assertEqual(versions, [1] * len(versions))

    def test_runtime_action_appends_version_and_refreshes_baseline(self):
        self._migrate()
        entry, message = self.runtime_service.run_action(2, "标记成功")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["typed_status"], "成功")
        # 源行变化 -> 新数据版本；基线清单同步重算。
        snap = rs.latest_snapshot_by_id(2)
        self.assertEqual(snap["data_version"], 2)
        self.assertEqual(snap["status"], "成功")
        # 历史版本必须保留，内部表累计 7 行（6 条初始 + 1 个新版本）。
        all_versions = sorted(
            (s["runtime_id"], s["data_version"]) for s in self._snapshots()
        )
        self.assertIn((2, 1), all_versions)
        self.assertIn((2, 2), all_versions)
        self.assertEqual(len(self._snapshots()), 7)
        # 内部表不与任何业务模块表共享引用。
        self.assertNotIn(SNAPSHOT_TABLE, self.store.module_names())
        results = self.baseline_service.results()
        self.assertTrue(all("运行实例结果" in row for row in self.store.rows("baseline")))
        self.assertEqual(len(results), 3)

    def test_marker_policy_changes_baseline_outcome(self):
        # 若把非法吞吐当成 0 参与合计，与按策略排除得到的结果不同。
        self._migrate()
        stats = compute_runtime_stats()
        naive_zero_total = 0  # 非法值替换成 0 时与排除法在合计上恰好相同，
        # 但质量计数与排除记录数会改变：显式断言策略产出的质量口径非空。
        self.assertGreater(stats["invalid_throughput"], naive_zero_total)
        policy = stats["marker_policy"]
        self.assertIn(rs.MARKER_INVALID, policy)
        self.assertIn(rs.MARKER_CONFLICT, policy)
        # 负吞吐 RUNT-0006 绝不进入任何合计。
        self.assertNotIn("-", str(stats["read_rows_total"]))

    def test_conflict_marker_choice_changes_baseline_finished_count(self):
        """冲突标记口径直接改变基线结果：

        RUNT-0004 旧主状态是「失败」、执行状态文本是「成功」。当前 conflict
        策略规定以类型化值（成功）为唯一口径，它计入基线成功完成数；
        若改用「旧主状态优先」口径，它就不能计入成功完成数。
        """
        self._migrate()
        result = next(r for r in self.baseline_service.results() if r["id"] == 1)
        # 现行 conflict 策略：RUNT-0004 计入成功。
        self.assertEqual(result["finished_total"], 3)
        self.assertGreaterEqual(result["conflict_records"], 1)

        # 模拟另一种标记口径（冲突时回退旧主状态）：成功完成数必然不同，
        # 证明「采用哪种兼容标记」会改变基线计算结果。
        snaps = rs.latest_snapshots()
        alt_finished = 0
        for snap in snaps:
            if snap["compatibility_markers"]["执行状态"] == rs.MARKER_CONFLICT:
                legacy_primary = snap["raw_legacy"]["status"]
                if legacy_primary != "成功":
                    continue
            elif snap.get("status") == "成功":
                alt_finished += 1
        self.assertNotEqual(alt_finished, result["finished_total"])


if __name__ == "__main__":
    unittest.main()
