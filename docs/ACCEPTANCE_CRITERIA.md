# WeChatMemory 授权服务审计与验收标准

> 基线：服务端 `0.2.7`、SQLite schema 3、授权协议 v1；审计日期 2026-11-02。
> 客户端数据不变量、真机矩阵和导出门禁见 `WeChatMemory-Client/docs/ACCEPTANCE_CRITERIA.md`。

## 1. 当前结论

服务端已实现高熵激活码、设备上限、refresh token 轮换、Ed25519 票据、本机管理 CLI、
SQLite 一致备份、顺序 migration 和 EL9 部署素材。本次审计未证实新 P0。专业套餐 capability、
浅/深健康检查、有界限流、独立 release venv、跨应用/Nginx 补偿回滚、版本化 migration、
许可证/SBOM 和候选前自动门禁都已形成源码候选；正式商业发布前仍有以下 P1：

- `multi_account` 的产品限制语义尚未冻结和落地；
- 隔离 EL9 上的部署故障注入、最终公网 HTTP 与授权金丝雀尚无 E4 证据；
- 只有本机 SQLite 备份，没有加密异地副本、新鲜度告警和隔离恢复演练；
- proprietary LICENSE 是工程基线，仍缺权利人与法律/隐私负责人的人工签字。

因此当前只能标记为“源码候选”，不能标记为“生产验证”。

## 2. 等级与授权不变量

| 等级 | 定义 | 发布处理 |
|---|---|---|
| P0 | 私钥/激活码/refresh token 泄漏，伪造票据可用，授权跨用户/设备错绑，生产库不可恢复破坏 | 立即阻断；已上线则进入事故响应 |
| P1 | 套餐权限错误、公网可用性风险、部署/迁移/备份无恢复证据、法务发布材料缺失 | 正式发布前必须清零 |
| P2 | 可观测性、文档精度、硬化和维护性差距 | 记录责任人与目标版本 |

必须始终成立的授权不变量：

1. 首次激活同时匹配用户和高熵激活码；用户不存在与码错误返回相同公开错误。
2. 库中只存带 namespace 的激活码和 refresh token 摘要；激活码明文只在创建响应中出现一次，
   首次成功激活必须在同一事务内原子吊销；后续及并发重放必须统一返回 401。
   旧版遗留的 `status='active' AND last_used_at IS NOT NULL` 凭据也必须失败关闭，不得在升级后额外成功一次。
3. refresh token 同时绑定 install ID、device hash、活跃设备和未过期 session；旧 token 重放必须失败。
4. 票据为 Ed25519 签名的规范 JSON，并必含来自已验证 `devices` 行的 `device_id`、`install_id`、
   `device_hash`；任一签名、`key_id`、issuer/audience、设备绑定或时间顺序错误都不解锁。
5. 客户端只信任内置公钥；`/v1/keys` 只是诊断信息，不能在运行时扩大信任根。
6. 已验签但到期/暂停/撤销的权益只签发 `read_only`；无效签名不得因为历史激活而解锁。
7. 设备解绑/封禁和默认的激活码重置必须撤销相关 refresh sessions。
8. refresh session 保留清理只能删除超过保留期且终态时间有效的 `rotated/revoked`；`active` 永不删除，单次工作量必须有硬上限；`audit_events` 不属于该清理范围。
9. 客户端 `platform`/`app_version` 和管理员创建的身份文本必须拒绝非可打印字符（包括 C0/C1、ANSI ESC 和 Unicode 双向控制）；管理 CLI 对历史脏数据必须输出可见 `\\uXXXX` 转义，不得把原始控制字符写入 root 终端。

## 3. 套餐 capability 合同

每个套餐必须从服务端签发票据开始，实际调用客户端执行点，同时验证“允许的成功”和“未授权的拒绝”。

| capability | 客户端执行点 |
|---|---|
| `read_only` | 打开已有档案 |
| `manual_sync` / `realtime_sync` | 手动/自动同步 |
| `single_session_export` | 无筛选单会话导出 |
| `classified_export` | 带筛选的单会话导出 |
| `batch_export` | 账号/多会话导出 |
| `standard_analytics` / `advanced_analytics` | 对应统计 API 和界面 |
| `multi_account` | 归档、显示、切换和导出多账号 |
| `settings_write` | 修改数据源、同步频率和主题 |

## 4. 数据库、备份与密钥轮换

- 每个 schema 版本都必须有顺序 migration，不允许只重跑最新 `CREATE TABLE IF NOT EXISTS` 并改版本号。
- 原生执行的 migration 必须记录名称和 SQL 文件 SHA-256；同版本文件发生漂移时失败关闭。
  对 migration ledger 出现前的旧库，必须先验证该版本不可变的最低结构合同，再以
  `legacy-schema-vN-adopted` 和合同 SHA-256 明示采用；不得伪称当前 SQL 文件曾执行。
- 每个受支持旧版到当前版都有真实历史 schema fixture 测试，验证行数、外键、套餐快照、设备和
  刷新会话不变；版本号正确但最低结构不完整的库必须失败关闭。
- migration 前创建并校验 SQLite 一致备份；失败回滚全批。
- schema v3 的终态时间部分索引必须存在；日常任务先完成一致备份，再按固定批次清理旧 `rotated/revoked`，不得进入 HTTP 请求路径。
- 本机每日备份不等于灾备；必须有加密异地副本、新鲜度告警和隔离恢复演练。
- 签名私钥不进入 Git、SQLite、日志、release 或普通备份；使用分开的加密离线备份。

密钥轮换必须按顺序进行：生成全新 `key_id` → 新公钥以非 active 状态公布 → 同步客户端信任根 → 先发布信任新旧 key 的客户端 → 等待覆盖率和离线窗口 → 服务端切换 active 私钥 → 旧票据过期和回滚窗口结束后才移除旧 key。同一 `key_id` 永不改绑。

## 5. 部署、回滚和公网可用性

部署必须按以下顺序，任一项失败都进入回滚：

1. 干净 `main`、精确 commit、两仓库公钥清单字节相等；
2. 文档检查、服务端全测试、Shell 检查、依赖与私钥扫描；
3. 构建不可变 release 和独立 venv；
4. 校验私钥↔公钥，创建并校验数据库备份；
5. 在候选 release 中跑 migration 和自检；
6. 原子切换应用、venv 和配置，重启；
7. 检查本机 health/ready/keys，再切换 Nginx；
8. 检查公网 HTTP ready/keys，再用专用金丝雀完成 activate → 激活码重放拒绝 → refresh → refresh token 重放拒绝；
9. 全部通过才标记完成。失败时恢复 Nginx（包括本次显式退役的站点文件）、`current`、`service.env`、systemd
   单元和服务状态；existing TLS 模式还必须按字节恢复覆盖前的证书与私钥。SQLite migration
   进入可写阶段后不自动降级；脚本必须明示备份位置和
   人工停写/恢复边界，不得将运行态补偿误报为数据库原子回滚。

公网端点要求：

- Python 仅监听 `127.0.0.1:9100`，Nginx 覆盖而不追加 `X-Forwarded-For`；
- 激活/刷新有每源和全局限流，限流 key 会回收；
- `/healthz` 必须廉价；`/readyz` 不在每个公网请求中执行整库完整性扫描；
- 完整性扫描改由定时健康任务执行，或 ready 仅允许受信监控源；
- 记录 p50/p95/p99、5xx/429、SQLite busy、失败类别和备份新鲜度，不记录凭据和完整设备标识。

## 6. 法务与发布物

闭源商业服务端发行物当前已经包含：

- 明确的自有许可证 `LICENSE`；
- `THIRD_PARTY_NOTICES.md`，列出 Flask、Waitress、Cryptography 及传递依赖的版本、许可证和通知；
- 可机读 SPDX 2.3 SBOM、精确依赖约束和来源说明；
- 源码、部署素材和文档的来源/版权声明。

尚未完成的是依赖漏洞扫描报告、带哈希的批准 wheelhouse，以及人工法律/隐私审批。可以客户端
`WeChatMemory Proprietary Software License Version 1.0` 作为工程基线，但必须由权利人和法律顾问
确认服务器运营、SaaS/托管、数据处理、管辖法、责任限制和客户合同是否需要独立条款。复用工程
基线不等于完成法律审查。

## 7. 当前 P0/P1/P2 清单

### P0

本次审计未证实新 P0。以下仍为零容忍：私钥进仓/日志/普通备份，明文凭据入库，伪造票据可用，设备跨权益错绑，生产库不可恢复破坏。

### P1

| 编号 | 问题与证据 | 关闭标准 |
|---|---|---|
| S-P1-01 | **源码关闭**：专业订阅同时签发 `standard_analytics` 与 `advanced_analytics`，历史专业套餐快照在响应层补基础统计，不改写历史库 | 新套餐与历史快照回归均通过；目标环境授权联调并入 S-P1-07 |
| S-P1-02 | 客户端中 `multi_account` 没有执行点，单账号套餐可累积多账号档案 | 先决定限制新同步/显示/导出的产品语义，再在客户端加 fail-closed 用例 |
| S-P1-03 | **源码关闭**：公网 `/readyz` 仅检查 schema 并进入应用限流；`/readyz/deep` 才执行 `quick_check`，仅允许 loopback，Nginx 对公网精确返回 404 | API/Nginx 合同回归通过；目标环境探针证据并入 S-P1-07 |
| S-P1-04 | **源码关闭**：除廉价 `/healthz` 与仅本机 `/readyz/deep` 外，`/readyz`、`/v1/time`、`/v1/keys` 和授权路由均进入限流器；限流器同时维护每源和进程全局窗口、最多 4,096 个活跃 key，周期清理过期 key，键表满时拒绝陌生 key | 元数据 API 429/探针豁免、5,000 唯一来源、2,000 并发调用和时间推进回收测试通过 |
| S-P1-05 | **基础生产部署已通过，故障注入仍开放**：每个 release 使用自己的 `.venv`；systemd 的解释器、工作目录、预检脚本和公钥清单全部渲染为同一物理 release，运行期不再解引用 `current`；管理员入口只解析一次 `current`；正常切换和回滚在恢复指针、环境或单元前都先停稳备份 timer、备份 oneshot 与 API，停止失败则保留事务且禁止改路径；同一内核 `flock` 覆盖源码 fast-forward、部署、管理写入和备份；Server `0.2.7` 已在目标 EL9 主机从物理 release 启动并通过 deep ready | 现场基础证据见 `DEPLOYMENT_ACCEPTANCE_2026-11-02.md`；仍需注入 `after_application_switch` 故障，证明失败补偿后所有模块、指针、依赖和服务恢复到同一旧 release |
| S-P1-06 | **源码候选已修复，待现场证据**：一键部署的外层补偿事务覆盖 `current`、`service.env`、systemd/Timer、Nginx 配置和服务状态；从旧 TLS 切换 HTTP 时项目专属 Certbot timer 也事务化停用；existing TLS 证书/私钥在覆盖前捕获并按字节恢复 | 本地事务自测对证书/私钥执行 overwrite→fault→rollback→`cmp`；再在隔离 EL9 验收机分别注入 `after_application_install` 和 `after_nginx_switch`，验证恢复同一部署前快照 |
| S-P1-07 | **启动/健康现场通过，授权金丝雀仍开放**：候选发布已强制私钥标记扫描、依赖闭包/精确 SBOM、严格编译、服务端全测、全部 EL9 Shell 语法和事务自测，且位于 env/DB/current 变更之前；候选树权限固定为目录 `0755`/普通文件 `0644`，仅 Git `100755` 精确白名单恢复执行位，pytest 由 `wkm-license` 非 root 运行；启动预检会从已安装 distribution 的原始 `RECORD` 逐文件验证 Waitress 的 SHA-256/大小/边界并真实导入启动链；目标主机的 preflight、deep ready、public ready 与 `/v1/keys` 已通过 | 使用专用金丝雀凭据在目标环境完成 activate → 同激活码 401 → refresh → 旧 token 重放拒绝 → deactivate，保存账号之外的 commit/schema/key-id/时间戳和脱敏结果 |
| S-P1-08 | **源码关闭**：v1/v2/v3 使用连续顺序 SQL migration；全部 pending migration 在单一 `BEGIN IMMEDIATE` 中执行；原生 ledger 记录 SQL SHA，pre-ledger 旧库经版本化最低结构合同验证后记录 adoption 合同 SHA | fresh、真实历史 v1→v3 与 v2→v3 保数、incomplete schema 拒绝、失败全批回滚、已应用 SQL/ledger 漂移拒绝回归通过 |
| S-P1-09 | 仅有本机 SQLite 备份；异地加密副本和恢复演练只在文档中 | 实际配置异地备份，产出新鲜度告警和月度恢复 RPO/RTO 证据 |
| S-P1-10 | **工程材料关闭、人工审批仍开放**：仓库已有 proprietary `LICENSE`、11 个运行依赖通知、确定性 SPDX、精确约束和强制发布门禁 | 权利人、法律/隐私负责人完成适用地区、SaaS、EULA、隐私和第三方义务签字；补漏洞扫描审批记录 |
| S-P1-11 | **源码关闭**：旧 `rotated/revoked` refresh sessions 使用状态专用部分索引、90 天默认保留、500 条固定批次和 20 批默认上限；备份成功后定时执行，HTTP 请求不清理 | active/近期/异常时间保留、过期终态清理、批量边界、并发与外键、v2→v3 数据保留、索引计划和 timer 顺序回归通过 |
| S-P1-12 | **目标主机 HTTP/退役路径已通过，故障补偿仍开放**：共享主机默认只拥有专属 Nginx 文件，拒绝其他站点的 IP/端口冲突与并发漂移；操作员已显式退役停用且冲突的 `entityecho.conf`，原文件恢复副本和 committed 记录保留；Nginx `:9000`、回环 `:9100`、语法与公网 ready/keys 均通过；默认 HTTP 不开放 80/安装 Certbot | 现场基础证据见 `DEPLOYMENT_ACCEPTANCE_2026-11-02.md`；仍需在隔离/维护窗口注入 Nginx 切换故障，确认按字节恢复且不影响其他站点 |

### P2

| 编号 | 问题 |
|---|---|
| S-P2-01 | **已关闭**：文档明确 `/readyz` 是廉价 schema 检查，`/readyz/deep` 是仅本机完整性检查；`/v1/keys`/签名 canary 是独立门禁 |
| S-P2-02 | **已关闭**：单进程 Waitress 的每源/全局限流在应用层执行；Nginx 负责公网 HTTP、可信转发头和阻断公网 deep probe |
| S-P2-03 | 前置检查失败可留下未完整 release 目录；候选状态应与已发布状态分开 |
| S-P2-04 | **已关闭**：refresh days、ticket hours、每源/全局限流和 key 上限都做整数、范围及全局≥每源校验，非法环境启动失败 |
| S-P2-05 | 精确约束和 SBOM 可阻止版本漂移，但尚无带 SHA-256 的批准 wheelhouse 与自动漏洞扫描 |
| S-P2-06 | **已关闭**：删除运行时不消费的 `PUBLIC_BASE_URL`、`APP_BIND`、`APP_PORT`、`LOG_LEVEL` 伪配置；loopback 地址由 systemd 固定，公网 URL 由部署参数和 Nginx 管理 |
| S-P2-07 | **已关闭**：`/v1/time` 与 `/v1/keys` 不再绕过应用限流；只有廉价 liveness 和仅本机 deep probe 保持豁免 |
| S-P2-08 | **已关闭**：默认 HTTP 不依赖 Certbot；显式 letsencrypt 模式固定 `5.9.0`，并做格式与最低 `5.4.0` 数值门禁 |

## 8. 可直接修复与需用户决策

已直接完成：专业套餐覆盖基础统计；ready 有界化并限源；限流 key 回收；每 release 独立 venv；
应用/Nginx/existing TLS 补偿回滚；候选全测/SBOM/Shell 门禁；schema migration、原生 SQL/旧库合同
ledger SHA-256 和真实历史 v1→3 回归；终态 refresh session 有界保留清理；
同机 Nginx/firewalld/SELinux/Certbot 共存边界与专属命名；
许可证/第三方通知/精确依赖约束；配置范围校验。仍可直接继续的是失败候选清理、监控指标和带哈希
wheelhouse；真实公网金丝雀、异地备份与法务签字需要外部凭据或责任人。

确实需要决策：

| 决策 | 建议基线 | 影响 |
|---|---|---|
| `multi_account` 限制语义 | 不删已归档数据；非多账号套餐只选定一个账号继续新同步，其他只读 | 影响同步、账号切换、导出和降级 |
| 暂停/撤销的离线延迟 | 高风险套餐缩短离线窗口 | 安全与离线可用性取舍 |
| 数据库 RPO/RTO | 建议 RPO ≤ 24h、RTO ≤ 2h，再按用户量调整 | 备份频率、异地存储和恢复自动化 |
| 闭源许可证/EULA/隐私政策 | 工程上复用客户端 baseline，正式文本由权利人和法律顾问审批 | 服务器运营、代码交付、SaaS、隐私和责任分配 |
| 发布负载/SLO | 冻结 activate/refresh p95 和单机最大设备数后，再决定是否继续 SQLite 单主 | 限流、Waitress 线程、监控和数据库方案 |

## 9. 未通过自动循环

```text
DISCOVER -> CHECK -> BUILD -> DEPLOY_CANDIDATE -> VERIFY -> PROMOTE
                ^             |                 |
                |             v                 v
                +---------- FIX/RETRY <----- ROLLBACK
```

- 任一 P0/P1、测试、migration、备份、私钥、HTTP 入口、健康或金丝雀不通过都进入 `ROLLBACK`；
- 回滚后重新验证旧版 ready、activate/refresh、schema 和公钥；
- 证据不足进入 `FIX/RETRY`，修复后从 `CHECK` 重跑全链，不跳过前置门禁；
- 只有 `PROMOTE` 产生“已部署”记录，包含 commit、两端版本、schema、active key、哈希、恢复点和验收证据。

## 10. 发布签字清单

- [ ] 工作树干净，commit/版本/schema/API 已冻结；
- [ ] P0=0、P1=0，P2 有责任人和目标版本；
- [ ] 客户端与服务端公钥清单字节相等，私钥匹配 active key；
- [ ] 三套餐 capability 允许/拒绝端到端通过；
- [ ] 全测试、安全扫描、负载和 Shell 故障注入通过；
- [ ] 备份、migration、异地到达和隔离恢复通过；
- [ ] 应用/Nginx/venv/config 补偿回滚通过；migration 后的数据库恢复边界和升级前备份经演练确认；
- [ ] 本机与公网 HTTP health/ready/keys 通过；
- [ ] 金丝雀 activate → refresh → 旧 token 重放拒绝 → deactivate 通过；
- [ ] `LICENSE`、第三方通知、SBOM、依赖锁定和法律审查记录齐全；
- [ ] 发布证据包已异地保存。
