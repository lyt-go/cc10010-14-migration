"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

迁移相关的两张内部表（带 ``__internal__`` 前缀，不参与业务模块概览）：
- ``__internal__runtime_snapshot``：历史运行实例文本记录迁移后得到的
  「带版本类型化运行数据快照」；
- ``__internal__baseline_result``：基线管理基于最新快照重算出的运行实例结果。

仓库提供 ``transaction()`` 事务上下文：迁移转换与基线重算在同一事务内提交，
任一步骤抛错则整体回滚，外部只能看到「旧状态」或「迁移+重算后的新状态」，
不会读到转换完成但基线尚未消费的中间态。
"""
from __future__ import annotations

import copy
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from app.seed import SEED_ROWS

# 内部表：类型化快照与基线重算结果，不属于前端业务模块清单。
# 使用内部前缀命名，避免与任何业务模块表同名而被 rows()/setdefault 共享引用。
SNAPSHOT_TABLE = "__internal__runtime_snapshot"
BASELINE_RESULT_TABLE = "__internal__baseline_result"
INTERNAL_TABLES = (SNAPSHOT_TABLE, BASELINE_RESULT_TABLE)


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        for table in INTERNAL_TABLES:
            self._tables.setdefault(table, [])
        self._lock = threading.RLock()
        self._tx_depth = 0
        self._tx_backup: dict[str, list[dict[str, Any]]] | None = None

    def module_names(self) -> list[str]:
        # 内部迁移表不出现在业务模块概览里。
        return sorted(name for name in self._tables if name not in INTERNAL_TABLES)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    @contextmanager
    def transaction(self) -> Iterator["Store"]:
        """串行化写事务：进入时整库备份，异常时整体回滚。

        内存库没有真正的数据库隔离级别，这里用可重入锁 + 拷贝快照实现
        「同一事务可见」：事务持锁期间，其它入口（例如迁移过程中到达的重跑
        动作）只能在事务提交/回滚之后读到数据，杜绝读到半迁移状态。
        """
        with self._lock:
            if self._tx_depth == 0:
                # 深拷贝整库：行内含兼容标记、质量摘要等嵌套字典，
                # 回滚时必须恢复到事务前的完整对象状态。
                self._tx_backup = copy.deepcopy(self._tables)
            self._tx_depth += 1
            try:
                yield self
            except Exception:
                self._tx_depth -= 1
                if self._tx_depth == 0 and self._tx_backup is not None:
                    self._tables = self._tx_backup
                    self._tx_backup = None
                raise
            else:
                self._tx_depth -= 1
                if self._tx_depth == 0:
                    self._tx_backup = None

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
