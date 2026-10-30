# Ed25519 签名密钥生成、发布、备份与轮换

> 本文的首要原则：`key_id` 一旦与公钥绑定，永不改绑。当前
> `2026-10-30` 私钥如果丢失，只能恢复加密离线备份；严禁使用相同 `key_id`
> 重新生成。

## 1. 信任模型

- 服务端私钥仅用于签署权益票据，不进入 Git、SQLite、日志、发布目录或普通备份；
- 仓库只跟踪 `config/signing-public-keys.json`，它是可审计的公开信任源；
- 客户端发行包内置公钥，不动态信任 `/v1/keys` 返回的任意公钥；
- 清单顶层固定为 `schema` 、`algorithm` 、`encoding` 、`active_key_id` 和
  `keys`；每条密钥固定为 `key_id` 、`public_key` 、`raw_sha256` 、
  `spki_sha256` 和 `status`；
- `active` 是当前签名密钥，`retiring` 只为轮换窗口保留验签能力；
- 清单同时保存 raw 公钥和 DER SPKI 的 SHA-256，消除编码歧义。

## 2. 当前生产密钥验证

`/root/wkm-signing-ed25519.pem` 不是 HTTPS 证书，而是授权票据的
Ed25519 签名私钥。当前匹配密钥由 `Ed25519PrivateKey.generate()` 使用操作系统
CSPRNG 生成，再序列化为未加密 PKCS#8 PEM。它的公钥是唯一派生值，不能从公钥
或 `key_id` 反推、重建同一私钥。`key_id=2026-10-30` 只是轮换标识，
不参与密钥数学生成。

生产服务使用的明文 PEM 之所以不再加密，是为了让受限的 systemd 进程能够无人值守
启动；它依靠 `root:wkm-license 0640` 、受限目录和 systemd 沙箱保护。
离线备份则必须另外加密。

在源码目录执行：

```bash
python -m license_server signing-key verify \
  --path /root/wkm-signing-ed25519.pem \
  --key-id 2026-10-30 \
  --manifest config/signing-public-keys.json
```

该命令只输出公开元数据。任意一项不匹配都会以非零退出，部署器也会在安装前执行同等校验。

如果私钥文件不存在，不要执行无清单约束的生成命令。应停止发布，从加密离线备份恢复，再次执行上述验证。

## 3. 新密钥的安全生成与发布

### 3.1 生成但不切换活动密钥

为新密钥选择从未使用过的 ID，例如 `2027-04-01`：

```bash
umask 077
python -m license_server signing-key ensure \
  --path /root/wkm-signing-ed25519-2027-04-01.pem \
  --key-id 2027-04-01 \
  --manifest config/signing-public-keys.json \
  --publish
```

行为保证：

1. 私钥路径不存在时才以 `0600` 原子创建；已存在则只加载；
2. 新公钥原子写入清单，默认状态为 `retiring`，不会悄然切换签名密钥；
3. 清单已有相同 `key_id` 且公钥不同时立即拒绝；
4. 清单已有相同绑定时幂等成功，不改写私钥或清单；
5. 清单已有该 ID 但私钥缺失时，`ensure` 无论是否带 `--publish` 都会拒绝重新生成。

查看变更并审核两个 SHA-256：

```bash
git diff -- config/signing-public-keys.json
python -m license_server signing-key manifest \
  --manifest config/signing-public-keys.json
```

### 3.2 先同步客户端信任根

在客户端仓库执行，将已审核的服务端清单复制为客户端信任源：

```bash
install -m 0644 \
  ../WeChatMemory-LicenseServer/config/signing-public-keys.json \
  security/license-public-keys.json
python scripts/sync_license_public_keys.py \
  --manifest ../WeChatMemory-LicenseServer/config/signing-public-keys.json
python scripts/sync_license_public_keys.py --check
python -m pytest tests/test_license_trust.py -q
git diff -- security/license-public-keys.json src/wechat_memory/license_trust.py
```

先发布并覆盖足够多的客户端。在此阶段，新旧公钥都已内置，但服务端仍使用旧私钥签名。

### 3.3 显式激活新密钥

客户端信任根发布完成后，才执行：

```bash
python -m license_server signing-key publish \
  --path /root/wkm-signing-ed25519-2027-04-01.pem \
  --key-id 2027-04-01 \
  --manifest config/signing-public-keys.json \
  --activate
```

该操作会把新密钥设为唯一 `active`，把之前的 `active` 改为 `retiring`。
审核、提交并推送公开清单后，部署新活动私钥：

```bash
sudo bash deploy/el9/deploy-from-data.sh \
  --key-id 2027-04-01 \
  --signing-key /root/wkm-signing-ed25519-2027-04-01.pem
```

部署器会从跟踪的清单取出预期公钥，反向校验私钥；它不包含第二份硬编码公钥。

### 3.4 移除旧公钥

必须同时满足以下条件：

- 所有由旧密钥签发的离线票据已超过最长有效期；
- 已发布并普及内置新公钥的客户端；
- 回滚窗口已结束，不再需要旧私钥签名。

然后才能从后续清单和客户端信任根移除旧公钥。旧私钥的销毁应有双人复核和审计记录。

## 4. 备份和恢复

SQLite 定时备份故意不包含签名私钥。对每一把生产私钥：

1. 使用受控离线介质保存至少两份加密备份，分开保管；
2. 备份标签只记录 `key_id`、创建时间和公钥指纹，不在普通系统中记录私钥内容；
3. 加密口令与备份介质分开保管；
4. 每季度在隔离环境演练恢复，恢复后只执行 `signing-key verify`；
5. 恢复到服务器后设置 `root:root 0600`，部署器安装到 `/etc` 后使用
   `root:wkm-license 0640`。

建议在离线介质上创建加密 PKCS#8 备份。下面的口令由 OpenSSL 交互读取；
不要把口令写入命令行、环境变量、脚本或 Shell 历史：

```bash
sudo openssl pkcs8 -topk8 \
  -in /root/wkm-signing-ed25519.pem \
  -out /media/offline/wkm-signing-2026-10-30.encrypted.pem \
  -v2 aes-256-cbc -v2prf hmacWithSHA256 -iter 600000
sudo chmod 0400 /media/offline/wkm-signing-2026-10-30.encrypted.pem
```

备份完成不等于可恢复。应在隔离环境解密到权限 `0600` 的临时文件，执行
`signing-key verify`，再立即销毁临时明文。只记录 `key_id`、raw/SPKI 指纹和演练时间，
不记录私钥内容。

恢复验收：

```bash
sudo chown root:root /root/wkm-signing-ed25519.pem
sudo chmod 0600 /root/wkm-signing-ed25519.pem
python -m license_server signing-key verify \
  --path /root/wkm-signing-ed25519.pem \
  --key-id 2026-10-30 \
  --manifest config/signing-public-keys.json
```

## 5. 故障处理

| 现象 | 原因 | 处置 |
|---|---|---|
| `private key does not match` | 私钥与清单不配对 | 停止部署，核对 `key_id`，从备份恢复 |
| `already bound to a different public key` | 重用了旧 `key_id` | 不要覆盖，为新密钥分配新 ID |
| `missing; restore its backup` | 清单已有记录但私钥缺失 | 只允许恢复，严禁重生成 |
| `manifest is being updated` | 存在并发操作或遗留锁 | 确认没有运行中进程后，人工检查提示的 `.lock` |
| 新客户端拒绝票据 | 客户端信任根未同步 | 立即回滚服务端活动密钥，重做“先客户端、后服务端”流程 |

## 6. 发布前检查

```bash
python -m license_server signing-key manifest \
  --manifest config/signing-public-keys.json
python -m unittest discover -s tests -v
bash -n deploy/el9/*.sh deploy/el9/wkm-license-admin
git diff --check
git status --short
```

只能提交公开清单、代码和文档。提交前必须确认 `git status` 中没有 PEM、`.secrets`、
`service.env`、SQLite 或私钥备份。
