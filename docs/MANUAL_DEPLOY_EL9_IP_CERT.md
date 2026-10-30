# Enterprise Linux 9 手工部署：IP 短期证书

> 本文只适用于未来显式选择 `--tls-mode letsencrypt` 的可选路径。当前默认生产入口是
> `http://104.244.90.172:9000`，默认一键部署不需要本文的 Certbot、80 端口或证书步骤。

> **硬性兼容门禁：** Client `0.3.5` 只允许固定 HTTP origin，不兼容本文的 HTTPS 入口。必须先发布内置
> 匹配 HTTPS origin/信任策略的新客户端，再使用 `--ack-client-https-origin-ready` 启用 TLS。
> Client `0.3.4` 及更旧版虽固定 HTTPS，但也不能据此推断其 origin/信任根与本服务匹配。

> 本文用于理解和人工排查底层步骤。常规生产部署优先使用
> [`SERVER_OPERATIONS_RUNBOOK.md`](SERVER_OPERATIONS_RUNBOOK.md) 与
> [`ONE_CLICK_DEPLOY_AND_ADMIN.md`](ONE_CLICK_DEPLOY_AND_ADMIN.md)。

> 目标服务器：`104.244.90.172`
> 公网入口：`https://104.244.90.172:9000`
> 适用范围：RHEL 9、Rocky Linux 9、AlmaLinux 9、Oracle Linux 9、CentOS Stream 9  
> 状态：可执行的手工部署手册；授权 API 已实现，尚需在目标服务器执行本手册

如果希望自动完成依赖、防火墙、SELinux、证书、应用、备份和验收，直接使用
[`ONE_CLICK_DEPLOY_AND_ADMIN.md`](ONE_CLICK_DEPLOY_AND_ADMIN.md) 中的
`deploy/el9/one-click-deploy.sh`。本文保留逐步手工排错流程。

## 1. 已确定的部署基线

- 人工登录服务器部署，不使用 Docker、Kubernetes 或自动发布平台；
- Nginx 监听公网 `9000/tcp` 并终止 TLS；
- 授权应用只监听 `127.0.0.1:9100`；
- SQLite3 文件只位于服务器本机磁盘；
- 使用 Let's Encrypt 受信任的 IP 短期证书；
- `80/tcp` 只用于 ACME HTTP-01 首次验证和自动续期，其他路径统一返回 404；
- Certbot 每 4 小时检查续期，成功后校验并热加载 Nginx；
- 客户聊天正文、媒体、微信数据库密钥、联系人和搜索词不得上传。

IP 短期证书当前有效期为 160 小时（约 6 天 16 小时）。所以“人工部署”只指发布流程，证书续期
本身必须自动化；不能依赖人工每周更新。

## 2. 部署前检查

在云平台安全组和服务器防火墙同时放行：

| 端口 | 来源 | 用途 |
|---|---|---|
| TCP 22 | 管理员固定 IP 或 VPN | SSH 运维 |
| TCP 80 | 公网 | IP 证书 HTTP-01 验证与续期 |
| TCP 9000 | 公网 | HTTPS Web/API |
| TCP 9100 | 不开放 | 应用只绑定 loopback |

还必须确认：

1. `104.244.90.172` 已直接绑定到这台服务器；
2. 上游没有把 80 或 9000 转发给别的主机；
3. 系统已升级到当前 EL9 小版本；
4. 服务器时间同步正常；
5. 证书通知邮箱为 `gladlygrady@tutamail.com`；
6. 已准备异地加密备份位置和管理员固定来源 IP。

在任何安装、防火墙或 Nginx 变更前，先执行与一键部署相同的只读归属门禁：

```bash
PUBLIC_IP=104.244.90.172
source /data/WeChatMemory-LicenseServer/deploy/el9/public-ip-safety.sh
wkm_assert_public_ip_bound "${PUBLIC_IP}" 0
```

返回非零时必须停止。例如实际地址 `104.244.90.172` 和已退役的错误旧地址
`104.224.90.172` 只差一位，也必须拒绝；
不能通过改写现有 Nginx 业务配置来“适配”错误的 IP。如果该公网 IP 是云平台已人工验证的
1:1 NAT/EIP，不会出现在客户机 `scope global` 列表中，一键部署才可显式使用
`--allow-unbound-public-ip`。启用例外前必须先在云平台核对绑定/NAT，并从服务器外部验证 TCP/80；
出站 NAT 地址不能作为入站归属证明。

## 3. 安装基础包

以下命令以 root 或具备 sudo 权限的管理员执行：

```bash
sudo dnf upgrade -y
sudo dnf install -y nginx firewalld policycoreutils-python-utils sqlite \
  python3.11 python3.11-pip
```

同机已有业务时，不得为了部署而盲目启动 firewalld：启动它可能接管当前 nftables
策略。先执行 `systemctl is-active firewalld` 并确认现有入站策略。新机也应先在带序列台的
维护窗口启用，不要让部署脚本代替这个网络决策。

Certbot 的 IP 证书 webroot 支持要求 Certbot 5.4 或更高版本。EL9 的系统 Python 3.9 不满足当前
Certbot 的 Python 要求，所以单独使用 Python 3.11 虚拟环境，并固定已验证版本：

```bash
sudo python3.11 -m venv /opt/wechat-memory-license-certbot
sudo /opt/wechat-memory-license-certbot/bin/python -m pip install --upgrade pip
sudo /opt/wechat-memory-license-certbot/bin/pip install 'certbot==5.9.0'
/opt/wechat-memory-license-certbot/bin/certbot --version
```

如果系统找不到 `python3.11`，先把 EL9 升级到仍受支持的当前小版本并启用发行版官方 AppStream；
不要为生产服务器添加来源不明的 Python RPM 仓库。

## 4. 创建最小权限目录

```bash
sudo useradd --system --home-dir /var/lib/wechat-memory-license \
  --shell /sbin/nologin wkm-license
sudo install -d -o root -g root -m 0755 /opt/wechat-memory-license
sudo install -d -o root -g wkm-license -m 0750 /etc/wechat-memory-license
sudo install -d -o wkm-license -g wkm-license -m 0700 \
  /var/lib/wechat-memory-license /var/lib/wechat-memory-license/backups
sudo install -d -o wkm-license -g wkm-license -m 0750 /var/log/wechat-memory-license
sudo install -d -o nginx -g nginx -m 0755 /var/www/wechat-memory-license-acme
```

以后每次手工发布使用不可变版本目录，例如
`/opt/wechat-memory-license/releases/2026.10.17.1`。每个版本在自身 `.venv` 中安装依赖，验证后再原子切换
`current` 符号链接。不要覆盖正在运行的版本目录，至少保留上一个带完整 `.venv` 的可回滚版本。

## 5. 配置防火墙与 SELinux

```bash
PUBLIC_ZONE=public  # 必须替换为公网网卡实际所属 zone
sudo firewall-cmd --zone="${PUBLIC_ZONE}" --add-service=http
sudo firewall-cmd --zone="${PUBLIC_ZONE}" --permanent --add-service=http
sudo firewall-cmd --zone="${PUBLIC_ZONE}" --add-port=9000/tcp
sudo firewall-cmd --zone="${PUBLIC_ZONE}" --permanent --add-port=9000/tcp
sudo bash -Eeuo pipefail -c '
  PUBLIC_PORT=9000
  SELINUX_PORT_ADDED=0
  SELINUX_BOOLEAN_CHANGED=0
  source /data/WeChatMemory-LicenseServer/deploy/el9/selinux-port-safety.sh
  configure_selinux
'
```

不使用 `firewall-cmd --reload`，因为它会重建整个 firewalld 运行态，并可能清除其他业务的临时规则。
安全库会先读取 `getenforce`：返回 Disabled 时完全不调用
`semanage`/`getsebool`/`setsebool`；返回 Enforcing 或 Permissive 时，先读取完整
`semanage port -l` 输出并解析单端口和端口段。只有 9000/tcp 完全未分配时才执行
`semanage port -a`；已归属 `http_port_t` 时不重复写入，已归属其他任何类型时立即失败关闭。
不得用 `semanage port -m` 抢占，也不得在 `semanage port -a` 失败后把“正在修改”当作可接受的自动回退。

不要关闭 SELinux，也不要开放 9100。

## 6. 第一次签发 IP 证书

### 6.1 启动仅含 ACME 的 Nginx

将仓库文件 `deploy/el9/nginx-bootstrap.conf` 复制到服务器：

```bash
sudo install -o root -g root -m 0644 deploy/el9/nginx-bootstrap.conf \
  /etc/nginx/conf.d/wechat-memory-license.conf
sudo nginx -t
if sudo systemctl is-active --quiet nginx; then
  sudo systemctl reload nginx
else
  sudo systemctl enable --now nginx
fi
```

从服务器外部访问以下地址应返回 404，但必须能建立 HTTP 连接：

```text
http://104.244.90.172/.well-known/acme-challenge/probe
```

### 6.2 多业务服务器的无损 ACME 排错

如果 Certbot 报 `Fetching http://<IP>/.well-known/acme-challenge/...: Connection refused`，
请先停止重试签发。这表示 CA 还没有进入 webroot 文件校验阶段；应先确认公网 IP、
Nginx 80 端口和入站防火墙。若服务器同时托管其他站点，不得删除、覆盖或改写其他
`/etc/nginx/conf.d/*.conf`；Nginx 可以根据 `server_name` 使多个 80 端口虚拟主机并存。
唯一例外是业务负责人已确认整个站点停用，并通过一键部署的
`--retire-nginx-conf` 显式授权事务退役；不得用 `rm` 或手工改名替代该流程。

先执行只读核对：

```bash
PUBLIC_IP=104.244.90.172
PUBLIC_IF="$(ip -4 route show default | awk 'NR == 1 { print $5 }')"
PUBLIC_ZONE="$(firewall-cmd --get-zone-of-interface="${PUBLIC_IF}")"
if [ -z "${PUBLIC_ZONE}" ] || [ "${PUBLIC_ZONE}" = "no zone" ]; then
  PUBLIC_ZONE="$(firewall-cmd --get-default-zone)"
fi

printf 'default-interface=%s\nfirewalld-zone=%s\n' "${PUBLIC_IF}" "${PUBLIC_ZONE}"
ip -4 -brief address show scope global
ip -4 route get 1.1.1.1
curl -4 --fail --silent --show-error --max-time 10 https://api.ipify.org
printf '\n'

sudo nginx -t
sudo systemctl is-active nginx
sudo ss -ltnp '( sport = :80 )'
sudo nginx -T 2>&1 | grep -E 'configuration file|listen .*80|server_name'
sudo firewall-cmd --zone="${PUBLIC_ZONE}" --query-service=http
sudo firewall-cmd --zone="${PUBLIC_ZONE}" --list-all
```

`ip address` 显示的地址可能是云服务商的内网地址；`api.ipify.org` 只能说明出站 NAT
地址，也不能单独证明入站 IP 已绑定。最终以云平台实例的公网 IPv4/EIP 绑定为准。
本项目早期误用 `104.224.90.172`，实际本机地址为 `104.244.90.172`。旧地址已退役，
不得再用它调用 Certbot。当已停用的 `entityecho.conf` 也声明实际 IP 时，应优先使用
一键部署的显式
`--retire-nginx-conf /etc/nginx/conf.d/entityecho.conf` 事务退役流程，不要直接删除文件。
该流程会校验文件确实与目标 IP/9000 冲突，成功时保留可恢复的字节副本，
任一步失败时恢复原路径和 Nginx 运行态。

只有在 Nginx 配置通过、服务处于 `active`、`ss` 确认 80 端口监听后，才针对公网
网卡实际所属 zone 精确开放 HTTP；不需要 reload firewalld，也不改动其他规则：

```bash
sudo firewall-cmd --zone="${PUBLIC_ZONE}" --add-service=http
sudo firewall-cmd --zone="${PUBLIC_ZONE}" --permanent --add-service=http
sudo firewall-cmd --zone="${PUBLIC_ZONE}" --query-service=http
sudo firewall-cmd --zone="${PUBLIC_ZONE}" --permanent --query-service=http
```

再为 WeChatMemory 的独立 webroot 放置一个临时探针，不触碰其他站点：

```bash
sudo install -d -o nginx -g nginx -m 0755 \
  /var/www/wechat-memory-license-acme/.well-known/acme-challenge
printf '%s\n' 'wechat-memory-acme-ok' | sudo tee \
  /var/www/wechat-memory-license-acme/.well-known/acme-challenge/wkm-probe.txt >/dev/null
sudo chmod 0644 \
  /var/www/wechat-memory-license-acme/.well-known/acme-challenge/wkm-probe.txt
sudo restorecon -RF /var/www/wechat-memory-license-acme

curl --fail --show-error --max-time 5 \
  -H "Host: ${PUBLIC_IP}" \
  http://127.0.0.1/.well-known/acme-challenge/wkm-probe.txt
```

本机探针必须精确返回 `wechat-memory-acme-ok`。然后从服务器之外的电脑或移动网络执行：

```bash
curl --fail --show-error --max-time 10 \
  http://104.244.90.172/.well-known/acme-challenge/wkm-probe.txt
```

如果本机成功、外部仍是 `connection refused`，问题位于主机外部：核对云安全组/入站
ACL、EIP 绑定、NAT/端口转发和上游防火墙，只新增目标实例的 `TCP/80` 公网入站规则。
未完成外部探针前不得继续 Certbot，避免触发 CA 失败限额。完成证书签发后可删除
`wkm-probe.txt`，但必须保留 80 端口和 ACME location 以用于自动续期。

### 6.3 先测试 CA，再签发正式证书

先使用 staging，避免排错时触发正式环境限制：

```bash
sudo /opt/wechat-memory-license-certbot/bin/certbot certonly --staging --non-interactive \
  --agree-tos --email 'gladlygrady@tutamail.com' \
  --preferred-profile shortlived \
  --webroot --webroot-path /var/www/wechat-memory-license-acme \
  --ip-address 104.244.90.172 --cert-name 104.244.90.172-staging
```

staging 使用独立的 cert-name，避免 Certbot 把未到期的测试证书当成正式证书线继续复用。
测试成功后再签发正式证书：

```bash
sudo /opt/wechat-memory-license-certbot/bin/certbot certonly --non-interactive \
  --agree-tos --email 'gladlygrady@tutamail.com' \
  --preferred-profile shortlived \
  --webroot --webroot-path /var/www/wechat-memory-license-acme \
  --ip-address 104.244.90.172 --cert-name 104.244.90.172
```

正式证书已验证可用后，可以删除隔离的 staging 线继：

```bash
sudo /opt/wechat-memory-license-certbot/bin/certbot delete --non-interactive \
  --cert-name 104.244.90.172-staging
```

不得把 staging 证书用于客户端。正式证书应出现在：

```text
/etc/letsencrypt/live/104.244.90.172/fullchain.pem
/etc/letsencrypt/live/104.244.90.172/privkey.pem
```

## 7. 启用 9000 HTTPS

用生产配置替换同一 Nginx 文件：

```bash
sudo install -o root -g root -m 0644 deploy/el9/nginx-production-tls.conf \
  /etc/nginx/conf.d/wechat-memory-license.conf
sudo nginx -t
sudo systemctl reload nginx
```

如果授权 API 尚未启动，访问 9000 得到 502 是预期现象；证书和 TLS 握手仍可单独验证：

```bash
openssl s_client -connect 104.244.90.172:9000 \
  -servername 104.244.90.172 -verify_return_error </dev/null
```

不得在 Windows 客户端关闭证书验证，也不得为 IP 地址设置“忽略证书错误”。

## 8. 安装自动续期

```bash
sudo install -o root -g root -m 0755 deploy/el9/certbot-deploy-hook.sh \
  /usr/local/sbin/wkm-certbot-deploy
sudo install -o root -g root -m 0644 deploy/el9/wechat-memory-certbot-renew.service \
  /etc/systemd/system/wechat-memory-certbot-renew.service
sudo install -o root -g root -m 0644 deploy/el9/wechat-memory-certbot-renew.timer \
  /etc/systemd/system/wechat-memory-certbot-renew.timer
sudo systemctl daemon-reload
sudo systemctl enable --now wechat-memory-certbot-renew.timer
sudo systemctl list-timers wechat-memory-certbot-renew.timer
```

这些单元、hook 和 Certbot 虚拟环境全部使用 WeChatMemory 专属名称。它们不会覆盖其他站点的
`certbot-renew.service/timer` 或 `/opt/certbot`，并且续期命令通过 `--cert-name 104.244.90.172`
只处理本项目的证书线。

若旧版 WeChatMemory 曾安装通用名称的 `certbot-renew.timer`，不要直接删除或停用。先执行
`systemctl cat certbot-renew.service certbot-renew.timer`，再查明同机其他证书由哪个 timer 续期。只有
确认通用单元是旧 WeChatMemory 专用且其他证书已有独立续期机制后，才在单独维护窗口
迁移它。新部署器不会自动改写这些通用单元。

立即进行一次模拟续期：

```bash
sudo /opt/wechat-memory-license-certbot/bin/certbot renew --dry-run \
  --cert-name 104.244.90.172 \
  --deploy-hook /usr/local/sbin/wkm-certbot-deploy
```

日常检查：

```bash
sudo systemctl status wechat-memory-certbot-renew.timer
sudo journalctl -u wechat-memory-certbot-renew.service --since '24 hours ago'
sudo openssl x509 -in /etc/letsencrypt/live/104.244.90.172/cert.pem \
  -noout -issuer -subject -dates
```

监控系统必须在证书剩余 48 小时时报警。因为证书约 6 天就到期，连续两天续期失败已经是严重故障。

## 9. 部署授权应用和 SQLite3

### 9.1 从 GitHub 取得发布源码

在服务器上使用仅有该仓库读权限的部署凭据。闭源仓库不要使用个人全权 GitHub token：

```bash
sudo install -d -o root -g root -m 0755 /data
sudo git clone --depth 1 \
  https://github.com/gladlyknow/WeChatMemory-LicenseServer.git \
  /data/WeChatMemory-LicenseServer
cd /data/WeChatMemory-LicenseServer
git rev-parse --short HEAD
```

正式部署建议按已复核的 tag/提交号 checkout，不要在服务器盲目跟随不固定的开发分支。

### 9.2 首次部署生产签名私钥

客户端 `0.3.5` 生成信任根中的生产公钥必须与服务器私钥一致。将开发端忽略目录中的
`.secrets/signing-ed25519.pem` 通过受控通道上传为 `/root/wkm-signing-ed25519.pem`，不能经过 Git、聊天工具或工单传输。

在服务器核对权限：

```bash
sudo chown root:root /root/wkm-signing-ed25519.pem
sudo chmod 0600 /root/wkm-signing-ed25519.pem
```

首次执行一键安装：

```bash
sudo bash deploy/el9/install-release.sh \
  "$PWD" /root/wkm-signing-ed25519.pem 2026-10-30
```

脚本会：

1. 创建 `wkm-license` 用户和受限目录，复制不可变 release 源码并验证文档契约；
2. 升级时使用 SQLite `.backup` 创建并校验完整备份；
3. 拒绝含私钥标记的候选包，将候选树权限归一为目录 `0755`/普通文件 `0644`，仅恢复经审计且与 Git `100755` 精确一致的可执行白名单；在 release 内建立独立 Python 3.11 `.venv`，安装依赖、检查依赖闭包、对比精确 SPDX SBOM；venv 保持 `root:wkm-license`、`g+rX,o-rwx` 且组不可写，并以 `wkm-license` 非 root 账号通过服务端全测，同时完成严格编译、全部 EL9 Shell 语法与部署事务自测；
4. 校验跟踪的公钥清单，把私钥安装到 `/etc/wechat-memory-license/signing-ed25519-2026-10-30.pem`；
5. 快照当前应用运行态，写入候选 `service.env`，再初始化或迁移 SQLite schema 和套餐；
6. 安装 systemd unit，原子切换 `current`，启动后检查仅本机可访问的 `/readyz/deep`。

私钥安装成功后，从 `/root` 清理临时上传副本前，先确认
`/etc/wechat-memory-license/signing-ed25519-2026-10-30.pem` 可用且权限为 `0640 root:wkm-license`。

### 9.3 验证进程和 SQLite

```bash
sudo systemctl status wechat-memory-license --no-pager
sudo journalctl -u wechat-memory-license -n 100 --no-pager
curl --fail --silent http://127.0.0.1:9100/healthz
curl --fail --silent http://127.0.0.1:9100/readyz/deep
curl --fail --silent http://127.0.0.1:9100/v1/keys
sudo -u wkm-license sqlite3 /var/lib/wechat-memory-license/license.sqlite3 \
  'PRAGMA integrity_check; PRAGMA user_version;'
```

数据库最低兼容 SQLite 3.34.1，与 EL9 系统 SQLite 对齐。

### 9.4 创建首个客户端用户

```bash
sudo -u wkm-license /opt/wechat-memory-license/current/.venv/bin/python \
  /opt/wechat-memory-license/current/register_user.py add \
  --db /var/lib/wechat-memory-license/license.sqlite3 \
  --username example-user --plan permanent_personal
```

激活码只显示一次。不得把终端输出写入工单、Shell 历史采集、普通日志或 CI 日志。

### 9.5 启用 Nginx 后做公网验证

```bash
curl --fail --silent --show-error https://104.244.90.172:9000/healthz
curl --fail --silent --show-error https://104.244.90.172:9000/readyz
curl --fail --silent --show-error https://104.244.90.172:9000/v1/keys
```

`/v1/keys` 返回的 `key_id` 应为 `2026-10-30`，`public_key` 应与客户端内置值完全相同。

## 10. 手工发布与回滚顺序

每次发布：

1. 在隔离构建环境生成带版本号的发布包并校验 SHA-256；
2. 上传到新的 release 目录，在其自身 `.venv` 安装和验证候选依赖，不修改当前 release；
3. 用副本数据库运行迁移测试和完整性检查；
4. 使用 SQLite `.backup` 备份当前数据库，单独保留匹配配置，并完成一次完整性验证；
5. 执行受版本控制的 migration，原子切换 `current`，并重启服务；
6. 检查本机 `127.0.0.1:9100/readyz/deep`，通过后才切换 Nginx；
7. 检查公网 `https://104.244.90.172:9000/readyz` 和 `/v1/keys`；
8. 失败时恢复上一版 `current`、`service.env`、systemd 单元/服务状态与 Nginx 配置/服务状态；
9. 不自动降级已执行的 SQLite migration。只有确认没有新写入并且已进入维护窗口时，才按恢复流程手工使用升级前备份。

SQLite 备份必须使用 SQLite Online Backup API 或 `sqlite3 .backup`，不能在运行时直接复制单独的
`.sqlite3` 文件。备份要加密并异地保存，且定期实际恢复验证。

## 11. 上线验收清单

- [ ] 云安全组只开放受控 SSH、80、9000；
- [ ] 9000 是受信任正式证书，证书标识包含 `104.244.90.172`；
- [ ] 80 除 ACME challenge 外全部返回 404；
- [ ] 9100 从公网不可达；
- [ ] 自动续期 dry-run 成功，timer 已启用；
- [ ] SELinux 为 Enforcing，Nginx 和 API 无拒绝事件；
- [ ] SQLite 在本机磁盘，WAL/外键/完整性检查正常；
- [ ] 日志中没有激活码、令牌、聊天数据或数据库密钥；
- [ ] 客户端在正常、到期、离线宽限、设备超限时行为均符合套餐；
- [ ] 已做数据库恢复演练和上一版本回滚演练。

## 12. 上线前仍需由运维确定的组织项

- EL9 的具体发行版与小版本；
- SSH 管理来源 IP/VPN；
- 远程监控告警的接收与升级流程；
- 异地加密备份目标和保留周期；
- Ed25519 签名私钥使用的 KMS/HSM；
- 管理端 MFA 方案。

这些项目必须在正式对公网提供激活服务前确定。
