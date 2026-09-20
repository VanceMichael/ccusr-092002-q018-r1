"""时间解析与设备时钟校正。

铁律：device_time 逐字保留，任何校正只产生新的 event_time，
并记录 clock_offset_s 与 correction_basis，绝不覆盖原始时间。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


class TimeError(ValueError):
    """时间串无法解析或缺少时区偏移。"""


def parse_iso(value: str, field: str = "time") -> datetime:
    """解析 ISO 8601 时间；必须带时区偏移（裸时间禁止入库）。"""
    if not isinstance(value, str) or not value.strip():
        raise TimeError(f"{field} 必须是非空 ISO 8601 字符串")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise TimeError(f"{field} 不是合法 ISO 8601 时间：{value!r}") from exc
    if dt.tzinfo is None:
        raise TimeError(f"{field} 必须带时区偏移，例如 2026-09-20T08:00:00+08:00")
    return dt.astimezone(timezone.utc)


def to_iso(dt: datetime) -> str:
    """统一输出 UTC ISO 8601（毫秒精度、Z 结尾）。"""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def now_iso() -> str:
    return to_iso(datetime.now(timezone.utc))


@dataclass(frozen=True)
class CorrectedTime:
    event_time: datetime          # 校正到参考时钟后的 UTC 时刻
    clock_offset_s: float | None  # event_time - device_time（秒）；未校正为 None
    basis: str                    # as_reported/explicit_offset/synced_time/late_log
    is_late: bool


def correct_device_time(
    device_time_raw: str,
    *,
    clock_offset_s: float | None = None,
    synced_time: str | None = None,
    received_at: datetime | None = None,
    late_threshold_s: float = 300.0,
) -> CorrectedTime:
    """按上报依据校正设备时钟。

    校正依据优先级：
    1. synced_time：上报方同时给出参考时钟时刻，以它为事件时刻，
       offset = synced - device；
    2. clock_offset_s：上报方显式给出偏移秒数；
    3. 都没有：不校正。

    迟到判定：后台收到时刻明显晚于（校正后的）事件时刻即为迟到补传；
    设备时间超前（未来时间戳）不算迟到，也绝不据此反改设备时间。
    """
    device_dt = parse_iso(device_time_raw, "device_time")
    received = received_at or datetime.now(timezone.utc)

    if synced_time is not None:
        synced_dt = parse_iso(synced_time, "synced_time")
        offset = (synced_dt - device_dt).total_seconds()
        event_dt = synced_dt
        basis = "synced_time"
    elif clock_offset_s is not None:
        offset = float(clock_offset_s)
        event_dt = device_dt + timedelta(seconds=offset)
        basis = "explicit_offset"
    else:
        offset = None
        event_dt = device_dt
        basis = "as_reported"

    is_late = (received - event_dt).total_seconds() > late_threshold_s
    if is_late and basis == "as_reported":
        # 无对时依据的迟到/漂移日志：按设备时间原样归位，仅标记可信度
        basis = "late_log"

    return CorrectedTime(
        event_time=event_dt,
        clock_offset_s=offset,
        basis=basis,
        is_late=is_late,
    )
