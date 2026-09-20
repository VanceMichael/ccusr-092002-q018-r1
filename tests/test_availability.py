"""端到端场景测试：时钟漂移/迟到日志、尝试关联分类、盲区判定。

直接使用临时 SQLite 文件驱动服务层，另含一组 HTTP 接口测试。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.client import IncompleteRead
from http.server import ThreadingHTTPServer
from pathlib import Path

from app import repository as repo
from app import service
from app.db import connect
from app.main import Handler
from scripts import migrate

# 场景基准（北京时间 2026-09-20 上午，盐边灾区）
T0 = "2026-09-20T08:00:00+08:00"
T1 = "2026-09-20T10:00:00+08:00"


class DatabaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.sqlite3"
        os.environ["DATABASE_PATH"] = str(self.db_path)
        migrate.run()
        self.conn = connect()

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()
        os.environ.pop("DATABASE_PATH", None)

    # ------------------------------------------------------------ 造数

    def _seed_base(self) -> None:
        """任务 + 两个基站（A 覆盖县城北，B 覆盖河谷）+ 三支队伍。"""
        repo.upsert_mission(self.conn, {
            "mission_ref": "M-YANBIAN",
            "name": "盐边地面网络中断应急组网",
            "starts_at": T0, "ends_at": T1,
        })
        repo.upsert_station(self.conn, {
            "station_ref": "ST-A", "mission_ref": "M-YANBIAN", "name": "翼龙-A"})
        repo.upsert_station(self.conn, {
            "station_ref": "ST-B", "mission_ref": "M-YANBIAN", "name": "翼龙-B"})
        repo.add_station_window(self.conn, {
            "station_ref": "ST-A", "starts_at": T0, "ends_at": T1})
        repo.add_station_window(self.conn, {
            "station_ref": "ST-B", "starts_at": "2026-09-20T08:30:00+08:00",
            "ends_at": T1})
        # 盐边县城约 (26.9, 101.5)；半径 8km
        repo.add_coverage_zone(self.conn, {
            "station_ref": "ST-A", "zone_ref": "Z-A",
            "center_lat": 26.9000, "center_lon": 101.5000, "radius_m": 8000})
        # 河谷方向另一个圆
        repo.add_coverage_zone(self.conn, {
            "station_ref": "ST-B", "zone_ref": "Z-B",
            "center_lat": 26.9500, "center_lon": 101.6000, "radius_m": 6000})
        for ref, name in (("TEAM-1", "县城搜救一队"),
                          ("TEAM-2", "河谷二队"),
                          ("TEAM-3", "远山三队")):
            repo.upsert_team(self.conn, {
                "team_ref": ref, "name": name,
                "mission_refs": ["M-YANBIAN"]})
        self.conn.commit()

    # ---------------------------------------------------- 时钟漂移与迟到依据

    def test_clock_drift_raw_time_preserved_and_rederived(self) -> None:
        self._seed_base()
        # 设备时钟慢 5 分钟：08:10 真实发生的事件，设备显示 08:05。
        repo.add_team_location(self.conn, {
            "team_ref": "TEAM-1", "device_id": "DEV-1",
            "lat": 26.9001, "lon": 101.5001,
            "observed_at_device": "2026-09-20T08:05:00+08:00"})
        repo.ingest_events(self.conn, {"events": [{
            "source_event_id": "E-1", "mission_ref": "M-YANBIAN",
            "station_ref": "ST-A", "team_ref": "TEAM-1", "device_id": "DEV-1",
            "session_ref": "S-1", "event_kind": "call_connected",
            "event_time_device": "2026-09-20T08:05:00+08:00",
        }]})
        self.conn.commit()

        before = self.conn.execute(
            "SELECT event_time_device, event_time_corrected, clock_correction_id"
            " FROM events WHERE source_event_id = 'E-1'").fetchone()
        # 尚无依据：校正时间等于原始时刻（统一以 UTC 表达，08:05+08 = 00:05Z）。
        self.assertEqual(before["clock_correction_id"], None)
        self.assertIn("00:05", before["event_time_corrected"])

        # GNSS 对时结果迟到 5 分钟（设备慢 300000ms）。
        repo.add_clock_correction(self.conn, {
            "device_id": "DEV-1", "offset_ms": 300_000,
            "basis": "gnss_sync", "evidence": "09:00 补传 GNSS PPS 对时"})
        self.conn.commit()

        after = self.conn.execute(
            "SELECT event_time_device, event_time_corrected, clock_correction_id,"
            " clock_offset_ms_used FROM events WHERE source_event_id = 'E-1'"
        ).fetchone()
        # 原始时间永不改写。
        self.assertEqual(after["event_time_device"],
                         "2026-09-20T08:05:00+08:00")
        # 校正后变为 08:10+08（即 00:10Z），且留痕依据与偏移。
        self.assertIn("00:10", after["event_time_corrected"])
        self.assertEqual(after["clock_offset_ms_used"], 300_000)
        self.assertIsNotNone(after["clock_correction_id"])

        loc = self.conn.execute(
            "SELECT observed_at_device, observed_at, clock_correction_id"
            " FROM team_locations WHERE team_ref = 'TEAM-1'").fetchone()
        self.assertIn("08:05", loc["observed_at_device"])
        self.assertIn("00:10", loc["observed_at"])

        # 旧依据保留并标记 superseded。
        history = self.conn.execute(
            "SELECT COUNT(*) AS n FROM clock_corrections"
            " WHERE device_id = 'DEV-1'").fetchone()["n"]
        self.assertEqual(history, 1)
        repo.add_clock_correction(self.conn, {
            "device_id": "DEV-1", "offset_ms": 305_000,
            "basis": "manual_estimate",
            "evidence": "指挥员复核漂移率后修正"})
        self.conn.commit()
        rows = self.conn.execute(
            "SELECT basis, superseded FROM clock_corrections"
            " WHERE device_id = 'DEV-1' ORDER BY id").fetchall()
        self.assertEqual([(r["basis"], r["superseded"]) for r in rows],
                         [("gnss_sync", 1), ("manual_estimate", 0)])

    # ------------------------------------------------------------ 迟到日志

    def test_late_arriving_logs_ordered_by_corrected_time(self) -> None:
        self._seed_base()
        # 先收到 09:00 的记录，再补传 08:20 的记录（迟到日志）。
        repo.ingest_events(self.conn, {"events": [{
            "source_event_id": "LATE-2", "mission_ref": "M-YANBIAN",
            "station_ref": "ST-A", "team_ref": "TEAM-2",
            "event_kind": "call_connected",
            "event_time_device": "2026-09-20T09:00:00+08:00"}]})
        repo.ingest_events(self.conn, {"events": [{
            "source_event_id": "LATE-1", "mission_ref": "M-YANBIAN",
            "station_ref": "ST-A", "team_ref": "TEAM-2",
            "event_kind": "access_attempt",
            "event_time_device": "2026-09-20T08:20:00+08:00"}]})
        self.conn.commit()
        report = service.availability_report(self.conn, "M-YANBIAN")
        team2 = next(t for t in report["teams"] if t["team_ref"] == "TEAM-2")
        # 迟到的 08:20 尝试与其后 09:00 的接通按校正时间正确关联，
        # 而不是按接收顺序把尝试误关联成失败。
        self.assertEqual(team2["successful_calls"], 1)
        chain = team2["chains"][0]
        self.assertEqual(chain["outcome"], "connected")
        self.assertIn("00:20", chain["first_attempt_at"])
        self.assertIn("01:00", chain["connected_at"])

    # ---------------------------------------------------- 成功/重试/未知/拒绝

    def test_attempt_classification_success_retries_unknown(self) -> None:
        self._seed_base()
        events = [
            # TEAM-1：3 次尝试（2 次冗余重试）后接通，随后中断。
            {"source_event_id": "A1", "team_ref": "TEAM-1",
             "station_ref": "ST-A", "session_ref": "S1",
             "event_kind": "access_attempt",
             "event_time_device": "2026-09-20T08:10:00+08:00"},
            {"source_event_id": "A2", "team_ref": "TEAM-1",
             "station_ref": "ST-A", "session_ref": "S1",
             "event_kind": "access_attempt",
             "event_time_device": "2026-09-20T08:11:00+08:00"},
            {"source_event_id": "A3", "team_ref": "TEAM-1",
             "station_ref": "ST-A", "session_ref": "S1",
             "event_kind": "access_attempt",
             "event_time_device": "2026-09-20T08:12:00+08:00"},
            {"source_event_id": "C1", "team_ref": "TEAM-1",
             "station_ref": "ST-A", "session_ref": "S1",
             "event_kind": "call_connected",
             "event_time_device": "2026-09-20T08:12:30+08:00"},
            {"source_event_id": "D1", "team_ref": "TEAM-1",
             "station_ref": "ST-A", "session_ref": "S1",
             "event_kind": "call_dropped",
             "event_time_device": "2026-09-20T08:20:00+08:00"},
            # TEAM-2：2 次尝试后被明确拒绝。
            {"source_event_id": "B1", "team_ref": "TEAM-2",
             "station_ref": "ST-B", "session_ref": "S2",
             "event_kind": "access_attempt",
             "event_time_device": "2026-09-20T08:40:00+08:00"},
            {"source_event_id": "B2", "team_ref": "TEAM-2",
             "station_ref": "ST-B", "session_ref": "S2",
             "event_kind": "access_attempt",
             "event_time_device": "2026-09-20T08:41:00+08:00"},
            {"source_event_id": "R1", "team_ref": "TEAM-2",
             "station_ref": "ST-B", "session_ref": "S2",
             "event_kind": "access_rejected",
             "event_time_device": "2026-09-20T08:41:05+08:00"},
            # TEAM-3：只有尝试，时段结束无终局 → 未知结果。
            {"source_event_id": "U1", "team_ref": "TEAM-3",
             "station_ref": "ST-A", "session_ref": "S3",
             "event_kind": "access_attempt",
             "event_time_device": "2026-09-20T09:30:00+08:00"},
            {"source_event_id": "U2", "team_ref": "TEAM-3",
             "station_ref": "ST-A", "session_ref": "S3",
             "event_kind": "access_attempt",
             "event_time_device": "2026-09-20T09:31:00+08:00"},
        ]
        repo.ingest_events(self.conn, {"mission_ref": "M-YANBIAN", "events": events})
        self.conn.commit()

        report = service.availability_report(self.conn, "M-YANBIAN")
        totals = report["totals"]
        self.assertEqual(totals["access_attempts"], 7)
        self.assertEqual(totals["retry_attempts"], 4)   # A1/B1/U1 为首次
        self.assertEqual(totals["chains_connected"], 1)
        self.assertEqual(totals["chains_rejected"], 1)
        self.assertEqual(totals["chains_unknown"], 1)
        self.assertEqual(totals["calls_dropped"], 1)

        t1 = next(t for t in report["teams"] if t["team_ref"] == "TEAM-1")
        # 关键口径：3 次尝试只对应 1 次成功通话，冗余重试不虚增获救数。
        self.assertEqual(t1["access_attempts"], 3)
        self.assertEqual(t1["successful_calls"], 1)
        self.assertTrue(t1["chains"][0]["dropped"])
        self.assertTrue(t1["chains"][0]["call_established"])

        t3 = next(t for t in report["teams"] if t["team_ref"] == "TEAM-3")
        self.assertEqual(t3["successful_calls"], 0)
        self.assertEqual(t3["unknown_outcome"], 1)
        self.assertEqual(t3["retry_attempts"], 1)

    def test_connected_without_attempt_does_not_fabricate_attempt(self) -> None:
        self._seed_base()
        repo.ingest_events(self.conn, {"events": [{
            "source_event_id": "X1", "mission_ref": "M-YANBIAN",
            "team_ref": "TEAM-1", "station_ref": "ST-A",
            "event_kind": "call_connected",
            "event_time_device": "2026-09-20T08:50:00+08:00"}]})
        self.conn.commit()
        report = service.availability_report(self.conn, "M-YANBIAN")
        t1 = report["teams"][0]
        self.assertEqual(t1["successful_calls"], 1)
        self.assertEqual(t1["access_attempts"], 0)  # 不凭空补尝试

    # ------------------------------------------------------------ 盲区

    def test_blind_spots_and_last_effective_contact(self) -> None:
        self._seed_base()
        # TEAM-1 在覆盖圆内（县城北）；TEAM-2 在 ST-B 河谷圆内；TEAM-3 在远山（圈外）。
        repo.add_team_location(self.conn, {
            "team_ref": "TEAM-1", "lat": 26.9005, "lon": 101.5005,
            "observed_at_device": "2026-09-20T07:30:00+08:00"})
        repo.add_team_location(self.conn, {
            "team_ref": "TEAM-2", "lat": 26.9502, "lon": 101.6002,
            "observed_at_device": "2026-09-20T07:30:00+08:00"})
        repo.add_team_location(self.conn, {
            "team_ref": "TEAM-3", "lat": 27.2000, "lon": 102.0000,
            "observed_at_device": "2026-09-20T07:30:00+08:00"})

        # TEAM-1 昨天（更早）曾真正通话；今天只有失败的尝试。
        repo.ingest_events(self.conn, {"events": [
            {"source_event_id": "OLD-CALL", "mission_ref": "M-YANBIAN",
             "team_ref": "TEAM-1", "station_ref": "ST-A",
             "event_kind": "call_connected",
             "event_time_device": "2026-09-19T22:00:00+08:00"},
            {"source_event_id": "TRY1", "mission_ref": "M-YANBIAN",
             "team_ref": "TEAM-1", "station_ref": "ST-A",
             "event_kind": "access_attempt",
             "event_time_device": "2026-09-20T09:00:00+08:00"},
            {"source_event_id": "TRY2", "mission_ref": "M-YANBIAN",
             "team_ref": "TEAM-1", "station_ref": "ST-A",
             "event_kind": "access_attempt",
             "event_time_device": "2026-09-20T09:05:00+08:00"},
            # TEAM-2 反复尝试，从未接通——即使身处覆盖区也仍是通信盲区。
            {"source_event_id": "TRY3", "mission_ref": "M-YANBIAN",
             "team_ref": "TEAM-2", "station_ref": "ST-B",
             "event_kind": "access_attempt",
             "event_time_device": "2026-09-20T09:10:00+08:00"},
        ]})
        self.conn.commit()

        result = service.blind_spots(self.conn, "M-YANBIAN")
        by_ref = {t["team_ref"]: t for t in result["teams"]}
        self.assertEqual(result["blind_team_count"], 3)

        t1 = by_ref["TEAM-1"]
        self.assertEqual(t1["coverage_at_window_end"], "covered_but_no_call")
        self.assertEqual(t1["access_attempts_in_window"], 2)
        # 最近一次有效联络是昨天的真实通话，而非今天的尝试。
        self.assertIsNotNone(t1["last_effective_contact"])
        self.assertIn("2026-09-19", t1["last_effective_contact"]["connected_at"])

        t2 = by_ref["TEAM-2"]
        self.assertEqual(t2["coverage_at_window_end"], "covered_but_no_call")
        self.assertIsNone(t2["last_effective_contact"])
        self.assertEqual(t2["access_attempts_in_window"], 1)

        t3 = by_ref["TEAM-3"]
        self.assertEqual(t3["coverage_at_window_end"], "not_covered")
        self.assertEqual(t3["access_attempts_in_window"], 0)

    def test_contacted_team_excluded_from_blind_spots(self) -> None:
        self._seed_base()
        repo.add_team_location(self.conn, {
            "team_ref": "TEAM-1", "lat": 26.9005, "lon": 101.5005,
            "observed_at_device": "2026-09-20T08:00:00+08:00"})
        repo.ingest_events(self.conn, {"events": [{
            "source_event_id": "OK1", "mission_ref": "M-YANBIAN",
            "team_ref": "TEAM-1", "station_ref": "ST-A",
            "event_kind": "call_connected",
            "event_time_device": "2026-09-20T09:00:00+08:00"}]})
        self.conn.commit()
        result = service.blind_spots(
            self.conn, "M-YANBIAN",
            start="2026-09-20T08:30:00+08:00",
            end="2026-09-20T09:30:00+08:00")
        refs = {t["team_ref"] for t in result["teams"]}
        self.assertNotIn("TEAM-1", refs)
        self.assertIn("TEAM-3", refs)  # 花名册中、全程无联络

    # ------------------------------------------------------------ 覆盖窗口

    def test_zone_only_active_during_service_window(self) -> None:
        self._seed_base()
        repo.add_team_location(self.conn, {
            "team_ref": "TEAM-1", "lat": 26.9005, "lon": 101.5005,
            "observed_at_device": "2026-09-20T06:00:00+08:00"})
        self.conn.commit()
        from app.timeutil import parse_iso
        # 基站尚未升空：几何存在也不算覆盖。
        self.assertIs(service.is_covered(
            self.conn, "TEAM-1", parse_iso("2026-09-20T07:00:00+08:00")), False)
        # 升空后在圆内。
        self.assertIs(service.is_covered(
            self.conn, "TEAM-1", parse_iso("2026-09-20T09:00:00+08:00")), True)
        # 无位置数据：未知。
        self.assertIsNone(service.is_covered(
            self.conn, "TEAM-2", parse_iso("2026-09-20T09:00:00+08:00")))

    # ------------------------------------------------------------ 幂等

    def test_ingest_is_idempotent(self) -> None:
        self._seed_base()
        payload = {"events": [{
            "source_event_id": "DUP1", "mission_ref": "M-YANBIAN",
            "team_ref": "TEAM-1", "event_kind": "access_attempt",
            "event_time_device": "2026-09-20T08:10:00+08:00"}]}
        first = repo.ingest_events(self.conn, payload)
        second = repo.ingest_events(self.conn, payload)
        self.conn.commit()
        self.assertEqual((first["accepted"], first["duplicates"]), (1, 0))
        self.assertEqual((second["accepted"], second["duplicates"]), (0, 1))


class HttpTest(unittest.TestCase):
    """通过真实 HTTP 端口走通写入→摄入→查询。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["DATABASE_PATH"] = str(Path(cls.tmp.name) / "http.sqlite3")
        migrate.run()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.tmp.cleanup()
        os.environ.pop("DATABASE_PATH", None)

    def _request(self, method: str, path: str, body: object = None) -> tuple[int, dict]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())
        except IncompleteRead:  # pragma: no cover
            raise

    def test_full_flow_over_http(self) -> None:
        status, _ = self._request("POST", "/admin/missions", {
            "mission_ref": "M-HTTP", "starts_at": T0, "ends_at": T1})
        self.assertEqual(status, 200)
        status, _ = self._request("POST", "/admin/stations", {
            "station_ref": "S1", "mission_ref": "M-HTTP"})
        self.assertEqual(status, 200)
        status, _ = self._request("POST", "/admin/station-windows", {
            "station_ref": "S1", "starts_at": T0, "ends_at": T1})
        self.assertEqual(status, 200)
        status, _ = self._request("POST", "/admin/coverage-zones", {
            "station_ref": "S1", "center_lat": 26.9, "center_lon": 101.5,
            "radius_m": 5000})
        self.assertEqual(status, 200)
        status, _ = self._request("POST", "/admin/teams", {
            "team_ref": "T1", "mission_refs": ["M-HTTP"]})
        self.assertEqual(status, 200)
        status, _ = self._request("POST", "/admin/team-locations", {
            "team_ref": "T1", "lat": 26.9, "lon": 101.5,
            "observed_at_device": "2026-09-20T08:01:00+08:00"})
        self.assertEqual(status, 200)

        # 坏请求：未知事件类型。
        status, body = self._request("POST", "/events/ingest", {"events": [{
            "source_event_id": "BAD", "mission_ref": "M-HTTP",
            "team_ref": "T1", "event_kind": "rescued",
            "event_time_device": "2026-09-20T08:05:00+08:00"}]})
        self.assertEqual(status, 400)
        self.assertIn("event_kind", body["detail"])

        status, body = self._request("POST", "/events/ingest", {"events": [
            {"source_event_id": "P1", "mission_ref": "M-HTTP",
             "team_ref": "T1", "station_ref": "S1", "session_ref": "SE",
             "event_kind": "access_attempt",
             "event_time_device": "2026-09-20T08:05:00+08:00"},
            {"source_event_id": "P2", "mission_ref": "M-HTTP",
             "team_ref": "T1", "station_ref": "S1", "session_ref": "SE",
             "event_kind": "access_attempt",
             "event_time_device": "2026-09-20T08:06:00+08:00"},
            {"source_event_id": "P3", "mission_ref": "M-HTTP",
             "team_ref": "T1", "station_ref": "S1",
             "event_kind": "call_connected",
             "event_time_device": "2026-09-20T08:07:00+08:00"},
        ]})
        self.assertEqual(status, 200)
        self.assertEqual(body["accepted"], 3)

        status, report = self._request(
            "GET", "/missions/M-HTTP/availability")
        self.assertEqual(status, 200)
        t1 = report["teams"][0]
        self.assertEqual(t1["access_attempts"], 2)
        self.assertEqual(t1["successful_calls"], 1)
        self.assertEqual(t1["retry_attempts"], 1)

        status, blind = self._request(
            "GET", "/missions/M-HTTP/blind-spots")
        self.assertEqual(status, 200)
        self.assertEqual(blind["blind_team_count"], 0)

        status, contact = self._request("GET", "/teams/T1/last-contact")
        self.assertEqual(status, 200)
        self.assertIn("00:07", contact["last_effective_contact"]["connected_at"])

        status, events = self._request(
            "GET", "/events?mission_ref=M-HTTP&team_ref=T1")
        self.assertEqual(status, 200)
        self.assertEqual(events["count"], 3)
        # 原始时间随查询可见。
        self.assertTrue(all(e["event_time_device"] for e in events["events"]))


if __name__ == "__main__":
    unittest.main()
