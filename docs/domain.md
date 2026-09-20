# 领域资料

盐边灾区地面网络中断后，翼龙无人机升空承担临时空中基站。本服务回答指挥员两个问题：

1. 空中基站到底覆盖了哪些救援点？
2. 哪些队伍仍处于通信盲区，他们最近一次真正通上话是什么时候？

`contracts/entities.json` 保存外部数据的交换字段与取值口径，`fixtures/example.json` 是不含真实身份的虚构样例。运行时持久化文件由 `DATABASE_PATH` 决定；时间字段使用带时区偏移的 ISO 8601 字符串，裸时间（无偏移）一律拒收。

## 两类记录不能混算

设备上报分两类，后台严格区分：

- **接入侧**：`access_attempt`（外部接入尝试）、`access_accept`（允许附着）、`access_reject`（拒绝）。
  接入尝试次数包含同一终端的重复重试，也包含来路不明终端的外部探测，**不是成功通话人数**；
  即便附着成功，也只说明终端挂上了基站，**不能报成“已通话”或“已获救”**。
- **通话侧**：`call_connected`（通话建立）、`call_ended`（正常结束）、`call_dropped`（掉话）。
  全系统只有 `call_connected` 能证明队伍真的通上了话，也只有它计入“最近一次有效联络”。

## 接入链归类

同一接入过程用 `attempt_id` 串成链。一条链的结果取四类之一：

| 结果 | 判定 |
| --- | --- |
| `success` | 链上有 `access_accept` 且有 `call_connected` |
| `attached_only` | 有 `access_accept` 但全链无通话证据（接入冗余，不是通话） |
| `rejected` | 链上有 `access_reject` |
| `unknown` | 只有 `access_attempt`、没有终态（结果未知，不得臆断为成功） |

没有 `attempt_id` 的接入尝试计入 `unlinked_external_attempts`，不归到任何在册队伍头上。

## 覆盖与盲区

- 无人机任务时段（`station_missions`）与覆盖圆（`footprints`，带 `valid_from/valid_to`、圆心、半径）共同决定覆盖；
  只有处于节点任务时段内、且脚印生效的覆盖圆才作数。
- 队伍位置取登记坐标，若事件带坐标则用**不晚于判定时刻的最后一次上报位置**（队伍移动时仍可判）。
- 判定口径：位置已知、且在查询窗口**末端**不在任何有效覆盖圆内 → `in_blind_zone`。
  仅有接入尝试或附着不改变盲区判定。位置未知的队伍进入 `teams_location_unknown`，不武断报为盲区。

## 时钟漂移与迟到日志

设备时钟不可信，但原始时间是现场证据，绝不覆盖。每条事件同时保存：

- `device_time_raw`：设备时间逐字留存；
- `event_time`：校正到参考时钟后的时刻；
- `clock_offset_s`：校正量（event_time − device_time，秒），未校正为 null；
- `correction_basis`：`as_reported` / `explicit_offset`（显式偏移）/ `synced_time`（随报参考时刻）/ `late_log`（无对时依据的迟到补传）；
- `received_at` 与 `is_late_log`：后台收到时刻与迟到标记。

迟到日志按设备时间原样归位并标记，绝不静默改写。

## 幂等

`(source_ref, source_event_id)` 是来源方对同一记录的识别符，唯一约束。重传识别为 `duplicate`，
不会产生第二条记录，也不会把重复的接入尝试计成新的尝试。
