"""领域模型与分页游标载体。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Task:
    """一条任务记录（与 tasks 表行一一对应）。"""

    id: int
    name: str
    status: str
    version: int
    created_at: int  # 单调递增的逻辑时间戳（纳秒），同时作为分页排序键
    updated_at: int  # 最近一次写入的物理时间戳（纳秒）

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "version": self.version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class Page:
    """游标分页的一页结果。"""

    items: list[Task] = field(default_factory=list)
    next_cursor: str | None = None
