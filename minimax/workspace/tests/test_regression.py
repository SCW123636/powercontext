"""回归测试：修改前后都必须通过。"""

from pathlib import Path

import pytest

from fleetdesk import MissingColumnError, WorkOrderService, load_csv

DATA = Path(__file__).resolve().parent.parent / "data"


@pytest.fixture()
def service() -> WorkOrderService:
    return WorkOrderService(load_csv(DATA / "work_orders.csv"))


def test_list_all_orders_keeps_import_order(service: WorkOrderService) -> None:
    assert [o.id for o in service.list_orders()] == [
        "WO-1001",
        "WO-1002",
        "WO-1003",
        "WO-1004",
        "WO-1005",
        "WO-1006",
    ]


def test_filter_by_status(service: WorkOrderService) -> None:
    assert [o.id for o in service.list_orders(status="open")] == ["WO-1001", "WO-1003", "WO-1006"]


def test_counts_by_status(service: WorkOrderService) -> None:
    assert service.counts_by_status() == {"open": 3, "in_progress": 2, "done": 1}


def test_open_orders_for_vehicle(service: WorkOrderService) -> None:
    assert [o.id for o in service.open_orders_for("京A-1024")] == ["WO-1001"]


def test_csv_fields_roundtrip(service: WorkOrderService) -> None:
    first = service.orders[0]
    assert (first.id, first.vehicle, first.status, first.priority, first.opened_on, first.note) == (
        "WO-1001",
        "京A-1024",
        "open",
        "P0",
        "2026-10-20",
        "制动片磨损到限",
    )


def test_missing_required_column_raises(tmp_path: Path) -> None:
    broken = tmp_path / "broken.csv"
    broken.write_text("id,vehicle,status\nWO-1,京A-1024,open\n", encoding="utf-8")
    with pytest.raises(MissingColumnError):
        load_csv(broken)


def test_missing_note_column_becomes_empty(tmp_path: Path) -> None:
    sparse = tmp_path / "sparse.csv"
    sparse.write_text(
        "id,vehicle,status,priority,opened_on\nWO-9,京A-1024,open,P2,2026-10-28\n",
        encoding="utf-8",
    )
    assert load_csv(sparse)[0].note == ""
