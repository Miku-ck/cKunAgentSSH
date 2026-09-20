---
name: linux-ops-triage
slug: linux-ops-triage
version: 1.0.0
description: Linux 服务器故障排查方法论：系统化分层定位（进程/端口/资源/日志/网络），避免乱枪打鸟式试命令
category: 运维
trigger_words: [排查, 故障, 宕机, 无响应, 负载高, 服务起不来, 端口不通, OOM, 内存溢出, 磁盘满]
---

## 适用场景

SSH 运维中需要对 Linux 目标机上的异常现象做定位：服务不可用、负载异常、端口不通、磁盘/内存耗尽、日志报错但根因不明。

## 排查顺序（先只读、后变更）

按成本从低到高逐层确认，每层得出结论再进入下一层，不要跳层直接改：

1. **进程层**：`ps aux --sort=-%mem | head`、`systemctl status <svc>`、`journalctl -u <svc> -n 100 --no-pager`
   - 服务不在 active → 先看 status 的 ExitCode 与最近日志，再决定下一步
2. **端口层**：`ss -tlnp | grep <port>`（监听）+ `ss -tnp | grep <port>`（连接）
   - 无监听 → 进程没起来或配置绑定错；有监听但连不上 → 防火墙/安全组
3. **资源层**：`df -h`、`free -m`、`uptime`、`vmstat 1 3`、`dmesg -T | tail -30`（OOM killer 记录）
   - `dmesg` 出现 `Out of memory: Killed process` → 先确认被杀进程再谈重启
4. **日志层**：应用日志 > 系统日志 > 内核日志，按故障时间点前后 5 分钟取窗口
5. **网络层**：目标机上 `curl -sv http://127.0.0.1:<port>` 本机回环先验证，再排查中间链路

## 输出要求

- 每层给出「现象 → 推断 → 下一步验证」三段式，不要只贴命令输出
- 根因未确定前，不提出重启/清理/改配置等变更类建议
- 需要变更时，明确回滚方式（改配置文件先备份：`cp x.conf x.conf.bak-$(date +%s)`）
- 结论中区分「已证实」与「疑似」，疑似项给出验证命令而非直接下结论

## 常见陷阱

- `top` 的 %CPU 是单核口径，容器内读数会超 100%，看 `vmstat` 的 wa/sy 更可靠
- `df -h` 正常但写入报 No space：检查 inode（`df -ih`）与已删除未释放句柄（`lsof +L1`）
- systemd 服务 `Active: active (exited)` 不代表失败，一次性服务看 `status` 退出码
