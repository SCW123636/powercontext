"""工单查询服务。"""

from __future__ import annotations

from collections.abc import Iterable

from .models import WorkOrder

VALID_STATUS = ("open", "in_progress", "done")


class WorkOrderService:
    def __init__(self, orders: Iterable[WorkOrder]) -> None:
        self._orders = list(orders)

    @property
    def orders(self) -> tuple[WorkOrder, ...]:
        return tuple(self._orders)

    def list_orders(self, status: str | None = None) -> list[WorkOrder]:
        """按状态列出工单；status 为 None 时返回全部。"""
        return [o for o in self._orders if status is None or o.status == status]

    def counts_by_status(self) -> dict[str, int]:
        counts = {status: 0 for status in VALID_STATUS}
        for order in self._orders:
            counts[order.status] = counts.get(order.status, 0) + 1
        return counts

    def open_orders_for(self, vehicle: str) -> list[WorkOrder]:
        return [o for o in self._orders if o.vehicle == vehicle and o.status != "done"]
