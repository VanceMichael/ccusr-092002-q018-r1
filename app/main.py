"""应急通信可用性后台 HTTP 接口（标准库实现）。

路由：
  GET  /health
  POST /admin/missions           登记/更新无人机任务时段
  POST /admin/teams              登记救援队伍（坐标可空）
  POST /admin/station-missions   基站（空中节点）任务时段
  POST /admin/footprints         基站覆盖圆（随时间生效）
  POST /ingest                   上报事件（单条或 {"events": [...]}），幂等
  GET  /missions/{ref}/attempt-chains?start=&end=   接入尝试链与通话口径
  GET  /missions/{ref}/blind-zones?start=&end=      盲区队伍与最近有效联络
  GET  /missions/{ref}/events?start=&end=&team=     原始事件核查（含校正依据）
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .db import connect
from .service import AvailabilityService
from .store import Store, ValidationError
from .timeutil import TimeError


def _json_bytes(payload: dict | list, status: int = 200) -> tuple[int, bytes]:
    return status, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "EmergencyAvailability/1.0"

    # ---- 连接在 server 上共享，避免每请求重建 ----
    def _store(self) -> Store:
        return Store(self.server.db)  # type: ignore[attr-defined]

    def _service(self) -> AvailabilityService:
        return AvailabilityService(self.server.db)  # type: ignore[attr-defined]

    def _send(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        self._send(*_json_bytes({"error": message}, status))

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise ValidationError("请求体必须是 JSON")
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError(f"JSON 解析失败：{exc}") from exc

    # ---------------- GET ----------------

    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        if parts.path == "/health":
            self._send(*_json_bytes({"status": "ok"}))
            return
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        segments = [p for p in parts.path.split("/") if p]

        try:
            if len(segments) == 3 and segments[0] == "missions":
                ref, resource = segments[1], segments[2]
                if resource == "attempt-chains":
                    self._send(*_json_bytes(
                        self._service().attempt_chains(
                            ref, query["start"], query["end"]
                        )
                    ))
                    return
                if resource == "blind-zones":
                    self._send(*_json_bytes(
                        self._service().blind_zones(
                            ref, query["start"], query["end"]
                        )
                    ))
                    return
                if resource == "events":
                    self._send(*_json_bytes(
                        self._service().list_events(
                            ref,
                            query.get("start"),
                            query.get("end"),
                            query.get("team"),
                        )
                    ))
                    return
            self._error(404, "未找到对应接口")
        except KeyError as exc:
            self._error(400, f"缺少查询参数：{exc.args[0]}")
        except (ValueError, TimeError) as exc:
            self._error(400, str(exc))

    # ---------------- POST ----------------

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        try:
            data = self._read_json()
            store = self._store()
            if path == "/admin/missions":
                store.upsert_mission(data)
                self._send(*_json_bytes({"status": "ok", "mission_ref": data.get("mission_ref")}))
            elif path == "/admin/teams":
                store.upsert_team(data)
                self._send(*_json_bytes({"status": "ok", "team_ref": data.get("team_ref")}))
            elif path == "/admin/station-missions":
                store.upsert_station_mission(data)
                self._send(*_json_bytes({"status": "ok", "station_ref": data.get("station_ref")}))
            elif path == "/admin/footprints":
                foot_id = store.insert_footprint(data)
                self._send(*_json_bytes({"status": "ok", "footprint_id": foot_id}))
            elif path == "/ingest":
                envelope_received = None
                if isinstance(data, dict) and isinstance(data.get("events"), list):
                    if data.get("received_at"):
                        envelope_received = parse_iso(data["received_at"], "received_at")
                    result = store.ingest_batch(data["events"], envelope_received)
                elif isinstance(data, list):
                    result = store.ingest_batch(data)
                elif isinstance(data, dict):
                    result = store.ingest_batch([data])
                    if result["rejected"]:
                        # 单对象直交：直接返回 400 与原因，而不是批量 207
                        self._error(400, result["rejected"][0]["error"])
                        return
                else:
                    raise ValidationError("/ingest 需要事件对象或 {\"events\": [...]}")
                status = 207 if result["rejected"] else 200
                self._send(*_json_bytes(result, status))
            else:
                self._error(404, "未找到对应接口")
        except (ValidationError, TimeError) as exc:
            self._error(400, str(exc))

    def log_message(self, format: str, *args: object) -> None:
        return


def build_server(database_path: str | None = None) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
    server.db = connect(database_path)  # type: ignore[attr-defined]
    return server


def main() -> None:
    port = int(os.getenv("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.db = connect()  # type: ignore[attr-defined]
    try:
        server.serve_forever()
    finally:
        server.db.close()


if __name__ == "__main__":
    main()
