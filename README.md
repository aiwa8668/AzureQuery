# Azure 查询工作台

基于 Python / Flask 的 Azure 只读查询工具，集中展示 AzureAI Tier、AzureDiagnostics 调用日志、AzureAI Token 指标以及单租户 / 多租户成本数据。

> 本文以当前仓库代码为准。功能“已实现”不代表已通过真实 Azure 环境验收或具备生产安全性。当前适合个人本地、PoC 和受控内部环境，不应直接暴露到公网。

## 一、项目概述

### 定位与目标

工作台将分散在 Azure 门户和管理 API 中的订阅费用、AI 等级、模型指标与调用日志统一到一个浏览器界面。主要面向 FinOps / 财务分析人员、Azure 运维人员、AI 平台工程师及需要跨租户核对费用的管理员，减少切换订阅、重复登录、手工整理表格和限流重试的工作量。

项目执行数据查询和导出，不创建资源、不修改配额、不启用诊断设置，也不执行模型推理。成本、指标和日志是不同数据源，不能直接视为相同计费口径。

### 五个业务入口

所有入口均由 `GET /` 返回同一个模板，查询参数只控制前端视图，不是五个独立后端页面。

| 侧栏名称 | 直接访问路径 | 身份方式 |
| --- | --- | --- |
| AzureAI Tier查询 | `/?view=openai-tier` | Microsoft Entra 用户授权码登录 |
| AzureDiagnostics查询 | `/?view=azure-diagnostics` | 复用同一 Entra 登录会话 |
| AzureAI Token统计 | `/?view=azure-token-usage` | 复用同一 Entra 登录会话 |
| 单租户费用查询 | `/?view=costs&costMode=single` | 复用用户会话，选择当前身份可见订阅 |
| 多租户费用查询 | `/?view=costs&costMode=multi` | 浏览器提交多组服务主体凭据 |

`app-shell.js` 在没有有效 `view` 参数时默认进入 `openai-tier`。费用模式优先使用 URL 的 `costMode`，其次读取浏览器保存的模式，最后默认为 `multi`；保存的活动视图不用于覆盖默认入口。

## 二、全功能状态

状态定义：**已实现**表示存在代码路径；**条件可用**表示还依赖 Azure 权限、登录配置或上游数据；**受限**表示已实现但存在完整性、安全性或运行边界；**未实现**不能作为现有能力承诺。

### 核心业务功能

| 模块 | 已落地功能 | 状态及边界 |
| --- | --- | --- |
| AzureAI Tier | 登录 / 本地会话退出、Enabled 订阅发现、订阅搜索与多选、刷新、`quotaTiers` 查询、版本和路径回退 | 条件可用；只读，不申请升级；`OK`、`未使用`与错误行需分别理解 |
| Tier 结果 | 当前 Tier、分配日期、升级策略、下一 Tier、升级状态与日期、查询状态；表格筛选、排序、CSV、本地结果缓存 | 已实现；日期显示为 UTC+8 日期；预览 API 可用性由 Azure 决定 |
| AzureDiagnostics 发现 | 订阅、Log Analytics 工作区、OpenAI / AIServices 账号发现；两阶段诊断状态补全；日志分类、分类组、目标工作区检查 | 条件可用；`pending` / `unknown` 不等于关闭日志；不自动配置日志 |
| AzureDiagnostics 查询 | 日期 / 时区校验、订阅与资源过滤、生成并显示 KQL、调用明细、22 字段白名单、字段搜索 / 全选 / 清空、中文标签、横向滚动、当前可见字段 CSV | 受限；最多 31 天，每工作区最新 5000 行；不去重，不提供完整用量账本 |
| AzureAI Token | 多订阅选择、日期及 UTC 偏移、逐资源 Azure Monitor 指标查询、模型汇总 / 账号明细、列筛选、排序、合计与本地缓存 | 条件可用；统计 `ProcessedPromptTokens`、`GeneratedTokens`、`TokenTransaction`，不是逐请求日志或账单 |
| Token API 扩展 | `interval`、`scope=subscription`；订阅级跨资源指标不支持时回退逐资源查询 | 已实现于后端；当前页面提交时采用默认 `FULL`、`resource`，未提供所有 API 参数控件 |
| 单租户费用 | 用户订阅多选 / 搜索 / 全选 / 清空 / 刷新重试、月 / 日期选择、本月 / 上月快捷选择、费用查询与缓存恢复 | 条件可用；“单租户”指用户登录模式，不是额外实现的严格租户隔离策略 |
| 多租户费用 | 多组服务主体配置、可选订阅 ID、订阅发现、跨认证上下文并发查询、成功结果合并和认证警告 | 条件可用；不需要用户登录，但要提交真实 Client Secret |
| 费用结果 | 总费用与订阅概览、月 / 日口径交互、订阅与按天表格、行选择、列值搜索 / 筛选、默认 Cost `>0`、CSV | 已实现；后端还返回资源汇总与详单结构，不应据此宣称当前页面有四个独立结果页签 |
| 费用可靠性 | 先汇总后明细、零汇总跳过明细、分页、429 / 暂态错误重试、指数退避与抖动、范围二分降级、部分失败警告 | 受限；警告下明细可能不完整；并发治理仅在单进程内有效 |
| 费用后台任务 | `?async=1` 返回 202 和任务 ID，前端轮询结果；同步请求仍可用 | 受限；进程内字典及 daemon 线程，不是持久化任务队列 |

### 辅助与技术功能

| 模块 | 已落地功能 | 状态及边界 |
| --- | --- | --- |
| 凭据设置 | 添加 / 删除 / 保存配置行、必填及重复值校验、拒绝 Secret 掩码、CSV 导入 / 导出、清理认证与查询缓存 | 受限；Secret 明文存储 / 导出；认证 CSV 是简单逗号分隔，不支持字段内逗号 |
| 本地状态 | 主题、费用模式、费用查询历史、Tier 与 Token 查询缓存 | 已实现；仅当前站点来源与浏览器 Profile，非服务端历史、非用户级权限隔离 |
| 页面与静态资源 | 单模板工作台、深浅主题、筛选浮层 Portal、静态资源版本参数、主页禁缓存 | 已实现；不依赖 Node 构建或前端包管理器 |
| 可观测性 | 控制台和滚动文件日志、SQLite API 审计与限流事件、请求 ID、审计筛选和趋势接口 | 受限；仅本地存储，无独立审计页面、管理员鉴权或集中监控平台 |
| 离线回归 | `unittest`、禁止网络及 `.env` 读取的测试入口、main 导入路由烟雾测试、独立 Playwright 诊断 UI 测试 | 已实现；模拟测试不证明真实 Azure 权限或生产性能 |
| CLI 辅助 | `check-quota-tiers.sh` 输出 table / JSON / CSV，支持输出文件和 API 版本覆盖 | 条件可用；需 Bash、Azure CLI、jq，table 还需 column，与 Web 登录相互独立 |

**未实现能力：**服务端安全凭据库、服务端会话存储、自动令牌续期、统一站点鉴权 / 管理员 RBAC、完整 CSRF 防护、任务归属隔离、任务 TTL / 取消 / 恢复 / 排队总量限制、跨进程限流、持久化业务历史、自动同步调度、汇率转换、容器 / IaC / CI/CD 部署、专用健康检查、自动伸缩与高可用。仓库也未提供 Gunicorn / Waitress 依赖或生产服务管理配置。

## 三、技术架构

### 技术栈与分层

| 层次 | 技术与代码位置 | 选择逻辑与职责 |
| --- | --- | --- |
| 前端视图 | `templates/index.html`、`public/css/styles.css` | Jinja2 提供初始页面，原生 CSS 实现工作台及主题，减少构建链依赖 |
| 前端交互 | `public/js/*.js` | 原生 JavaScript、Fetch、DOM 和 localStorage，负责视图、筛选、轮询及缓存 |
| HTTP / 业务层 | `main.py`、Flask | 定义页面、认证、成本 / Tier / Token API，统一错误与审计，成本业务主要集中在 main |
| 专用业务模块 | `azure_diagnostics.py`、`azure_token_usage.py` | 分离日志发现 / KQL 查询和模型指标采集，复用认证及日期工具 |
| Azure 集成 | requests、MSAL | requests 调 ARM / OAuth 接口；MSAL 处理 Entra 授权码流程，无 Azure 管理 SDK 依赖 |
| 并发与治理 | 标准库线程池、锁、Condition、thread-local | 并发执行 I/O，限制 Cost 请求及诊断列表任务，避免无限提交发现任务 |
| 本地持久化 | sqlite3、logging、配置文件 | SQLite 仅存审计，日志落文件，配置由文本加载；不持久化费用任务 |

运行依赖来自 `requirements.txt`：`Flask==3.0.3`、`requests==2.32.3`、`msal>=1.28,<2`。类型语法要求 Python 3.10+；部署建议固定一个经过回归的 Python 版本，不把语法下限视为全部版本兼容保证。Jinja2 / Werkzeug 为 Flask 的依赖，SQLite 与线程池来自标准库。

### 目录与职责

```text
AzureQuery/
├── main.py
├── azure_diagnostics.py
├── azure_token_usage.py
├── requirements.txt
├── .env.example
├── config/sync.config
├── templates/index.html
├── public/css/styles.css
├── public/js/
│   ├── app-shell.js
│   ├── storage.js
│   ├── query.js
│   ├── openai-tier.js
│   ├── azure-token-usage.js
│   ├── azure-diagnostics.js
│   └── filter-popup-portal.js
├── tests/
│   ├── run_offline.py
│   ├── test_main_route_registration.py
│   ├── test_single_tenant_api.py
│   ├── test_azure_diagnostics.py
│   ├── test_diagnostics_concurrency.py
│   └── diagnostics_ui_browser.py
├── check-quota-tiers.sh
└── README.md
```

`main.py` 导入时初始化日志，并将 `app` 与当前模块传入诊断路由注册函数。因此开发启动为 `python main.py`，WSGI 导入目标为 **`main:app`**，无需另行注册诊断路由。`azure_diagnostics.create_app()` 是兼容辅助入口，不是部署文档的主入口。

### 系统交互与外部依赖

```text
浏览器 → Flask 页面 / JSON API
  多租户费用 → 客户端凭据 → Entra access token → ARM 订阅 / Cost API
  其他四入口 → MSAL 授权码回调 → Flask session → ARM 订阅与业务 API
  费用异步 → 进程内任务字典 / daemon 线程 → 浏览器轮询
  业务结果 → 页面内存 / localStorage / CSV
  API 请求与限流事件 → SQLite 审计 + 滚动日志
```

网络需允许访问 `https://login.microsoftonline.com`、`https://management.azure.com`。用户浏览器也需能完成 Microsoft 登录。当前端点针对 Azure 公有云，未实现主权云端点切换。日志查询使用 ARM 的工作区 `/query` 路径，而不是直接调用独立 Log Analytics 数据平面端点。

主要 API 版本：Cost Query `2023-03-01`；服务主体订阅发现 `2020-01-01`；用户订阅发现 `2022-12-01`；Cognitive Services accounts `2024-10-01`；Metrics `2023-10-01`；diagnosticSettings `2021-05-01-preview`；workspaces `2023-09-01`；工作区 query `2017-10-01`。Tier 优先尝试自定义版本，再尝试 `2026-01-15-preview`、`2025-10-01-preview`。

## 四、模块技术

### 4.1 工作台、认证与浏览器状态

`app-shell.js` 管理五入口导航、URL 与费用模式同步；`query.js` 管理两种费用模式、凭据设置和结果；其余三个业务脚本各自维护视图状态。`filter-popup-portal.js` 提供筛选弹层移动、定位与状态恢复，解决表格滚动容器裁剪弹层的问题。

用户登录通过 `GET /api/openai-tier/auth/login-url?source=...` 发起，MSAL 校验授权码流程，`/auth/openai/callback` 保存访问令牌、到期时间和用户信息后回到对应入口。随机 OAuth state 包含来源信息；四个用户登录入口共用一次会话，不拥有四套独立账户。到期时间提前 60 秒判失效，失效后需重新登录；没有刷新令牌自动续期。`/auth/openai/logout` 只清理应用本地会话，不执行 Microsoft 全局退出，也不清理全部 localStorage。

用户订阅列表缓存 TTL 为 300 秒，存于进程内；“重新校验订阅权限”仍可能使用 TTL 内缓存，不保证每次都向 ARM 即时重新发现，实际资源读取权限由 Azure 检查。

多租户凭据字段对应关系：

| 界面 / CSV 字段 | JSON 字段 | 约束 |
| --- | --- | --- |
| `App_Client_ID` | `azureClientId` | 应用 Client ID，必填 |
| `App_Client_Secret` | `azureClientSecret` | Secret Value，非 Secret ID，必填 |
| `Tenant_ID` | `azureTenantId` | 租户 ID，必填 |
| `Tenant_Name` | `tenantName` | 显示标签及成本治理分组，必填 |
| `Subscription_ID` | `subscriptionId` | 可选，不填则发现可见订阅 |

认证 CSV 表头为 `App_Client_ID,App_Client_Secret,Tenant_ID,Tenant_Name,Subscription_ID`，最后一列可省略。前端保存 / 导入 / 导出拒绝必填缺失、掩码 Secret 和多行任意非空值重复，因此相同凭据拆为多行可能被拒绝。CSV 内容包含明文 Secret，不能用于普通共享附件。

浏览器键包括凭据 `azure-cost-analyzer-auth-configs-v1`、旧版兼容键 `azure-cost-analyzer-config-v1`、费用缓存前缀 `azure-cost-analyzer-query-cache-v1`、按模式保存且最多 60 项的日期范围历史前缀 `azure-cost-analyzer-single-tenant-query-cache-v1`、Tier 缓存 `azure-query-openai-tier-cache-v2`、Token 缓存前缀 `azure-query-azure-token-cache-v3-model-fallback`、视图 / 模式偏好及 `color-theme`。Tier / Token 缓存按查询维度组织，不是可靠的用户权限隔离；诊断结果当前只保存在页面内存。

### 4.2 费用查询与进程内任务

多租户接口 `POST /api/costs/query` 接收 `startDate`、`endDate`、`authList`，兼容单个 `auth`。各凭据通过客户端凭据模式获取 `https://management.azure.com/.default` 令牌，然后发现订阅或匹配可选订阅 ID。单租户接口 `POST /api/single-tenant/query` 接收日期及 `subscriptionIds`，校验登录身份可见订阅后复用相同聚合函数。

核心处理链：校验 → 身份 / 订阅发现 → 每订阅汇总 → 非零汇总才取 Daily 明细 → 分页与范围降级 → 资源名解析 → 聚合。非零包括负数，不是仅正成本。成本请求为 `ActualCost`、`PreTaxCost` 求和；日期为 UTC `00:00:00Z` 至 `23:59:59Z`，不是 Token / 日志页面的用户偏移时间范围。明细分组为 `ResourceId`、`ResourceLocation`、`ServiceName`、`Product`。

返回 `summary`、`bySubscription`、`byResourceType`、`byDate`，其中 `byDate` 是按日、订阅、资源和产品聚合的明细结构，页面按天表格再做聚合。名称 `byResourceType` 实际存资源名称 / 区域 / 服务汇总，不是严格资源类型统计。后端 CSV `POST /api/costs/export` 接收客户端回传的 `data` 和 `dimension`，支持 `subscription`、`resourceType`、`date`、`all`；按天 CSV 在前端生成。导出不是再次向 Azure 验证数据。

费用数据限制必须随结果一起解释：

- 汇总成功但明细失败时保留汇总，因此总费用可能大于详单合计；汇总失败的订阅可能显示 0 并附警告，0 不一定表示没有消费。
- 多认证失败由 `authWarnings` 返回，订阅或日期段失败由 `warnings` 返回；界面成功不意味着所有数据完整。
- 金额采用浮点与两位小数舍入；多币种结果没有汇率转换或按币种分账，不能将混合币种总和用作财务结算。
- 部分明细聚合键使用订阅显示名与资源短名，并非始终使用完整唯一资源 ID；同名数据可能合并，不是审计级原始账单。

两个费用接口加 `?async=1` 后立即返回 `{jobId, status: "queued"}` 和 HTTP 202，后台状态依次为 `queued`、`running`、`succeeded` / `failed`。轮询路径分别为 `/api/costs/query/jobs/<job_id>`、`/api/single-tenant/query/jobs/<job_id>`；不存在返回 404。**任务没有真实 TTL**，虽然错误文案包含“已过期”，代码没有定期清理任务；后台使用测试请求上下文调用同步函数，不等于执行完整请求审计钩子，不能把提交 / 轮询审计当作完整任务审计。

### 4.3 Cost API 并发与重试

`CostQueryGovernor` 用锁和 Condition 管理进程全局、租户、订阅 Scope 在途数量，平滑 Tenant / Scope 发送速率，并根据 429 响应头传播冷却。服务主体路径的租户分组使用调用方租户标签，不能作为不可绕过的身份隔离或分布式配额保证。

订阅线程池根据含首尾的天数降级：`>=31` 天最多 1，`14–30` 天最多 2，其余使用 `max_workers`，最终不超过订阅数。HTTPAdapter 设置连接池与 `pool_block=True`。连接池限制不等于全局请求速率限制。

请求对网络异常、429、408 / 500 / 502 / 503 / 504 执行有限重试；结合 `Retry-After` 和 Cost 专用限流响应头、指数退避、随机抖动、单次延迟与累计重试等待预算。累计等待预算不是查询全链路硬超时，也不覆盖全部治理等待。持续 429 的明细时间段递归二分直至单日，失败单日保留警告；403 不进行此类重试。

### 4.4 AzureAI Tier

`openai-tier.js` 负责订阅选择、结果表、筛选 / 排序、CSV 和缓存；`main.py` 负责 `GET /api/openai-tier/subscriptions`、`POST /api/openai-tier/query` 及 quotaTiers 请求。后端重新发现 Enabled 订阅，并按非空选择列表过滤；无选择过滤时查询全部可见 Enabled 订阅。

每个订阅依次尝试 `/providers/Microsoft.CognitiveServices/quotaTiers` 和 `/quotaTiers/default`，再回退 API 版本。列表响应优先提取名为 `default` 的项，也兼容对象响应。字段为 `subscriptionName`、`subscriptionId`、`currentTierName`、`assignmentDate`、`tierUpgradePolicy`、`nextTierName`、`nextTierStatus`、`nextTierDate`、`queryStatus`。无有效对象且未遇到硬失败可标记“未使用”，不能直接断言订阅从未使用 AI。

并发由 `OPENAI_TIER_MAX_WORKERS` 或配置计算，范围 1–16；默认启用自适应，根据上一批失败比例、限流比例及批次耗时调整。此控制器仅存于当前进程，Tier 的回退机制也不是 Cost 的完整重试 / 限速方案。

### 4.5 AzureAI Token 指标

`GET /api/azure-token/subscriptions` 返回当前用户可见订阅，`POST /api/azure-token/query` 接收 `subscriptionIds`（兼容单个 `subscriptionId`）、`startDate`、`endDate`、`tz` 及可选 `interval` / `scope`，复用 Entra 登录并校验订阅访问范围。

`azure_token_usage.collect()` 发现 Cognitive Services 账号，按资源查询 Azure Monitor Metrics，并以资源 ID、部署、模型和模型版本组合累计指标，再输出模型汇总、账号明细和警告。模型名称为空时回退部署名。`Total` 与 `grandTotal` 使用 **`TokenTransaction`**，不另将输入与输出相加，也不等于诊断明细的 Token 计数。

日期包含本地结束日，转换为 UTC 半开区间；默认偏移 `+8`、粒度 `FULL`、范围 `resource`。后端支持订阅范围按区域跨资源查询，遇特定不支持错误回退逐资源。指标查询的 HTTP 超时固定 120 秒、`top=1000`，并非所有请求都由 `sync.config` 控制；没有实现指标结果自动分页或 Cost 同等级重试治理。多订阅线程数由 `AZURE_TOKEN_MAX_WORKERS` 控制，单个订阅内资源顺序处理。部分失败合并警告，全部订阅失败返回 502；缺失指标填 0 不证明实际没有用量。

### 4.6 AzureDiagnostics 两阶段发现与日志查询

三个路由由 `azure_diagnostics.register_routes()` 注册，并通过 `app.extensions` 避免重复注册：

| 方法及路径 | 输入与输出 |
| --- | --- |
| `GET /api/azure-diagnostics/workspaces` | 重复的 `subscriptionIds` 或兼容 `subscriptionId`；返回 `workspaces` |
| `GET /api/azure-diagnostics/resources` | 同上，加 `phase` 与第二阶段 `resourceIds`；返回 `resources` |
| `POST /api/azure-diagnostics/query` | 日期、`tz`、`subscriptionIds`、`workspaceIds`、可选 `resourceIds`；返回 KQL、列、行、时间范围与警告 |

**当前前端已经接入两阶段模式**：工作区请求与 `phase=accounts` 并行，先显示 `kind=OpenAI/AIServices` 的账号与 `pending` 状态；随后每批 20 个资源 ID、最多 2 个后台请求获取 `phase=diagnostics`，按 ID 合并状态，不等待所有诊断状态完成才允许选择与查询。版本号防止旧请求覆盖新订阅选择；不等于取消所有已发送网络请求。

不传 `phase` 默认 `full`，一次返回账号和诊断信息；`accounts` 跳过诊断设置；`diagnostics` 只读取指定 ID 的设置，不依赖首阶段缓存。后端单个 ID 列表限制 1–100 项；校验 UUID、资源类型、所选订阅归属，再由 ARM 检查读取权限。诊断设置 401 / 403 转为 `unknown`，不能标记为“未启用”；启用任意日志不等于启用 `AzureOpenAIRequestUsage`。`allLogs` 分类组或该分类的启用状态单独形成 `usageLogsEnabled`；日志可能仅发往存储 / Event Hub，工作区也可能在其他订阅。

账号、工作区及诊断设置采用 `BoundedSessionPool`：每请求最多 `min(max(1, max_workers), 8)` 个活动 / 待完成任务，每线程独占复用 Session，失败后取消未执行任务、等待在途任务并关闭连接。默认 full 模式复用两阶段线程池。ARM 列表分页限制域名、HTTPS、路径、版本、重复地址和最多 100 页；不把上游错误正文直接返回给浏览器。

KQL 使用 `AzureDiagnostics` 表，按 UTC 半开区间、`MICROSOFT.COGNITIVESERVICES`、订阅与可选资源过滤；**没有限定 Category，也不执行 RequestId 去重或 `arg_max`**。同一请求 ID 的不同操作、IP 和重复日志均独立保留。资源过滤用内部 `_ResourceIdFilter`，优先读取存在的 `_ResourceId` 列，否则读 `ResourceId`，不覆盖原始大小写；内部 `p` 与过滤列在返回前移除。请求 ID 优先关联 ID，再取日志属性 requestId，缺失时用哈希兜底，并非全局去重键。

Token 字段从 `properties_s` 的数组首元素读取并计算：`InputTokens=PromptTokens`、`OutputTokens=GeneratedTokens`、`CachedInputTokens=CachedTokens`、`UncachedInputTokens=InputTokens-CachedInputTokens`；缺失计数默认 0。缓存写入、长上下文计数独立列出，不重复累加；没有保证未缓存输入一定非负或各模型日志 schema 完全一致。

**22 个前端明细字段白名单（按显示顺序）**：只有上游返回列或行中存在的字段才进入选择器，初次出现默认勾选；不是后端固定只返回 22 列。

| 序号 | 字段 | 序号 | 字段 |
| --- | --- | --- | --- |
| 1 | `TimeGenerated` | 12 | `GeneratedTokens` |
| 2 | `RequestId` | 13 | `LongContextPromptTokens` |
| 3 | `OperationName` | 14 | `LongContextCachedTokens` |
| 4 | `CallerIPAddress` | 15 | `LongContextCacheWriteTokens` |
| 5 | `DurationMs` | 16 | `LongContextGeneratedTokens` |
| 6 | `ResultSignature` | 17 | `Model` |
| 7 | `event_s` | 18 | `InputTokens` |
| 8 | `threadID_d` | 19 | `OutputTokens` |
| 9 | `PromptTokens` | 20 | `CachedInputTokens` |
| 10 | `CachedTokens` | 21 | `UncachedInputTokens` |
| 11 | `CacheWriteTokens` | 22 | `properties_s` |

后端保留其他原始列、计算字段及附加 `workspaceId`，但前端不展示白名单外字段，例如 `Deployment`、`ModelVersion`、`CorrelationId`。原始空字符串 / null 在 API 保留，页面 null 显示为“—”，对象转为 JSON；时间当前按 `+8` 显示上海时间，否则显示 UTC。字段导出只导出当前选择列，采用 UTF-8 BOM、引号转义及公式前缀防护；不能据此宣称其他 CSV 出口都有相同公式防护。

查询最多 31 天，不允许未来结束日期，当天截止当前时刻；每工作区查询 5001 行以检测截断，只返回最新 5000 行。多个工作区顺序查询、独立返回，可能含相同日志，不可直接累加。上游 error / 部分结果被拒绝计入统计；没有自动跨时间分页取全量。页面目前一次选择一个订阅、工作区和资源，后端支持多个；工作区必须归属请求选择的可见订阅。空日志需检查采集目标、表、分类及采集延迟，不能归结为零调用。

### 4.7 日志、审计与配置

控制台与 `logs/azure-cost-query.log` 输出 INFO 日志；文件每份最大 5 MiB，保留 5 个滚动备份。`logs/azure-cost-query-audit.db` 保存 API 请求、租户 / 订阅维度及限流事件，建表 / 兼容列补充由代码完成。API 请求响应带 `X-Request-ID`。审计过期清理在 API 请求前触发，不是独立定时任务。

`GET /api/audit-logs` 支持账号、租户、订阅、路径、状态、来源 IP、限流规则 / 路径等筛选；`GET /api/audit-logs/throttle-trends` 支持时间和分组趋势。返回的耗时与性能目标只是当次测量 / 代码目标，不构成 SLA。账号和订阅、IP、原始调用日志都可能是敏感业务信息，日志文件与下载内容需要访问控制。反向代理场景当前 `request.remote_addr` 可能只是代理 IP，没有内置可信代理修正。

**配置优先级必须区分两类：**

- 查询 / 治理参数：`config/sync.config` 中有效值优先，缺失或非法值回退 `SyncConfig` 默认值；这些同名项不会从环境变量覆盖。配置在请求 / 查询时重新加载，新请求无需重启，已运行任务不保证切换到新参数。文件无法读取等 I/O 故障不等于值校验回退。
- 登录等环境参数：启动时自定义加载器读取根目录 `.env`，使用 `os.environ.setdefault`，因此 **已有进程环境变量优先于 `.env`，再回退代码默认值**。这不是 python-dotenv，不承诺变量插值等高级语法。修改 `.env` 或部署环境后需重启进程；`FLASK_SECRET_KEY` 在模块导入时设置。

当前 `config/sync.config` 与代码默认值如下，实际部署应以目标环境文件为准，不照搬注释或旧文档示例：

| 配置项 | 当前文件值 | 代码默认值 | 用途 |
| --- | --- | --- | --- |
| `max_workers` | 5 | 2 | 费用认证 / 订阅并发基数，诊断线程上限基数 |
| `http_pool_connections` | 64 | 16 | HTTP 连接池分组数 |
| `http_pool_maxsize` | 96 | 64 | 单池连接数 |
| `subscription_list_timeout_seconds` | 60 | 60 | 订阅与诊断发现超时秒 |
| `cost_query_timeout_seconds` | 120 | 120 | Cost / 日志查询超时秒 |
| `cost_query_max_retries` | 3 | 3 | 首次失败后重试数，总尝试为 1 + 此值 |
| `cost_query_retry_base_seconds` | 10 | 2 | 退避基数秒 |
| `global_cost_api_max_inflight` | 10 | 12 | 进程级 Cost 在途上限 |
| `tenant_cost_api_max_inflight` | 5 | 3 | 租户组在途上限 |
| `cost_query_retry_max_delay_seconds` | 120 | 45 | 单次重试延迟上限秒 |
| `scope_cost_api_max_inflight` | 2 | 1 | 订阅 Scope 在途上限；当前文件值不是强制串行 |
| `cost_query_rate_limit_enabled` | true | true | 平滑限速与冷却开关 |
| `tenant_cost_api_requests_per_minute` | 15 | 15 | Tenant 平均请求速率 |
| `scope_cost_api_requests_per_minute` | 3 | 3 | Scope 平均请求速率 |
| `scope_cost_api_min_interval_seconds` | 20 | 20 | Scope 最小发送间隔秒 |
| `cost_query_retry_jitter_max_seconds` | 3 | 3 | 抖动上限秒 |
| `cost_query_retry_total_wait_seconds` | 600 | 600 | 单逻辑请求累计重试等待预算秒 |
| `cost_query_client_type` | Azureaq-Superpop | AzureCost-FinOpsPortal | 非敏感稳定客户端标识 |
| `max_subscriptions_per_query` | 0 | 0 | 单租户费用订阅选择上限，0 为不限制 |
| `audit_log_retention_days` | 5 | 5 | SQLite 审计保留天数 |

`max_workers` 等正整数项非法时回退；重试次数、最小间隔、抖动、订阅上限支持非负值。`save_sync_config()` 当前只写部分配置项，没有页面配置 API；不要将其作为保留全部参数的通用编辑器。其余说明文件予以保留，有冲突时以实际加载函数和配置为准。

## 五、开发测试生产部署

### 5.1 环境与 Azure 前置条件

- 准备 Python 3.10+、pip 和现代浏览器；建议使用独立虚拟环境。Web 服务不需要 Node、npm、Azure CLI；Node 仅用于可选 JS 语法检查。
- 确认网络可访问 Entra 与 ARM，主机可写项目下 `logs/`，有足够磁盘空间；安装依赖需访问可信 PyPI 镜像。
- 多租户费用的服务主体需拥有目标订阅读取和 Cost Management 查询权限；按组织策略授予 Cost Management Reader / Reader 或等效最小权限，账单类型与成本可见性策略也可能限制结果。
- 其余四入口需 Web 类型 Entra 应用、Client ID / Secret、Azure Service Management 委托权限 `user_impersonation`、必要同意，以及登录用户对实际资源的 RBAC。委托权限同意不会自动授予订阅或日志读取权限。
- 诊断查询还需工作区 / 诊断设置读取与日志查询权限，例如符合组织策略的 Log Analytics Reader 或等效授权；目标资源日志已送到可见工作区的 `AzureDiagnostics` 表。仅启用指标或发送到其他目标不满足前提；工具不会补采启用之前的日志。

### 5.2 Windows 本地开发

在 PowerShell 中进入仓库，使用虚拟环境解释器，避免激活脚本执行策略问题：

```powershell
Set-Location D:\AzureQuery
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip check
```

可选激活为 `.\.venv\Scripts\Activate.ps1`，不需要更改系统全局执行策略。

**服务端环境变量：**示例文件为 `.env.example`，不应覆盖已有真实 `.env`。可以由部署平台注入进程环境；本地也可从模板创建一个尚不存在的 `.env` 后手工填写，不能提交真实凭据。本次文档验收不读取该文件。

| 变量 | 必需性 / 默认值 | 配置说明 |
| --- | --- | --- |
| `FLASK_SECRET_KEY` | 受控部署必须配置；默认 `dev-only-change-me` 不安全 | 长随机稳定密钥；重启保持稳定，轮换会使既有 Cookie 无效 |
| `OPENAI_TIER_CLIENT_ID` | 四个用户登录入口必需 | Entra 应用 Client ID |
| `OPENAI_TIER_CLIENT_SECRET` | 四个用户登录入口必需 | Entra Secret Value，由安全渠道注入 |
| `OPENAI_TIER_AUTHORITY` | 默认 `https://login.microsoftonline.com/organizations` | 根据应用支持的账户类型设置，可采用组织允许的租户 authority |
| `OPENAI_TIER_REDIRECT_URI` | 可选，缺省从当前请求生成 | 本地建议明确设为 `http://localhost:5100/auth/openai/callback`，必须与应用注册一致 |
| `OPENAI_TIER_API_VERSION` | 可选 | 自定义版本优先，随后仍尝试两项内置回退版本 |
| `OPENAI_TIER_MAX_WORKERS` | 可选 | Tier 并发上限，最终限制 1–16；未设置时取 `max(sync.max_workers,6)` |
| `OPENAI_TIER_ADAPTIVE_CONCURRENCY_ENABLED` | 默认 true | Tier 自适应并发开关 |
| `AZURE_TOKEN_MAX_WORKERS` | 默认 6 | Token 订阅并发，需有效正整数；非法字符串会导致查询失败 |

使用当前 PowerShell 的安全输入注入本地凭据，不在命令历史中写真实 Secret：

```powershell
$env:FLASK_SECRET_KEY = python -c "import secrets; print(secrets.token_hex(32))"
$env:OPENAI_TIER_CLIENT_ID = Read-Host 'Entra Client ID'
$env:OPENAI_TIER_CLIENT_SECRET = Read-Host 'Entra Secret Value' -MaskInput
$env:OPENAI_TIER_AUTHORITY = 'https://login.microsoftonline.com/organizations'
$env:OPENAI_TIER_REDIRECT_URI = 'http://localhost:5100/auth/openai/callback'
.\.venv\Scripts\python.exe main.py
```

`-MaskInput` 需 PowerShell 7.1+；环境变量仍是进程可访问的明文，不是安全凭据库。示例随机 session key 每次重新生成都会使旧会话失效，仅适合本地开发。若只使用多租户费用，用户登录变量可不配置，用户登录入口会提示未配置，主页仍能打开。

主入口默认监听 `0.0.0.0:5100`、`debug=False`，浏览器访问 `http://localhost:5100/`。这意味着开发服务器也会绑定所有网卡，应限制防火墙访问；没有内置 `PORT` / `HOST` 环境变量覆盖。仅需回环绑定或断点调试时，可用 Flask CLI 显式关闭其自动 dotenv 加载：

```powershell
$env:FLASK_SKIP_DOTENV = '1'
.\.venv\Scripts\python.exe -m flask --app main:app run --host 127.0.0.1 --port 5100
```

需要重载 / 调试器时在最后增加 `--debug`，仅限回环开发环境，绝不能用于共享或生产服务。`FLASK_SKIP_DOTENV` 只影响 Flask CLI，不会关闭 main 自己的 `.env` 加载器；如必须不读 `.env`，使用下一节的屏蔽式离线测试，而不是普通启动或导入。

### 5.3 自动测试与本地验证

推荐离线测试入口：

```powershell
.\.venv\Scripts\python.exe -m py_compile main.py azure_diagnostics.py azure_token_usage.py
.\.venv\Scripts\python.exe tests/run_offline.py
```

`py_compile` 只编译源文件，不执行模块导入；`run_offline.py` 在导入前屏蔽 `.env` 存在性检查并禁止 socket 外连，再发现 `test_*.py`。覆盖单租户授权 / 参数、Cost 并发与重试、诊断时间 / 资源 / 分页 / 原值与截断、诊断有界调度、独立进程正常 `import main` 路由注册等。运行仍可能创建 `logs/` 与 Python 缓存，这不是文档或业务源码变更。

可选 JS 检查（需要 Node，仅检查语法）：

```powershell
Get-ChildItem public/js/*.js | ForEach-Object { node --check $_.FullName; if ($LASTEXITCODE -ne 0) { throw 'JavaScript syntax check failed' } }
```

诊断 UI 浏览器测试不是默认 discovery 的一部分；Playwright 不在运行依赖中，需要显式安装测试依赖及 Chromium：

```powershell
.\.venv\Scripts\python.exe -m pip install playwright
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe -c "import runpy,sys; from pathlib import Path; from unittest.mock import patch; original=Path.exists; sys.argv=['tests/diagnostics_ui_browser.py']; safe=lambda p: False if p.name=='.env' else original(p); exec('with patch.object(Path, '+repr('exists')+', safe):\n runpy.run_path('+repr('tests/diagnostics_ui_browser.py')+', run_name='+repr('__main__')+')')"
```

最后一条在导入 main 前屏蔽 `.env`，页面与 API 用模拟路由响应，不需要真实 Azure 凭据；验证 22 字段、字段筛选、中文标签、布局宽度、横向滚动及清理。首次浏览器安装需要网络。它不验证真实 Microsoft 登录、Azure 调用或生产负载。

启动后的基础验证：`/` 返回 200、`/favicon.ico` 返回 204、`/api/openai-tier/auth/status` 返回 200。登录配置完整但未登录时，诊断工作区 / 资源 GET 和查询 POST 应返回 **401 JSON，而非 404**；配置缺失时业务鉴权可能先返回 500 配置错误，不能套用 401 断言。

```powershell
(Invoke-WebRequest 'http://localhost:5100/').StatusCode
Invoke-RestMethod 'http://localhost:5100/api/openai-tier/auth/status'
```

之后手工验收五入口：Tier 订阅与版本回退、诊断账号先出现再补状态及字段 CSV、Token 同范围指标对照、单租户选择与越权拒绝、多租户小范围费用与警告 / 导出。与门户对照必须统一身份、订阅、工作区、时间、币种及粒度，不以单次 HTTP 200 判断数据完整。

### 5.4 测试环境部署

1. 准备独立目录、虚拟环境、测试 Entra 应用 / 最小权限订阅，避免复用生产凭据、Cookie 和浏览器 Profile。
2. 安装 `requirements.txt`，设置稳定测试 session key、测试 authority / Secret 和明确回调 URL；在 Entra 注册完全相同的 Web 回调地址。
3. 配置 `config/sync.config`，先小时间范围 / 低并发验证；确保 logs 可写，不共享开发审计数据库。
4. 执行离线回归及可选 UI 测试。个人临时测试可使用回环 Flask 服务器；共享测试需选定 WSGI 服务并限制网络访问，不能把开发服务器当作正式托管方案。
5. 通过受控入口完成五模块真实数据验收、错误 / 401 / 403 / 429 检查、HTTPS 与 Cookie 检查、重启导致任务丢失的演练，保存脱敏验收记录。

仓库无“一键部署测试环境”脚本，也没有云端资源模板。测试环境的域名、证书、代理和服务管理由运维提供，不是应用自动完成。

### 5.5 生产前置审查与条件化运行

**当前代码不满足公开、多用户、高可用生产服务要求。**以下是生产化门槛与受控单机运行参考，不是已经具备生产能力的声明。若未完成整改，只可由组织风险审批后在低并发、可信人员、隔离网络中试运行；否则应停止上线。

必须评估并整改的安全 / 可靠性限制：

- **Cookie 安全：**默认 Flask session 是客户端签名 Cookie，签名保证完整性但不加密；访问令牌及 OAuth flow 放在其中，可能超过浏览器 Cookie 大小限制。现有 HttpOnly、SameSite=Lax 不等于加密或完整 CSRF 防护，代码未设置 `SESSION_COOKIE_SECURE=True`，默认 session key 已知且不安全。正式方案应改为服务端会话和安全令牌存储、HTTPS / Secure Cookie，并验证 CSRF 与登录失效策略。
- **localStorage 安全：**多租户 Client Secret 明文保存，站点 JavaScript / XSS 和本机访问者可读取；认证 CSV 同样暴露秘密，缓存也可能跨登录用户残留。正式方案应避免 Secret 进入前端，实施受控服务端凭据存储、用户隔离、缓存生命周期和密钥轮换。仓库未集成 Key Vault 或托管身份。
- **访问控制：**主页、多租户成本 / 导出、审计 / 趋势与任务轮询没有统一站点登录保护；任务读取也没有验证调用者身份或任务归属，UUID 不等于权限。必须对全站设置准入并补充应用级授权；仅网关登录不能实现任务及审计数据的用户隔离。
- **任务安全与可用性：**每次异步提交启动 daemon 线程，任务字典无容量上限 / 清理 / 取消 / 持久化；重启或进程退出丢任务。多 worker / 多副本各自持有任务、缓存和限流器，轮询可能落到另一进程返回 404，全局限流也不再全局。未经重构不要启用多 worker / 自动伸缩；正式方案需共享队列 / 状态与任务授权、恢复及排空机制。
- **部署与审计：**没有专用健康检查、监控告警、集中审计、统一备份 / 回滚与可信代理配置；本地 SQLite、滚动日志和异步审计存在覆盖及并发边界。其他 CSV 出口的公式注入防护需单独核对，不能泛化诊断导出的防护。

受控单机 WSGI 标准操作参考（Windows）：

1. 固定经回归的代码版本与 Python 版本，在独立发布目录安装运行依赖；通过服务管理器安全注入稳定的环境变量，注册实际 HTTPS 域名的 `/auth/openai/callback`。不要使用示例随机 key 作为每次启动配置。
2. 运维另行选择并安装 WSGI 服务。以下使用额外的 Waitress，**它不在仓库 requirements 中**，需评审版本并纳入部署依赖记录。Flask `app.run` 不是生产服务器。
3. 单进程回环监听，通过受控反向代理提供 HTTPS、外部准入、请求限制与合理超时；禁止绕过代理直连。显式回调 URL 避免代理协议识别错误，可信代理 / Secure Cookie 仍需实际配置或代码整改，不能只写未被代码读取的环境变量。

```powershell
Set-Location D:\AzureQuery
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install waitress
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\waitress-serve.exe --listen=127.0.0.1:5100 --threads=8 main:app
```

该命令只是进程启动，不会创建 Windows 服务、证书、反向代理或完成安全整改；后台业务线程与 HTTP 线程是不同层次，`--threads=8` 不是任务总并发上限。Linux 可另选经评审 WSGI 服务，导入目标同为 `main:app`，费用异步模型未改前仍需单 worker；仓库未提供对应安装或 system service 配置。

4. 由组织批准的服务管理器托管进程、限制文件权限与重启行为；持久化并备份 logs / SQLite，避免记录或公开环境变量、Cookie 和请求 Secret。
5. 按 5.3 基础状态和五模块进行真实验收，额外验证全站准入、任务归属、审计权限、Secure Cookie、并发限流、错误和备份恢复。主页 200 只是应用存活，不代表 Azure 网络 / 权限就绪。
6. 发布前停止新查询，等待在途任务结束后再重启；没有自动排空接口。保留上一发布版本与配置备份，故障时回退代码 / 配置并重新验收；任务不可恢复，用户需重新提交，session key 轮换还需重新登录。

不承诺吞吐、SLA、零停机或横向伸缩。Cost 平台限流与数据延迟仍可能发生，生产性能需在授权环境进行独立测试。

### 5.6 运维排查

| 现象 | 核对顺序 |
| --- | --- |
| 登录未配置 / 500 | 进程变量是否注入、环境优先级、Client ID / Secret Value、authority，修改后是否重启 |
| 登录回调失败 | Web URI 精确匹配、应用账户类型 / 同意、Cookie 大小、代理协议、session key 是否稳定 |
| 401 / 403 | 会话是否到期、实际 Azure RBAC、所选订阅 / 工作区归属；不要用门户另一个身份对照 |
| Secret 错误 | 是否误填 Secret ID、过期或掩码；更正凭据并安全清理浏览器 / CSV 副本 |
| 429 / 查询很慢 | 缩短日期范围，降低并发和速率，检查 Retry-After、审计限流趋势与日志；禁止盲目增加线程 |
| 诊断状态 unknown / 无日志 | 设置读取权限、usage 分类 / allLogs、工作区目标、日志表及采集延迟；unknown 不是关闭 |
| 费用为零 / 合计不一致 | Cost 默认 `>0` 筛选、失败警告、汇总与明细完整性、币种、同名聚合及时间口径 |
| 任务 404 | 进程是否重启、多 worker / 副本是否误启用；任务未持久化，不存在恢复命令 |
| 日志 / 审计异常 | logs 写权限、磁盘、SQLite 锁与文件备份，代理来源 IP 和异步审计边界 |

## 六、维护贡献

### 代码与提交规范

当前仓库没有已落地的分支保护、提交钩子、统一格式化器或 CI 工作流。以下为后续协作规范建议，不是已启用的自动保障：

- 每项修改通过独立分支与评审提交，遵循现有 Python / 原生 JS 风格；不引入未经声明的依赖或顺手重构，保持 `main:app` 及现有路由兼容。
- 提交信息建议采用 `feat:`、`fix:`、`docs:`、`test:`、`refactor:` 等类型，描述变更目的；只有维护者明确授权才创建提交 / 发布。
- 变更附带测试命令、预期与实际结果、数据口径和兼容性说明；涉及诊断时检查两阶段状态、22 字段、原值保留、边界与截断；涉及任务 / 权限时补充归属和失效回归。
- 提交前执行离线测试、编译检查及适用的 JS / UI 检查，审阅差异；不得提交 `.env`、真实 Secret、令牌、Cookie、认证 CSV、生产日志和业务数据。`.gitignore` 已忽略 `.env`、logs、虚拟环境与 Python 缓存，但不会自动防止其他路径泄密。
- 修改路由、参数、环境配置、数据口径或部署方式时同步更新 README。保留其余文档与文件，不以本次文档整理为理由删除文件；配置注释和辅助文档出现冲突时核对代码。

### 问题反馈与安全报告

通过仓库所在平台的 Issue 或团队工单提供：代码版本、Python / 依赖版本、入口、脱敏参数、时间范围 / 时区、状态码、`X-Request-ID`、警告文本、复现步骤、预期与实际结果、是否使用真实 Azure。截图和日志必须移除账号、租户 / 订阅标识、IP 等不必要信息，绝不上传 Secret、完整 Cookie / access token 或原始凭据 CSV。

身份绕过、任务泄露或凭据泄露等安全问题先通过组织私密渠道报告并轮换受影响凭据，不在公开 Issue 发布利用细节；仓库未提供单独 SECURITY 策略文件或专用安全联系人。

### 版本迭代与运行维护

建议采用语义化版本：修复为 patch、向后兼容新增为 minor、接口 / 数据口径不兼容为 major。仓库当前未定义应用版本号、自动发布流程或变更日志机制；发布时记录代码版本、依赖解析结果、配置及 Azure API 版本、验收范围和回滚方案，而不是假定已有版本治理。

定期检查 Entra Secret 到期、依赖与预览 API 兼容性、日志磁盘与 SQLite 保留策略、浏览器敏感缓存；在隔离环境验证密钥轮换和恢复。不要通过随意提升并发掩盖限流，不将本地审计数据库当作业务备份，不将离线模拟测试当作生产验收。
