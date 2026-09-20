---
name: ops-change-safety
slug: ops-change-safety
version: 1.0.0
description: 生产变更安全规程：变更前备份与回滚预案、变更窗口、灰度与验证步骤，确保任何远程操作可撤销
category: 运维
trigger_words: [变更, 改配置, 修改配置, 重启服务, 升级, 部署, 回滚, 上线, 调整参数]
---

## 适用场景

任何会对目标机状态产生影响的执行：改配置文件、重启服务、装/卸软件包、清理文件、调整系统参数。执行前必须满足本技能的准备清单。

## 变更四步法（顺序不可调换）

1. **备份**：动哪个文件就先备份哪个
   - 文件：`cp -a <path> <path>.bak-$(date +%Y%m%d-%H%M%S)`
   - 服务单元/配置目录：`tar czf /root/bak-<name>-$(date +%s).tgz <dir>`
   - 数据库：先确认有可用快照/dump，没有则不执行任何写操作
2. **预案**：写清楚「回到变更前」的确切命令，不是"恢复备份"这种空话
   - 例：`cp -a /etc/nginx/nginx.conf.bak-20260101 /etc/nginx/nginx.conf && systemctl reload nginx`
3. **执行 + 即时验证**：变更命令与验证命令成对提交
   - nginx：`nginx -t` 通过才 reload（reload 不是 restart）
   - systemd 服务：`systemctl is-active <svc>` + 关键端口 `ss -tlnp` + 健康接口 `curl -fsS localhost:<port>/health`
   - 软件包：安装后 `which <bin> && <bin> --version` 确认实际生效版本
4. **观察窗**：变更完成后至少看一次日志尾部确认无新报错
   - `journalctl -u <svc> --since "-2 min" --no-pager | grep -iE "error|fail" || echo "无报错"`

## 硬性红线

- 生产环境禁止未备份直接编辑配置；禁止 `restart` 代替 `reload`（除非明确需要重建进程）
- 禁止 `rm -rf` 通配路径未先 `ls` 确认；删除前列目录并给出将删对象数量
- 一次只做一件事：合并多个变更 = 回滚时无法定位是哪一步引入问题
- `systemctl daemon-reload` 只在改了 unit 文件后需要，普通配置变更不需要
- 包管理事务失败后先 `dpkg --audit` / `rpm -Va` 检查残留状态，再决定是否重试

## 输出要求

- 执行前列出「本次变更清单」：目标文件/服务、备份位置、回滚命令、验证方式
- 执行后逐项对照清单报告验证结果，任何一项失败立即建议回滚而不是继续修补
