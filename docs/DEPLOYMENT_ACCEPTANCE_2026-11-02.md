# Server 0.2.7 生产部署验收记录（2026-11-02）

本文记录 `WeChatMemory-LicenseServer 0.2.7` 在目标 Enterprise Linux 9
服务器上的首次 HTTP 生产部署证据。它是一次现场事实记录，不替代
[验收标准](ACCEPTANCE_CRITERIA.md)；尚未执行的授权金丝雀、故障注入和恢复演练继续保持开放。

## 1. 发布标识

| 项目 | 现场结果 |
|---|---|
| Git 提交 | `a7f9848`（`Pin license runtime to immutable releases`） |
| 服务端版本 | `0.2.7` |
| 物理 release | `/opt/wechat-memory-license/releases/20261102T031345Z-1332268` |
| 公网入口 | `http://104.244.90.172:9000` |
| 内部入口 | `http://127.0.0.1:9100` |
| SQLite schema | `3`，WAL |
| 签名 key-id | `2026-10-30` |
| 退役配置备份 | `/etc/wechat-memory-license/retired-nginx/retirement.dfSN2HCy` |

## 2. 部署前门禁

- `/root/wkm-signing-ed25519.pem` 是 `root:root 0600` 普通文件；未输出 PEM 内容；
- `nginx -t` 通过；
- 候选独立 venv 安装完成；
- `120 passed, 31 subtests passed`；
- 部署事务自测通过；
- `service preflight: ready`，说明最终环境、数据库、签名私钥、公钥清单和
  Waitress 安装文件/导入链均在服务身份下通过；
- 公钥响应中的 raw/SPKI SHA-256 与客户端跟踪清单一致。

公开指纹（不是私钥）：

```text
raw_sha256=f4dd27e3a4e926635a3925c44fa5aa270bd2b0b905c744e93b27c836e49aec7b
spki_sha256=11c1e1a9475518e135753cc6ae21637695e9842840f05aed0bdbfa8e666073bf
```

## 3. 运行态验收

| 检查 | 结果 |
|---|---|
| `wechat-memory-license` | `active`、`enabled` |
| `wechat-memory-license-backup.timer` | `active`、`enabled` |
| `nginx` | `active` |
| `127.0.0.1:9100/readyz/deep` | `{"check":"deep","status":"ready"}` |
| `104.244.90.172:9000/readyz` | `{"status":"ready"}` |
| `104.244.90.172:9000/v1/keys` | active key `2026-10-30` |
| 应用监听 | Python 仅监听 `127.0.0.1:9100` |
| 公网监听 | Nginx 监听 IPv4/IPv6 `:9000` |
| Nginx 语法 | 通过 |
| 旧站点配置 | `entityecho.conf` 不再位于 `conf.d`，恢复副本已保留 |

systemd 单元在本次版本中由部署器渲染到上述物理 release。运行中解释器、工作目录、
启动预检和公钥清单不通过可变的 `current/.venv` 加载，从而关闭旧 `0.2.5`
启动/回滚期间跨 release 混用模块的问题。

## 4. 已通过与仍开放

本次现场证据已证明：

- 目标主机可以从干净 `main` 创建不可变 release；
- 最终服务身份可以加载 Waitress、数据库、私钥和公钥清单；
- HTTP `:9000`、回环 `:9100`、Nginx、深度就绪检查和公钥接口工作正常；
- 已停用的 `entityecho.conf` 已经显式、事务化退役，并保留可恢复副本；
- 应用和备份 timer 已由 systemd 接管。

下列项目没有因本次基础部署自动关闭：

1. 专用测试用户的 `activate → 同码重放 401 → refresh → 旧 token 拒绝 → deactivate` 金丝雀；
2. `after_application_switch`、`after_nginx_switch` 等现场故障注入与回滚证据；
3. 手工触发一次数据库备份、检查备份完整性和恢复演练；
4. Client `0.3.5` 最终 Windows/Ubuntu 包与该 HTTP 入口的实机联动；
5. HTTP 明文传输的架构风险。Ed25519 只保护授权票据真实性，不提供链路保密。

这些项目必须继续按验收标准执行，不得用本记录中的健康检查替代。
