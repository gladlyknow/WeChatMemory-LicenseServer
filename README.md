# WeChatMemory License Server

闭源商业版微信知识库的独立云端授权服务仓库。当前首个交付只包含 SQLite3 schema 和管理员新用户
注册脚本，不包含聊天记录处理代码，也不依赖 `WeChatMsg-4x`。

## 安全边界

- 云端不接收聊天正文、媒体、数据库密钥、联系人或搜索词；
- 用户不需要验证邮箱或手机号；
- 用户存在性不等于身份认证，激活时必须同时提供脚本生成的高熵激活码；
- 激活码只显示一次，SQLite3 只保存 SHA-256 摘要；
- 支付、退款和发票不在当前范围；
- Ed25519 签名私钥后续只能放入 KMS/HSM，不能写入 SQLite3。

本仓库是独立商业实现的起点。正式版权声明、EULA 和隐私政策须在权利链审计和法律复核后加入；
当前不从 GPL 原型仓库复制客户端实现。

## 初始化数据库

```bash
python register_user.py init --db ./data/license.sqlite3
```

数据库会自动使用 WAL、外键、`synchronous=FULL` 和 5 秒忙等待。文件在支持 POSIX 权限的平台上
设为 `0600`。

## 注册用户

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

## 当前套餐基线

| 套餐 | 设备 | 离线期限 | 基线能力 |
|---|---:|---:|---|
| `free` | 1 | 30 天 | 只读、手动同步、基础单会话导出 |
| `permanent_personal` | 2 | 180 天 | 永久使用已购版本、1 年免费升级、标准个人功能 |
| `pro_subscription` | 3 | 30 天 | 实时同步、多账号、组合批量导出、高级统计 |

套餐会以快照写入每条权益，后续修改套餐不会意外改变已经售出的历史权益。

