"""工单 CSV 导入。"""

from __future__ import annotations

import csv
from pathlib import Path

from .models import WorkOrder

REQUIRED_COLUMNS = ("id", "vehicle", "status", "opened_on")


class MissingColumnError(ValueError):
    """CSV 缺少必需列。"""

    def __init__(self, column: str, path: Path) -> None:
        self.column = column
        self.path = path
        super().__init__(f"{path.name} 缺少必需列 {column!r}")


def load_csv(path: str | Path) -> list[WorkOrder]:
    path = Path(path)
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames or []
        for column in REQUIRED_COLUMNS:
            if column not in fields:
                raise MissingColumnError(column, path)
        orders = []
        for row in reader:
            orders.append(
                WorkOrder(
                    id=row["id"].strip(),
                    vehicle=row["vehicle"].strip(),
                    status=row["status"].strip().lower(),
                    priority=(row.get("priority") or "").strip(),
                    opened_on=row["opened_on"].strip(),
                    note=(row.get("note") or "").strip(),
                )
            )
    return orders
