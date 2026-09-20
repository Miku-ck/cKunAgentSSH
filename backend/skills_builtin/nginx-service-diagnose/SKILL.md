---
name: nginx-service-diagnose
slug: nginx-service-diagnose
version: 1.0.0
description: Nginx/反向代理与 systemd 服务故障速查：502/504、证书、端口冲突、unit 启动失败的定位路径
category: 运维
trigger_words: [nginx, 502, 504, 反向代理, 证书, https报错, unit, systemctl失败, 服务启动失败, 端口冲突]
---

## 适用场景

目标机上 Nginx（或同类反代）报 5xx、HTTPS 证书异常、systemd unit 启动失败、端口被占。

## 502 / 504 定位路径

1. 区分谁在报：`curl -sI https://<site>` 看 `Server:` 头；502/504 若来自上游服务本身则与 nginx 无关
2. nginx 错误日志按时间对齐：`tail -50 /var/log/nginx/error.log`
   - `connect() failed (111: Connection refused)` → 上游进程没监听期望端口
   - `upstream timed out (110)` → 上游进程活着但处理超时（看应用负载/慢查询）
   - `no live upstreams` → upstream 组全部被标记失败，先验证各成员再 `nginx -s reload` 清失败状态
3. 从 nginx 机器视角验证上游：`curl -sv http://<upstream_host>:<port>/<health>`（不是从本机 localhost 测）
4.  SELinux/防火墙：`getenforce`、`grep denied /var/log/audit/audit.log | tail`——连不上但端口明明在监听时优先查这里

## 证书问题

- 过期：`echo | openssl s_client -servername <host> -connect <host>:443 2>/dev/null | openssl x509 -noout -dates`
- 链条不全（浏览器正常但 curl/移动端报错）：`openssl s_client -connect <host>:443 -showcerts` 数证书张数，只有 1 张说明缺中间证书，fullchain 文件重新拼接
- certbot 续期失败：看 `journalctl -u certbot.renew --since "-7 days"`，80 端口被占是常见原因

## systemd unit 启动失败

1. `systemctl status <svc> -l --no-pager` 拿 Exit-Code 与最后几行日志
2. Exit code 1 → 应用自己报错，直接手跑前台命令复现：读 unit 的 `ExecStart=` 去掉 `-` 前缀原样执行
3. Exit code 203/EXEC → `ExecStart` 路径不存在或不可执行（常见于装完包没 reload、venv 路径变了）
4. `timeout` 类失败 → 检查 `TimeoutStartSec` 与依赖挂载/网络 unit 的顺序（`After=`/`Requires=`）
5. 配置语法：nginx `nginx -t`、`systemd-analyze verify <unit>`——报错先修语法再谈启动

## 端口冲突

- `ss -tlnp | grep <port>` 找到占用 PID，`readlink /proc/<pid>/exe` 确认是哪个二进制
- 禁止直接 kill 占用进程了事：先确认该进程归属（`systemctl status <pid>` 反查服务），给出「改监听端口」与「停冲突服务」两个选项让用户决策

## 输出要求

- 每条结论附支撑命令与其关键输出摘录，不允许只给结论
- 修复建议按「最小改动优先」排序：reload > restart > 改配置 > 装/卸载
