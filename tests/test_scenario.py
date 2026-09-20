"""盐边灾区场景端到端测试：覆盖、接入链、盲区、时钟校正、幂等。"""

from __future__ import annotations

import http.client
import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

from app.main import Handler

MISSION = "YB-2026-0920"
WINDOW = "?start=2026-09-20T10:00:00%2B08:00&end=2026-09-20T12:00:00%2B08:00"


def _free_port() -> int:
    probe = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = probe.server_address[1]
    probe.server_close()
    return port


class ApiClient:
    def __init__(self, host: str, port: int) -> None:
        self.host, self.port = host, port

    def call(self, method: str, path: str, payload=None):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
        body = (
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
            if payload is not None
            else None
        )
        headers = {"Content-Type": "application/json"} if body is not None else {}
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, data


class YanbianScenarioTest(unittest.TestCase):
    api: ApiClient

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.db_path = os.path.join(cls.tmpdir.name, "test.sqlite3")
        os.environ["DATABASE_PATH"] = cls.db_path
        cls.port = _free_port()
        from app.db import connect

        cls.server = ThreadingHTTPServer(("127.0.0.1", cls.port), Handler)
        cls.server.db = connect(cls.db_path)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.api = ApiClient("127.0.0.1", cls.port)
        cls._seed()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.server.db.close()
        cls.tmpdir.cleanup()

    @classmethod
    def _post(cls, path: str, payload):
        status, body = cls.api.call("POST", path, payload)
        assert status == 200, (path, status, body)
        return body

    @classmethod
    def _seed(cls) -> None:
        # ---- 任务：翼龙无人机 08:00-14:00 (+08:00) ----
        cls._post("/admin/missions", {
            "mission_ref": MISSION,
            "name": "盐边地面网络中断空中组网",
            "started_at": "2026-09-20T08:00:00+08:00",
            "ended_at": "2026-09-20T14:00:00+08:00",
        })
        cls._post("/admin/station-missions", {
            "mission_ref": MISSION,
            "station_ref": "WING-1",
            "node_kind": "uav_relay",
            "altitude_m": 3200,
            "started_at": "2026-09-20T08:00:00+08:00",
            "ended_at": "2026-09-20T14:00:00+08:00",
        })
        # 覆盖圆：盐边北部，半径 8 km
        cls._post("/admin/footprints", {
            "mission_ref": MISSION,
            "station_ref": "WING-1",
            "valid_from": "2026-09-20T08:00:00+08:00",
            "valid_to": "2026-09-20T14:00:00+08:00",
            "center_lat": 26.9000, "center_lon": 101.5100, "radius_m": 8000,
        })
        # ---- 队伍：甲/乙在覆盖内、丙在山外盲区、丁位置未知 ----
        for ref, name, lat, lon in [
            ("TEAM-A", "甲组（覆盖内）", 26.9050, 101.5050),
            ("TEAM-B", "乙组（覆盖内）", 26.9100, 101.5200),
            ("TEAM-C", "丙组（山外盲区）", 27.2000, 101.9000),
        ]:
            cls._post("/admin/teams", {
                "team_ref": ref, "name": name, "latitude": lat, "longitude": lon,
            })
        cls._post("/admin/teams", {"team_ref": "TEAM-D", "name": "丁组（位置未知）"})

        events = [
            # 丙组最近一次真正通话：昨天 20:00 UTC，设备时钟慢 8 分钟（显式偏移校正）
            {"source_ref": "wing1-log", "source_event_id": "old-call",
             "mission_ref": MISSION, "station_ref": "WING-1", "team_ref": "TEAM-C",
             "event_kind": "call_connected", "call_id": "CALL-OLD",
             "device_time": "2026-09-19T19:52:00+00:00", "clock_offset_s": 480},
            # 甲组：两次接入尝试（首次失败后重试），附着成功，随后真正通话
            {"source_ref": "wing1-log", "source_event_id": "a1",
             "mission_ref": MISSION, "station_ref": "WING-1", "team_ref": "TEAM-A",
             "event_kind": "access_attempt", "attempt_id": "ATT-A",
             "device_time": "2026-09-20T10:05:00+08:00"},
            {"source_ref": "wing1-log", "source_event_id": "a2",
             "mission_ref": MISSION, "station_ref": "WING-1", "team_ref": "TEAM-A",
             "event_kind": "access_attempt", "attempt_id": "ATT-A",
             "device_time": "2026-09-20T10:05:30+08:00"},
            {"source_ref": "wing1-log", "source_event_id": "a3",
             "mission_ref": MISSION, "station_ref": "WING-1", "team_ref": "TEAM-A",
             "event_kind": "access_accept", "attempt_id": "ATT-A",
             "device_time": "2026-09-20T10:06:00+08:00"},
            {"source_ref": "wing1-log", "source_event_id": "a4",
             "mission_ref": MISSION, "station_ref": "WING-1", "team_ref": "TEAM-A",
             "event_kind": "call_connected", "attempt_id": "ATT-A", "call_id": "CALL-1",
             "device_time": "2026-09-20T10:07:00+08:00"},
            {"source_ref": "wing1-log", "source_event_id": "a5",
             "mission_ref": MISSION, "station_ref": "WING-1", "team_ref": "TEAM-A",
             "event_kind": "call_connected", "call_id": "CALL-2",
             "device_time": "2026-09-20T11:00:00+08:00"},
            {"source_ref": "wing1-log", "source_event_id": "a6",
             "mission_ref": MISSION, "station_ref": "WING-1", "team_ref": "TEAM-A",
             "event_kind": "call_dropped", "call_id": "CALL-2",
             "device_time": "2026-09-20T11:10:00+08:00"},
            # 乙组：3 次尝试（2 次重复重试）后附着，但始终没有通话；设备时钟快 60s
            {"source_ref": "wing1-log", "source_event_id": "b1",
             "mission_ref": MISSION, "station_ref": "WING-1", "team_ref": "TEAM-B",
             "event_kind": "access_attempt", "attempt_id": "ATT-B",
             "device_time": "2026-09-20T10:21:00+08:00", "clock_offset_s": -60},
            {"source_ref": "wing1-log", "source_event_id": "b2",
             "mission_ref": MISSION, "station_ref": "WING-1", "team_ref": "TEAM-B",
             "event_kind": "access_attempt", "attempt_id": "ATT-B",
             "device_time": "2026-09-20T10:22:00+08:00"},
            {"source_ref": "wing1-log", "source_event_id": "b3",
             "mission_ref": MISSION, "station_ref": "WING-1", "team_ref": "TEAM-B",
             "event_kind": "access_attempt", "attempt_id": "ATT-B",
             "device_time": "2026-09-20T10:23:00+08:00"},
            {"source_ref": "wing1-log", "source_event_id": "b4",
             "mission_ref": MISSION, "station_ref": "WING-1", "team_ref": "TEAM-B",
             "event_kind": "access_accept", "attempt_id": "ATT-B",
             "device_time": "2026-09-20T10:24:00+08:00"},
            # 丙组：只有一次接入尝试，无终态（未知结果）；用 synced_time 对时
            {"source_ref": "handheld-c", "source_event_id": "c1",
             "mission_ref": MISSION, "station_ref": "WING-1", "team_ref": "TEAM-C",
             "event_kind": "access_attempt", "attempt_id": "ATT-C",
             "device_time": "2026-09-20T10:29:30+08:00",
             "synced_time": "2026-09-20T10:30:00+08:00"},
            # 无 attempt_id、无队伍的外部探测尝试：不得记到任何队伍头上
            {"source_ref": "rf-probe", "source_event_id": "p1",
             "mission_ref": MISSION, "station_ref": "WING-1",
             "event_kind": "access_attempt",
             "device_time": "2026-09-20T10:40:00+08:00"},
            {"source_ref": "rf-probe", "source_event_id": "p2",
             "mission_ref": MISSION, "station_ref": "WING-1",
             "event_kind": "access_attempt",
             "device_time": "2026-09-20T10:41:00+08:00"},
            # 与 a2 完全相同的来源事件重传：必须识别为重复，不产生第二次尝试
            {"source_ref": "wing1-log", "source_event_id": "a2",
             "mission_ref": MISSION, "station_ref": "WING-1", "team_ref": "TEAM-A",
             "event_kind": "access_attempt", "attempt_id": "ATT-A",
             "device_time": "2026-09-20T10:05:30+08:00"},
        ]
        status, body = cls.api.call("POST", "/ingest", {"events": events})
        assert status == 200, (status, body)
        assert len(body["accepted"]) == 14, body
        assert len(body["duplicates"]) == 1, body
        assert len(body["rejected"]) == 0, body

    def test_health(self) -> None:
        status, body = self.api.call("GET", "/health")
        self.assertEqual(200, status)
        self.assertEqual("ok", body["status"])

    def test_attempt_chains_classification(self) -> None:
        status, body = self.api.call("GET", f"/missions/{MISSION}/attempt-chains{WINDOW}")
        self.assertEqual(200, status)
        totals = body["totals"]
        # 6 条尝试事件 = 甲 2 + 乙 3 + 丙 1；外部探测不计入链；重传不计第二次
        self.assertEqual(6, totals["access_attempt_events"])
        self.assertEqual(3, totals["retries"])
        self.assertEqual(3, totals["attempt_chains"])
        self.assertEqual(1, totals["chains_success"])
        self.assertEqual(1, totals["chains_attached_only"])
        self.assertEqual(1, totals["chains_unknown"])
        self.assertEqual(0, totals["chains_rejected"])
        self.assertEqual(2, totals["unlinked_external_attempts"])
        # 真正通话数按 call_id 去重；附着再多也不增加
        self.assertEqual(2, totals["calls_connected"])
        self.assertEqual(1, totals["calls_dropped"])
        self.assertEqual(1, totals["teams_with_call"])

        by_id = {c["attempt_id"]: c for c in body["chains"]}
        self.assertEqual("success", by_id["ATT-A"]["outcome"])
        self.assertTrue(by_id["ATT-A"]["has_call_connected"])
        self.assertEqual("attached_only", by_id["ATT-B"]["outcome"])
        self.assertFalse(by_id["ATT-B"]["has_call_connected"])
        self.assertEqual(2, by_id["ATT-B"]["retries"])
        self.assertEqual("unknown", by_id["ATT-C"]["outcome"])
        self.assertTrue(by_id["ATT-C"]["unknown_result"])

    def test_blind_zones_and_last_contact(self) -> None:
        status, body = self.api.call("GET", f"/missions/{MISSION}/blind-zones{WINDOW}")
        self.assertEqual(200, status)
        self.assertEqual(4, body["summary"]["teams_total"])
        self.assertEqual(1, body["summary"]["teams_in_blind_zone"])
        self.assertEqual(1, body["summary"]["teams_location_unknown"])
        self.assertEqual(1, body["summary"]["teams_with_call_in_window"])

        teams = {t["team_ref"]: t for t in body["teams"]}
        alpha, bravo, charlie, delta = (
            teams[k] for k in ("TEAM-A", "TEAM-B", "TEAM-C", "TEAM-D")
        )

        # 甲组：覆盖内、窗口内有通话；接入冗余没有影响判定
        self.assertFalse(alpha["in_blind_zone"])
        self.assertTrue(alpha["had_call_in_window"])
        self.assertGreater(alpha["covered_seconds_in_window"], 7000)
        self.assertEqual("CALL-2", alpha["last_valid_contact"]["call_id"])

        # 乙组：附着成功但没通话——不能报成获救/通话
        self.assertFalse(bravo["in_blind_zone"])
        self.assertFalse(bravo["had_call_in_window"])
        self.assertIsNone(bravo["last_valid_contact"])

        # 丙组：窗口内只有一次未知结果的接入尝试，仍是盲区；
        # 但能找到最近一次有效联络（昨天的真正通话，含原始时间与校正依据）
        self.assertTrue(charlie["in_blind_zone"])
        self.assertFalse(charlie["had_call_in_window"])
        self.assertEqual(0, charlie["covered_seconds_in_window"])
        contact = charlie["last_valid_contact"]
        self.assertIsNotNone(contact)
        self.assertEqual("CALL-OLD", contact["call_id"])
        self.assertEqual("2026-09-19T19:52:00+00:00", contact["device_time_raw"])
        self.assertEqual("2026-09-19T20:00:00.000Z", contact["at"])
        self.assertEqual("explicit_offset", contact["correction_basis"])

        # 丁组：位置未知，单独列出，不武断报为盲区
        self.assertFalse(delta["in_blind_zone"])
        self.assertFalse(delta["location_known"])
        self.assertIn("TEAM-D", body["teams_location_unknown"])

        self.assertEqual(
            {"TEAM-C"}, {t["team_ref"] for t in body["blind_zone_teams"]}
        )

    def test_raw_time_and_correction_basis_preserved(self) -> None:
        status, body = self.api.call(
            "GET",
            f"/missions/{MISSION}/events"
            "?start=2026-09-20T10:29:00%2B08:00&end=2026-09-20T10:31:00%2B08:00",
        )
        self.assertEqual(200, status)
        self.assertEqual(1, len(body["events"]))
        ev = body["events"][0]
        self.assertEqual("2026-09-20T10:29:30+08:00", ev["device_time_raw"])
        self.assertEqual("2026-09-20T02:30:00.000Z", ev["event_time"])
        self.assertEqual(30.0, ev["clock_offset_s"])
        self.assertEqual("synced_time", ev["correction_basis"])

    def test_late_log_marked_but_original_time_kept(self) -> None:
        # 事后补传、且无对时依据：原始时间保留，标记 late_log，不静默改写
        status, body = self.api.call("POST", "/ingest", {
            "source_ref": "field-late", "source_event_id": "late-1",
            "mission_ref": MISSION, "station_ref": "WING-1", "team_ref": "TEAM-C",
            "event_kind": "call_ended", "call_id": "CALL-LATE",
            "device_time": "2026-09-19T08:00:00+08:00",  # 事件发生时刻
            "received_at": "2026-09-20T20:00:00+08:00",  # 次日才补传到后台
        })
        self.assertEqual(200, status)
        self.assertEqual("late_log", body["accepted"][0]["correction_basis"])
        self.assertTrue(body["accepted"][0]["is_late"])

        status, listing = self.api.call(
            "GET",
            f"/missions/{MISSION}/events?team=TEAM-C"
            "&start=2026-09-19T00:00:00%2B08:00&end=2026-09-19T12:00:00%2B08:00",
        )
        self.assertEqual(200, status)
        self.assertEqual(1, len(listing["events"]))
        self.assertEqual("2026-09-19T08:00:00+08:00", listing["events"][0]["device_time_raw"])
        self.assertTrue(listing["events"][0]["is_late_log"])

    def test_ingest_rejects_naive_time(self) -> None:
        status, body = self.api.call("POST", "/ingest", {
            "source_ref": "x", "source_event_id": "bad-1",
            "mission_ref": MISSION, "event_kind": "access_attempt",
            "device_time": "2026-09-20T10:00:00",  # 无偏移
        })
        self.assertEqual(400, status)
        self.assertIn("偏移", body["error"])

    def test_ingest_rejects_unknown_kind(self) -> None:
        status, body = self.api.call("POST", "/ingest", {
            "source_ref": "x", "source_event_id": "bad-2",
            "mission_ref": MISSION, "event_kind": "rescued",  # 禁止编造获救事件
            "device_time": "2026-09-20T10:00:00+08:00",
        })
        self.assertEqual(400, status)
        self.assertIn("event_kind", body["error"])

    def test_bad_window_rejected(self) -> None:
        status, _ = self.api.call(
            "GET",
            f"/missions/{MISSION}/blind-zones"
            "?start=2026-09-20T12:00:00%2B08:00&end=2026-09-20T10:00:00%2B08:00",
        )
        self.assertEqual(400, status)


if __name__ == "__main__":
    unittest.main()
