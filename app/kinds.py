"""事件种类与归类约定。

两类记录严格区分：
- 接入侧（access_*）：只说明终端尝试/获准附着空中基站。接入尝试次数是冗余信号，
  附着成功也不等于通话成功，更不能算作“获救”；
- 通话侧（call_*）：只有 call_connected 才证明实际建立了语音通话。
"""

from __future__ import annotations

ACCESS_KINDS = {
    "access_attempt",   # 外部接入尝试（可能来自非在册终端/探测，默认不计入在册队伍）
    "access_accept",    # 网络允许附着
    "access_reject",    # 网络拒绝附着
}

CALL_KINDS = {
    "call_connected",   # 实际通话建立（唯一“真正通上话”的证据）
    "call_ended",       # 通话正常结束
    "call_dropped",     # 通话中断（掉话）
}

VALID_KINDS = ACCESS_KINDS | CALL_KINDS | {"coverage_sample"}

# 接入链终态：没有后续 accept/reject 的 attempt 属于未知结果
ACCESS_TERMINAL = {"access_accept", "access_reject"}
