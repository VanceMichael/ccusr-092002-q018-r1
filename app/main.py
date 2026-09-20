"""应急通信可用性后台 HTTP 接口（标准库实现，无第三方依赖）。

写入（参考数据）：
  POST /admin/missions            任务（时段）
  POST /admin/stations            空中基站
  POST /admin/station-windows     基站实际服务窗口
  POST /admin/coverage-zones      覆盖圆（圆心+半径+生效时段）
  POST /admin/teams               救援队伍
  POST /admin/team-locations      队伍位置上报（保留设备原始时间）
  POST /admin/clock-corrections   时钟校正依据（迟到依据触发重算）

事件：
  POST /events/ingest             批量接入/通话事件（source_event_id 幂等）

查询：
  GET  /missions/<ref>/availability?start=&end=   尝试关联与成功/重试/未知分类
  GET  /missions/<ref>/blind-spots?start=&end=    盲区队伍与最近有效联络
  GET  /teams/<ref>/last-contact                  单队最近一次成功通话
  GET  /events?mission_ref=&team_ref=&start=&end= 原始事件（含原始时间/校正留痕）
  GET  /health
"""

from __future__ import annotations

import json
import os
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import repository as repo
from . import service
from .db import transaction
from .timeutil import format_iso, parse_iso


class Handler(BaseHTTPRequestHandler):
    server_version = "EmergencyComms/1.0"

    # ------------------------------------------------------------ 基础工具

    def _send_json(self, status: int, body: object) -> None:
        data = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise repo.ValidationError("请求体必须是 JSON 对象")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise repo.ValidationError(f"请求体不是合法 JSON：{exc}") from exc
        if not isinstance(payload, dict):
            raise repo.ValidationError("请求体必须是 JSON 对象")
        return payload

    @staticmethod
    def _query(params: dict[str, list[str]], key: str) -> str | None:
        values = params.get(key)
        return values[0] if values else None

    # ------------------------------------------------------------ 路由

    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        path = parts.path.rstrip("/") or "/"
        try:
            if path == "/health":
                self._send_json(200, {"status": "ok"})
            elif path.startswith("/missions/") and path.endswith("/availability"):
                mission_ref = path.split("/")[2]
                with transaction() as conn:
                    body = service.availability_report(
                        conn, mission_ref,
                        self._query(query, "start"),
                        self._query(query, "end"),
                    )
                self._send_json(200, body)
            elif path.startswith("/missions/") and path.endswith("/blind-spots"):
                mission_ref = path.split("/")[2]
                with transaction() as conn:
                    body = service.blind_spots(
                        conn, mission_ref,
                        self._query(query, "start"),
                        self._query(query, "end"),
                    )
                self._send_json(200, body)
            elif path.startswith("/teams/") and path.endswith("/last-contact"):
                team_ref = path.split("/")[2]
                with transaction() as conn:
                    self._send_json(200, {
                        "team_ref": team_ref,
                        "last_effective_contact": service._last_effective_contact(
                            conn, team_ref
                        ),
                    })
            elif path.startswith("/devices/") and path.endswith("/clock-corrections"):
                device_id = path.split("/")[2]
                with transaction() as conn:
                    rows = conn.execute(
                        "SELECT id, device_id, offset_ms, basis, evidence,"
                        " effective_from_device, recorded_at, superseded"
                        " FROM clock_corrections WHERE device_id = ?"
                        " ORDER BY recorded_at",
                        (device_id,),
                    ).fetchall()
                self._send_json(200, {
                    "device_id": device_id,
                    "corrections": [dict(r) for r in rows]})
            elif path == "/events":
                self._list_events(query)
            else:
                self._send_json(404, {"error": "not_found", "path": path})
        except (ValueError, KeyError) as exc:
            self._send_json(400, {"error": "bad_request", "detail": str(exc)})

    def do_POST(self) -> None:
        parts = urlsplit(self.path)
        path = parts.path.rstrip("/") or "/"
        routes = {
            "/admin/missions": repo.upsert_mission,
            "/admin/stations": repo.upsert_station,
            "/admin/station-windows": repo.add_station_window,
            "/admin/coverage-zones": repo.add_coverage_zone,
            "/admin/teams": repo.upsert_team,
            "/admin/team-locations": repo.add_team_location,
            "/admin/clock-corrections": repo.add_clock_correction,
            "/events/ingest": repo.ingest_events,
        }
        handler = routes.get(path)
        if handler is None:
            self._send_json(404, {"error": "not_found", "path": path})
            return
        try:
            payload = self._read_json()
            with transaction() as conn:
                body = handler(conn, payload)
            self._send_json(200, body)
        except repo.ValidationError as exc:
            self._send_json(400, {"error": "bad_request", "detail": str(exc)})
        except sqlite3.IntegrityError as exc:
            self._send_json(409, {"error": "integrity_error", "detail": str(exc)})
        except ValueError as exc:
            self._send_json(400, {"error": "bad_request", "detail": str(exc)})

    # ------------------------------------------------------------ 查询

    def _list_events(self, query: dict[str, list[str]]) -> None:
        mission_ref = self._query(query, "mission_ref")
        if not mission_ref:
            self._send_json(400, {
                "error": "bad_request",
                "detail": "必须提供 mission_ref",
            })
            return
        sql = "SELECT * FROM events WHERE mission_ref = ?"
        params: list[object] = [mission_ref]
        team_ref = self._query(query, "team_ref")
        if team_ref:
            sql += " AND team_ref = ?"
            params.append(team_ref)
        start = self._query(query, "start")
        end = self._query(query, "end")
        if start:
            sql += " AND event_time_corrected >= ?"
            params.append(format_iso(parse_iso(start, field="start")))
        if end:
            sql += " AND event_time_corrected <= ?"
            params.append(format_iso(parse_iso(end, field="end")))
        sql += " ORDER BY event_time_corrected LIMIT 1000"
        with transaction() as conn:
            rows = conn.execute(sql, params).fetchall()
        events = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json") or "{}")
            events.append(item)
        self._send_json(200, {"count": len(events), "events": events})

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> None:
    port = int(os.getenv("PORT", "8080"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
