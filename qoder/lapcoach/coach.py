"""复盘逻辑：解析遥测 → 找最快圈与最亏弯道 → 生成可入库的结论文本。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date as date_type
from pathlib import Path

from . import telemetry

CORNER_RE = re.compile(r"C(\d+)=([\d.]+)")
WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9._-]*")


def search_query(question: str) -> str:
    """记忆检索走 FTS，中文整句匹配不到，先抽问题里的赛道名/弯号等关键词。"""
    words = WORD_RE.findall(question)
    return " ".join(dict.fromkeys(words)) if words else question


@dataclass
class Analysis:
    track: str
    day: str
    file: str
    laps: list[dict]
    fastest: telemetry.Lap
    reference: telemetry.Lap
    compare: dict
    corners: list[dict]

    @property
    def best_s(self) -> float:
        return round(self.fastest.time, 3)

    @property
    def delta_s(self) -> float:
        return round(self.fastest.time - self.reference.time, 3)


def analyse(path: Path, track: str, day: str) -> Analysis:
    laps, _meta = telemetry._load(path)
    fastest = min(laps, key=lambda lap: lap.time)
    others = [lap for lap in laps if lap.number != fastest.number]
    reference = min(others, key=lambda lap: lap.time) if others else fastest
    return Analysis(
        track=track,
        day=day,
        file=path.name,
        laps=[telemetry._lap_payload(lap) for lap in laps],
        fastest=fastest,
        reference=reference,
        compare=telemetry._compare(reference, fastest, 6),
        corners=telemetry._corners(fastest),
    )


def corner_rows(analysis: Analysis) -> list[dict]:
    """最快圈自身每个弯的数据，叠加相对参考圈的差值。"""
    rows = []
    for corner in analysis.corners:
        match = next(
            (row for row in analysis.compare["corners"]
             if row["delta_corner_s"] is not None and abs(row["apex_m"] - corner["apex_m"]) <= 80.0),
            None,
        )
        rows.append({
            "corner": corner["corner"],
            "apex_m": corner["apex_m"],
            "apex_kmh": round(corner["apex_speed_mps"] * 3.6, 1),
            "d_apex_kmh": None if match is None else match["delta_apex_kmh"],
            "d_brake_m": None if match is None else match["delta_brake_m"],
            "d_s": None if match is None else match["delta_corner_s"],
        })
    rows.sort(key=lambda row: row["corner"])
    return rows


def losses(analysis: Analysis) -> list[dict]:
    return sorted(
        (row for row in corner_rows(analysis) if row["d_s"] is not None and row["d_s"] > 0),
        key=lambda row: row["d_s"],
        reverse=True,
    )


def report(analysis: Analysis) -> str:
    lines = [
        f"{analysis.track} · {analysis.day} · {analysis.file}",
        telemetry._table(
            analysis.laps,
            [("lap", "lap"), ("time_s", "time (s)"), ("distance_m", "length (m)"),
             ("max_speed_kmh", "max km/h"), ("mean_speed_kmh", "mean km/h")],
        ),
        "",
        f"最快 {analysis.fastest.number} 圈 {analysis.best_s:.3f}s，"
        f"比参考 {analysis.reference.number} 圈快 {abs(analysis.delta_s):.3f}s",
        telemetry._table(
            corner_rows(analysis),
            [("corner", "corner"), ("apex_m", "apex (m)"), ("apex_kmh", "apex km/h"),
             ("d_apex_kmh", "d apex"), ("d_brake_m", "d brake (m)"), ("d_s", "d time (s)")],
        ),
    ]
    worst = losses(analysis)
    if worst:
        summary = ", ".join(f"C{row['corner']} {row['d_s']:+.3f}s" for row in worst[:3])
        lines.append(f"最亏：{summary}")
    return "\n".join(lines)


def measurement_text(analysis: Analysis, driver: str) -> str:
    rows = corner_rows(analysis)
    corners = ",".join(f"C{row['corner']}={row['apex_kmh']}" for row in rows)
    worst = losses(analysis)
    worst_label = f"C{worst[0]['corner']}" if worst else "none"
    headline = (
        f"{analysis.track} {analysis.day}：第 {analysis.fastest.number} 圈最快 {analysis.best_s:.3f}s，"
        f"比第 {analysis.reference.number} 圈快 {abs(analysis.delta_s):.3f}s"
        + (f"，最亏的是 C{worst[0]['corner']}（{worst[0]['d_s']:+.3f}s）。" if worst else "。")
    )
    trailer = (
        f"track={analysis.track} date={analysis.day} kind=measurement driver={driver} "
        f"file={analysis.file} lap={analysis.fastest.number} best_s={analysis.best_s:.3f} "
        f"ref_lap={analysis.reference.number} delta_s={analysis.delta_s:+.3f} worst={worst_label} corners={corners}"
    )
    return f"{headline}\n{trailer}"


def observation_texts(analysis: Analysis) -> list[str]:
    out = []
    for row in losses(analysis)[:3]:
        apex_delta = row["d_apex_kmh"] or 0.0
        brake_delta = row["d_brake_m"] or 0.0
        brake_text = "刹车点相同" if abs(brake_delta) < 0.5 else (
            f"刹车点{'早' if brake_delta < 0 else '晚'} {abs(brake_delta):.0f} m"
        )
        out.append(
            f"{analysis.track} {analysis.day}：C{row['corner']} 是全圈最亏的弯，apex {row['apex_kmh']} km/h，"
            f"比参考圈{'低' if apex_delta < 0 else '高'} {abs(apex_delta):.1f} km/h，{brake_text}"
            f"（{row['d_s']:+.3f}s）。下一场先练这个弯。\n"
            f"track={analysis.track} date={analysis.day} kind=observation corner=C{row['corner']} "
            f"apex_kmh={row['apex_kmh']} delta_apex_kmh={row['d_apex_kmh']} "
            f"delta_brake_m={row['d_brake_m']} delta_s={row['d_s']:+.3f}"
        )
    return out


def parse_measurement(text: str) -> dict | None:
    trailer = next((line for line in text.splitlines() if "kind=measurement" in line), None)
    if trailer is None:
        return None
    parts = trailer.split()
    fields: dict[str, object] = {}
    for part in parts:
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        if key == "corners":
            fields["corners"] = {f"C{number}": float(speed) for number, speed in CORNER_RE.findall(value)}
        else:
            fields[key] = value
    return fields


def progress_lines(previous: dict, analysis: Analysis) -> list[str]:
    lines = [f"上次记忆：{previous.get('date', '?')} 在 {previous.get('track', '?')} 最快 {previous.get('best_s', '?')}s"]
    if previous.get("best_s") and previous.get("best_s") != "?":
        try:
            diff = analysis.best_s - float(previous["best_s"])
        except ValueError:
            diff = None
        if diff is not None:
            lines.append(f"本场最快 {analysis.best_s:.3f}s，比上次 {'快' if diff < 0 else '慢'} {abs(diff):.3f}s")
    last_corners = previous.get("corners") or {}
    today_corners = {f"C{row['corner']}": row["apex_kmh"] for row in corner_rows(analysis)}
    changes = []
    for name, speed in today_corners.items():
        old = last_corners.get(name)
        if old is None or speed is None:
            continue
        changes.append((speed - float(old), name, float(old), speed))
    changes.sort(key=lambda item: abs(item[0]), reverse=True)
    for diff, name, old, speed in changes[:3]:
        if abs(diff) < 0.05:
            continue
        arrow = "↑" if diff > 0 else "↓"
        lines.append(f"{name} apex {old:.1f} → {speed:.1f} km/h（{arrow}{abs(diff):.1f}）")
    return lines


def today() -> str:
    return date_type.today().isoformat()
