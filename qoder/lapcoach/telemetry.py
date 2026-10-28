#!/usr/bin/env python3
"""Read a racing telemetry CSV log and report laps, sectors and corners.

Standard library only: the log is read as text, columns are matched by name,
units are detected from the header or from the data, and every subcommand can
print JSON instead of a table.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

KMH = 1.0 / 3.6
MPH = 0.44704

_TIME_ALIASES = ("time", "session_time", "timestamp", "elapsed_time", "log_time", "time_s", "t")
_LAP_TIME_ALIASES = ("lap_time", "laptime", "current_lap_time")
_DIST_ALIASES = ("distance", "dist", "lap_distance", "lapdist", "trip_distance", "distance_m", "lap_dist")
_SPEED_ALIASES = ("speed", "gps_speed", "gpsspeed", "velocity", "vehicle_speed", "v", "speed_kmh", "speed_mph")
_THROTTLE_ALIASES = ("throttle", "throttle_pos", "throttle_position", "tps", "accelerator", "accel")
_BRAKE_ALIASES = ("brake", "brake_pos", "brake_pressure", "brakepressure")
_GEAR_ALIASES = ("gear", "gear_pos")
_LAP_ALIASES = ("lap", "lap_number", "lap_no", "lapnum", "lap_index", "lap_count", "laps")
_LAT_ALIASES = ("lat", "latitude", "gps_lat", "gps_latitude")
_LON_ALIASES = ("lon", "long", "longitude", "gps_lon", "gps_longitude", "lng")


def _normalise(header: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", header.strip().lower())).strip("_")


def _unit_of(header: str) -> str | None:
    text = header.lower()
    for needle, unit in (("mph", "mph"), ("km/h", "kmh"), ("kmh", "kmh"), ("kmph", "kmh"),
                         ("m/s", "mps"), ("ms-1", "mps"), ("m_s", "mps")):
        if needle in text:
            return unit
    return None


def _number(raw, *, comma_decimal: bool = False) -> float | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text or text.lower() in {"nan", "null", "none", "-"}:
        return None
    if comma_decimal and "," in text and "." not in text:
        text = text.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def _time(raw, *, comma_decimal: bool = False) -> float | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if ":" in text:
        total = 0.0
        for part in text.split(":"):
            value = _number(part, comma_decimal=comma_decimal)
            if value is None:
                return None
            total = total * 60.0 + value
        return total
    return _number(text, comma_decimal=comma_decimal)


@dataclass
class Sample:
    t: float
    dist: float
    speed: float | None
    throttle: float | None = None
    brake: float | None = None
    gear: float | None = None


@dataclass
class Lap:
    number: int
    samples: list[Sample]

    @property
    def start(self) -> float:
        return self.samples[0].t

    @property
    def end(self) -> float:
        return self.samples[-1].t

    @property
    def time(self) -> float:
        return self.end - self.start

    @property
    def length(self) -> float:
        return max(item.dist for item in self.samples) - min(item.dist for item in self.samples)

    def speeds(self) -> list[float]:
        return [item.speed for item in self.samples if item.speed is not None]


def _sniff(text: str) -> csv.Dialect:
    try:
        return csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
    except csv.Error:
        return csv.excel


def _read_table(path: Path) -> tuple[list[str], list[list[str]], bool]:
    raw = path.read_text(encoding="utf-8-sig", errors="replace")
    dialect = _sniff(raw)
    rows = list(csv.reader(raw.splitlines(), dialect))
    rows = [row for row in rows if any(cell.strip() for cell in row)]
    if not rows:
        raise SystemExit(f"{path.name}: no rows found")
    header, body = rows[0], rows[1:]
    comma_decimal = getattr(dialect, "delimiter", ",") == ";"
    return header, body, comma_decimal


def _pick(header: Sequence[str], names: Sequence[str], body: Sequence[Sequence[str]],
          parse) -> int | None:
    """Return the column whose normalised name matches a role and whose cells parse."""
    wanted = {_normalise(name) for name in names}
    long_form = [name for name in wanted if len(name) >= 4]
    ranked: list[tuple[int, int]] = []
    for index, name in enumerate(header):
        key = _normalise(name)
        if key in wanted:
            ranked.append((0 if key == _normalise(names[0]) else 1, index))
        elif any(name_ in key for name_ in long_form):
            ranked.append((2, index))
    ranked.sort()
    for _, index in ranked:
        sample = [row[index] for row in body[:80] if index < len(row)]
        parsed = [parse(cell) for cell in sample]
        good = [value for value in parsed if value is not None]
        if good and len(good) >= max(3, len(parsed) // 2):
            return index
    return None


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def _speed_scale(header: str, values: Sequence[float]) -> float:
    unit = _unit_of(header)
    if unit == "kmh":
        return KMH
    if unit == "mph":
        return MPH
    if unit == "mps":
        return 1.0
    peak = max(values) if values else 0.0
    return KMH if peak > 100 else 1.0


def _load(path: Path) -> tuple[list[Lap], dict]:
    header, body, comma_decimal = _read_table(path)
    num = lambda cell: _number(cell, comma_decimal=comma_decimal)  # noqa: E731

    time_index = _pick(header, _TIME_ALIASES, body, lambda cell: _time(cell, comma_decimal=comma_decimal))
    lap_time_index = None
    if time_index is None:
        lap_time_index = _pick(header, _LAP_TIME_ALIASES, body, lambda cell: _time(cell, comma_decimal=comma_decimal))
        time_index = lap_time_index
    dist_index = _pick(header, _DIST_ALIASES, body, num)
    speed_index = _pick(header, _SPEED_ALIASES, body, num)
    throttle_index = _pick(header, _THROTTLE_ALIASES, body, num)
    brake_index = _pick(header, _BRAKE_ALIASES, body, num)
    gear_index = _pick(header, _GEAR_ALIASES, body, num)
    lap_index = _pick(header, _LAP_ALIASES, body, num)
    lat_index = _pick(header, _LAT_ALIASES, body, num)
    lon_index = _pick(header, _LON_ALIASES, body, num)

    if time_index is None:
        raise SystemExit(f"{path.name}: no time-like column found (looked for {', '.join(_TIME_ALIASES)})")
    if speed_index is None:
        raise SystemExit(f"{path.name}: no speed-like column found (looked for {', '.join(_SPEED_ALIASES)})")
    if dist_index is None and (lat_index is None or lon_index is None):
        raise SystemExit(f"{path.name}: need a distance column or GPS latitude/longitude")

    times: list[float] = []
    for row in body:
        if time_index >= len(row):
            continue
        value = _time(row[time_index], comma_decimal=comma_decimal)
        if value is not None:
            times.append(value)
    if len(times) < 20:
        raise SystemExit(f"{path.name}: only {len(times)} timed rows, need at least 20")
    scale = _speed_scale(header[speed_index], [num(row[speed_index]) for row in body[:400]
                                               if speed_index < len(row) and num(row[speed_index]) is not None])
    distance_scale = 1.0
    raw_dist = [num(row[dist_index]) for row in body if dist_index is not None and dist_index < len(row)
                and num(row[dist_index]) is not None] if dist_index is not None else []
    if raw_dist and max(raw_dist) < 30.0:
        distance_scale = 1000.0

    samples: list[Sample] = []
    gps_total = 0.0
    previous_gps: tuple[float, float] | None = None
    clock_offset = 0.0
    previous_raw: float | None = None
    for row in body:
        raw_time = _time(row[time_index], comma_decimal=comma_decimal) if time_index < len(row) else None
        if raw_time is None:
            continue
        if previous_raw is not None and raw_time < previous_raw:
            clock_offset += previous_raw + 0.0
        previous_raw = raw_time
        clock = raw_time + clock_offset
        speed = num(row[speed_index]) if speed_index < len(row) else None
        if dist_index is not None and dist_index < len(row):
            dist = num(row[dist_index])
            dist = None if dist is None else dist * distance_scale
        else:
            dist = None
        if dist is None and lat_index is not None and lon_index is not None:
            lat = num(row[lat_index]) if lat_index < len(row) else None
            lon = num(row[lon_index]) if lon_index < len(row) else None
            if lat is not None and lon is not None:
                if previous_gps is not None:
                    gps_total += _haversine(previous_gps[0], previous_gps[1], lat, lon)
                previous_gps = (lat, lon)
            dist = gps_total
        if dist is None:
            dist = gps_total
        samples.append(Sample(
            t=clock,
            dist=dist,
            speed=None if speed is None else speed * scale,
            throttle=num(row[throttle_index]) if throttle_index is not None and throttle_index < len(row) else None,
            brake=num(row[brake_index]) if brake_index is not None and brake_index < len(row) else None,
            gear=num(row[gear_index]) if gear_index is not None and gear_index < len(row) else None,
        ))
    if not samples:
        raise SystemExit(f"{path.name}: no usable rows")

    laps = _split_laps(samples, body, lap_index, num)
    meta = {
        "file": path.name,
        "rows": len(samples),
        "columns": header,
        "time_column": header[time_index],
        "distance_column": header[dist_index] if dist_index is not None else "gps",
        "speed_column": header[speed_index],
        "lap_column": header[lap_index] if lap_index is not None else None,
        "distance_source": "gps" if dist_index is None else "column",
    }
    return laps, meta


def _split_laps(samples: list[Sample], body: Sequence[Sequence[str]], lap_index: int | None,
                num) -> list[Lap]:
    keys: list[float | None] = []
    if lap_index is not None:
        for row in body:
            value = num(row[lap_index]) if lap_index < len(row) else None
            keys.append(value)
        keys = [key for key in keys if key is not None]
    if lap_index is not None and len(keys) == len(samples) and len(set(keys)) > 1:
        groups: list[Lap] = []
        current_key = keys[0]
        bucket: list[Sample] = []
        for key, sample in zip(keys, samples):
            if key != current_key and bucket:
                groups.append(Lap(number=len(groups) + 1, samples=bucket))
                bucket = []
            current_key = key
            bucket.append(sample)
        if bucket:
            groups.append(Lap(number=len(groups) + 1, samples=bucket))
        return [lap for lap in groups if len(lap.samples) >= 20]

    deltas = [b.t - a.t for a, b in zip(samples, samples[1:]) if b.t > a.t]
    median = statistics.median(deltas) if deltas else 0.0
    cuts = [0]
    for index in range(1, len(samples)):
        gap = samples[index].t - samples[index - 1].t
        drop = samples[index].dist < samples[index - 1].dist - 1.0
        if (median and gap > median * 3) or drop:
            if index - cuts[-1] >= 20:
                cuts.append(index)
    cuts.append(len(samples))
    laps = [Lap(number=n + 1, samples=samples[a:b]) for n, (a, b) in enumerate(zip(cuts, cuts[1:]))]
    return [lap for lap in laps if len(lap.samples) >= 20]


def _monotonic(lap: Lap) -> list[tuple[float, float]]:
    pairs = [(sample.dist, sample.t) for sample in lap.samples]
    running = pairs[0][0]
    fixed: list[tuple[float, float]] = []
    for dist, t in pairs:
        running = max(running, dist)
        fixed.append((running, t))
    return fixed


def _time_at(pairs: Sequence[tuple[float, float]], target: float) -> float | None:
    if not pairs or target < pairs[0][0] or target > pairs[-1][0]:
        return None
    for (d0, t0), (d1, t1) in zip(pairs, pairs[1:]):
        if d1 >= target:
            if d1 == d0:
                return t0
            share = (target - d0) / (d1 - d0)
            return t0 + share * (t1 - t0)
    return pairs[-1][1]


def _sectors(lap: Lap, count: int) -> list[dict]:
    pairs = _monotonic(lap)
    start, end = pairs[0][0], pairs[-1][0]
    step = (end - start) / count
    marks = [start + step * index for index in range(count + 1)]
    times = [_time_at(pairs, mark) for mark in marks]
    out = []
    for index in range(count):
        a, b = times[index], times[index + 1]
        out.append({
            "sector": index + 1,
            "start_m": round(marks[index], 1),
            "end_m": round(marks[index + 1], 1),
            "time_s": None if a is None or b is None else round(b - a, 3),
        })
    return out


def _smooth(values: Sequence[float | None], window: int) -> list[float | None]:
    half = max(1, window // 2)
    out: list[float | None] = []
    for index, value in enumerate(values):
        if value is None:
            out.append(None)
            continue
        window_values = [item for item in values[max(0, index - half): index + half + 1] if item is not None]
        out.append(sum(window_values) / len(window_values) if window_values else value)
    return out


def _corners(lap: Lap, minimum_speed_ratio: float = 0.85) -> list[dict]:
    speeds = _smooth([sample.speed for sample in lap.samples], 5)
    diffs = [b.t - a.t for a, b in zip(lap.samples, lap.samples[1:]) if b.t > a.t]
    dt = statistics.median(diffs) if diffs else 0.1
    reach = max(2, int(round(1.2 / dt)))
    known = [value for value in speeds if value is not None]
    if len(known) < 10:
        return []
    limit = statistics.median(known) * minimum_speed_ratio
    minima: list[int] = []
    for index, value in enumerate(speeds):
        if value is None or value > limit:
            continue
        window = range(max(0, index - reach), min(len(speeds), index + reach + 1))
        candidates = [(speeds[i], i) for i in window if speeds[i] is not None]
        if candidates and min(candidates)[1] == index:
            minima.append(index)
    merged: list[int] = []
    for candidate in minima:
        if merged and lap.samples[candidate].dist - lap.samples[merged[-1]].dist < 150.0:
            if (speeds[candidate] or 1e9) < (speeds[merged[-1]] or 1e9):
                merged[-1] = candidate
            continue
        merged.append(candidate)
    corners = []
    distances = [sample.dist for sample in lap.samples]
    for number, apex in enumerate(merged, start=1):
        apex_speed = speeds[apex] or 0.0
        back = apex
        while back > 0 and distances[apex] - distances[back] < 350.0:
            back -= 1
        plateau = max(range(back, apex + 1), key=lambda i: speeds[i] if speeds[i] is not None else -1.0)
        entry_speed = speeds[plateau] or apex_speed
        brake = plateau
        for index in range(plateau, apex):
            if speeds[index] is not None and speeds[index + 1] is not None \
                    and speeds[index + 1] < speeds[index] - 0.2:
                brake = index
                break
        ceiling = entry_speed * 0.95
        exit_index = apex
        while exit_index < len(speeds) - 1 and exit_index - apex < 800 \
                and speeds[exit_index] is not None and (speeds[exit_index] or 0.0) < ceiling:
            exit_index += 1
        corners.append({
            "corner": number,
            "apex_m": round(lap.samples[apex].dist, 1),
            "apex_speed_mps": round(apex_speed, 2),
            "brake_m": round(lap.samples[brake].dist, 1),
            "exit_m": round(lap.samples[exit_index].dist, 1),
            "exit_speed_mps": round(speeds[exit_index] or apex_speed, 2),
            "corner_s": round(lap.samples[exit_index].t - lap.samples[brake].t, 3),
        })
    return corners


def _lap_payload(lap: Lap) -> dict:
    speeds = lap.speeds()
    return {
        "lap": lap.number,
        "time_s": round(lap.time, 3),
        "samples": len(lap.samples),
        "distance_m": round(lap.length, 1),
        "max_speed_kmh": round(max(speeds) * 3.6, 1) if speeds else None,
        "min_speed_kmh": round(min(speeds) * 3.6, 1) if speeds else None,
        "mean_speed_kmh": round(sum(speeds) / len(speeds) * 3.6, 1) if speeds else None,
    }


def _match(a: Sequence[dict], b: Sequence[dict], reach: float = 80.0) -> list[tuple[dict, dict | None]]:
    pairs: list[tuple[dict, dict | None]] = []
    used: set[int] = set()
    for item in a:
        best, best_gap = None, reach
        for index, other in enumerate(b):
            if index in used:
                continue
            gap = abs(other["apex_m"] - item["apex_m"])
            if gap < best_gap:
                best, best_gap = index, gap
        if best is None:
            pairs.append((item, None))
        else:
            used.add(best)
            pairs.append((item, b[best]))
    return pairs


def _compare(first: Lap, second: Lap, sectors: int) -> dict:
    a_sectors = _sectors(first, sectors)
    b_sectors = _sectors(second, sectors)
    sector_rows = []
    for a, b in zip(a_sectors, b_sectors):
        delta = None if a["time_s"] is None or b["time_s"] is None else round(b["time_s"] - a["time_s"], 3)
        sector_rows.append({**a, "other_time_s": b["time_s"], "delta_s": delta})
    a_corners, b_corners = _corners(first), _corners(second)
    corner_rows = []
    for mine, theirs in _match(a_corners, b_corners):
        row = {
            "corner": mine["corner"],
            "apex_m": mine["apex_m"],
            "apex_speed_kmh": round(mine["apex_speed_mps"] * 3.6, 1),
            "delta_apex_kmh": None,
            "delta_brake_m": None,
            "delta_exit_kmh": None,
            "delta_corner_s": None,
        }
        if theirs is not None:
            row["delta_apex_kmh"] = round((theirs["apex_speed_mps"] - mine["apex_speed_mps"]) * 3.6, 1)
            row["delta_brake_m"] = round(theirs["brake_m"] - mine["brake_m"], 1)
            row["delta_exit_kmh"] = round((theirs["exit_speed_mps"] - mine["exit_speed_mps"]) * 3.6, 1)
            row["delta_corner_s"] = round(theirs["corner_s"] - mine["corner_s"], 3)
        corner_rows.append(row)
    losses = [row for row in corner_rows if row["delta_corner_s"] is not None]
    losses.sort(key=lambda row: row["delta_corner_s"], reverse=True)
    return {
        "laps": [first.number, second.number],
        "lap_time_s": [round(first.time, 3), round(second.time, 3)],
        "delta_s": round(second.time - first.time, 3),
        "sectors": sector_rows,
        "corners": corner_rows,
        "biggest_losses": losses[:3],
        "biggest_gains": sorted(losses, key=lambda row: row["delta_corner_s"])[:3],
    }


def _table(rows: Sequence[dict], columns: Sequence[tuple[str, str]]) -> str:
    header = "| " + " | ".join(title for _, title in columns) + " |"
    rule = "|" + "|".join("---" for _ in columns) + "|"
    body = []
    for row in rows:
        cells = []
        for key, _ in columns:
            value = row.get(key)
            cells.append("—" if value is None else str(value))
        body.append("| " + " | ".join(cells) + " |")
    return "\n".join([header, rule, *body])


def _emit(payload, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(payload)


def _pick_lap(laps: Sequence[Lap], number: int | None) -> Lap:
    if number is None:
        return laps[-1]
    for lap in laps:
        if lap.number == number:
            return lap
    raise SystemExit(f"lap {number} not found; available: {[lap.number for lap in laps]}")


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Racing telemetry CSV: laps, sectors, corners.")
    sub = parser.add_subparsers(dest="command", required=True)

    laps_cmd = sub.add_parser("laps", help="list every lap with time and speed range")
    laps_cmd.add_argument("file", type=Path)
    laps_cmd.add_argument("--json", action="store_true")

    corners_cmd = sub.add_parser("corners", help="braking point, apex and exit per corner")
    corners_cmd.add_argument("file", type=Path)
    corners_cmd.add_argument("--lap", type=int)
    corners_cmd.add_argument("--json", action="store_true")

    compare_cmd = sub.add_parser("compare", help="sector and corner deltas between two laps")
    compare_cmd.add_argument("file", type=Path)
    compare_cmd.add_argument("--laps", required=True, help="pair of lap numbers, e.g. 2,4")
    compare_cmd.add_argument("--sectors", type=int, default=6)
    compare_cmd.add_argument("--json", action="store_true")

    args = parser.parse_args(list(argv) if argv is not None else None)
    laps, meta = _load(args.file)

    if args.command == "laps":
        rows = [_lap_payload(lap) for lap in laps]
        if args.json:
            _emit({"meta": meta, "laps": rows}, True)
        else:
            label = "lap" if len(laps) == 1 else "laps"
            print(f"{args.file.name}: {len(laps)} {label}, {meta['rows']} rows")
            print(_table(rows, [("lap", "lap"), ("time_s", "time (s)"), ("distance_m", "length (m)"),
                                ("samples", "samples"), ("max_speed_kmh", "max km/h"),
                                ("mean_speed_kmh", "mean km/h")]))
        return 0

    if args.command == "corners":
        lap = _pick_lap(laps, args.lap)
        rows = _corners(lap)
        if args.json:
            _emit({"lap": lap.number, "corners": rows}, True)
        else:
            print(f"lap {lap.number} ({lap.time:.3f}s): {len(rows)} corners")
            print(_table([{**row, "apex_kmh": round(row["apex_speed_mps"] * 3.6, 1)} for row in rows],
                         [("corner", "corner"), ("brake_m", "brake (m)"), ("apex_m", "apex (m)"),
                          ("apex_kmh", "apex km/h"), ("exit_m", "exit (m)"), ("corner_s", "time (s)")]))
        return 0

    pair = [int(part) for part in args.laps.replace(" ", "").split(",") if part]
    if len(pair) != 2:
        raise SystemExit("--laps needs exactly two lap numbers, e.g. --laps 2,4")
    first, second = _pick_lap(laps, pair[0]), _pick_lap(laps, pair[1])
    payload = _compare(first, second, max(1, args.sectors))
    if args.json:
        _emit(payload, True)
        return 0
    print(f"lap {first.number} {first.time:.3f}s  vs  lap {second.number} {second.time:.3f}s  "
          f"delta {payload['delta_s']:+.3f}s")
    print()
    print(_table(payload["sectors"], [("sector", "sector"), ("start_m", "from (m)"), ("end_m", "to (m)"),
                                      ("time_s", "A (s)"), ("other_time_s", "B (s)"), ("delta_s", "delta (s)")]))
    print()
    print(_table(payload["corners"], [("corner", "corner"), ("apex_m", "apex (m)"), ("apex_speed_kmh", "apex km/h"),
                                      ("delta_apex_kmh", "d apex km/h"), ("delta_exit_kmh", "d exit km/h"),
                                      ("delta_brake_m", "d brake (m)"), ("delta_corner_s", "d time (s)")]))
    losses = payload["biggest_losses"]
    if losses:
        worst = ", ".join(f"C{row['corner']} {row['delta_corner_s']:+.3f}s" for row in losses)
        print()
        print(f"lost most in: {worst}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
