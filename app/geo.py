"""覆盖区几何：大圆距离与点是否在圆形覆盖区内。"""

from __future__ import annotations

import math

EARTH_RADIUS_M = 6_371_008.8


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """两点间大圆距离（米）。"""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def in_circle(lat: float, lon: float, zone: object) -> bool:
    """点是否落在圆心+半径覆盖区内。zone 为含 center_lat/center_lon/radius_m 的映射。"""
    return (
        haversine_m(lat, lon, zone["center_lat"], zone["center_lon"])
        <= zone["radius_m"]
    )
