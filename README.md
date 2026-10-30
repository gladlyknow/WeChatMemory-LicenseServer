# WeChatMemory License Server

闭源商业版微信知识库的独立云端授权服务仓库。当前交付包含 SQLite3 schema、管理员新用户注册脚本、
激活/刷新/吊销 API、Ed25519 权益票据和 EL9 部署素材。不包含聊天记录处理代码，也不依赖 `WeChatMsg-4x`。

## 安全边界

- 云端不接收聊天正文、媒体、数据库密钥、联系人或搜索词；
- 用户不需要验证邮箱或手机号；
- 用户存在性不等于身份认证，激活时必须同时提供脚本生成的高熵激活码；
- 激活码只显示一次，SQLite3 只保存 SHA-256 摘要；
- 支付、退款和发票不在当前范围；
- Ed25519 签名私钥只能位于服务器受限文件或 KMS/HSM，不能写入 SQLite3、Git 或日志。

## 当前生产部署基线

- 服务器：`104.224.90.172`
- 公网 Web/API：`https://104.224.90.172:9000`
- 内部应用：`127.0.0.1:9100`，不对公网开放
- SQLite3：服务器本机持久磁盘，只允许内部应用进程访问

9000 必须使用 HTTPS，不能用明文 HTTP。详细拓扑、安全规则和客户端权限刷新协议见
[`docs/DEPLOYMENT_104.224.90.172.md`](docs/DEPLOYMENT_104.224.90.172.md)。正式生产仍推荐为该 IP
绑定专用域名，以简化未来迁移；当前已确定采用 Enterprise Linux 9 人工部署及受信任的 IP 短期
证书。逐条部署手册见
[`docs/MANUAL_DEPLOY_EL9_IP_CERT.md`](docs/MANUAL_DEPLOY_EL9_IP_CERT.md)。
整体原理、数据模型、API、激活序列和运维边界见
[`docs/LICENSE_SERVER_ARCHITECTURE.md`](docs/LICENSE_SERVER_ARCHITECTURE.md)。
一键部署、升级、新增用户、套餐调整、续期、停用、激活码重置、设备解绑和备份恢复见
[`docs/ONE_CLICK_DEPLOY_AND_ADMIN.md`](docs/ONE_CLICK_DEPLOY_AND_ADMIN.md)。
文档阅读顺序、权威级别和安全红线见 [`docs/README.md`](docs/README.md)；生产值班统一入口见
[`docs/SERVER_OPERATIONS_RUNBOOK.md`](docs/SERVER_OPERATIONS_RUNBOOK.md)。
文档与代码同版发布的必修规则见
[`docs/DOCUMENTATION_POLICY.md`](docs/DOCUMENTATION_POLICY.md)。
生产签名密钥的生成、幂等发布、客户端信任根同步、备份恢复和轮换见
[`docs/SIGNING_KEY_OPERATIONS.md`](docs/SIGNING_KEY_OPERATIONS.md)。
目标服务器的标准源码目录是 `/data/WeChatMemory-LicenseServer`，可从该目录执行
`deploy/el9/deploy-from-data.sh`；证书通知邮箱默认为 `gladlygrady@tutamail.com`。

本仓库是独立商业实现的起点。正式版权声明、EULA 和隐私政策须在权利链审计和法律复核后加入；
当前不从 GPL 原型仓库复制客户端实现。

## 初始化数据库

```bash
python register_user.py init --db ./data/license.sqlite3
```

数据库会自动使用 WAL、外键、`synchronous=FULL` 和 5 秒忙等待。文件在支持 POSIX 权限的平台上
设为 `0600`。

## 注册用户

生产服务器部署后优先使用固定入口：

```bash
sudo wkm-license-admin add --username demo-user --plan free
```

完整管理员命令见一键部署与授权运维手册。以下 `register_user.py` 方式主要用于开发环境和首次数据库
测试。

免费版：

```bash
python register_user.py add \
  --db ./data/license.sqlite3 \
  --username demo-user \
  --plan free
```

永久个人版：

```bash
python register_user.py add \
  --db ./data/license.sqlite3 \
  --username alice \
  --display-name Alice \
  --plan permanent_personal
```

专业订阅版必须给出有效期：

```bash
python register_user.py add \
  --db ./data/license.sqlite3 \
  --username bob \
  --plan pro_subscription \
  --days 365
```

脚本成功后输出：

- 用户 ID；
- 权益 ID；
- 套餐、设备上限和离线天数；
- 只显示一次的激活码。

激活码应通过安全渠道交给用户。丢失后不能从数据库还原，后续应通过“吊销旧码并生成新码”的管理
命令处理，不能把明文激活码写入数据库或日志。

自动化系统可增加 `--json`，但输出仍含一次性激活码，应避免进入 CI 日志：

```bash
python register_user.py add --db ./data/license.sqlite3 \
  --username demo-user --plan free --json
```

## 验证

```bash
python -m unittest discover -s tests -v
```

Shell 脚本静态校验：

```bash
bash -n deploy/el9/*.sh deploy/el9/wkm-license-admin
```

文档契约校验（不访问外网）：

```bash
python scripts/check_documentation.py
```

## 本地 API 启动（仅开发）

```bash
python -m license_server signing-key ensure \
  --path ./.secrets/dev.pem --key-id dev-1 \
  --manifest ./.secrets/dev-public-keys.json --publish --activate
export DATABASE_PATH="$PWD/data/license.sqlite3"
export SIGNING_PRIVATE_KEY_PATH="$PWD/.secrets/dev.pem"
export SIGNING_KEY_ID=dev-1
export SIGNING_PUBLIC_KEYS_PATH="$PWD/.secrets/dev-public-keys.json"
python register_user.py init --db "$DATABASE_PATH"
python -m license_server --host 127.0.0.1 --port 9100
```

生产不使用 Flask 开发服务器，模块入口使用 Waitress。对公网的 TLS 只由 Nginx 终止。

## 当前套餐基线

| 套餐 | 设备 | 离线期限 | 基线能力 |
|---|---:|---:|---|
| `free` | 1 | 30 天 | 只读、手动同步、基础单会话导出 |
| `permanent_personal` | 2 | 180 天 | 永久使用已购版本、1 年免费升级、标准个人功能 |
| `pro_subscription` | 3 | 30 天 | 实时同步、多账号、组合批量导出、高级统计 |

套餐会以快照写入每条权益，后续修改套餐不会意外改变已经售出的历史权益。
