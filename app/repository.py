"""数据写入：参考数据登记与事件摄入（幂等、只追加原始时间）。"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable, Optional

from .clock import correct
from .timeutil import format_iso, now_utc, parse_iso

VALID_EVENT_KINDS = {
    "access_attempt",
    "access_rejected",
    "call_connected",
    "call_dropped",
    "call_ended",
}


class ValidationError(ValueError):
    """请求数据不合法。"""


def _require(payload: dict, key: str) -> Any:
    value = payload.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValidationError(f"缺少必填字段：{key}")
    return value


def _float(payload: dict, key: str) -> float:
    value = _require(payload, key)
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{key} 必须是数字") from exc


def upsert_mission(conn: sqlite3.Connection, payload: dict) -> dict:
    ref = _require(payload, "mission_ref")
    starts = format_iso(parse_iso(_require(payload, "starts_at"), field="starts_at"))
    ends = payload.get("ends_at")
    ends = format_iso(parse_iso(ends, field="ends_at")) if ends else None
    conn.execute(
        """
        INSERT INTO missions(mission_ref, name, starts_at, ends_at, created_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(mission_ref) DO UPDATE SET
            name=excluded.name,
            starts_at=excluded.starts_at,
            ends_at=COALESCE(excluded.ends_at, missions.ends_at)
        """,
        (ref, payload.get("name"), starts, ends, format_iso(now_utc())),
    )
    return {"mission_ref": ref, "starts_at": starts, "ends_at": ends}


def upsert_station(conn: sqlite3.Connection, payload: dict) -> dict:
    ref = _require(payload, "station_ref")
    mission_ref = _require(payload, "mission_ref")
    if not conn.execute(
        "SELECT 1 FROM missions WHERE mission_ref = ?", (mission_ref,)
    ).fetchone():
        raise ValidationError(f"任务不存在：{mission_ref}")
    conn.execute(
        """
        INSERT INTO stations(station_ref, mission_ref, name)
        VALUES (?, ?, ?)
        ON CONFLICT(station_ref) DO UPDATE SET
            mission_ref=excluded.mission_ref, name=excluded.name
        """,
        (ref, mission_ref, payload.get("name")),
    )
    return {"station_ref": ref, "mission_ref": mission_ref}


def add_station_window(conn: sqlite3.Connection, payload: dict) -> dict:
    station_ref = _require(payload, "station_ref")
    if not conn.execute(
        "SELECT 1 FROM stations WHERE station_ref = ?", (station_ref,)
    ).fetchone():
        raise ValidationError(f"基站不存在：{station_ref}")
    starts = format_iso(parse_iso(_require(payload, "starts_at"), field="starts_at"))
    ends = payload.get("ends_at")
    ends = format_iso(parse_iso(ends, field="ends_at")) if ends else None
    cur = conn.execute(
        "INSERT INTO station_windows(station_ref, starts_at, ends_at, note)"
        " VALUES (?, ?, ?, ?)",
        (station_ref, starts, ends, payload.get("note")),
    )
    return {"id": cur.lastrowid, "station_ref": station_ref,
            "starts_at": starts, "ends_at": ends}


def add_coverage_zone(conn: sqlite3.Connection, payload: dict) -> dict:
    station_ref = _require(payload, "station_ref")
    if not conn.execute(
        "SELECT 1 FROM stations WHERE station_ref = ?", (station_ref,)
    ).fetchone():
        raise ValidationError(f"基站不存在：{station_ref}")
    lat = _float(payload, "center_lat")
    lon = _float(payload, "center_lon")
    radius = _float(payload, "radius_m")
    if not -90 <= lat <= 90:
        raise ValidationError("center_lat 超出 [-90, 90]")
    if not -180 <= lon <= 180:
        raise ValidationError("center_lon 超出 [-180, 180]")
    if radius <= 0:
        raise ValidationError("radius_m 必须为正数")
    valid_from = payload.get("valid_from")
    valid_to = payload.get("valid_to")
    valid_from = (
        format_iso(parse_iso(valid_from, field="valid_from")) if valid_from else None
    )
    valid_to = format_iso(parse_iso(valid_to, field="valid_to")) if valid_to else None
    cur = conn.execute(
        """
        INSERT INTO coverage_zones(station_ref, zone_ref, center_lat, center_lon,
                                   radius_m, valid_from, valid_to)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (station_ref, payload.get("zone_ref"), lat, lon, radius, valid_from,
         valid_to),
    )
    return {"id": cur.lastrowid, "station_ref": station_ref,
            "center_lat": lat, "center_lon": lon, "radius_m": radius}


def upsert_team(conn: sqlite3.Connection, payload: dict) -> dict:
    ref = _require(payload, "team_ref")
    conn.execute(
        """
        INSERT INTO teams(team_ref, name) VALUES (?, ?)
        ON CONFLICT(team_ref) DO UPDATE SET name=excluded.name
        """,
        (ref, payload.get("name")),
    )
    mission_refs = payload.get("mission_refs") or (
        [payload["mission_ref"]] if payload.get("mission_ref") else []
    )
    for mission_ref in mission_refs:
        if not conn.execute(
            "SELECT 1 FROM missions WHERE mission_ref = ?", (mission_ref,)
        ).fetchone():
            raise ValidationError(f"任务不存在：{mission_ref}")
        conn.execute(
            "INSERT OR IGNORE INTO mission_teams(mission_ref, team_ref)"
            " VALUES (?, ?)",
            (mission_ref, ref),
        )
    return {"team_ref": ref, "mission_refs": mission_refs}


def add_team_location(conn: sqlite3.Connection, payload: dict) -> dict:
    team_ref = _require(payload, "team_ref")
    if not conn.execute("SELECT 1 FROM teams WHERE team_ref = ?", (team_ref,)).fetchone():
        raise ValidationError(f"队伍不存在：{team_ref}")
    lat = _float(payload, "lat")
    lon = _float(payload, "lon")
    raw = _require(payload, "observed_at_device")
    device_id = payload.get("device_id")
    corrected_dt, correction_id, offset_used = correct(
        conn, device_id, raw,
        inline_offset_ms=payload.get("clock_offset_ms"),
    )
    cur = conn.execute(
        """
        INSERT INTO team_locations(team_ref, device_id, lat, lon,
                                   observed_at_device, observed_at,
                                   clock_correction_id, received_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (team_ref, device_id, lat, lon, raw, format_iso(corrected_dt),
         correction_id, format_iso(now_utc())),
    )
    return {"id": cur.lastrowid, "team_ref": team_ref,
            "observed_at_device": raw, "observed_at": format_iso(corrected_dt),
            "clock_correction_id": correction_id,
            "clock_offset_ms_used": offset_used}


def add_clock_correction(conn: sqlite3.Connection, payload: dict) -> dict:
    """登记一条设备时钟校正依据。

    新依据默认取代同设备旧依据（历史行保留、仅置 superseded），随后用最新
    依据重算该设备已摄入事件与位置的校正时间——原始时间串永不改写。
    """
    device_id = _require(payload, "device_id")
    try:
        offset_ms = int(_require(payload, "offset_ms"))
    except (TypeError, ValueError) as exc:
        raise ValidationError("offset_ms 必须是整数毫秒") from exc
    basis = _require(payload, "basis")
    effective = payload.get("effective_from_device")
    recorded = format_iso(now_utc())
    if payload.get("supersede_previous", True):
        conn.execute(
            "UPDATE clock_corrections SET superseded = 1 WHERE device_id = ?",
            (device_id,),
        )
    cur = conn.execute(
        """
        INSERT INTO clock_corrections(device_id, offset_ms, basis, evidence,
                                      effective_from_device, recorded_at, superseded)
        VALUES (?, ?, ?, ?, ?, ?, 0)
        """,
        (device_id, offset_ms, basis, payload.get("evidence"), effective, recorded),
    )
    correction_id = cur.lastrowid
    _rederive_device_times(conn, device_id)
    return {"id": correction_id, "device_id": device_id,
            "offset_ms": offset_ms, "basis": basis, "recorded_at": recorded}


def _rederive_device_times(conn: sqlite3.Connection, device_id: str) -> None:
    """用当前最佳依据重算某设备全部事件与位置的校正时间（原始列保持不动）。"""
    for row in conn.execute(
        "SELECT id, event_time_device FROM events WHERE device_id = ?",
        (device_id,),
    ).fetchall():
        corrected_dt, correction_id, offset_used = correct(
            conn, device_id, row["event_time_device"]
        )
        conn.execute(
            "UPDATE events SET event_time_corrected = ?, clock_correction_id = ?,"
            " clock_offset_ms_used = ? WHERE id = ?",
            (format_iso(corrected_dt), correction_id, offset_used, row["id"]),
        )
    for row in conn.execute(
        "SELECT id, observed_at_device FROM team_locations"
        " WHERE device_id = ? AND observed_at_device IS NOT NULL",
        (device_id,),
    ).fetchall():
        corrected_dt, correction_id, _ = correct(
            conn, device_id, row["observed_at_device"]
        )
        conn.execute(
            "UPDATE team_locations SET observed_at = ?, clock_correction_id = ?"
            " WHERE id = ?",
            (format_iso(corrected_dt), correction_id, row["id"]),
        )


def ingest_events(conn: sqlite3.Connection, payload: dict) -> dict:
    """批量摄入事件。source_event_id 幂等；迟到日志按校正时间照常入库。"""
    items = payload.get("events")
    if not isinstance(items, list) or not items:
        raise ValidationError("events 必须是非空数组")
    accepted, duplicates = 0, 0
    received = format_iso(now_utc())
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValidationError(f"events[{index}] 必须是对象")
        source_id = _require(item, "source_event_id")
        team_ref = _require(item, "team_ref")
        kind = _require(item, "event_kind")
        raw_time = _require(item, "event_time_device")
        if kind not in VALID_EVENT_KINDS:
            raise ValidationError(
                f"events[{index}].event_kind 非法：{kind}；"
                f"允许 {sorted(VALID_EVENT_KINDS)}"
            )
        mission_ref = item.get("mission_ref") or payload.get("mission_ref")
        if not mission_ref:
            raise ValidationError(f"events[{index}] 缺少 mission_ref")
        device_id = item.get("device_id")
        corrected_dt, correction_id, offset_used = correct(
            conn, device_id, raw_time,
            inline_offset_ms=item.get("clock_offset_ms"),
        )
        try:
            conn.execute(
                """
                INSERT INTO events(source_event_id, mission_ref, station_ref,
                                   team_ref, device_id, session_ref, event_kind,
                                   event_time_device, event_time_corrected,
                                   clock_correction_id, clock_offset_ms_used,
                                   received_at, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_id, mission_ref, item.get("station_ref"), team_ref,
                    device_id, item.get("session_ref"), kind, raw_time,
                    format_iso(corrected_dt), correction_id, offset_used, received,
                    json.dumps(item.get("payload", {}), ensure_ascii=False),
                ),
            )
            accepted += 1
        except sqlite3.IntegrityError:
            duplicates += 1
    return {"accepted": accepted, "duplicates": duplicates, "received_at": received}
