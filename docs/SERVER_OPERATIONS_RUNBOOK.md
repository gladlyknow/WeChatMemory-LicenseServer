# WeChatMemory 授权服务统一运维手册

> 适用：Enterprise Linux 9，源码 `/data/WeChatMemory-LicenseServer`，
> 公网 `http://104.244.90.172:9000`，邮箱 `gladlygrady@tutamail.com`。
> 本文是值班人员的统一入口；密钥轮换等高风险操作会链接到专题手册。

## 1. 系统做什么

授权服务只管理账号、激活凭据摘要、匿名设备、套餐权益、refresh session 和
Ed25519 签名票据。它不存储微信聊天正文、图片、语音、视频、联系人、搜索词和微信
数据库密钥。

```mermaid
flowchart LR
    C["Windows / Ubuntu 客户端"] -->|"HTTP 9000"| N["Nginx：反向代理与限流"]
    N -->|"127.0.0.1:9100"| A["Waitress + Flask API"]
    A --> D["SQLite3 WAL"]
    A --> K["Ed25519 签名私钥"]
    O["SSH 管理员"] --> W["wkm-license-admin"]
    W --> D
    T["systemd timer"] --> B["SQLite 一致备份"]
```

当前没有公网管理 Web，也没有 `/admin` 路由。用户和设备管理只能通过 SSH 后的
`sudo wkm-license-admin` 完成。

当前 HTTP 是明确接受的传输风险：Ed25519 防票据伪造，但不防链路窃听。一次性激活码在首次成功
激活的同一事务内立即吊销，refresh token 默认 365 天且每次刷新轮换并绑定设备。长寿命是为了覆盖
永久个人版 180 天离线承诺，也会扩大明文链路窃听后的风险窗口；这些措施只能限损，
不能提供保密性。尽量用云安全组限制 9000 来源；WebSocket 也不是加密替代品。

## 2. 运行目录与服务

| 路径或单元 | 作用 | 备注 |
|---|---|---|
| `/data/WeChatMemory-LicenseServer` | 干净的 `main` 源码工作树 | 不放私钥、数据库、日志 |
| `/opt/wechat-memory-license/releases` | 不可变发布版 | 每个 release 自带独立 `.venv` |
| `/opt/wechat-memory-license/current` | 当前发布软链接 | 原子切换 |
| `/opt/wechat-memory-license/current/.venv` | 当前 release 的 Python 3.11 环境 | 不修改其他 release 的依赖 |
| `/etc/wechat-memory-license/service.env` | 应用配置 | `root:wkm-license 0640` |
| `/etc/wechat-memory-license/signing-ed25519-<key-id>.pem` | 签名私钥 | 不进 Git/SQLite/普通备份 |
| `/var/lib/wechat-memory-license/license.sqlite3` | 授权数据库 | 本机磁盘 |
| `/var/lib/wechat-memory-license/backups` | 每日一致备份 | 默认保留 30 天 |
| `wechat-memory-license.service` | 授权 API | 非 root 运行 |
| `wechat-memory-license-backup.timer` | 数据库备份及终态 refresh session 有界清理 | UTC 03:15，随机延迟 20 分钟；先备份后清理 |
| `wechat-memory-certbot-renew.timer` | 仅可选 letsencrypt 模式安装 | 默认 HTTP 不存在/不启用 |

## 3. 首次部署

### 3.1 前置检查

- 云安全组与 firewalld 允许 `9000/tcp`；
- `22/tcp` 限定管理来源；`9100/tcp` 不对公网开放；
- `104.244.90.172` 指向本机；
- 已安全准备与跟踪公钥清单匹配的 `/root/wkm-signing-ed25519.pem`；
- 系统时间同步正常，磁盘不是 NFS/SMB。

私钥权限：

```bash
sudo chown root:root /root/wkm-signing-ed25519.pem
sudo chmod 0600 /root/wkm-signing-ed25519.pem
```

不要在服务器上使用相同 `key_id` 临时生成一把新私钥。丢失当前私钥时必须从加密
离线备份恢复。

### 3.2 获取代码并部署

```bash
sudo install -d -o root -g root -m 0755 /data
sudo git clone https://github.com/gladlyknow/WeChatMemory-LicenseServer.git \
  /data/WeChatMemory-LicenseServer
cd /data/WeChatMemory-LicenseServer
git status --short
git branch --show-current

sudo bash deploy/el9/deploy-from-data.sh \
  --signing-key /root/wkm-signing-ed25519.pem \
  --firewall-zone public
```

入口脚本默认使用：

- `/data/WeChatMemory-LicenseServer`；
- `104.244.90.172:9000`；
- `tls-mode=http`；
- `gladlygrady@tutamail.com`；
- `key-id=2026-10-30`；
- `origin/main` 的 fast-forward 更新。

脚本只允许干净的 `main` 工作树。发布器对 Git 工作树使用 `git archive HEAD`，因此未提交的
修改既不会也不应进入生产发布。

### 3.3 部署后验收

```bash
sudo systemctl is-active wechat-memory-license
sudo systemctl is-active nginx
sudo systemctl is-enabled wechat-memory-license-backup.timer

curl --fail --silent --show-error http://127.0.0.1:9100/readyz/deep
curl --fail --silent --show-error http://104.244.90.172:9000/readyz
curl --fail --silent --show-error http://104.244.90.172:9000/v1/keys

sudo -u wkm-license sqlite3 \
  /var/lib/wechat-memory-license/license.sqlite3 \
  'PRAGMA integrity_check; PRAGMA journal_mode; PRAGMA foreign_keys;'
sudo ss -lntp | grep -E ':(9000|9100)\b'
```

验收标准：数据库完整性为 `ok`，日志模式为 `wal`，9000 为 Nginx HTTP，9100 只绑定
`127.0.0.1`，`/v1/keys` 的 `active_key_id` 与客户端内置清单一致。

## 4. 日常升级和回滚

### 4.1 升级

```bash
cd /data/WeChatMemory-LicenseServer
git status --short
sudo bash deploy/el9/deploy-from-data.sh
```

一键入口在任何 Nginx 配置修改前先校验源码文档契约。发布器随后从已提交的
`HEAD` 创建新 release，再次校验 release 内文档，然后通过 SQLite `.backup` 生成并校验
一致备份。备份成功后，发布器先拒绝含标准私钥标记的候选包，再以 `requirements.lock` 约束精确版本，
只在候选 release 的 `.venv` 中安装依赖，验证依赖闭包与精确 SBOM、
严格编译、服务端全测、全部 EL9 Shell 语法和部署事务自测，再校验签名私钥。候选树先被强制归一为目录 `0755`/普通文件 `0644`，只有审计白名单且与 Git `100755` 模式完全一致的入口可执行；上传包也使用同一白名单。两个 venv 都由 `root` 持有且不允许组写，只向 `wkm-license` 组开放读取/遍历/执行；pytest 由 `wkm-license` 非 root 运行，不继承 root 的主机修改能力。所有门禁通过后才写入候选配置、
执行 migration；migration ledger 会校验原生 SQL 的名称/SHA-256；pre-ledger 旧库只有在通过版本化
最低结构合同后，才记录显式 adoption 名称和合同 SHA-256。随后原子切换
`current`、重启服务并检查仅本机可访问的 `/readyz/deep`。当前版本的依赖不会被候选版本污染。

已有通过校验的 Nginx 生产配置时，升级过程不会先切换到 bootstrap。从写入候选 `service.env`
到公网 `/readyz` 和 `/v1/keys` 验收完成前，统一补偿事务覆盖 `current`、`service.env`、systemd 单元/服务状态、
Nginx 配置/服务状态以及相关 timer；默认 HTTP 会事务化停用本项目专属 Certbot timer，失败时恢复；
existing TLS 模式还捕获覆盖前的证书和私钥。任一检查失败都恢复部署前快照。
首次从已停用 `entityecho.conf` 接管公网 IP 时，仅可显式传入
`--retire-nginx-conf /etc/nginx/conf.d/entityecho.conf`。退役前会保存事务副本；成功后副本与记录位于
`/etc/wechat-memory-license/retired-nginx/retirement.*/`；失败时按字节恢复。原路径若在部署期间被重建，
回滚不会覆盖它，而是保留快照并停止自动 reload。普通升级不得重复传入退役参数。

风险：

- 补偿回滚不替代业务回归测试；回滚失败时脚本会明确报警，不会误报成功；
- SQLite migration 不在服务可能产生新写入后自动降级。恢复升级前备份必须先停写、确认数据丢失窗口并获得运维批准；
- `SIGKILL`/掉电无法执行 Shell EXIT 补偿；遗留的 `/etc/wechat-memory-license/.*transaction.*` 会阻断下次部署。必须先核对 `current`、`service.env`、systemd、Nginx、`retired-nginx` 和备份，不能仅删目录或重跑脚本；
- dnf 软件包、Let's Encrypt 已签发证书、失败候选 release 和未激活版本化私钥不在自动删除范围；
  本次部署新增的 firewalld/SELinux 规则会精确补偿，不重载整个防火墙，也不改写其他端口类型；
  existing 模式固定路径上的证书/私钥属于旧流量运行态，失败时会从事务快照恢复；
- 密钥轮换不属于普通升级，必须使用专题流程。

### 4.2 已成功上线后的人工代码回滚

先记录当前指向并查找上一版：

```bash
readlink -f /opt/wechat-memory-license/current
sudo ls -1dt /opt/wechat-memory-license/releases/* | head -n 5
```

仅在确认目标 release 与当前数据库 schema、`service.env` 和签名密钥兼容后，才原子切换：

```bash
read -r -p '请输入已确认的上一版 release 完整路径：' WKM_PREVIOUS_RELEASE
WKM_PREVIOUS_RELEASE="$(readlink -f -- "${WKM_PREVIOUS_RELEASE}")"
case "${WKM_PREVIOUS_RELEASE}" in
  /opt/wechat-memory-license/releases/*) ;;
  *) echo 'release 路径不在受控目录' >&2; exit 2 ;;
esac
if [[ ! -x "${WKM_PREVIOUS_RELEASE}/.venv/bin/python" ]]; then
  echo 'release 不存在或缺少独立 .venv' >&2; exit 2
fi
sudo ln -sfn "${WKM_PREVIOUS_RELEASE}" \
  /opt/wechat-memory-license/current.rollback
sudo mv -Tf /opt/wechat-memory-license/current.rollback \
  /opt/wechat-memory-license/current
sudo systemctl restart wechat-memory-license
curl --fail --silent --show-error http://127.0.0.1:9100/readyz/deep
```

若上一次发布包含密钥切换或不可逆数据库变更，不得只回滚软链接；必须同时恢复
匹配的 `service.env`、私钥选择和 SQLite 备份。

## 5. 用户和授权管理

统一入口：

```bash
sudo wkm-license-admin --help
```

常用命令：

```bash
# 免费版
sudo wkm-license-admin add --username demo-user --plan free

# 永久个人版
sudo wkm-license-admin add \
  --username alice --display-name "Alice" --plan permanent_personal

# 365 天专业订阅
sudo wkm-license-admin add \
  --username bob --plan pro_subscription --days 365

# 查询
sudo wkm-license-admin list
sudo wkm-license-admin show --username alice
sudo wkm-license-admin devices --username alice

# 套餐、限额和续期
sudo wkm-license-admin set-plan \
  --username alice --plan pro_subscription --days 365
sudo wkm-license-admin set-limits \
  --username alice --device-limit 2 --offline-days 30
sudo wkm-license-admin extend --username alice --days 90

# 暂停、恢复和重置激活码
sudo wkm-license-admin set-status --username alice --status suspended
sudo wkm-license-admin set-status --username alice --status active
sudo wkm-license-admin reset-code --username alice
```

新增或重置时的激活码只显示一次，不得进入 Shell 输出采集、CI、工单或普通文本文件。
首次成功激活后该码立即失效；同码并发只有一个请求可以成功。重新激活必须执行 `reset-code`
生成新码，管理员不得从数据库寻找或复用旧摘要。套餐允许多设备时，为第二台或第三台设备生成新码应使用
`reset-code --keep-sessions`；不带该参数会撤销已激活设备的刷新会话，它们下次联网刷新将失效。
降低设备上限且已有超额设备时，命令会拒绝；只有经确认才使用 `--deactivate-excess`。
所有完整命令与状态语义见 [一键部署与用户授权](ONE_CLICK_DEPLOY_AND_ADMIN.md)。

### 5.1 refresh session 保留维护

日常 timer 会在备份成功后自动执行。需要手工消化积压时使用相同的固定上限命令：

```bash
sudo wkm-license-admin --json prune-refresh-sessions \
  --retention-days 90 \
  --batch-size 500 \
  --max-batches 20
sudo -u wkm-license sqlite3 \
  /var/lib/wechat-memory-license/license.sqlite3 \
  'SELECT status,COUNT(*) FROM refresh_sessions GROUP BY status ORDER BY status;'
```

只删除终态时间早于截止点的 `rotated/revoked`。活跃、`expired`、近期终态和终态时间为空的行保留。
单次最多删除 10,000 条；若结果 `more_remaining=true`，分次重跑或等待下一日 timer，禁止把批次上限
改成无界扫描。每批都会写入不含 token 的 `refresh_sessions.pruned` 审计事件。
该命令绝不删除 `audit_events`。每日同时检查审计表行数、数据库大小和增长率；未来需要归档时，必须
先确定法律保留期、加密不可篡改存储、恢复查询和批准流程，再另行实现，不能复用本清理命令删证据。

## 6. 备份、恢复和恢复回退

### 6.1 日常备份验收

```bash
sudo systemctl list-timers wechat-memory-license-backup.timer --no-pager
sudo systemctl start wechat-memory-license-backup.service
sudo journalctl -u wechat-memory-license-backup.service -n 50 --no-pager
sudo ls -lh /var/lib/wechat-memory-license/backups
read -r -p '请输入要验证的备份文件名：' WKM_BACKUP_NAME
if [[ ! "${WKM_BACKUP_NAME}" =~ ^license-[0-9]{8}T[0-9]{6}Z(-[0-9]+)?\.sqlite3$ ]]; then
  echo '备份文件名格式不合法' >&2; exit 2
fi
sudo -u wkm-license sqlite3 \
  "/var/lib/wechat-memory-license/backups/${WKM_BACKUP_NAME}" \
  'PRAGMA integrity_check;'
```

备份脚本使用 SQLite `.backup`，只在完整性为 `ok` 后原子公布备份。默认保留 30 天。备份公布后，
同一 oneshot 才执行 refresh session 保留清理；清理失败会让该次 systemd 任务失败并进入告警，但不会
撤销已验证的备份。
默认清理参数为 90 天、500 条/批、20 批/次；如需调整，使用 systemd drop-in 覆盖
`REFRESH_SESSION_RETENTION_DAYS`、`REFRESH_SESSION_PRUNE_BATCH_SIZE` 和
`REFRESH_SESSION_PRUNE_MAX_BATCHES`，仍须遵守 CLI 的硬上限。
本机备份不是灾备；必须实施加密异地备份，且签名私钥使用独立的离线备份流程。

### 6.2 数据库恢复

恢复会中断授权 API，必须在维护窗口执行。先将备份路径改为已通过完整性检查的文件：

```bash
read -r -p '请输入要恢复的备份完整路径：' WKM_RESTORE_BACKUP
WKM_RESTORE_BACKUP="$(readlink -f -- "${WKM_RESTORE_BACKUP}")"
case "${WKM_RESTORE_BACKUP}" in
  /var/lib/wechat-memory-license/backups/license-*.sqlite3) ;;
  *) echo "备份路径不在受控目录" >&2; exit 2 ;;
esac
WKM_RESTORE_NAME="${WKM_RESTORE_BACKUP##*/}"
if [[ ! "${WKM_RESTORE_NAME}" =~ ^license-[0-9]{8}T[0-9]{6}Z(-[0-9]+)?\.sqlite3$ ]]; then
  echo '备份文件名格式不合法' >&2; exit 2
fi
sudo -u wkm-license sqlite3 "${WKM_RESTORE_BACKUP}" 'PRAGMA integrity_check;'
```

停服务，把当前数据库及 WAL/SHM 移到事故保留目录，而不是删除：

```bash
: "${WKM_RESTORE_BACKUP:?请先执行上一段备份路径验证}"
sudo systemctl stop wechat-memory-license
WKM_INCIDENT_DIR="/var/lib/wechat-memory-license/restore-before-$(date -u +%Y%m%dT%H%M%SZ)"
sudo install -d -o wkm-license -g wkm-license -m 0700 "${WKM_INCIDENT_DIR}"
for WKM_NAME in license.sqlite3 license.sqlite3-wal license.sqlite3-shm; do
  if sudo test -e "/var/lib/wechat-memory-license/${WKM_NAME}"; then
    sudo mv -- "/var/lib/wechat-memory-license/${WKM_NAME}" \
      "${WKM_INCIDENT_DIR}/${WKM_NAME}"
  fi
done
sudo -u wkm-license sqlite3 \
  /var/lib/wechat-memory-license/license.sqlite3 \
  ".restore '${WKM_RESTORE_BACKUP}'"
sudo chown wkm-license:wkm-license \
  /var/lib/wechat-memory-license/license.sqlite3
sudo chmod 0600 /var/lib/wechat-memory-license/license.sqlite3
sudo -u wkm-license sqlite3 \
  /var/lib/wechat-memory-license/license.sqlite3 \
  'PRAGMA integrity_check; PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;'
sudo systemctl start wechat-memory-license
curl --fail --silent --show-error http://127.0.0.1:9100/readyz/deep
```

再验证一个专用验收账号的激活或刷新流程。在验收完成前不得删除 `${WKM_INCIDENT_DIR}`。

若恢复后验收失败，立即停止服务，先把失败恢复产物移入新的保留目录，再将
`${WKM_INCIDENT_DIR}` 内的原始数据库、WAL 和 SHM 原样移回。不要把新旧 WAL 交叉组合。

## 7. 签名密钥轮换

轮换顺序必须是：

1. 为新 `key_id` 生成新私钥，发布公钥为 `retiring`；
2. 同步客户端信任根，发布同时信任新旧公钥的客户端；
3. 客户端覆盖足够后，显式 `--activate` 新密钥；
4. 使用新 key-id 和私钥部署服务端；
5. 等待旧票据、离线宽限和回滚窗口结束，才移除旧公钥。

严禁跳过第 2 步。完整命令、指纹验证、备份和事故处理见
[签名密钥运维](SIGNING_KEY_OPERATIONS.md)。

## 8. 排障矩阵

| 现象 | 首先检查 | 处置 | 风险/回滚 |
|---|---|---|---|
| 公网 9000 不通 | `systemctl status nginx`、云安全组、firewalld | 先查 Nginx HTTP 监听，不要开放 9100 绕过 | 9100 绕过代理会失去可信来源头与入口控制 |
| 本机 9100 不通 | `journalctl -u wechat-memory-license` | 检查 `service.env`、密钥/清单匹配、SQLite 权限 | 不要临时关闭验签 |
| `/readyz` 503 | SQLite 路径、权限、schema 版本 | 先用 loopback `/readyz/deep` 与独立 `integrity_check` 定界，必要时停写恢复 | 不要直接删 WAL |
| 部署拒绝工作树 | `git status --short` | 审核、提交或放弃本地变更 | 不用 `--skip-update` 绕过脏工作树检查 |
| 私钥不匹配 manifest | `signing-key verify` | 停止部署，恢复正确私钥或使用新 key-id 轮换 | 不覆盖同 key-id |
| 可选 TLS 证书续期失败 | `wechat-memory-certbot-renew.timer` 与 journal | 仅 letsencrypt 模式确认 80、ACME webroot 和 Certbot | 默认 HTTP 不启用此 timer |
| SQLite `locked/busy` 增多 | WAL 大小、磁盘、长事务 | 停止非必要管理写入，收集证据 | 不把 SQLite 放 NFS/SMB |
| 用户无法激活 | `show`、`devices`、权益状态和设备上限 | 重置激活码或解绑确认的设备 | 不从数据库寻找明文激活码 |

常用日志检查：

```bash
sudo journalctl -u wechat-memory-license --since '30 minutes ago' --no-pager
sudo tail -n 100 /var/log/nginx/wechat-memory-license.error.log
```

日志不得记录 request body、Authorization、refresh token、激活码、完整设备摘要或私钥。

## 9. 建议运维节奏

| 频率 | 检查 |
|---|---|
| 每 5 分钟 | 公网 HTTP `/healthz` 与 `/readyz`、HTTP 5xx/429 |
| 每日 | 备份 timer、最新备份年龄与大小、refresh session 清理结果/积压、audit_events 增长、磁盘、SQLite WAL |
| 每周 | 审计管理变更、失败激活异常、异地备份到达情况 |
| 每月 | 在隔离环境执行 SQLite 恢复演练 |
| 每季度 | 签名私钥备份恢复验证、代码回滚演练 |

## 10. 变更前最终检查

- 已记录当前 commit、release、`current` 目标和数据库备份；
- 已明确回滚是“仅代码”还是“代码 + 配置 + 数据库 + 密钥”；
- 已验证备份完整性，而不只是看到文件存在；
- 已保证私钥、激活码和 token 不会出现在命令输出采集中；
- 变更后将同时验证本机 API、公网 HTTP、SQLite 和一个专用验收账号。

代码发布前还必须执行：

```bash
python3.11 scripts/check_documentation.py
/opt/wechat-memory-license/current/.venv/bin/python -m unittest discover -s tests -v
bash -n deploy/el9/*.sh deploy/el9/wkm-license-admin
git diff --check
```

生产机首次安装尚不存在 `/opt/wechat-memory-license/current/.venv` 时，服务端单元测试应已在开发/发布机
的 `.venv` 中通过；目标机仍必须用 `python3.11 scripts/check_documentation.py` 执行无依赖文档门禁。

文档变更规则见 [文档随代码发布规则](DOCUMENTATION_POLICY.md)。
