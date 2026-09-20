# 空中基站救援通话可用性账

盐边灾区地面网络中断、翼龙无人机升空组网后，指挥员需要知道空中基站覆盖了哪些救援点、哪些队伍仍在通信盲区。
本服务关联**无人机任务时段、基站覆盖区、接入尝试、通话建立与中断事件**，区分成功连接、重复重试与未知结果；
设备时钟漂移或迟到日志保留原始时间与校正依据，不把接入冗余误报成获救或实际通话。

本服务采用 HTTP 接口和 SQLite 本地文件（仅标准库）。运行参数 `PORT` 指定监听端口，`DATABASE_PATH` 指定数据文件；
`fixtures/example.json` 保存不含真实身份的交换示例，`contracts/entities.json` 记录字段约定，`docs/domain.md` 介绍来源与口径。

## 本地开发

`make migrate` 初始化数据文件，`make test` 运行自动化检查，`make run` 启动服务。
`docker compose up --build` 可以启动隔离容器，`APP_PORT` 可调整宿主机端口。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/admin/missions` | 登记/更新无人机任务时段 |
| POST | `/admin/teams` | 登记救援队伍（坐标可空，未知位置不武断判盲区） |
| POST | `/admin/station-missions` | 基站（空中节点）任务时段 |
| POST | `/admin/footprints` | 基站覆盖圆（圆心、半径、生效时段） |
| POST | `/ingest` | 上报事件，单条对象 / 数组 / `{"received_at":..., "events":[...]}`，幂等 |
| GET | `/missions/{ref}/attempt-chains?start=&end=` | 接入尝试链归类与通话口径汇总 |
| GET | `/missions/{ref}/blind-zones?start=&end=` | 盲区队伍与最近一次有效联络 |
| GET | `/missions/{ref}/events?start=&end=&team=` | 原始事件核查（含原始时间与校正依据） |

时间参数一律带时区偏移，如 `start=2026-09-20T10:00:00+08:00`。

### 关键口径

- `access_attempt` 含重复重试与外部探测，**不是人数**；`access_accept` 只是附着，不是通话。
- 接入链结果：`success`（附着且 `call_connected`）/ `attached_only`（仅附着）/ `rejected` / `unknown`（无终态）。
- 真正通话与“最近一次有效联络”只认 `call_connected`。
- 盲区：窗口末端队伍位置不在任何任务时段内的有效覆盖圆中；位置未知单列。
- 时钟校正只新增 `event_time` + `clock_offset_s` + `correction_basis`，`device_time` 原始值永远保留。
- `(source_ref, source_event_id)` 重传识别为重复，不重复计数。

### 快速试一下

```bash
make migrate run            # 启动服务（另开终端执行后续命令）
curl -s localhost:8080/health
# 登记任务、覆盖、队伍、事件见 fixtures/example.json
curl -s "localhost:8080/missions/YB-DEMO/blind-zones?start=2026-09-20T10:00:00%2B08:00&end=2026-09-20T12:00:00%2B08:00"
```
