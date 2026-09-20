-- 应急通信可用性账：任务、空中基站、覆盖区、救援队伍、时钟校正与原始事件流。
-- 约定：所有 *_at（非 *_device）列均为校正后的 UTC ISO 8601 字符串；
-- *_device 列保留设备原始时间串，任何校正都不得覆盖。

CREATE TABLE IF NOT EXISTS missions (
    mission_ref   TEXT PRIMARY KEY,              -- 任务编号（外部标识）
    name          TEXT,
    starts_at     TEXT NOT NULL,                 -- 任务开始（校正后 UTC）
    ends_at       TEXT,                          -- 任务结束，NULL 表示仍在进行
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stations (
    station_ref   TEXT PRIMARY KEY,              -- 空中基站编号（翼龙挂载基站）
    mission_ref   TEXT NOT NULL REFERENCES missions(mission_ref),
    name          TEXT
);

-- 基站实际升空服务时段（同一基站可多架次/多窗口）。
CREATE TABLE IF NOT EXISTS station_windows (
    id            INTEGER PRIMARY KEY,
    station_ref   TEXT NOT NULL REFERENCES stations(station_ref),
    starts_at     TEXT NOT NULL,                 -- 起播服务时间（校正后 UTC）
    ends_at       TEXT,                          -- 终止服务时间，NULL 表示仍在空中
    note          TEXT
);
CREATE INDEX IF NOT EXISTS idx_station_windows_ref ON station_windows(station_ref);

-- 基站覆盖区：以圆心+半径（米）表达，可随时间调整（航向/高度变化）。
CREATE TABLE IF NOT EXISTS coverage_zones (
    id            INTEGER PRIMARY KEY,
    station_ref   TEXT NOT NULL REFERENCES stations(station_ref),
    zone_ref      TEXT,                          -- 覆盖区编号，可空
    center_lat    REAL NOT NULL,
    center_lon    REAL NOT NULL,
    radius_m      REAL NOT NULL,
    valid_from    TEXT,                          -- 几何生效起点，NULL 表示随窗口起
    valid_to      TEXT                           -- 几何生效终点，NULL 表示仍有效
);
CREATE INDEX IF NOT EXISTS idx_coverage_zones_ref ON coverage_zones(station_ref);

CREATE TABLE IF NOT EXISTS teams (
    team_ref      TEXT PRIMARY KEY,              -- 救援队伍编号
    name          TEXT
);

-- 任务-队伍花名册：被编入某任务的队伍，即使全程无事件也必须出现在盲区名单中。
CREATE TABLE IF NOT EXISTS mission_teams (
    mission_ref   TEXT NOT NULL REFERENCES missions(mission_ref),
    team_ref      TEXT NOT NULL REFERENCES teams(team_ref),
    PRIMARY KEY (mission_ref, team_ref)
);

-- 队伍位置上报（含设备原始时间，供覆盖判定回放）。
CREATE TABLE IF NOT EXISTS team_locations (
    id                     INTEGER PRIMARY KEY,
    team_ref               TEXT NOT NULL REFERENCES teams(team_ref),
    device_id              TEXT,                 -- 上报设备，用于迟到时钟校正回放
    lat                    REAL NOT NULL,
    lon                    REAL NOT NULL,
    observed_at_device     TEXT,                 -- 设备原始时间串
    observed_at            TEXT NOT NULL,        -- 校正后 UTC
    clock_correction_id    INTEGER,
    received_at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_team_locations_team_time
    ON team_locations(team_ref, observed_at);

-- 设备时钟校正台账：corrected_time = device_time + offset_ms。
-- 校正可能晚于事件到达，因此保留全部历史与依据，不删除旧校正。
CREATE TABLE IF NOT EXISTS clock_corrections (
    id                     INTEGER PRIMARY KEY,
    device_id              TEXT NOT NULL,
    offset_ms              INTEGER NOT NULL,     -- 校正量：校正后 = 设备时间 + 偏移
    basis                  TEXT NOT NULL,        -- 依据：gnss_sync / network_sync / manual_estimate / drift_model ...
    evidence               TEXT,                 -- 依据说明（对时来源、漂移率、操作人等）
    effective_from_device  TEXT,                 -- 在设备时间轴上的生效起点（原始串），NULL 表示从最早事件起
    recorded_at            TEXT NOT NULL,        -- 服务器收到该依据的时间
    superseded             INTEGER NOT NULL DEFAULT 0  -- 1 表示已被更新依据取代，历史保留
);
CREATE INDEX IF NOT EXISTS idx_clock_corrections_device
    ON clock_corrections(device_id, recorded_at);

-- 原始事件流（只追加）。任何派生统计都不得把 access_attempt 计作通话。
CREATE TABLE IF NOT EXISTS events (
    id                     INTEGER PRIMARY KEY,
    source_event_id        TEXT NOT NULL UNIQUE, -- 来源记录标识，幂等摄入
    mission_ref            TEXT NOT NULL,
    station_ref            TEXT,
    team_ref               TEXT NOT NULL,
    device_id              TEXT,
    session_ref            TEXT,                 -- 通话/尝试会话号（来源提供时用于关联）
    event_kind             TEXT NOT NULL CHECK (
        event_kind IN (
            'access_attempt',   -- 外部接入尝试（不代表成功）
            'access_rejected',  -- 接入被拒/明确失败
            'call_connected',   -- 通话成功建立（“有效联络”以此为准）
            'call_dropped',     -- 通话中断
            'call_ended'        -- 通话正常结束
        )
    ),
    event_time_device      TEXT NOT NULL,        -- 设备原始时间串，永不改写
    event_time_corrected   TEXT NOT NULL,        -- 校正后 UTC
    clock_correction_id    INTEGER,              -- 采用的校正依据（NULL=原始串自带可信偏移/无需校正）
    clock_offset_ms_used   INTEGER,              -- 摄入时实际使用的偏移量，留痕
    received_at            TEXT NOT NULL,        -- 服务器接收时间（识别迟到日志）
    payload_json           TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_team_time ON events(team_ref, event_time_corrected);
CREATE INDEX IF NOT EXISTS idx_events_mission_time ON events(mission_ref, event_time_corrected);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_ref);
CREATE INDEX IF NOT EXISTS idx_events_kind_time ON events(event_kind, event_time_corrected);
