# Enterprise Linux 9 手工部署：IP 短期证书

> 目标服务器：`104.224.90.172`  
> 公网入口：`https://104.224.90.172:9000`  
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

1. `104.224.90.172` 已直接绑定到这台服务器；
2. 上游没有把 80 或 9000 转发给别的主机；
3. 系统已升级到当前 EL9 小版本；
4. 服务器时间同步正常；
5. 已准备一个可接收证书异常提醒的运维邮箱；
6. 已准备异地加密备份位置和管理员固定来源 IP。

## 3. 安装基础包

以下命令以 root 或具备 sudo 权限的管理员执行：

```bash
sudo dnf upgrade -y
sudo dnf install -y nginx firewalld policycoreutils-python-utils sqlite \
  python3.11 python3.11-pip
sudo systemctl enable --now firewalld
```

Certbot 的 IP 证书 webroot 支持要求 Certbot 5.4 或更高版本。EL9 的系统 Python 3.9 不满足当前
Certbot 的 Python 要求，所以单独使用 Python 3.11 虚拟环境，并固定已验证版本：

```bash
sudo python3.11 -m venv /opt/certbot
sudo /opt/certbot/bin/python -m pip install --upgrade pip
sudo /opt/certbot/bin/pip install 'certbot==5.8.0'
/opt/certbot/bin/certbot --version
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
`/opt/wechat-memory-license/releases/2026.10.17.1`，验证后再原子切换 `current` 符号链接。不要覆盖正在
运行的版本目录，至少保留上一个可回滚版本。

## 5. 配置防火墙与 SELinux

```bash
sudo firewall-cmd --permanent --add-service=http
sudo firewall-cmd --permanent --add-port=9000/tcp
sudo firewall-cmd --reload
sudo semanage port -a -t http_port_t -p tcp 9000
sudo setsebool -P httpd_can_network_connect 1
sudo restorecon -RFv /var/www/wechat-memory-license-acme
```

若 `semanage port -a` 提示 9000 已定义，先运行
`sudo semanage port -l | grep -w 9000`。只有确认它未归属 `http_port_t` 时，才执行：

```bash
sudo semanage port -m -t http_port_t -p tcp 9000
```

不要关闭 SELinux，也不要开放 9100。

## 6. 第一次签发 IP 证书

### 6.1 启动仅含 ACME 的 Nginx

将仓库文件 `deploy/el9/nginx-bootstrap.conf` 复制到服务器：

```bash
sudo install -o root -g root -m 0644 deploy/el9/nginx-bootstrap.conf \
  /etc/nginx/conf.d/wechat-memory-license.conf
sudo nginx -t
sudo systemctl enable --now nginx
```

从服务器外部访问以下地址应返回 404，但必须能建立 HTTP 连接：

```text
http://104.224.90.172/.well-known/acme-challenge/probe
```

### 6.2 先测试 CA，再签发正式证书

把 `<运维邮箱>` 换成真实地址。先使用 staging，避免排错时触发正式环境限制：

```bash
sudo /opt/certbot/bin/certbot certonly --staging --non-interactive \
  --agree-tos --email '<运维邮箱>' \
  --preferred-profile shortlived \
  --webroot --webroot-path /var/www/wechat-memory-license-acme \
  --ip-address 104.224.90.172 --cert-name 104.224.90.172
```

staging 成功后删除 `--staging`，再次运行同一命令签发正式证书：

```bash
sudo /opt/certbot/bin/certbot certonly --non-interactive \
  --agree-tos --email '<运维邮箱>' \
  --preferred-profile shortlived \
  --webroot --webroot-path /var/www/wechat-memory-license-acme \
  --ip-address 104.224.90.172 --cert-name 104.224.90.172
```

不得把 staging 证书用于客户端。正式证书应出现在：

```text
/etc/letsencrypt/live/104.224.90.172/fullchain.pem
/etc/letsencrypt/live/104.224.90.172/privkey.pem
```

## 7. 启用 9000 HTTPS

用生产配置替换同一 Nginx 文件：

```bash
sudo install -o root -g root -m 0644 deploy/el9/nginx-production.conf \
  /etc/nginx/conf.d/wechat-memory-license.conf
sudo nginx -t
sudo systemctl reload nginx
```

如果授权 API 尚未启动，访问 9000 得到 502 是预期现象；证书和 TLS 握手仍可单独验证：

```bash
openssl s_client -connect 104.224.90.172:9000 \
  -servername 104.224.90.172 -verify_return_error </dev/null
```

不得在 Windows 客户端关闭证书验证，也不得为 IP 地址设置“忽略证书错误”。

## 8. 安装自动续期

```bash
sudo install -o root -g root -m 0755 deploy/el9/certbot-deploy-hook.sh \
  /usr/local/sbin/wkm-certbot-deploy
sudo install -o root -g root -m 0644 deploy/el9/certbot-renew.service \
  /etc/systemd/system/certbot-renew.service
sudo install -o root -g root -m 0644 deploy/el9/certbot-renew.timer \
  /etc/systemd/system/certbot-renew.timer
sudo systemctl daemon-reload
sudo systemctl enable --now certbot-renew.timer
sudo systemctl list-timers certbot-renew.timer
```

立即进行一次模拟续期：

```bash
sudo /opt/certbot/bin/certbot renew --dry-run \
  --deploy-hook /usr/local/sbin/wkm-certbot-deploy
```

日常检查：

```bash
sudo systemctl status certbot-renew.timer
sudo journalctl -u certbot-renew.service --since '24 hours ago'
sudo openssl x509 -in /etc/letsencrypt/live/104.224.90.172/cert.pem \
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

客户端 `0.3.2` 生成信任根中的生产公钥必须与服务器私钥一致。将开发端忽略目录中的
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

1. 创建 `wkm-license` 用户和受限目录；
2. 复制不可变 release，建立 Python 3.11 虚拟环境并安装 Waitress/Flask/cryptography；
3. 校验跟踪的公钥清单，把私钥安装到 `/etc/wechat-memory-license/signing-ed25519-2026-10-30.pem`；
4. 创建 `service.env`，写入公钥清单路径，初始化 SQLite schema v2 和套餐；
5. 安装 systemd unit，原子切换 `current`，启动后检查 `/readyz`；
6. 升级时使用 SQLite `.backup` 创建并校验完整备份。

私钥安装成功后，从 `/root` 清理临时上传副本前，先确认
`/etc/wechat-memory-license/signing-ed25519-2026-10-30.pem` 可用且权限为 `0640 root:wkm-license`。

### 9.3 验证进程和 SQLite

```bash
sudo systemctl status wechat-memory-license --no-pager
sudo journalctl -u wechat-memory-license -n 100 --no-pager
curl --fail --silent http://127.0.0.1:9100/healthz
curl --fail --silent http://127.0.0.1:9100/readyz
curl --fail --silent http://127.0.0.1:9100/v1/keys
sudo -u wkm-license sqlite3 /var/lib/wechat-memory-license/license.sqlite3 \
  'PRAGMA integrity_check; PRAGMA user_version;'
```

数据库最低兼容 SQLite 3.34.1，与 EL9 系统 SQLite 对齐。

### 9.4 创建首个客户端用户

```bash
sudo -u wkm-license /opt/wechat-memory-license/venv/bin/python \
  /opt/wechat-memory-license/current/register_user.py add \
  --db /var/lib/wechat-memory-license/license.sqlite3 \
  --username example-user --plan permanent_personal
```

激活码只显示一次。不得把终端输出写入工单、Shell 历史采集、普通日志或 CI 日志。

### 9.5 启用 Nginx 后做公网验证

```bash
curl --fail --silent --show-error https://104.224.90.172:9000/healthz
curl --fail --silent --show-error https://104.224.90.172:9000/readyz
curl --fail --silent --show-error https://104.224.90.172:9000/v1/keys
```

`/v1/keys` 返回的 `key_id` 应为 `2026-10-30`，`public_key` 应与客户端内置值完全相同。

## 10. 手工发布与回滚顺序

每次发布：

1. 在隔离构建环境生成带版本号的发布包并校验 SHA-256；
2. 上传到新的 `releases/<版本>`，所有者为 `root:root`，应用代码只读；
3. 用副本数据库运行迁移测试和完整性检查；
4. 备份当前 SQLite 数据库、WAL 和配置，并完成一次可读取验证；
5. 停止应用，执行受版本控制的迁移，切换 `current`；
6. 启动应用，检查本机 `127.0.0.1:9100/healthz`；
7. 检查公网 `https://104.224.90.172:9000/healthz`；
8. 失败时停止应用、恢复上一版代码和匹配的数据库备份，再次验证。

SQLite 备份必须使用 SQLite Online Backup API 或 `sqlite3 .backup`，不能在运行时直接复制单独的
`.sqlite3` 文件。备份要加密并异地保存，且定期实际恢复验证。

## 11. 上线验收清单

- [ ] 云安全组只开放受控 SSH、80、9000；
- [ ] 9000 是受信任正式证书，证书标识包含 `104.224.90.172`；
- [ ] 80 除 ACME challenge 外全部返回 404；
- [ ] 9100 从公网不可达；
- [ ] 自动续期 dry-run 成功，timer 已启用；
- [ ] SELinux 为 Enforcing，Nginx 和 API 无拒绝事件；
- [ ] SQLite 在本机磁盘，WAL/外键/完整性检查正常；
- [ ] 日志中没有激活码、令牌、聊天数据或数据库密钥；
- [ ] 客户端在正常、到期、离线宽限、设备超限时行为均符合套餐；
- [ ] 已做数据库恢复演练和上一版本回滚演练。

## 12. 尚未确定但不阻塞基础设施准备

- EL9 的具体发行版与小版本；
- SSH 管理来源 IP/VPN；
- 运维邮箱；
- 异地加密备份目标和保留周期；
- Ed25519 签名私钥使用的 KMS/HSM；
- 管理端 MFA 方案。

这些项目必须在正式对公网提供激活服务前确定。
