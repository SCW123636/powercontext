"""工单领域模型。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Priority(str, Enum):
    """工单优先级，P0 最高。"""

    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"

    @classmethod
    def parse(cls, value: str) -> Priority:
        return cls(value.strip().upper())


class InvalidPriority(ValueError):
    """优先级取值不合法。"""

    def __init__(self, value: object, line: int | None = None) -> None:
        self.value = value
        self.line = line
        where = f"（CSV 第 {line} 行）" if line is not None else ""
        super().__init__(f"非法优先级 {value!r}{where}，只允许 P0/P1/P2/P3")


@dataclass(frozen=True)
class WorkOrder:
    id: str
    vehicle: str
    status: str
    priority: str
    opened_on: str
    note: str = ""
