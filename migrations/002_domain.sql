-- 应急通信可用性后台：任务时段、空中基站覆盖脚印、队伍、原始事件
-- 时间约定：所有 *_time 列均为 UTC ISO 8601 字符串（带偏移或 Z）；
-- device_time_raw 按设备本地时钟逐字保留，event_time 是校正到参考时钟后的时刻。

CREATE TABLE IF NOT EXISTS missions (
    mission_ref   TEXT PRIMARY KEY,
    name          TEXT NOT NULL DEFAULT '',
    started_at    TEXT NOT NULL,          -- 无人机升空组网开始（UTC ISO 8601）
    ended_at      TEXT,                   -- 任务结束；NULL 表示尚未结束
    source        TEXT NOT NULL DEFAULT 'manual',
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS teams (
    team_ref      TEXT PRIMARY KEY,
    name          TEXT NOT NULL DEFAULT '',
    latitude      REAL,                   -- 可仅按 ref 登记，坐标可空
    longitude     REAL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS station_missions (
    -- 同一架次内单个基站（空中节点）的任务时段
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_ref   TEXT NOT NULL REFERENCES missions(mission_ref),
    station_ref   TEXT NOT NULL,
    node_kind     TEXT NOT NULL DEFAULT 'uav_relay',
    altitude_m    REAL,
    started_at    TEXT NOT NULL,          -- 该节点入网广播开始覆盖的时刻
    ended_at      TEXT,                   -- 返航/切出时刻；NULL 表示仍在空中
    UNIQUE(mission_ref, station_ref)
);

CREATE TABLE IF NOT EXISTS footprints (
    -- 基站在某个时刻的覆盖圆（地面投影），同一节点多个脚印表示覆盖区随时间变化
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_ref   TEXT NOT NULL REFERENCES missions(mission_ref),
    station_ref   TEXT NOT NULL,
    valid_from    TEXT NOT NULL,          -- 此脚印生效起始（参考时钟）
    valid_to      TEXT,                   -- 失效时刻；NULL 表示持续有效
    center_lat    REAL NOT NULL,
    center_lon    REAL NOT NULL,
    radius_m      REAL NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_footprints_time
    ON footprints(mission_ref, valid_from);

CREATE TABLE IF NOT EXISTS events (
    -- 设备上报的原始事件，幂等键为 (source_ref, source_event_id)
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_ref       TEXT NOT NULL,
    source_event_id  TEXT NOT NULL,
    mission_ref      TEXT NOT NULL,
    station_ref      TEXT NOT NULL DEFAULT '',
    team_ref         TEXT NOT NULL DEFAULT '',
    event_kind       TEXT NOT NULL,       -- access_attempt/access_accept/access_reject/
                                          -- call_connected/call_ended/call_dropped/...
    device_time_raw  TEXT NOT NULL,       -- 逐字保留的设备时间串
    event_time       TEXT NOT NULL,       -- 校正到参考时钟后的时刻
    clock_offset_s   REAL,                -- event_time - device_time（秒）；未校正为 NULL
    correction_basis TEXT NOT NULL DEFAULT 'as_reported',
                                          -- as_reported/explicit_offset/synced_time/order_late_log
    received_at      TEXT NOT NULL,      -- 后台收到时刻（迟到日志的判定依据）
    is_late          INTEGER NOT NULL DEFAULT 0,
    attempt_id       TEXT NOT NULL DEFAULT '',  -- 接入尝试链标识；空串表示无归属
    call_id          TEXT NOT NULL DEFAULT '',
    latitude         REAL,
    longitude        REAL,
    payload          TEXT NOT NULL DEFAULT '{}',
    UNIQUE(source_ref, source_event_id)
);
CREATE INDEX IF NOT EXISTS idx_events_lookup
    ON events(mission_ref, event_time);
CREATE INDEX IF NOT EXISTS idx_events_team
    ON events(team_ref, event_time);
CREATE INDEX IF NOT EXISTS idx_events_attempt
    ON events(attempt_id);

INSERT OR IGNORE INTO schema_migrations(version) VALUES ('002_domain');
