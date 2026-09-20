"""核心可用性服务：覆盖判定、接入尝试关联分类、盲区与最近有效联络。

关键口径（防止误报）：
- access_attempt 只是“外部接入尝试”，永远不计为通话或获救；
- 有效联络以 call_connected 为准；同一通话链路内的其余尝试计为重复重试；
- 时段结束仍无任何终局事件（接通/拒绝）的尝试链路计为 unknown（未知结果）；
- call_dropped 不抹掉“曾接通”的事实，只标注中断。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from .geo import in_circle
from .timeutil import format_iso, now_utc, parse_iso

TERMINAL_KINDS = {"call_connected", "access_rejected"}


@dataclass
class AttemptChain:
    """一次接入过程：从首个尝试到终局（接通/拒绝）或悬而未决。"""

    key: str
    team_ref: str
    station_ref: Optional[str] = None
    session_ref: Optional[str] = None
    attempts: list[sqlite3.Row] = field(default_factory=list)
    state: str = "pending"  # pending / connected / rejected
    connected_event: Optional[sqlite3.Row] = None
    dropped_event: Optional[sqlite3.Row] = None
    ended_event: Optional[sqlite3.Row] = None

    @property
    def retry_count(self) -> int:
        return max(len(self.attempts) - 1, 0)

    def to_dict(self) -> dict:
        first = self.attempts[0] if self.attempts else None
        last_attempt = self.attempts[-1] if self.attempts else None
        connected_at = (
            self.connected_event["event_time_corrected"]
            if self.connected_event else None
        )
        return {
            "team_ref": self.team_ref,
            "station_ref": self.station_ref
            or (self.connected_event["station_ref"] if self.connected_event else None)
            or (first["station_ref"] if first else None),
            "session_ref": self.session_ref,
            "outcome": self.state if self.state != "pending" else "unknown",
            "attempt_count": len(self.attempts),
            "retry_count": self.retry_count,
            "first_attempt_at": first["event_time_corrected"] if first else None,
            "first_attempt_at_device": first["event_time_device"] if first else None,
            "last_attempt_at": (
                last_attempt["event_time_corrected"] if last_attempt else None
            ),
            "connected_at": connected_at,
            "connected_at_device": (
                self.connected_event["event_time_device"]
                if self.connected_event else None
            ),
            "call_established": self.state == "connected",
            "dropped": self.dropped_event is not None,
            "dropped_at": (
                self.dropped_event["event_time_corrected"]
                if self.dropped_event else None
            ),
            "ended_normally": self.ended_event is not None,
            "attempt_source_ids": [e["source_event_id"] for e in self.attempts],
        }


def _chain_key(event: sqlite3.Row) -> str:
    return event["session_ref"] or "_anon"


def classify_team_events(events: list[sqlite3.Row]) -> list[AttemptChain]:
    """把单支队伍按时序排列的事件归入接入链路并分类。"""
    chains: dict[str, AttemptChain] = {}
    last_connected: Optional[AttemptChain] = None

    for event in sorted(events, key=lambda e: e["event_time_corrected"]):
        kind = event["event_kind"]
        key = _chain_key(event)
        chain = chains.get(key)

        if kind == "access_attempt":
            # 同一会话在终局之后又来尝试：视为新的一轮接入。
            if chain is not None and chain.state in TERMINAL_KINDS:
                chain = None
                key = f"{key}#r{len(chains)}"
            if chain is None:
                chain = AttemptChain(
                    key=key, team_ref=event["team_ref"],
                    station_ref=event["station_ref"],
                    session_ref=event["session_ref"],
                )
                chains[key] = chain
            chain.attempts.append(event)
            if chain.station_ref is None:
                chain.station_ref = event["station_ref"]

        elif kind == "access_rejected":
            if chain is None:
                # 拒绝记录早于/缺失尝试：仍作为一条无尝试的终局链保留。
                chain = AttemptChain(
                    key=key, team_ref=event["team_ref"],
                    station_ref=event["station_ref"],
                    session_ref=event["session_ref"],
                )
                chains[key] = chain
            chain.state = "rejected"

        elif kind == "call_connected":
            if chain is None or chain.state in TERMINAL_KINDS:
                # 未观测到尝试（日志缺口）或同会话新一轮：以接通本身建链，
                # 标记 attempt_gap，绝不凭空补尝试数。
                chain = AttemptChain(
                    key=key if chain is None else f"{key}#c{len(chains)}",
                    team_ref=event["team_ref"],
                    station_ref=event["station_ref"],
                    session_ref=event["session_ref"],
                )
                chains[chain.key] = chain
            chain.state = "connected"
            chain.connected_event = event
            last_connected = chain

        elif kind == "call_dropped":
            target = chain if chain and chain.state == "connected" else last_connected
            if target is not None and target.dropped_event is None:
                target.dropped_event = event
            else:
                # 没有对应接通记录的中断：单列，避免被误算成成功通话。
                orphan = AttemptChain(
                    key=f"orphan-drop-{event['source_event_id']}",
                    team_ref=event["team_ref"], station_ref=event["station_ref"],
                    session_ref=event["session_ref"], state="pending",
                )
                orphan.dropped_event = event
                chains[orphan.key] = orphan

        elif kind == "call_ended":
            target = chain if chain and chain.state == "connected" else last_connected
            if target is not None:
                target.ended_event = event

    return list(chains.values())


# ---------------------------------------------------------------- 覆盖判定

def active_zones_at(
    conn: sqlite3.Connection, station_ref: str, moment: datetime
) -> list[sqlite3.Row]:
    """某基站在指定时刻有效的覆盖几何（必须落在实际服务窗口内）。"""
    moment_iso = format_iso(moment)
    window = conn.execute(
        """
        SELECT 1 FROM station_windows
        WHERE station_ref = ? AND starts_at <= ?
          AND (ends_at IS NULL OR ends_at >= ?)
        LIMIT 1
        """,
        (station_ref, moment_iso, moment_iso),
    ).fetchone()
    if window is None:
        return []
    zones = conn.execute(
        """
        SELECT * FROM coverage_zones
        WHERE station_ref = ?
          AND (valid_from IS NULL OR valid_from <= ?)
          AND (valid_to IS NULL OR valid_to >= ?)
        """,
        (station_ref, moment_iso, moment_iso),
    ).fetchall()
    return zones


def team_position_as_of(
    conn: sqlite3.Connection, team_ref: str, moment: datetime
) -> Optional[sqlite3.Row]:
    return conn.execute(
        """
        SELECT * FROM team_locations
        WHERE team_ref = ? AND observed_at <= ?
        ORDER BY observed_at DESC, id DESC LIMIT 1
        """,
        (team_ref, format_iso(moment)),
    ).fetchone()


def is_covered(
    conn: sqlite3.Connection, team_ref: str, moment: datetime
) -> Optional[bool]:
    """队伍在某时刻是否处于任一在空中基站的覆盖区内。

    无位置数据返回 None（未知），区别于明确的 False。
    """
    position = team_position_as_of(conn, team_ref, moment)
    if position is None:
        return None
    stations = conn.execute(
        "SELECT DISTINCT station_ref FROM coverage_zones"
    ).fetchall()
    for row in stations:
        for zone in active_zones_at(conn, row["station_ref"], moment):
            if in_circle(position["lat"], position["lon"], zone):
                return True
    return False


# ---------------------------------------------------------------- 时段汇总

def _resolve_window(
    conn: sqlite3.Connection,
    mission_ref: str,
    start: Optional[str],
    end: Optional[str],
) -> tuple[datetime, datetime]:
    mission = conn.execute(
        "SELECT * FROM missions WHERE mission_ref = ?", (mission_ref,)
    ).fetchone()
    if mission is None:
        raise ValueError(f"任务不存在：{mission_ref}")
    start_dt = parse_iso(start) if start else parse_iso(mission["starts_at"])
    if end:
        end_dt = parse_iso(end)
    elif mission["ends_at"]:
        end_dt = parse_iso(mission["ends_at"])
    else:
        end_dt = now_utc()
    if end_dt < start_dt:
        raise ValueError("时段结束早于开始")
    return start_dt, end_dt


def _events_in_window(
    conn: sqlite3.Connection, mission_ref: str,
    start_dt: datetime, end_dt: datetime,
    team_ref: Optional[str] = None,
) -> list[sqlite3.Row]:
    sql = (
        "SELECT * FROM events WHERE mission_ref = ?"
        " AND event_time_corrected >= ? AND event_time_corrected <= ?"
    )
    params: list[object] = [
        mission_ref, format_iso(start_dt), format_iso(end_dt)
    ]
    if team_ref:
        sql += " AND team_ref = ?"
        params.append(team_ref)
    sql += " ORDER BY event_time_corrected"
    return conn.execute(sql, params).fetchall()


def availability_report(
    conn: sqlite3.Connection, mission_ref: str,
    start: Optional[str] = None, end: Optional[str] = None,
) -> dict:
    """指定时段的接入尝试关联分类汇总（按队伍）。"""
    start_dt, end_dt = _resolve_window(conn, mission_ref, start, end)
    events = _events_in_window(conn, mission_ref, start_dt, end_dt)

    by_team: dict[str, list[sqlite3.Row]] = {}
    for event in events:
        by_team.setdefault(event["team_ref"], []).append(event)
    # 花名册中但时段内零事件的队伍同样列出，不因其沉默而消失。
    for team_ref in _mission_teams(conn, mission_ref):
        by_team.setdefault(team_ref, [])

    teams_out = []
    totals = {
        "access_attempts": 0,
        "retry_attempts": 0,
        "chains_connected": 0,
        "chains_rejected": 0,
        "chains_unknown": 0,
        "calls_dropped": 0,
    }
    for team_ref, team_events in sorted(by_team.items()):
        chains = classify_team_events(team_events)
        chain_dicts = [c.to_dict() for c in chains]
        attempts = sum(c["attempt_count"] for c in chain_dicts)
        connected = sum(1 for c in chain_dicts if c["outcome"] == "connected")
        rejected = sum(1 for c in chain_dicts if c["outcome"] == "rejected")
        unknown = sum(1 for c in chain_dicts if c["outcome"] == "unknown")
        dropped = sum(1 for c in chain_dicts if c["dropped"])
        teams_out.append({
            "team_ref": team_ref,
            "access_attempts": attempts,
            # 明确口径：尝试数 ≠ 通话数；冗余重试单列。
            "successful_calls": connected,
            "rejected": rejected,
            "unknown_outcome": unknown,
            "retry_attempts": sum(c["retry_count"] for c in chain_dicts),
            "calls_dropped": dropped,
            "chains": chain_dicts,
        })
        totals["access_attempts"] += attempts
        totals["retry_attempts"] += sum(c["retry_count"] for c in chain_dicts)
        totals["chains_connected"] += connected
        totals["chains_rejected"] += rejected
        totals["chains_unknown"] += unknown
        totals["calls_dropped"] += dropped

    return {
        "mission_ref": mission_ref,
        "window_start": format_iso(start_dt),
        "window_end": format_iso(end_dt),
        "totals": totals,
        "interpretation": (
            "access_attempts 为外部接入尝试总次数，包含重复重试；"
            "successful_calls 以 call_connected 为准；"
            "unknown_outcome 为时段结束仍无终局的尝试；尝试次数不代表获救或实际通话。"
        ),
        "teams": teams_out,
    }


# ---------------------------------------------------------------- 盲区

def _mission_teams(conn: sqlite3.Connection, mission_ref: str) -> list[str]:
    rostered = {
        r["team_ref"] for r in conn.execute(
            "SELECT team_ref FROM mission_teams WHERE mission_ref = ?",
            (mission_ref,),
        )
    }
    seen_events = {
        r["team_ref"] for r in conn.execute(
            "SELECT DISTINCT team_ref FROM events WHERE mission_ref = ?",
            (mission_ref,),
        )
    }
    return sorted(rostered | seen_events)


def _last_effective_contact(
    conn: sqlite3.Connection, team_ref: str,
    before: Optional[datetime] = None,
) -> Optional[dict]:
    sql = (
        "SELECT * FROM events WHERE team_ref = ? AND event_kind = 'call_connected'"
    )
    params: list[object] = [team_ref]
    if before is not None:
        sql += " AND event_time_corrected <= ?"
        params.append(format_iso(before))
    sql += " ORDER BY event_time_corrected DESC LIMIT 1"
    row = conn.execute(sql, params).fetchone()
    if row is None:
        return None
    correction = None
    if row["clock_correction_id"]:
        correction = conn.execute(
            "SELECT basis, evidence, offset_ms FROM clock_corrections WHERE id = ?",
            (row["clock_correction_id"],),
        ).fetchone()
    return {
        "connected_at": row["event_time_corrected"],
        "connected_at_device": row["event_time_device"],
        "station_ref": row["station_ref"],
        "mission_ref": row["mission_ref"],
        "clock_offset_ms_used": row["clock_offset_ms_used"],
        "clock_basis": correction["basis"] if correction else None,
        "clock_evidence": correction["evidence"] if correction else None,
        "source_event_id": row["source_event_id"],
    }


def blind_spots(
    conn: sqlite3.Connection, mission_ref: str,
    start: Optional[str] = None, end: Optional[str] = None,
) -> dict:
    """找出指定时段内没有有效联络（call_connected）的队伍。

    附两类判定依据，避免把接入冗余当成获救：
    - coverage：窗口末端时刻的覆盖状态（covered / not_covered / unknown）；
    - last_effective_contact：该队伍最近一次真正接通（可早于本时段），
      同时给出设备原始时间与所用时钟校正依据。
    """
    start_dt, end_dt = _resolve_window(conn, mission_ref, start, end)
    end_iso = format_iso(end_dt)

    contacted_in_window = {
        r["team_ref"] for r in conn.execute(
            """
            SELECT DISTINCT team_ref FROM events
            WHERE mission_ref = ? AND event_kind = 'call_connected'
              AND event_time_corrected BETWEEN ? AND ?
            """,
            (mission_ref, format_iso(start_dt), end_iso),
        )
    }

    result_teams = []
    for team_ref in _mission_teams(conn, mission_ref):
        if team_ref in contacted_in_window:
            continue
        covered = is_covered(conn, team_ref, end_dt)
        if covered is None:
            status = "unknown_position"
        elif covered:
            status = "covered_but_no_call"
        else:
            status = "not_covered"
        attempts_in_window = conn.execute(
            """
            SELECT COUNT(*) AS n FROM events
            WHERE mission_ref = ? AND team_ref = ?
              AND event_kind = 'access_attempt'
              AND event_time_corrected BETWEEN ? AND ?
            """,
            (mission_ref, team_ref, format_iso(start_dt), end_iso),
        ).fetchone()["n"]
        result_teams.append({
            "team_ref": team_ref,
            "coverage_at_window_end": status,
            "access_attempts_in_window": attempts_in_window,
            # 明示：有尝试不等于联系上，队伍仍在盲区名单。
            "note": (
                "时段内无成功通话；access_attempts_in_window 仅为接入尝试次数"
                if attempts_in_window else "时段内无成功通话，亦无接入尝试"
            ),
            "last_effective_contact": _last_effective_contact(conn, team_ref),
        })

    return {
        "mission_ref": mission_ref,
        "window_start": format_iso(start_dt),
        "window_end": end_iso,
        "blind_team_count": len(result_teams),
        "teams": result_teams,
    }
