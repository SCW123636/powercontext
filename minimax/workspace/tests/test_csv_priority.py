"""目标测试：SPEC-4.1 CSV 导入的优先级缺省与校验。修改前应当失败。"""

from pathlib import Path

import pytest

from fleetdesk import InvalidPriority, load_csv

DATA = Path(__file__).resolve().parent.parent / "data"


def test_missing_priority_column_defaults_to_p2() -> None:
    orders = load_csv(DATA / "work_orders_no_priority.csv")
    assert [o.priority for o in orders] == ["P2", "P2"]


def test_empty_priority_cell_defaults_to_p2(tmp_path: Path) -> None:
    rows = tmp_path / "empty_priority.csv"
    rows.write_text(
        "id,vehicle,status,priority,opened_on\nWO-9,京A-1024,open,,2026-10-28\n",
        encoding="utf-8",
    )
    assert load_csv(rows)[0].priority == "P2"


def test_invalid_priority_reports_csv_line_number() -> None:
    with pytest.raises(InvalidPriority) as excinfo:
        load_csv(DATA / "work_orders_bad_priority.csv")
    assert excinfo.value.line == 3


def test_lowercase_priority_is_normalized() -> None:
    orders = load_csv(DATA / "work_orders_lower_priority.csv")
    assert [o.priority for o in orders] == ["P1", "P0"]
