"""可用性服务：接入链归类、覆盖时间轴、盲区队伍与最近有效联络。

口径（防止误报）：
- access_attempt 次数含同一终端的重复重试；重复上报（同一 source_event_id）不计两次；
- 接入链结果分 success / attached_only / rejected / unknown：
  只有随后出现 call_connected 才算 success（真正通上话）；
  access_accept 后无通话证据只是 attached_only，绝不计为通话或获救；
  只有 attempt、没有终态的链是 unknown（设备外部尝试数就落在这里）；
- “最近一次有效联络”只认 call_connected；附着、尝试都不算。
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime
from typing import Any

from .coverage import haversine_m
from .timeutil import parse_iso


def _iso_window(start: str, end: str) -> tuple[datetime, datetime]:
    s, e = parse_iso(start, "start"), parse_iso(end, "end")
    if e <= s:
        raise ValueError("end 必须晚于 start")
    return s, e


class AvailabilityService:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.conn = connection

    # ---------------- 接入链 ----------------

    def attempt_chains(
        self, mission_ref: str, start: str, end: str
    ) -> dict[str, Any]:
        s, e = _iso_window(start, end)
        rows = self.conn.execute(
            "SELECT * FROM events WHERE mission_ref=? AND event_time>=? AND event_time<?"
            " ORDER BY event_time, id",
            (mission_ref, s.isoformat(), e.isoformat()),
        ).fetchall()

        chains: dict[str, list[sqlite3.Row]] = defaultdict(list)
        unlinked_attempts = 0  # 无 attempt_id 的外部/来路不明接入尝试
        call_events: list[sqlite3.Row] = []
        dropped = 0

        for row in rows:
            kind = row["event_kind"]
            if kind == "access_attempt" and not row["attempt_id"]:
                unlinked_attempts += 1
            if row["attempt_id"]:
                chains[row["attempt_id"]].append(row)
            if kind == "call_connected":
                call_events.append(row)
            elif kind == "call_dropped":
                dropped += 1

        chain_reports: list[dict[str, Any]] = []
        outcome_counts = {"success": 0, "attached_only": 0, "rejected": 0, "unknown": 0}
        attempt_events_total = 0
        retries_total = 0

        for attempt_id, events in sorted(chains.items()):
            kinds = [ev["event_kind"] for ev in events]
            attempts = kinds.count("access_attempt")
            attempt_events_total += attempts
            retries = max(0, attempts - 1)
            retries_total += retries

            if "access_reject" in kinds:
                outcome = "rejected"
            elif "access_accept" in kinds:
                outcome = "success" if "call_connected" in kinds else "attached_only"
            else:
                outcome = "unknown"  # 只有尝试，没有终态
            outcome_counts[outcome] += 1

            chain_reports.append(
                {
                    "attempt_id": attempt_id,
                    "team_ref": next((ev["team_ref"] for ev in events if ev["team_ref"]), ""),
                    "station_ref": events[0]["station_ref"],
                    "started_at": events[0]["event_time"],
                    "ended_at": events[-1]["event_time"],
                    "outcome": outcome,
                    "attempts": attempts,          # 含重复重试
                    "retries": retries,
                    "has_call_connected": "call_connected" in kinds,
                    "unknown_result": outcome == "unknown",
                }
            )

        call_ids = {row["call_id"] for row in call_events if row["call_id"]}
        return {
            "window": {"start": s.isoformat(), "end": e.isoformat()},
            "mission_ref": mission_ref,
            "totals": {
                "access_attempt_events": attempt_events_total,
                "retries": retries_total,
                "attempt_chains": len(chain_reports),
                "chains_success": outcome_counts["success"],
                "chains_attached_only": outcome_counts["attached_only"],
                "chains_rejected": outcome_counts["rejected"],
                "chains_unknown": outcome_counts["unknown"],
                "unlinked_external_attempts": unlinked_attempts,
                "calls_connected": len(call_ids) if call_ids else len(call_events),
                "calls_dropped": dropped,
                "teams_with_call": len(
                    {row["team_ref"] for row in call_events if row["team_ref"]}
                ),
            },
            "interpretation": (
                "access_attempt_events 含重复重试，不是人数；"
                "chains_attached_only 只表示附着成功、并无通话；"
                "真正通话与有效联络只看 calls_connected / call_connected 事件。"
            ),
            "chains": chain_reports,
        }

    # ---------------- 覆盖与盲区 ----------------

    def _load_coverage_model(self, mission_ref: str):
        nodes = {
            row["station_ref"]: row
            for row in self.conn.execute(
                "SELECT * FROM station_missions WHERE mission_ref=?",
                (mission_ref,),
            ).fetchall()
        }
        footprints = self.conn.execute(
            "SELECT * FROM footprints WHERE mission_ref=? ORDER BY valid_from, id",
            (mission_ref,),
        ).fetchall()
        teams = self.conn.execute("SELECT * FROM teams ORDER BY team_ref").fetchall()
        positions = self.conn.execute(
            "SELECT team_ref, latitude, longitude, event_time FROM events"
            " WHERE mission_ref=? AND latitude IS NOT NULL AND longitude IS NOT NULL"
            " AND team_ref != '' ORDER BY event_time, id",
            (mission_ref,),
        ).fetchall()
        return nodes, footprints, teams, positions

    def _coverage_segments(self, footprints, nodes, s: datetime, e: datetime):
        """返回 [(seg_start, seg_end, [生效脚印])]，时间轴按脚印边界切分。"""
        bounds = [s, e]
        for fp in footprints:
            vf = parse_iso(fp["valid_from"])
            vt = parse_iso(fp["valid_to"]) if fp["valid_to"] else None
            if vf > s and vf < e:
                bounds.append(vf)
            if vt and vt > s and vt < e:
                bounds.append(vt)
        bounds = sorted(set(bounds))
        segments = []
        for a, b in zip(bounds, bounds[1:]):
            if b <= a:
                continue
            mid = a + (b - a) / 2
            active = []
            for fp in footprints:
                vf = parse_iso(fp["valid_from"])
                vt = parse_iso(fp["valid_to"]) if fp["valid_to"] else None
                if not (vf <= mid and (vt is None or vt > mid)):
                    continue
                node = nodes.get(fp["station_ref"])
                if node is not None:
                    n_start = parse_iso(node["started_at"])
                    n_end = parse_iso(node["ended_at"]) if node["ended_at"] else None
                    if not (n_start <= mid and (n_end is None or n_end > mid)):
                        continue
                active.append(fp)
            segments.append((a, b, active))
        return segments

    def blind_zones(
        self, mission_ref: str, start: str, end: str
    ) -> dict[str, Any]:
        s, e = _iso_window(start, end)
        nodes, footprints, teams, positions = self._load_coverage_model(mission_ref)
        segments = self._coverage_segments(footprints, nodes, s, e)

        pos_by_team: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for row in positions:
            pos_by_team[row["team_ref"]].append(row)

        # 最近一次有效联络（只认 call_connected），截止窗口末端
        contact_rows = self.conn.execute(
            "SELECT team_ref, event_time, station_ref, call_id, device_time_raw,"
            " correction_basis, is_late FROM events"
            " WHERE mission_ref=? AND event_kind='call_connected' AND event_time<?"
            " ORDER BY event_time DESC",
            (mission_ref, e.isoformat()),
        ).fetchall()
        last_contact: dict[str, sqlite3.Row] = {}
        for row in contact_rows:
            if row["team_ref"] and row["team_ref"] not in last_contact:
                last_contact[row["team_ref"]] = row

        # 窗口内真正通话的队伍集合
        called_in_window = {
            row["team_ref"]
            for row in self.conn.execute(
                "SELECT DISTINCT team_ref FROM events"
                " WHERE mission_ref=? AND event_kind='call_connected'"
                " AND event_time>=? AND event_time<? AND team_ref!=''",
                (mission_ref, s.isoformat(), e.isoformat()),
            ).fetchall()
        }

        team_reports = []
        for team in teams:
            ref = team["team_ref"]
            samples = pos_by_team.get(ref, [])

            def position_at(t: datetime):
                lat, lon = team["latitude"], team["longitude"]
                for sample in samples:  # 已按时间升序
                    if parse_iso(sample["event_time"]) <= t:
                        lat, lon = sample["latitude"], sample["longitude"]
                    else:
                        break
                return lat, lon

            covered_s = 0.0
            covered_at_end = False
            location_unknown = False
            for a, b, active in segments:
                lat, lon = position_at(a)
                if lat is None or lon is None:
                    continue
                hit = any(
                    haversine_m(lat, lon, fp["center_lat"], fp["center_lon"])
                    <= fp["radius_m"]
                    for fp in active
                )
                if hit:
                    covered_s += (b - a).total_seconds()
                    if b == e:
                        covered_at_end = True

            if team["latitude"] is None and not samples:
                location_unknown = True

            contact = last_contact.get(ref)
            report = {
                "team_ref": ref,
                "name": team["name"],
                "location_known": not location_unknown,
                "covered_seconds_in_window": round(covered_s, 1),
                "covered_at_window_end": covered_at_end,
                "had_call_in_window": ref in called_in_window,
                # 盲区：窗口末端不在任何有效覆盖圆内（位置已知）；
                # 仅有接入尝试/附着不改变该判定。
                "in_blind_zone": (not location_unknown) and (not covered_at_end),
                "last_valid_contact": None
                if contact is None
                else {
                    "at": contact["event_time"],
                    "station_ref": contact["station_ref"],
                    "call_id": contact["call_id"],
                    "device_time_raw": contact["device_time_raw"],
                    "correction_basis": contact["correction_basis"],
                    "is_late_log": bool(contact["is_late"]),
                    "seconds_before_window_end": round(
                        (e - parse_iso(contact["event_time"])).total_seconds(), 1
                    ),
                },
            }
            team_reports.append(report)

        blind = [t for t in team_reports if t["in_blind_zone"]]
        unknown_pos = [t["team_ref"] for t in team_reports if not t["location_known"]]
        return {
            "window": {"start": s.isoformat(), "end": e.isoformat()},
            "mission_ref": mission_ref,
            "summary": {
                "teams_total": len(team_reports),
                "teams_in_blind_zone": len(blind),
                "teams_location_unknown": len(unknown_pos),
                "teams_with_call_in_window": len(called_in_window),
            },
            "blind_zone_teams": blind,
            "teams_location_unknown": unknown_pos,
            "teams": team_reports,
        }

    # ---------------- 原始事件核查 ----------------

    def list_events(
        self,
        mission_ref: str,
        start: str | None = None,
        end: str | None = None,
        team_ref: str | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        sql = "SELECT * FROM events WHERE mission_ref=?"
        params: list[Any] = [mission_ref]
        if start:
            sql += " AND event_time>=?"
            params.append(parse_iso(start, "start").isoformat())
        if end:
            sql += " AND event_time<?"
            params.append(parse_iso(end, "end").isoformat())
        if team_ref:
            sql += " AND team_ref=?"
            params.append(team_ref)
        sql += " ORDER BY event_time, id LIMIT ?"
        params.append(min(max(1, limit), 1000))
        rows = self.conn.execute(sql, params).fetchall()
        return {
            "events": [
                {
                    "source_ref": r["source_ref"],
                    "source_event_id": r["source_event_id"],
                    "event_kind": r["event_kind"],
                    "team_ref": r["team_ref"],
                    "station_ref": r["station_ref"],
                    "attempt_id": r["attempt_id"],
                    "call_id": r["call_id"],
                    "device_time_raw": r["device_time_raw"],
                    "event_time": r["event_time"],
                    "clock_offset_s": r["clock_offset_s"],
                    "correction_basis": r["correction_basis"],
                    "received_at": r["received_at"],
                    "is_late_log": bool(r["is_late"]),
                }
                for r in rows
            ]
        }
