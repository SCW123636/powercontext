"""工单查询服务。"""

from __future__ import annotations

from collections.abc import Iterable

from .models import InvalidPriority, WorkOrder

VALID_STATUS = ("open", "in_progress", "done")
VALID_PRIORITIES = ("P0", "P1", "P2", "P3")


class WorkOrderService:
    def __init__(self, orders: Iterable[WorkOrder]) -> None:
        self._orders = list(orders)

    @property
    def orders(self) -> tuple[WorkOrder, ...]:
        return tuple(self._orders)

    def list_orders(
        self, status: str | None = None, priority: str | None = None
    ) -> list[WorkOrder]:
        """按状态和优先级列出工单；筛选条件为 None 时不按该条件筛选。"""
        if priority is not None:
            normalized_priority = priority.strip().upper()
            if normalized_priority not in VALID_PRIORITIES:
                raise InvalidPriority(
                    f"非法优先级 {priority!r}，有效值为 {', '.join(VALID_PRIORITIES)}"
                )
        else:
            normalized_priority = None

        return [
            order
            for order in self._orders
            if (status is None or order.status == status)
            and (
                normalized_priority is None
                or order.priority.strip().upper() == normalized_priority
            )
        ]

    def counts_by_status(self) -> dict[str, int]:
        counts = {status: 0 for status in VALID_STATUS}
        for order in self._orders:
            counts[order.status] = counts.get(order.status, 0) + 1
        return counts

    def open_orders_for(self, vehicle: str) -> list[WorkOrder]:
        return [o for o in self._orders if o.vehicle == vehicle and o.status != "done"]
