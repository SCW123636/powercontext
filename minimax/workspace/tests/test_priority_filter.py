"""目标测试：SPEC-3.2 按优先级过滤。修改前应当失败。"""

from pathlib import Path

import pytest

from fleetdesk import InvalidPriority, WorkOrderService, load_csv

DATA = Path(__file__).resolve().parent.parent / "data"


@pytest.fixture()
def service() -> WorkOrderService:
    return WorkOrderService(load_csv(DATA / "work_orders.csv"))


def test_filter_by_priority(service: WorkOrderService) -> None:
    assert [o.id for o in service.list_orders(priority="P0")] == ["WO-1001", "WO-1005"]


def test_status_and_priority_intersect(service: WorkOrderService) -> None:
    assert [o.id for o in service.list_orders(status="open", priority="P1")] == ["WO-1006"]


def test_none_priority_keeps_full_list(service: WorkOrderService) -> None:
    assert len(service.list_orders(priority=None)) == 6


def test_unknown_priority_filter_raises(service: WorkOrderService) -> None:
    with pytest.raises(InvalidPriority):
        service.list_orders(priority="P9")


def test_lowercase_priority_filter_is_normalized(service: WorkOrderService) -> None:
    assert [o.id for o in service.list_orders(priority="p1")] == ["WO-1002", "WO-1006"]
