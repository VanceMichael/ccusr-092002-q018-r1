"""时间工具：统一解析为带时区的 UTC datetime，输出带偏移的 ISO 8601 字符串。"""

from __future__ import annotations

from datetime import datetime, timezone


def parse_iso(value: str, *, field: str = "time") -> datetime:
    """解析 ISO 8601 时间串。

    接受 ``2026-09-20T08:00:00+08:00`` 与 ``...Z``。
    裸串（无偏移）按 UTC 处理——契约要求带偏移，裸串只做兼容。
    """
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} 必须是非空 ISO 8601 字符串")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field} 不是合法的 ISO 8601 时间：{value!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_iso(dt: datetime) -> str:
    """格式化校正后的 UTC 时间（保留显式偏移）。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
