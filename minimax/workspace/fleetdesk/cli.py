"""命令行入口：fleetdesk list / counts。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .importer import load_csv
from .service import WorkOrderService

DEFAULT_DATA = Path(__file__).resolve().parent.parent / "data" / "work_orders.csv"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fleetdesk", description="车队维修工单")
    parser.add_argument("--data", default=str(DEFAULT_DATA), help="工单 CSV 路径")
    sub = parser.add_subparsers(dest="command", required=True)

    listing = sub.add_parser("list", help="列出工单")
    listing.add_argument("--status", choices=["open", "in_progress", "done"])
    listing.add_argument("--priority", choices=["P0", "P1", "P2", "P3"])

    sub.add_parser("counts", help="按状态统计工单数")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    service = WorkOrderService(load_csv(args.data))
    if args.command == "counts":
        for status, count in service.counts_by_status().items():
            print(f"{status}\t{count}")
        return 0

    kwargs = {"status": args.status}
    if args.priority:
        kwargs["priority"] = args.priority
    for order in service.list_orders(**kwargs):
        print(f"{order.id}\t{order.vehicle}\t{order.status}\t{order.priority}\t{order.opened_on}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
