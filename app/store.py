"""持久化仓储：任务时段、队伍、覆盖脚印、事件入库（含校验与幂等）。"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .kinds import VALID_KINDS
from .timeutil import TimeError, correct_device_time, now_iso, parse_iso, to_iso


class ValidationError(ValueError):
    """单条上报未通过校验。"""


def _require(obj: dict[str, Any], key: str) -> Any:
    value = obj.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValidationError(f"缺少必填字段：{key}")
    return value


def _coord(obj: dict[str, Any], key: str) -> float | None:
    value = obj.get(key)
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{key} 必须是数值") from exc


class Store:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.conn = connection

    # ---------------- 基础资料 ----------------

    def upsert_mission(self, data: dict[str, Any]) -> None:
        ref = _require(data, "mission_ref")
        started = to_iso(parse_iso(_require(data, "started_at"), "started_at"))
        ended = data.get("ended_at")
        ended_iso = to_iso(parse_iso(ended, "ended_at")) if ended else None
        with self.conn:
            self.conn.execute(
                "INSERT INTO missions(mission_ref, name, started_at, ended_at, source, created_at)"
                " VALUES (?,?,?,?,?,?) ON CONFLICT(mission_ref) DO UPDATE SET"
                " name=excluded.name, started_at=excluded.started_at,"
                " ended_at=excluded.ended_at, source=excluded.source",
                (
                    ref,
                    str(data.get("name", "")),
                    started,
                    ended_iso,
                    str(data.get("source", "manual")),
                    now_iso(),
                ),
            )

    def upsert_team(self, data: dict[str, Any]) -> None:
        ref = _require(data, "team_ref")
        lat = _coord(data, "latitude")
        lon = _coord(data, "longitude")
        if (lat is None) != (lon is None):
            raise ValidationError("latitude 与 longitude 必须同时给出或同时省略")
        if lat is not None and not -90.0 <= lat <= 90.0:
            raise ValidationError("latitude 超出 [-90, 90]")
        if lon is not None and not -180.0 <= lon <= 180.0:
            raise ValidationError("longitude 超出 [-180, 180]")
        with self.conn:
            self.conn.execute(
                "INSERT INTO teams(team_ref, name, latitude, longitude, created_at)"
                " VALUES (?,?,?,?,?) ON CONFLICT(team_ref) DO UPDATE SET"
                " name=excluded.name, latitude=excluded.latitude, longitude=excluded.longitude",
                (ref, str(data.get("name", "")), lat, lon, now_iso()),
            )

    def upsert_station_mission(self, data: dict[str, Any]) -> None:
        mission_ref = _require(data, "mission_ref")
        station_ref = _require(data, "station_ref")
        started = to_iso(parse_iso(_require(data, "started_at"), "started_at"))
        ended = data.get("ended_at")
        ended_iso = to_iso(parse_iso(ended, "ended_at")) if ended else None
        with self.conn:
            self.conn.execute(
                "INSERT INTO station_missions"
                "(mission_ref, station_ref, node_kind, altitude_m, started_at, ended_at)"
                " VALUES (?,?,?,?,?,?) ON CONFLICT(mission_ref, station_ref) DO UPDATE SET"
                " node_kind=excluded.node_kind, altitude_m=excluded.altitude_m,"
                " started_at=excluded.started_at, ended_at=excluded.ended_at",
                (
                    mission_ref,
                    station_ref,
                    str(data.get("node_kind", "uav_relay")),
                    _coord(data, "altitude_m"),
                    started,
                    ended_iso,
                ),
            )

    def insert_footprint(self, data: dict[str, Any]) -> int:
        mission_ref = _require(data, "mission_ref")
        station_ref = _require(data, "station_ref")
        valid_from = to_iso(parse_iso(_require(data, "valid_from"), "valid_from"))
        valid_to = data.get("valid_to")
        valid_to_iso = to_iso(parse_iso(valid_to, "valid_to")) if valid_to else None
        lat = _coord(data, "center_lat")
        lon = _coord(data, "center_lon")
        radius = _coord(data, "radius_m")
        if lat is None or lon is None:
            raise ValidationError("覆盖脚印缺少 center_lat/center_lon")
        if radius is None or radius <= 0:
            raise ValidationError("radius_m 必须为正数")
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO footprints"
                "(mission_ref, station_ref, valid_from, valid_to,"
                " center_lat, center_lon, radius_m, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (
                    mission_ref,
                    station_ref,
                    valid_from,
                    valid_to_iso,
                    lat,
                    lon,
                    radius,
                    now_iso(),
                ),
            )
            return int(cur.lastrowid)

    # ---------------- 事件 ----------------

    def ingest_event(
        self,
        raw: dict[str, Any],
        *,
        received_at: datetime | None = None,
    ) -> dict[str, Any]:
        """校验并写入一条事件。

        返回 {"status": "inserted"|"duplicate", "id":..., "source_event_id":...}。
        重复上报（source_ref + source_event_id 相同）原样识别为 duplicate，
        不产生第二条记录，也不会把重复的接入尝试计成新的尝试。
        """
        source_ref = _require(raw, "source_ref")
        source_event_id = str(_require(raw, "source_event_id"))
        mission_ref = _require(raw, "mission_ref")
        kind = _require(raw, "event_kind")
        if kind not in VALID_KINDS:
            raise ValidationError(
                f"未知 event_kind：{kind}；允许 {sorted(VALID_KINDS)}"
            )
        device_time_raw = str(_require(raw, "device_time"))

        if received_at is None and raw.get("received_at"):
            received_at = parse_iso(str(raw["received_at"]), "received_at")

        offset = raw.get("clock_offset_s")
        if offset is not None:
            try:
                offset = float(offset)
            except (TypeError, ValueError) as exc:
                raise ValidationError("clock_offset_s 必须是数值（秒）") from exc

        corrected = correct_device_time(
            device_time_raw,
            clock_offset_s=offset,
            synced_time=raw.get("synced_time"),
            received_at=received_at,
        )

        duplicate = self.conn.execute(
            "SELECT id FROM events WHERE source_ref=? AND source_event_id=?",
            (source_ref, source_event_id),
        ).fetchone()
        if duplicate:
            return {
                "status": "duplicate",
                "id": duplicate["id"],
                "source_event_id": source_event_id,
            }

        lat = _coord(raw, "latitude")
        lon = _coord(raw, "longitude")
        if (lat is None) != (lon is None):
            raise ValidationError("latitude 与 longitude 必须同时给出或同时省略")

        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO events"
                "(source_ref, source_event_id, mission_ref, station_ref, team_ref,"
                " event_kind, device_time_raw, event_time, clock_offset_s,"
                " correction_basis, received_at, is_late, attempt_id, call_id,"
                " latitude, longitude, payload)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    source_ref,
                    source_event_id,
                    mission_ref,
                    str(raw.get("station_ref", "")),
                    str(raw.get("team_ref", "")),
                    kind,
                    device_time_raw,
                    to_iso(corrected.event_time),
                    corrected.clock_offset_s,
                    corrected.basis,
                    to_iso(received_at or datetime.now(timezone.utc)),
                    int(corrected.is_late),
                    str(raw.get("attempt_id", "")),
                    str(raw.get("call_id", "")),
                    lat,
                    lon,
                    json.dumps(raw.get("payload", {}), ensure_ascii=False),
                ),
            )
        return {
            "status": "inserted",
            "id": cur.lastrowid,
            "source_event_id": source_event_id,
            "correction_basis": corrected.basis,
            "is_late": corrected.is_late,
        }

    def ingest_batch(
        self,
        raws: list[dict[str, Any]],
        received_at: datetime | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        accepted: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        duplicates: list[dict[str, Any]] = []
        for index, raw in enumerate(raws):
            if not isinstance(raw, dict):
                rejected.append({"index": index, "error": "事件必须是 JSON 对象"})
                continue
            try:
                result = self.ingest_event(raw, received_at=received_at)
            except (ValidationError, TimeError) as exc:
                rejected.append(
                    {
                        "index": index,
                        "source_event_id": raw.get("source_event_id"),
                        "error": str(exc),
                    }
                )
                continue
            bucket = duplicates if result["status"] == "duplicate" else accepted
            bucket.append(result)
        return {"accepted": accepted, "duplicates": duplicates, "rejected": rejected}
