"""设备时钟校正。

校正公式：corrected_time = device_time + offset_ms。
校正依据可能晚于事件到达（例如 GNSS 对时结果稍后补传），因此：
- 事件表永久保留 event_time_device 原始串；
- 每次应用的依据编号与实际偏移写入事件行，可追溯；
- 旧依据标记 superseded 但不删除，支持回放当时的判断。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from .timeutil import format_iso, parse_iso


@dataclass(frozen=True)
class Correction:
    id: int
    offset_ms: int

    def apply(self, device_dt: datetime) -> datetime:
        return device_dt + timedelta(milliseconds=self.offset_ms)


def resolve_correction(
    connection: sqlite3.Connection,
    device_id: str,
    device_dt: datetime,
    *,
    as_of: Optional[datetime] = None,
    include_superseded: bool = False,
) -> Optional[Correction]:
    """选取适用于某设备时间点的最新校正依据。

    - effective_from_device 为空表示覆盖该设备最早事件；
    - 多条依据适用时，取 recorded_at 最新的一条；
    - as_of 用于回放“事件到达那一刻已知的最佳依据”。
    """
    sql = (
        "SELECT id, offset_ms, effective_from_device, recorded_at"
        " FROM clock_corrections "
        "WHERE device_id = ?"
    )
    params: list[object] = [device_id]
    if not include_superseded:
        sql += " AND superseded = 0"
    if as_of is not None:
        sql += " AND recorded_at <= ?"
        params.append(format_iso(as_of))
    rows = connection.execute(sql, params).fetchall()

    chosen: Optional[sqlite3.Row] = None
    chosen_recorded: Optional[datetime] = None
    for row in rows:
        if row["effective_from_device"]:
            effective = parse_iso(
                row["effective_from_device"], field="effective_from_device"
            )
            if device_dt < effective:
                continue
        recorded = parse_iso(row["recorded_at"], field="recorded_at")
        if chosen is None or recorded > chosen_recorded:
            chosen, chosen_recorded = row, recorded
    return Correction(chosen["id"], chosen["offset_ms"]) if chosen else None


def correct(
    connection: sqlite3.Connection,
    device_id: Optional[str],
    device_time_raw: str,
    *,
    inline_offset_ms: Optional[int] = None,
    as_of: Optional[datetime] = None,
) -> tuple[datetime, Optional[int], Optional[int]]:
    """返回（校正后时间, 校正依据 id, 实际使用偏移）。

    inline_offset_ms 为随记录上报的偏移；设备登记过依据时优先使用台账依据，
    台账无记录时退回行内偏移（依据 id 为 None，偏移仍留痕在事件行）。
    """
    device_dt = parse_iso(device_time_raw, field="device_time")
    correction: Optional[Correction] = None
    if device_id:
        correction = resolve_correction(connection, device_id, device_dt, as_of=as_of)
    if correction is not None:
        return correction.apply(device_dt), correction.id, correction.offset_ms
    if inline_offset_ms is not None:
        return device_dt + timedelta(milliseconds=inline_offset_ms), None, int(
            inline_offset_ms
        )
    return device_dt, None, None
