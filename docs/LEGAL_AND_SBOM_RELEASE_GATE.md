# 商业发布法务与 SBOM 门禁

> 状态：工程门禁已实现；正式法律审查尚未由本仓库声明完成。

本文规定许可证、第三方来源清单和软件物料清单（SBOM）的生成、复核与发布阻断规则。
自动化只能证明“发布物与已评审基线一致”，不能替代律师、隐私负责人或商业合同审批。

## 1. 发布物与事实来源

| 项目 | 事实来源 | 含义 |
|---|---|---|
| 服务端自有代码许可基线 | [`../LICENSE`](../LICENSE) | proprietary 工程基线；不代表已完成属地法律审批 |
| 第三方人工摘要 | [`../THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md) | 当前依赖家族、用途、人工复核结果和范围说明 |
| 精确依赖证据 | [`../sbom/server-runtime.spdx.json`](../sbom/server-runtime.spdx.json) | 候选环境中实际安装的直接/传递依赖、精确版本、关系和原始许可证元数据 |
| SBOM 生成器 | [`../scripts/generate_sbom.py`](../scripts/generate_sbom.py) | 只读取本机安装元数据，不访问网络 |
| 文档/发布静态门禁 | [`../scripts/check_documentation.py`](../scripts/check_documentation.py) | 检查上述文件存在、链接和 SPDX 基本结构 |

## 2. 为什么不能只看 `pyproject.toml`

`pyproject.toml` 中的版本范围描述兼容范围，不是生产发布清单。同一范围在不同日期可能安装不同版本，
传递依赖和许可证元数据也会变化。`requirements.lock` 因此先约束构建、运行时和测试依赖的精确版本；
每个候选 release 再在自己的 `.venv` 完成安装，并与仓库已评审 SBOM 基线比较。两个门禁必须同时
更新和通过，以下任一变化都应失败关闭：

- 直接或传递依赖新增、删除、改名或版本改变；
- 已安装包的依赖关系改变；
- `License-Expression`、`License`、许可证分类器或 `License-File` 元数据改变；
- 运行时声明已满足，但对应 distribution 不存在或版本不满足约束；
- SPDX 文档缺少应用包、`DESCRIBES`/`DEPENDS_ON` 关系或合法命名空间。

当前锁文件是精确版本约束，不是带 SHA-256 的离线 wheel 清单。更高保障环境应继续建设经审批的
wheelhouse、哈希清单和独立漏洞扫描；在那之前不得把“精确版本 + SBOM”描述成完整供应链签名。

## 3. 离线生成和校验

生成器不下载包、不调用外部 API，只读取当前 Python 环境的 `importlib.metadata`。在候选 release 的
独立虚拟环境中运行：

```bash
cd /data/WeChatMemory-LicenseServer
WKM_RELEASE="$(readlink -e /opt/wechat-memory-license/current)"
test -n "${WKM_RELEASE}"
SOURCE_DATE_EPOCH="$(git log -1 --format=%ct HEAD)" \
  "${WKM_RELEASE}/.venv/bin/python" \
  scripts/generate_sbom.py --output sbom/server-runtime.spdx.json
```

`SOURCE_DATE_EPOCH` 必须使用该 release 的 Git commit 时间戳，保证相同源码和环境生成逐字节相同的
SPDX JSON；若未设置，生成器使用 SPDX 可重复构建哨兵时间 `1970-01-01T00:00:00Z`，不得把该默认值
当成真实发布日期。

候选环境与已评审 SBOM 的发布校验：

```bash
"${WKM_RELEASE}/.venv/bin/python" \
  scripts/generate_sbom.py --check sbom/server-runtime.spdx.json
```

`--check` 会复用基线中的创建时间，重建完整文档并做规范化字节比较。任何输出差异均返回非零退出码。
仅检查文件结构而不读取环境时使用：

```bash
python3.11 scripts/generate_sbom.py \
  --validate sbom/server-runtime.spdx.json
python3.11 scripts/check_documentation.py
```

## 4. 依赖升级流程

1. 在隔离候选环境中安装拟发布源码；不要直接修改正在服务的 release 环境；
2. 用该环境生成新的 SPDX JSON，保存旧、新文档差异；
3. 对每个新增或变化的包检查上游许可证全文、NOTICE、再分发要求、专利/出口控制和安全公告；
4. 同步更新 `THIRD_PARTY_NOTICES.md` 的精确版本和结论；
5. 完成单元测试、文档检查、SBOM 环境比对和部署回滚测试；
6. 由指定的工程、法务/合规和发布负责人签字后，才允许将新 SBOM 作为基线合并；
7. 生产发布记录保存 commit、SPDX SHA-256、候选 release 路径和审批单编号。

禁止为了通过门禁直接在生产机修改 SBOM。基线变更必须回到源码仓库评审。

## 5. 正式商用前的人工门禁

以下项目没有办法由单元测试替代，任一未完成即不得标记“正式商用法律批准”：

- [ ] 确认 WeChatMemory 名称、代码、界面、文档和所有素材的权利链；
- [ ] 属地律师审核 proprietary LICENSE、客户 EULA、隐私政策、服务条款和责任限制；
- [ ] 确认账号、设备摘要、IP/安全日志的合法基础、保留期限、删除与数据主体请求流程；
- [ ] 核对全部第三方许可证全文、NOTICE、源代码/归属披露义务和商用兼容性；
- [ ] 对 TLS、密码学、软件出口、制裁名单和目标销售区域做适用性判断；
- [ ] 审核微信数据读取、数据库解密和终端用户授权范围，确认目标地区合规；
- [ ] 指定漏洞披露、安全更新、依赖升级和 SBOM 对外交付责任人；
- [ ] 记录批准人、日期、适用版本、适用地区和审批单编号。

建议发布记录采用以下字段：

| 字段 | 必填内容 |
|---|---|
| Source commit | 40 位 commit SHA |
| Server version | `pyproject.toml` 精确版本 |
| SBOM digest | `sha256sum sbom/server-runtime.spdx.json` |
| Engineering approver | 姓名、日期、证据链接 |
| Legal/compliance approver | 姓名、日期、地区、审批单编号 |
| Deployment approver | 姓名、日期、目标环境 |

没有人类法务/合规签字时，自动化状态只能写“工程候选通过”，不能写“法律审核通过”。
