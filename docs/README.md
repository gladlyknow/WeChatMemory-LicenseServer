# WeChatMemory License Server 文档索引

本目录是服务端文档的唯一入口。如果文档与实际参数不一致，以当前 `main`
分支中的脚本 `--help`、单元测试和部署脚本为准，不凭记忆修改生产环境。

## 当前固定基线

| 项目 | 值 |
|---|---|
| 服务器 | Enterprise Linux 9 |
| 源码目录 | `/data/WeChatMemory-LicenseServer` |
| 公网入口 | `https://104.224.90.172:9000` |
| 内部应用 | `http://127.0.0.1:9100` |
| 证书通知邮箱 | `gladlygrady@tutamail.com` |
| 数据库 | 本机 SQLite3 WAL |
| 当前签名 key-id | `2026-10-30` |
| 生产私钥初始上传路径 | `/root/wkm-signing-ed25519.pem` |
| 聊天数据 | 永不上传授权服务器 |

## 阅读路线

| 需求 | 首选文档 | 说明 |
|---|---|---|
| 首次接手或生产值班 | [统一运维手册](SERVER_OPERATIONS_RUNBOOK.md) | 从原理到部署、升级、授权、恢复和排障 |
| 理解系统原理 | [授权服务架构](LICENSE_SERVER_ARCHITECTURE.md) | 信任边界、数据模型、API 和客户端刷新流程 |
| 首次一键部署 | [一键部署与用户授权](ONE_CLICK_DEPLOY_AND_ADMIN.md) | 推荐的 `/data` 部署与所有管理命令 |
| 需要逐步理解 EL9 | [EL9 手工部署](MANUAL_DEPLOY_EL9_IP_CERT.md) | Nginx、SELinux、firewalld、IP 短证书 |
| 生成或轮换签名密钥 | [签名密钥运维](SIGNING_KEY_OPERATIONS.md) | 公钥清单、备份、恢复、客户端先行轮换 |
| 审查固定 IP 部署决策 | [104.224.90.172 部署设计](DEPLOYMENT_104.224.90.172.md) | 网络、TLS、客户端权限语义 |
| 修改文档或发布契约 | [文档随代码发布规则](DOCUMENTATION_POLICY.md) | 必修场景、校验命令和评审清单 |

## 文档权威顺序

1. 当前代码、`--help`、systemd unit 和测试；
2. [统一运维手册](SERVER_OPERATIONS_RUNBOOK.md)；
3. 各专题手册；
4. 历史设计文档。

不允许只修改其中一份文档就改变生产契约。端口、路径、邮箱、密钥 ID、
SQLite schema 或套餐变更必须同时修改代码、测试、统一手册和相关专题文档。

## 不可违反的安全红线

- 不向服务端上传聊天正文、媒体、联系人、搜索词或微信数据库密钥；
- 不把 Ed25519 私钥、TLS 私钥、激活码、refresh token 或 SQLite 数据库提交到 Git；
- 不使用相同 `key_id` 重新生成私钥；
- 不在服务运行时直接复制单独的 `.sqlite3` 作为备份；
- 不把 `9100` 暴露到公网，不关闭 TLS 校验；
- 不在未完成客户端公钥预发布时切换服务端签名密钥。

## 文档和脚本自检

```bash
cd /data/WeChatMemory-LicenseServer
bash -n deploy/el9/*.sh deploy/el9/wkm-license-admin
python3.11 scripts/check_documentation.py
/opt/wechat-memory-license/venv/bin/python -m unittest discover -s tests -v
/opt/wechat-memory-license/venv/bin/python -m license_server signing-key manifest \
  --manifest config/signing-public-keys.json
git diff --check
git status --short
```

上述 `/opt/wechat-memory-license/venv` 命令适用于已安装过服务的生产机。首次部署前，完整单元测试必须在
开发/发布机的项目 `.venv` 中完成；目标 EL9 上的无依赖文档门禁固定使用 `python3.11`。

`deploy-from-data.sh` 会拒绝非 `main` 分支和非干净工作树。有未提交的文档或代码时，
脚本拒绝生产部署是正常的安全行为。
