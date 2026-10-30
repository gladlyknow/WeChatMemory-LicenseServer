# 授权服务一键部署与用户授权运维手册

> 目标：Enterprise Linux 9，公网 `http://104.244.90.172:9000`
> 架构：Nginx HTTP → `127.0.0.1:9100` 授权 API → 本机 SQLite3
> 适用：首次安装、后续升级、新增用户、套餐调整、续期、停用和设备解绑
> 文档导航见 [`README.md`](README.md)，值班、完整恢复和回滚见
> [`SERVER_OPERATIONS_RUNBOOK.md`](SERVER_OPERATIONS_RUNBOOK.md)。

## 1. 一次性部署结构

```mermaid
flowchart LR
    Client["Windows / Ubuntu 客户端"] -->|"HTTP 9000"| Nginx["Nginx + 限流入口"]
    Nginx -->|"HTTP 127.0.0.1:9100"| API["Waitress 授权 API"]
    API --> DB["SQLite3 WAL"]
    API --> Key["Ed25519 签名私钥"]
    Admin["SSH 管理员"] --> CLI["wkm-license-admin"]
    CLI --> DB
    Timer["systemd 定时器"] --> Backup["SQLite Online Backup"]
    Backup --> DB
```

服务端只接收用户名、激活码摘要、匿名设备标识、客户端版本和授权状态。聊天正文、图片、视频、语音、
联系人、搜索词和微信数据库密钥不得上传。

> **客户端兼容门禁：** Client `0.3.5` 只允许当前固定 HTTP origin；Client `0.3.4` 及更旧版固定
> HTTPS，不能在 Server `0.2.6` 的 HTTP 入口刷新。已有用户必须协调发布 0.3.5/0.2.6，切换期依靠
> 尚在 `offline_until` 内的已签名票据过渡。尚无生产用户时不构成阻断。

## 2. 部署前必须准备

1. 服务器运行 RHEL 9、Rocky Linux 9、AlmaLinux 9、Oracle Linux 9 或 CentOS Stream 9；
2. 云安全组和服务器允许公网访问 `9000/tcp`；
3. `104.244.90.172` 直接绑定当前服务器；
4. 已通过 SSH 登录，当前账号具备 `sudo` 权限；
5. 已把与客户端公钥配对的 Ed25519 私钥安全上传到服务器，例如：

```text
/root/wkm-signing-ed25519.pem
```

该私钥不能进入 Git、聊天软件、工单、普通备份或 Shell 输出。首次源文件必须是非软链接普通文件，
且元数据精确为 `root:root 0600`。部署器会在安装软件包或修改主机前检查这些元数据，
不读取或输出 PEM 字节。必须先执行：

```bash
sudo chown root:root /root/wkm-signing-ed25519.pem
sudo chmod 0600 /root/wkm-signing-ed25519.pem
```

若匹配私钥尚在受控 Ubuntu 开发机的 `.secrets` 目录，可通过已验证的 SSH
通道分两步暂存和安装。下面命令不会在目标已存在时覆盖它：

```bash
# 只在保存匹配私钥的 Ubuntu 开发机上执行
scp WeChatMemory-LicenseServer/.secrets/signing-ed25519.pem \
  root@104.244.90.172:/root/.wkm-signing-ed25519.upload
ssh root@104.244.90.172 \
  'set -eu; umask 077; trap "rm -f /root/.wkm-signing-ed25519.upload" EXIT; \
   test ! -e /root/wkm-signing-ed25519.pem; \
   openssl pkey -in /root/.wkm-signing-ed25519.upload -check -noout; \
   install -o root -g root -m 0600 /root/.wkm-signing-ed25519.upload \
     /root/wkm-signing-ed25519.pem'
```

如果第二步因目标已存在而退出，不要删除或覆盖旧文件；先执行公钥指纹核对。
暂存 `.upload` 文件会由远程 `trap` 在成功或失败时清理。
首次校验通过后，发布器将它安装为
`/etc/wechat-memory-license/signing-ed25519-<key-id>.pem`，运行时元数据固定为
`root:wkm-license 0640`。升级会重新检查已安装文件，不会把首次源的 `0600` 规则误用到运行文件。
候选 release 内的验证命令随后只派生公钥并与跟踪的 manifest 精确比对，输出被丢弃；
任何元数据或公钥不匹配都会失败关闭。

默认 HTTP 部署不创建 ACME webroot、不开放 80 端口、不安装 Certbot venv，也不安装或启动证书
续期 timer。若未来显式使用 `--tls-mode letsencrypt`，IP 地址证书必须使用短期证书配置；Certbot
需要 5.4 或更高版本，本仓库默认固定 `5.9.0`。证书通知邮箱为
`gladlygrady@tutamail.com`，并须先完成服务器内外两侧的 ACME 探针。官方说明：

- <https://letsencrypt.org/2026/03/11/shorter-certs-certbot>
- <https://letsencrypt.org/ca/docs/profiles/>

一键部署器会在安装软件、改防火墙、写 Nginx 或调用 Certbot 之前，用
`ip -o -4 addr show scope global` 读取本机地址，并对 `--public-ip` 执行完全相等匹配。
例如本机是 `104.244.90.172`，而参数误写成已退役的旧地址
`104.224.90.172`，脚本会立即失败且不会修改主机。这是对输入错误和目标主机登录错误的强制
门禁，不能用出站 IP 查询代替。

只有云平台不把 1:1 NAT/EIP 显示为本机 `scope global` 地址时，才可在人工核对绑定关系、
NAT 转发和服务器外部 TCP/9000 探针后显式加入 `--allow-unbound-public-ip`。该开关会输出醒目警告，
不会自动证明 NAT/EIP 配置正确，也不得用来绕过拼写不一致。

## 3. 首次一键部署

### 3.1 获取服务器代码到 `/data`

在服务器 SSH 终端执行：

```bash
sudo install -d -o root -g root -m 0755 /data
sudo git clone https://github.com/gladlyknow/WeChatMemory-LicenseServer.git \
  /data/WeChatMemory-LicenseServer
cd /data/WeChatMemory-LicenseServer
git status --short
git rev-parse --short HEAD
```

私有仓库应使用只具备该仓库读取权限的 deploy key 或 fine-grained token，不要使用个人全权限 token。

`/data/WeChatMemory-LicenseServer` 是可审计的源码检出目录，不存放生产私钥、
SQLite 数据库或运行日志。实际发布版、状态和密钥仍分别位于 `/opt`、`/var/lib`和
`/etc`，避免 `git pull` 触及生产数据。

### 3.2 从 `/data` 执行一键部署（推荐）

```bash
cd /data/WeChatMemory-LicenseServer

sudo bash deploy/el9/deploy-from-data.sh \
  --signing-key /root/wkm-signing-ed25519.pem \
  --firewall-zone public
```

`public` 只是示例；必须使用公网网卡实际所属 zone。只有一个活动 zone 时可省略该参数；
检测到多个 zone 时脚本会失败关闭，不会把入站规则批量加到多个网络。

当前服务器的 `entityecho.conf` 已由管理员手工删除，因此上面的现行部署命令 **不带**
`--retire-nginx-conf`；当文件已不存在时绝对不要传该参数，否则预检会按设计失败关闭。
只有冲突文件仍存在，且已确认 **整个** `entityecho.conf`
不再承载任何业务时，才可把下面这一次性显式授权追加到首次命令：

```bash
sudo bash deploy/el9/deploy-from-data.sh \
  --signing-key /root/wkm-signing-ed25519.pem \
  --firewall-zone public \
  --retire-nginx-conf /etc/nginx/conf.d/entityecho.conf
```

脚本只接受 `/etc/nginx/conf.d` 下直接存在的普通 `.conf` 文件，拒绝软链接、
WeChatMemory 自身配置和未声明目标 IP/端口的无关站点。成功后原文件按字节保留到
`/etc/wechat-memory-license/retired-nginx/retirement.*/`，其中 `RETIREMENT.txt` 记录原路径、
SHA-256 和替代入口。部署任一步失败会在 Nginx reload 前恢复原文件和原运行态；
如果同期管理员在原路径重建了文件，回滚会失败关闭并保留两份证据，绝不覆盖并发更改。
首次成功后的普通升级不得再传该参数。

该入口默认执行 `origin/main` fast-forward，并固定：

- 源码目录：`/data/WeChatMemory-LicenseServer`；
- 可选 TLS 通知邮箱：`gladlygrady@tutamail.com`；
- 公网入口：`104.244.90.172:9000`；
- 传输模式：`http`；
- 分支：`main`；
- 只允许干净工作树。

如果已在 SSH 会话中人工审核并更新代码，可加 `--skip-update`。
仅对已按上文验证的 1:1 NAT/EIP，可再加 `--allow-unbound-public-ip`；
`deploy-from-data.sh` 会原样传给底层部署器。

### 3.3 直接执行底层部署器

默认 HTTP 的底层入口如下：

```bash
cd /data/WeChatMemory-LicenseServer

sudo bash deploy/el9/one-click-deploy.sh \
  --source "$PWD" \
  --signing-key /root/wkm-signing-ed25519.pem \
  --key-id 2026-10-30 \
  --public-ip 104.244.90.172 \
  --public-port 9000 \
  --firewall-zone public
```

脚本会自动完成：

1. 校验 EL9、IPv4、端口和源码，并在任何主机写操作之前确认公网 IP 精确存在于本机 `scope global` 列表，同时校验首次私钥源或已安装私钥的路径类型、owner 和 mode；候选 release 再反向计算公钥，确认它与仓库跟踪的公开清单一致；
2. 安装 Nginx、SQLite、Python 3.11、firewalld 和 SELinux 工具；默认不安装 Certbot；
3. 创建 `wkm-license` 无登录系统账号；
4. 在唯一的活动 firewalld zone 中只增加 `9000/tcp`（不全量 reload），配置 SELinux；SELinux 为 Disabled 时完全跳过 `semanage`/`getsebool`/`setsebool`，为 Enforcing 或 Permissive 时先只读整张端口类型表，确认端口未分配才新增；已属于非 `http_port_t` 类型则失败关闭；
5. 默认停用本项目历史遗留的专属 Certbot timer，并保留证书和 venv 供未来显式 TLS 迁移；不触碰其他业务或系统级 Certbot timer；
6. 创建不可变候选 release 源码目录；无论来自 Git 还是上传包，目录统一为 `0755`、普通文件统一为 `0644`，仅审计白名单中与 Git `100755` 完全一致的入口恢复 `0755`；目标缺失、出现符号链接或清单漂移均失败关闭；
7. 对旧库创建并校验 SQLite 一致备份；
8. 在候选 release 内扫描私钥标记，建立独立 `.venv`，验证依赖闭包与精确 SBOM，强制执行严格编译、服务端全测、全部 EL9 Shell 语法和部署事务自测；生产 venv 和一次性测试 venv 都保持 `root` owner，仅把 group 设为 `wkm-license` 并授予 `g+rX,o-rwx`，运行账号无写权；pytest 固定以无登录权限的 `wkm-license` 账号运行，测试替身即使失效也无权改写主机；校验签名私钥后再执行 SQLite init/migration；
9. 安装 systemd 单元和每日备份定时器，原子切换 `current`，启动授权服务并通过本机 `/readyz/deep`；
10. 切换 Nginx HTTP 生产配置，验证公网 `/readyz` 和签名公钥接口。

在同机多业务环境，脚本默认只拥有
`/etc/nginx/conf.d/wechat-memory-license.conf`：

- 不删除、改名或覆盖其他 `conf.d` 文件；唯一例外是操作员本次显式给出且通过
  路径、归属和冲突校验的 `--retire-nginx-conf`；
- 若目标文件不含项目 owner marker，也不符合旧版唯一特征，拒绝覆盖；
- 若其他配置已声明 `104.244.90.172` 或监听 `9000`，拒绝抢占；
- 写入前后会校验其他 Nginx 配置的摘要；部署期间它们发生变化就拒绝 reload；
- 默认 HTTP 不创建或检查 ACME 路由；只有显式 `letsencrypt` 模式才用本地 Host 头验证专属
  webroot；所有模式都先执行 `nginx -t` 再热加载。

`entityecho.conf` 在本次迁移前同时声明了 `104.244.90.172`，会被默认冲突门禁拒绝；
只能在业务已停用的前提下使用上述一次性退役参数。其他不抢占 IP/9000 的站点继续共存。
部署过程中不要同时修改其他 Nginx 站点；需要修改时先结束本次部署。

脚本不会删除已经存在的数据库。升级前会先通过 SQLite `.backup` 创建一致性备份。候选版本的
本机验收、Nginx 切换或公网验收失败时，脚本会恢复部署前的 `current`、`service.env`、
systemd 单元/服务状态和 Nginx 配置/服务状态；从旧 TLS 迁移到 HTTP 时，项目专属 Certbot timer
的文件、启停状态也在事务内；`--tls-mode existing` 还会按字节恢复覆盖前的
固定路径证书与私钥。但候选版本已经执行过的 SQLite migration 不会
在服务可能产生新写入后自动降级；这时日志会明确提示备份和人工恢复边界。
补偿回滚会只撤销本次新增的 firewalld 运行/永久规则、SELinux 端口和布尔值，不执行全局
`firewall-cmd --reload`，也不会改写已属于其他 SELinux 类型的端口。它不卸载系统软件包、删除 Let's Encrypt 已签发证书、
候选 release 或已安全安装但未激活的版本化签名私钥。这些不影响旧版运行态，保留用于取证或下次重试；
确认不再需要后再按明确路径人工清理。默认 HTTP 不产生新的证书或 Certbot 环境。进程被
`SIGKILL` 或主机掉电时 Shell 无法执行 EXIT 补偿，
事务快照会保留在 `/etc/wechat-memory-license/.one-click-transaction.*` 或 `.install-transaction.*`。下次部署会
失败关闭，拒绝覆盖这些恢复证据。必须按统一运维手册核对 `current`、`service.env`、systemd、
Nginx、`retired-nginx` 恢复副本和数据库备份，确认恢复完成后才能移走事务目录并重新开放流量。

可在隔离验收机上通过 `WKM_DEPLOY_ENABLE_FAULT_INJECTION=1` 和
`WKM_DEPLOY_TEST_FAULT=after_nginx_retirement|after_application_install|after_nginx_switch`
注入故障。生产日常部署不得设置这两个变量。

### 3.4 可选：使用已有受信任证书

当前 Client `0.3.5` 不兼容 HTTPS 入口，因此本节命令现在不得执行。只有先发布内置匹配 HTTPS
origin/信任策略的新客户端，并确认升级覆盖率后，才可对包含该公网 IP SAN 的正式证书执行：

```bash
sudo bash deploy/el9/one-click-deploy.sh \
  --source "$PWD" \
  --signing-key /root/wkm-signing-ed25519.pem \
  --key-id 2026-10-30 \
  --tls-mode existing \
  --ack-client-https-origin-ready \
  --tls-cert /root/tls/fullchain.pem \
  --tls-key /root/tls/privkey.pem
```

脚本会拒绝以下证书：

- 48 小时内过期；
- 不包含目标公网 IP SAN；
- 文件不存在或无法读取。

existing 证书会复制到 Nginx 固定路径。复制前脚本捕获旧证书和私钥；证书校验、应用安装、
Nginx 切换或公网验收任一步失败，都会在恢复旧 Nginx 配置并 reload 前先按字节恢复这两个文件。

### 3.5 可选：重新启用 Let's Encrypt

同样，只有已先发布支持匹配 HTTPS origin/信任策略的新客户端时才可执行；当前 Client `0.3.5`
不兼容。确认参数不是绕过开关，而是对客户端先行发布的运维记录：

```bash
sudo bash deploy/el9/deploy-from-data.sh \
  --tls-mode letsencrypt \
  --ack-client-https-origin-ready \
  --email gladlygrady@tutamail.com \
  --firewall-zone public
```

该模式才会开放 HTTP/80、创建 ACME webroot、安装项目专属 Certbot venv 和续期 timer。详细前置
探针、证书限制和排障见 [`MANUAL_DEPLOY_EL9_IP_CERT.md`](MANUAL_DEPLOY_EL9_IP_CERT.md)。外部探针未成功时禁止运行正式 Certbot。

## 4. 部署后验收

```bash
sudo systemctl status wechat-memory-license --no-pager
sudo systemctl status nginx --no-pager
sudo systemctl status wechat-memory-license-backup.timer --no-pager

curl --fail --silent --show-error http://127.0.0.1:9100/readyz/deep
curl --fail --silent --show-error http://104.244.90.172:9000/readyz
curl --fail --silent --show-error http://104.244.90.172:9000/v1/keys
```

检查监听范围：

```bash
sudo ss -lntp | grep -E ':(9000|9100)\b'
```

正确结果：

- `9000`：Nginx 公网 HTTP；
- `9100`：只能是 `127.0.0.1:9100`；
- SQLite 没有网络监听端口。

默认部署不应存在启用中的项目证书 timer：

```bash
sudo systemctl is-enabled wechat-memory-certbot-renew.timer 2>/dev/null || true
```

## 5. 后续升级

```bash
cd /data/WeChatMemory-LicenseServer
git pull --ff-only origin main
git rev-parse --short HEAD

sudo bash deploy/el9/deploy-from-data.sh
```

签名私钥已经安装后不需要再次传入。不要随意更换 `key-id` 或签名私钥；客户端必须提前内置
对应公钥，才能完成安全轮换。
完整的密钥创建、公开清单发布、客户端同步、备份和轮换步骤见
[`SIGNING_KEY_OPERATIONS.md`](SIGNING_KEY_OPERATIONS.md)。

## 6. 用户管理入口

部署完成后统一使用：

```bash
sudo wkm-license-admin --help
```

它会以 `wkm-license` 身份访问固定数据库：

```text
/var/lib/wechat-memory-license/license.sqlite3
```

没有开放公网管理 API，避免管理员密码、数据库写权限或批量用户信息暴露到网络。

### 6.1 新增免费用户

```bash
sudo wkm-license-admin add \
  --username demo-user \
  --display-name "演示用户" \
  --plan free
```

### 6.2 新增永久个人版用户

```bash
sudo wkm-license-admin add \
  --username alice \
  --display-name "Alice" \
  --plan permanent_personal
```

### 6.3 新增专业订阅用户

```bash
sudo wkm-license-admin add \
  --username bob \
  --plan pro_subscription \
  --days 365
```

新增或重置后显示的激活码只出现一次。请立即通过安全渠道交付，不要截图到公开群、写入工单、CI 日志
或普通文本文件。数据库只能保存激活码 SHA-256 摘要，服务端无法找回旧激活码。

### 6.4 查看用户

```bash
sudo wkm-license-admin list
sudo wkm-license-admin show --username alice
sudo wkm-license-admin devices --username alice
```

需要机器可读输出时，`--json` 必须放在子命令前：

```bash
sudo wkm-license-admin --json show --username alice
```

包含一次性激活码的 JSON 不应进入自动化日志。

## 7. 套餐、期限和额度调整

### 7.1 免费版升级为永久个人版

```bash
sudo wkm-license-admin set-plan \
  --username alice \
  --plan permanent_personal
```

### 7.2 更换为一年专业订阅

```bash
sudo wkm-license-admin set-plan \
  --username alice \
  --plan pro_subscription \
  --days 365
```

`set-plan --days` 表示从执行时刻重新计算订阅期限，不表示在旧期限上追加。追加期限应使用 `extend`。

### 7.3 延长订阅

```bash
sudo wkm-license-admin extend --username alice --days 90
```

如果旧授权仍未到期，从原到期日追加；如果已经到期，从当前时间追加，并恢复为 `active`。

### 7.4 单独调整设备数和离线期限

```bash
sudo wkm-license-admin set-limits \
  --username alice \
  --device-limit 5 \
  --offline-days 45
```

如果把设备上限降低到当前活跃设备数以下，命令默认失败，避免静默踢掉用户设备。确认按“保留最近活跃
设备”处理时显式执行：

```bash
sudo wkm-license-admin set-limits \
  --username alice \
  --device-limit 1 \
  --deactivate-excess
```

套餐、设备数、离线期限和功能集以权益快照保存，只影响指定用户，不会修改其他已售用户。

## 8. 暂停、到期、撤销和恢复

```bash
sudo wkm-license-admin set-status --username alice --status suspended
sudo wkm-license-admin set-status --username alice --status expired
sudo wkm-license-admin set-status --username alice --status revoked
sudo wkm-license-admin set-status --username alice --status active
```

状态含义：

| 状态 | 客户端行为 |
|---|---|
| `active` | 按套餐能力运行 |
| `suspended` | 下一次在线刷新后只读 |
| `expired` | 下一次在线刷新后只读 |
| `revoked` | 下一次在线刷新后只读，表示管理员撤销 |

过期订阅不能直接设为 `active`，必须先使用 `extend`。客户端已有的离线票据无法在完全离线时被远程
即时收回；这是离线授权的固有限制。高风险设备应同时执行设备封禁。

整账号停用和恢复：

```bash
sudo wkm-license-admin set-user-status --username alice --status disabled
sudo wkm-license-admin set-user-status --username alice --status active
```

## 9. 激活码重置

默认吊销旧激活码、生成新激活码，并撤销当前设备刷新会话：

```bash
sudo wkm-license-admin reset-code --username alice
```

用户需要使用新激活码重新激活。如果只是遗失激活码、确认现有设备可信，可保留设备会话：

```bash
sudo wkm-license-admin reset-code --username alice --keep-sessions
```

由于激活码首次成功使用后就失效，套餐允许多设备时，为第二台或第三台设备发码必须先执行
`reset-code --keep-sessions`。不带 `--keep-sessions` 的默认重置会撤销已有设备的 refresh session，它们在
下次刷新时会失效；这只适合账号凭据泄漏或明确要求所有设备重新激活的情况。

服务端也可能已提交“消费激活码 + 绑定设备 + 生成会话”事务，但 HTTP 响应在到达客户端前丢失。
此时同码重试必须返回 401，不能为了“幂等”重用旧码。管理员核对用户与设备后，执行
`reset-code --keep-sessions` 生成新码；只有在应撤销全部现有会话时才使用不带该参数的 `reset-code`。

审计日志只记录“发生了重置”和是否撤销会话，不记录新旧激活码明文。

从 Server `0.2.5` 或更旧版升级时，旧版可能已填写 `last_used_at` 却仍把凭据留为 `active`。
`0.2.6` 对这类历史已用码统一返回 401，不会在升级后再允许重放一次。需要新设备激活时，管理员必须
使用 `reset-code --keep-sessions` 生成新码；不得人工清空 `last_used_at` 或修改旧摘要。

## 10. 设备解绑和封禁

先查询设备 ID：

```bash
sudo wkm-license-admin devices --username alice
```

解绑设备：

```bash
read -r -p '请输入要解绑的完整设备 ID：' WKM_DEVICE_ID
if [[ ! "${WKM_DEVICE_ID}" =~ ^[0-9A-Fa-f-]{36}$ ]]; then
  echo '设备 ID 格式不合法' >&2; exit 2
fi
sudo wkm-license-admin device-status \
  --username alice \
  --device-id "${WKM_DEVICE_ID}" \
  --status deactivated
```

封禁疑似被盗设备：

```bash
read -r -p '请输入要封禁的完整设备 ID：' WKM_DEVICE_ID
if [[ ! "${WKM_DEVICE_ID}" =~ ^[0-9A-Fa-f-]{36}$ ]]; then
  echo '设备 ID 格式不合法' >&2; exit 2
fi
sudo wkm-license-admin device-status \
  --username alice \
  --device-id "${WKM_DEVICE_ID}" \
  --status blocked
```

恢复设备：

```bash
read -r -p '请输入要恢复的完整设备 ID：' WKM_DEVICE_ID
if [[ ! "${WKM_DEVICE_ID}" =~ ^[0-9A-Fa-f-]{36}$ ]]; then
  echo '设备 ID 格式不合法' >&2; exit 2
fi
sudo wkm-license-admin device-status \
  --username alice \
  --device-id "${WKM_DEVICE_ID}" \
  --status active
```

解绑或封禁会立即撤销该设备当前 refresh token。普通激活请求不能解除 `blocked`；只能由管理员显式
恢复。`deactivated` 设备重新激活和管理员恢复都必须重新检查设备数量上限。

## 11. refresh session 保留清理

旧 refresh token 的摘要需要短期保留以拒绝重放和支持审计，但 `rotated/revoked` 行不能无限增长。
默认保留 90 天，手工命令如下：

```bash
sudo wkm-license-admin --json prune-refresh-sessions \
  --retention-days 90 \
  --batch-size 500 \
  --max-batches 20
```

命令只删除超过保留期且终态时间有效的 `rotated/revoked`；不会删除 `active`、`expired`、近期终态
或时间戳缺失的行。每批是独立短事务，固定最多 500 条；一次最多 100 批，默认 20 批。返回
`more_remaining=true` 时分次重跑，不要绕过上限。

每日备份 timer 会在一致备份完成并校验后自动执行默认清理。可通过
`wechat-memory-license-backup.service` 的 systemd drop-in 设置：

- `REFRESH_SESSION_RETENTION_DAYS`；
- `REFRESH_SESSION_PRUNE_BATCH_SIZE`；
- `REFRESH_SESSION_PRUNE_MAX_BATCHES`。

建议先保留默认值，观察一周各状态行数和清理日志后再调整。
清理不会删除 `audit_events`；审计证据的保留与归档是独立合规工作，当前版本仅监控容量，不自动删除。

## 12. 数据库备份和恢复

每日 UTC 03:15 自动执行 SQLite `.backup`，完整性通过后才执行上述终态会话清理：

```bash
sudo systemctl list-timers wechat-memory-license-backup.timer
sudo systemctl start wechat-memory-license-backup.service
sudo journalctl -u wechat-memory-license-backup.service -n 50 --no-pager
sudo ls -l /var/lib/wechat-memory-license/backups
```

本机备份默认保留 30 天。本机备份不能替代加密异地备份；应把备份文件加密后同步到独立存储，并定期
实际恢复演练。

恢复前必须确认备份完整，并停止写入：

```bash
read -r -p '请输入要验证的备份文件名：' WKM_BACKUP_NAME
if [[ ! "${WKM_BACKUP_NAME}" =~ ^license-[0-9]{8}T[0-9]{6}Z(-[0-9]+)?\.sqlite3$ ]]; then
  echo '备份文件名格式不合法' >&2; exit 2
fi
sudo -u wkm-license sqlite3 \
  "/var/lib/wechat-memory-license/backups/${WKM_BACKUP_NAME}" \
  'PRAGMA integrity_check;'
```

恢复属于高风险操作，应先保留当前数据库，再使用 SQLite `.restore`，完成后重新检查完整性、文件权限
和仅本机可访问的 `/readyz/deep`。不要在服务运行时直接复制单独的 `.sqlite3` 文件，因为 WAL 中可能仍有未合并数据。
可执行的停服务恢复、原数据保留和恢复失败回退步骤见
[`SERVER_OPERATIONS_RUNBOOK.md`](SERVER_OPERATIONS_RUNBOOK.md#62-数据库恢复)。

## 13. 日志和故障排查

授权服务日志：

```bash
sudo journalctl -u wechat-memory-license -n 200 --no-pager
sudo journalctl -u wechat-memory-license --since '30 minutes ago'
```

Nginx 日志：

```bash
sudo tail -n 100 /var/log/nginx/wechat-memory-license.error.log
sudo tail -n 100 /var/log/nginx/wechat-memory-license.access.log
```

可选 letsencrypt 模式的证书续期：

```bash
sudo systemctl status wechat-memory-certbot-renew.timer --no-pager
sudo journalctl -u wechat-memory-certbot-renew.service --since '24 hours ago'
sudo /opt/wechat-memory-license-certbot/bin/certbot renew --dry-run \
  --cert-name 104.244.90.172 \
  --deploy-hook /usr/local/sbin/wkm-certbot-deploy
```

SQLite：

```bash
sudo -u wkm-license sqlite3 /var/lib/wechat-memory-license/license.sqlite3 \
  'PRAGMA integrity_check; PRAGMA journal_mode; PRAGMA foreign_keys;'
```

## 14. 重要目录

| 路径 | 用途 |
|---|---|
| `/opt/wechat-memory-license/releases/` | 不可变发布版本；每个 release 自带独立 `.venv` |
| `/opt/wechat-memory-license/current` | 当前版本符号链接 |
| `/opt/wechat-memory-license/current/.venv` | 当前 release 的 Python 环境；不与其他 release 共享 |
| `/etc/wechat-memory-license/service.env` | 服务配置 |
| `/etc/wechat-memory-license/signing-ed25519-<key-id>.pem` | 按 key-id 版本化的 Ed25519 私钥 |
| `/var/lib/wechat-memory-license/license.sqlite3` | 授权数据库 |
| `/var/lib/wechat-memory-license/backups/` | 本机一致性备份 |
| `/usr/local/sbin/wkm-license-admin` | 用户管理入口 |
| `/var/log/nginx/wechat-memory-license.*` | 公网代理日志 |

## 15. 上线安全检查

- [ ] 9100 未对公网开放；
- [ ] 9000 为 Nginx HTTP，9100 只监听 loopback；
- [ ] 默认 HTTP 没有启用项目 Certbot timer；若显式 TLS，则证书和续期另行验收；
- [ ] SQLite 仅 `wkm-license` 可写；
- [ ] 签名私钥不在 Git 和普通备份中；
- [ ] 日志中没有激活码、refresh token 或数据库密钥；
- [ ] 每日备份可用且存在加密异地副本；
- [ ] 免费、永久、订阅、过期、暂停、设备超限均完成真机验收；
- [ ] 客户端内置公钥与 `/v1/keys` 完全一致。
