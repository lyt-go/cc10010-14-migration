"""运行配置：端口、跨域、运行环境。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    app_name: str = "数据仓库ETL任务编排平台"
    env: str = "local"
    port: int = 8000
    allowed_origins: list[str] = field(
        default_factory=lambda: [
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        ]
    )
    page_size_default: int = 20
    page_size_max: int = 200
    # 类型化运行数据快照的结构版本：示例数据、本地开发与上线读取都固定使用这一版本，
    # 不随 APP_ENV 改变，保证各环境读到的快照口径一致。
    runtime_snapshot_version: int = 1
    # 历史文本记录的来源版本：迁移回填时原样保留到快照的 source_version，
    # 用于区分「历史文本迁移」与「迁移后动作产生」的数据来源。
    runtime_legacy_source_version: str = "legacy-text-v1"


settings = Settings()
