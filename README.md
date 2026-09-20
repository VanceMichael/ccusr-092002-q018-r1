# 空中基站救援通话可用性账

盐边灾区地面网络中断后，翼龙无人机携空中基站升空组网。本服务关联**无人机任务时段、基站服务窗口与覆盖区、接入尝试、通话建立与中断事件**，向调度员回答：空中基站覆盖了哪些救援点、哪些队伍仍在通信盲区、他们最近一次真正通话在何时。

核心口径：**接入尝试次数 ≠ 成功通话人数 ≠ 获救**。成功通话只以 `call_connected` 计数，重复重试单列，窗口结束仍无终局的尝试记为未知结果。详见 `docs/domain.md`。

本服务仅依赖 Python 标准库与 SQLite。`PORT` 指定监听端口，`DATABASE_PATH` 指定数据文件；时间字段一律使用带时区偏移的 ISO 8601 字符串（存储归一化为 UTC），设备原始时间与时钟校正依据永久留痕。

## 接口

写入参考数据（均为 POST JSON）：

| 路径 | 说明 |
| --- | --- |
| `/admin/missions` | 任务（`mission_ref`、`starts_at`、可选 `ends_at`） |
| `/admin/stations` | 空中基站（`station_ref`、`mission_ref`） |
| `/admin/station-windows` | 基站实际升空服务窗口 |
| `/admin/coverage-zones` | 覆盖圆（圆心 + 半径米 + 可选生效时段） |
| `/admin/teams` | 救援队伍（可用 `mission_refs` 编入花名册） |
| `/admin/team-locations` | 队伍位置上报（保留 `observed_at_device`） |
| `/admin/clock-corrections` | 时钟校正依据（迟到依据触发历史事件重算，旧依据保留） |
| `/events/ingest` | 批量事件摄入（`source_event_id` 幂等） |

查询：

| 路径 | 说明 |
| --- | --- |
| `GET /missions/<ref>/availability?start=&end=` | 按队伍列出尝试/成功/拒绝/未知/重试/中断与链路明细 |
| `GET /missions/<ref>/blind-spots?start=&end=` | 盲区队伍：覆盖状态、窗口内尝试次数、最近有效联络（含原始时间与校正依据） |
| `GET /teams/<ref>/last-contact` | 单队最近一次 `call_connected` |
| `GET /devices/<id>/clock-corrections` | 设备时钟依据台账（含已取代的历史依据） |
| `GET /events?mission_ref=&team_ref=&start=&end=` | 原始事件（含设备时间、校正时间与留痕） |

事件类型：`access_attempt`、`access_rejected`、`call_connected`、`call_dropped`、`call_ended`。

## 示例

```bash
curl -s localhost:8080/admin/missions -d '{"mission_ref":"M1","starts_at":"2026-09-20T08:00:00+08:00"}'
curl -s localhost:8080/events/ingest -d @fixtures/example.json
curl -s "localhost:8080/missions/M1/blind-spots"
```

## 本地开发

`make migrate` 初始化数据文件（自动执行 `migrations/` 下全部未应用迁移），`make test` 运行自动化检查，`make run` 启动服务。`docker compose up --build` 启动隔离容器，`APP_PORT` 调整宿主机端口。

`contracts/entities.json` 记录字段约定与计数口径，`fixtures/example.json` 为不含真实身份的交换示例。
