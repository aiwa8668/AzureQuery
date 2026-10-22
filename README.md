# Azure 费用查询

一个面向本地/内部使用的 Azure 成本查询 Web 工具。项目使用 Flask 提供后端代理接口，前端使用原生 HTML/CSS/JavaScript 实现单页查询界面。用户在浏览器中维护一组或多组 Azure 应用凭据，后端通过 Client Credentials 获取 Azure Management API 令牌，并调用 Azure Cost Management Query API 汇总订阅、资源和每日成本数据。

## 当前实现的主要功能

- 单页 Web 界面：`/` 为查询页面入口。
- 左侧导航工作台：支持在“Azure费用查询”和“OpenAI Tier查询”两个功能视图间切换。
- 多组 Azure 凭据：支持保存多行 `APP_Client_ID`、`APP_Client_Secret`、`Tenant_ID`、`Tenant_Name` 和可选 `Subscription_ID`。
- 跨租户/跨订阅查询：同一次查询可以处理多组认证配置，并合并成功返回的数据。
- 订阅发现与可选订阅过滤：未填写 `Subscription_ID` 时查询当前服务主体可见的全部订阅；填写后只查询该订阅。
- 时间范围快捷选择：支持上月、本月、本季度、本年度、昨天和自定义日期范围。
- 费用查询优化：每个订阅先查询时间范围汇总成本，汇总为 `0` 时跳过明细查询，减少 Cost API 调用。
- Azure Cost API 限流处理：支持 429 重试、指数退避、随机抖动、全局并发闸门、租户级并发闸门，以及明细查询按日期范围递归拆分降级。
- 结果展示：提供统计概览，以及“订阅”“按天”“资源”“详单”四个维度表格。
- 表格交互：表格支持行选中、列筛选、搜索、全选/清除筛选；`Cost` 列默认筛选 `>0`。
- CSV 导出：订阅、资源、详单维度由后端导出；按天维度由前端根据当前结果本地生成。
- 认证配置导入/导出：支持 CSV 导入导出认证参数。
- 本地缓存：认证参数、最近一次查询结果、主题偏好保存在浏览器 `localStorage`。
- 深色/浅色主题切换：主题偏好保存在当前浏览器。
- OpenAI Tier 查询：基于 Microsoft Entra 授权码登录，按当前登录用户可见的 Enabled 订阅查询 `quotaTiers` 信息，结果字段与 `check-quota-tiers.sh` 保持一致。
- 日志记录：后端日志输出到控制台和 `logs/azure-cost-query.log`，日志文件滚动保留。

## 技术栈

- 后端：Python、Flask、requests
- 前端：Jinja2 模板、原生 HTML/CSS/JavaScript
- 配置：服务端 `config/sync.config`，浏览器端 `localStorage`
- 配置：服务端 `config/sync.config` 和项目根目录 `.env`，浏览器端 `localStorage`
- Azure API：Microsoft Entra ID OAuth 2.0 Client Credentials、Azure Subscriptions API、Azure Cost Management Query API

## 目录结构

```text
AzureSubCost/
├── main.py                      # Flask 应用、Azure API 调用、成本聚合、CSV 导出
├── .env                         # 本地服务端配置（自动加载，不提交真实密钥）
├── .env.example                 # .env 模板
├── README.md                    # 项目说明
├── requirements.txt             # Python 依赖
├── config/
│   └── sync.config              # 查询并发、HTTP 连接池、超时和重试配置
├── logs/                        # 运行日志目录
│   └── azure-cost-query.log     # 运行后写入的滚动日志文件
├── public/
│   ├── css/
│   │   └── styles.css           # 页面样式和主题样式
│   └── js/
│       ├── query.js             # 查询交互、表格渲染、筛选、导出、缓存、设置弹窗
│       └── storage.js           # 浏览器认证配置读写
└── templates/
    └── index.html               # 单页应用模板
```

## 环境要求

- Python 3.10+。
- 当前机器可访问：
  - `https://login.microsoftonline.com`
  - `https://management.azure.com`
- Azure 应用注册/服务主体具备目标订阅的读取和 Cost Management 查询权限。

### OpenAI Tier 查询额外配置

服务端需要配置 Microsoft Entra 应用注册，用于浏览器登录并代表用户调用 Azure 管理面 API。

必需配置写入项目根目录 `.env`：

```dotenv
FLASK_SECRET_KEY=replace-with-a-random-secret
OPENAI_TIER_CLIENT_ID=your-app-client-id
OPENAI_TIER_CLIENT_SECRET=your-app-client-secret
OPENAI_TIER_AUTHORITY=https://login.microsoftonline.com/organizations
```

可选环境变量：

```dotenv
OPENAI_TIER_REDIRECT_URI=http://localhost:5100/auth/openai/callback
OPENAI_TIER_API_VERSION=2026-01-15-preview
```

项目已内置 `.env` 自动加载逻辑。启动前直接编辑根目录 `.env` 即可；如果要重置模板，可参考 `.env.example`。

应用注册需要：

- Web 重定向 URI 指向 `/auth/openai/callback`
- 已添加 Azure Service Management 的委托权限 `user_impersonation`
- 用户或管理员已完成同意

## 安装与启动

- 创建并激活虚拟环境：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

- 安装依赖：

```powershell
pip install -r requirements.txt
```

- 编辑根目录 `.env`，填入你自己的 Microsoft Entra 应用配置。

- 启动服务：

```powershell
python main.py
```

Flask 应用入口文件为 `main.py`，WSGI 导入目标为 `main:app`（即 `from main import app`）。正常导入 `main` 会注册 Azure Diagnostics 路由，无需额外调用路由注册函数。

- 打开页面：

  - 主页面：[http://localhost:5100/](http://localhost:5100/)

默认监听地址为 `0.0.0.0:5100`，调试模式关闭。

## 使用方式

### 1. 配置认证参数

点击页面左上角齿轮按钮，在“认证参数设置”中填写一行或多行认证参数。

| 界面字段 | 后端字段 | 说明 | 必填 |
| --- | --- | --- | --- |
| `APP_Client_ID` | `azureClientId` | Azure 应用注册的 Application/Client ID。 | 是 |
| `APP_Client_Secret` | `azureClientSecret` | 客户端密码的 Secret Value，不是 Secret ID。 | 是 |
| `Tenant_ID` | `azureTenantId` | Microsoft Entra 租户 ID。 | 是 |
| `Tenant_Name` | `tenantName` | 租户显示名，用于结果展示和租户级限流分组。 | 是 |
| `Subscription_ID` | `subscriptionId` | 可选。填写后只查询该订阅；不填则查询当前身份可见的全部订阅。 | 否 |

设置弹窗支持：

- 添加行：新增一组认证参数。
- 删除：删除当前行。
- 保存认证参数：保存到浏览器 `localStorage`。
- 导出：导出当前认证参数 CSV，文件名格式为 `azure-auth-config-YYYYMMDDHHmm.csv`。
- 导入：从 CSV 导入认证参数并保存。
- 删除本地缓存：清除认证参数、旧版单组配置、查询结果缓存和当前页面结果。

导入 CSV 表头要求：

```text
App_Client_ID,App_Client_Secret,Tenant_ID,Tenant_Name,Subscription_ID
```

其中 `Subscription_ID` 可选。当前 CSV 解析采用简单英文逗号分隔，字段值不应包含逗号。

保存、导出和导入时会校验：

- 至少存在一行完整的必填认证参数。
- 必填字段不能为空。
- `APP_Client_Secret` 不能是掩码值。
- 多行配置中任意非空字段值不能重复。

> 注意：Secret 会保存在浏览器 `localStorage`。使用后建议点击“删除本地缓存”清理敏感数据。

### 2. 查询 Azure 成本

选择时间范围后点击“查询”。可选时间范围包括：

- 上月
- 本月
- 本季度
- 本年度
- 昨天
- 自定义范围

查询成功后页面展示：

- 统计概览：时间范围、总费用、订阅数。
- 订阅：按租户、订阅 ID、订阅名称汇总成本。
- 按天：前端根据后端详单按日期、租户、订阅聚合，并补充订阅 ID。
- 资源：按资源名称、租户、订阅、区域、服务名称汇总成本。
- 详单：按日期、租户、订阅、资源名称、产品展示成本明细。

如果部分认证或部分订阅查询失败，成功部分仍会展示；页面顶部状态区域会显示 `warnings` 或 `authWarnings`。

### 3. 筛选与导出

- 表格首列为序号，点击数据行会高亮选中。
- 表头筛选支持搜索、勾选、全选和清除。
- `Cost` 列筛选项为 `=0` 与 `>0`，初始化时默认只展示 `>0` 数据。
- 当前维度右侧“下载”按钮会导出该维度 CSV。
- 后端支持导出 `subscription`、`resourceType`、`date` 和 `all`。
- 前端“按天”维度使用 `daily`，CSV 在浏览器中本地生成，不调用后端导出接口。

### 4. 本地缓存

浏览器会保存以下数据：

| localStorage Key | 用途 |
| --- | --- |
| `azure-cost-analyzer-auth-configs-v1` | 多组 Azure 认证参数。 |
| `azure-cost-analyzer-config-v1` | 旧版单组配置 Key，当前仅用于兼容清理。 |
| `azure-cost-analyzer-query-cache-v1` | 最近一次查询结果、查询日期范围和缓存时间。 |
| `color-theme` | 主题偏好，取值为 `light` 或 `dark`。 |

这些数据只保存在当前浏览器、当前用户 Profile、当前站点来源下。更换浏览器、清理站点数据或修改访问端口后需要重新配置。

## 查询逻辑

后端查询入口为 `POST /api/costs/query`。

整体流程：

1. 校验 `startDate`、`endDate` 和每组认证参数。
2. 对每组认证参数使用 `azureClientId`、`azureClientSecret`、`azureTenantId` 获取 Azure 访问令牌。
3. 调用订阅列表接口获取当前服务主体可见订阅。
4. 如配置了 `subscriptionId`，只保留匹配订阅；未匹配时返回错误。
5. 对每个订阅先查询整个时间范围内的汇总成本。
6. 若订阅汇总成本为 `0`，跳过该订阅明细查询。
7. 若订阅汇总成本大于 `0`，查询 Daily 粒度，并按 `ResourceId`、`ResourceLocation`、`ServiceName`、`Product` 分组。
8. 从 `ResourceId` 解析资源名称。
9. 后端聚合生成 `summary`、`bySubscription`、`byResourceType`、`byDate`。
10. 多组认证查询完成后合并成功结果；失败认证以 `authWarnings` 返回。
11. 明细查询遇到持续 429 限流时，按日期范围递归拆分重试；仍失败的日期段以 `warnings` 返回。

查询时间范围格式：

```text
from: YYYY-MM-DDT00:00:00Z
to:   YYYY-MM-DDT23:59:59Z
```

成本口径：

```text
type: ActualCost
aggregation: PreTaxCost Sum
summary query: 无 granularity/grouping
detail query: granularity = Daily，grouping = ResourceId / ResourceLocation / ServiceName / Product
```

## 并发与限流配置

后端每次成本查询都会重新读取 `config/sync.config`。修改配置后，下一次查询即可生效，无需重启服务。

| 参数 | 当前配置示例 | 说明 |
| --- | ---: | --- |
| `max_workers` | `1` | 单次查询内订阅级线程池最大工作线程数，实际值还会受订阅数量和查询天数限制。 |
| `http_pool_connections` | `24` | requests HTTP 连接池分组数。 |
| `http_pool_maxsize` | `96` | 每个连接池允许的最大连接数。 |
| `subscription_list_timeout_seconds` | `60` | 获取订阅列表接口超时秒数。 |
| `cost_query_timeout_seconds` | `120` | 单次 Cost API 查询超时秒数，汇总、明细、分页查询均使用。 |
| `cost_query_max_retries` | `7` | 429 限流后的最大重试次数；总尝试次数为 `1 + cost_query_max_retries`。 |
| `cost_query_retry_base_seconds` | `4` | 未返回 `Retry-After` 时的指数退避基础秒数。 |
| `cost_query_retry_max_delay_seconds` | `60` | 单次重试等待的最大秒数。 |
| `global_cost_api_max_inflight` | `4` | 进程级全局在途 Cost API 请求上限。 |
| `tenant_cost_api_max_inflight` | `1` | 单租户在途 Cost API 请求上限。 |

订阅级并发会根据查询天数自动降级：

- 查询天数 `>= 31`：最多 1 个订阅并发。
- 查询天数 `>= 14` 且 `< 31`：最多 2 个订阅并发。
- 查询天数 `< 14`：使用 `max_workers`。

## API 说明

### `GET /api/azure-diagnostics/resources`

所有模式均要求有效的 Microsoft 登录，并重新校验所选订阅的当前访问权限。`subscriptionIds` 为可重复的查询参数，也兼容 `subscriptionId`；最多 100 项，规范化并去重。

| `phase` | 请求参数 | 响应 `resources` |
| --- | --- | --- |
| 不传或 `full` | 所选订阅 | 保持原完整账号与诊断字段，无新增顶层字段。 |
| `accounts` | 所选订阅 | 仅读取 OpenAI/AIServices 账号，跳过诊断设置；`diagnosticStatus=pending`，日志开关和工作区字段为 `null`，分类列表为空。 |
| `diagnostics` | 所选订阅及可重复的 `resourceIds` | 仅读取指定账号的诊断设置；返回 `id` 与原诊断字段，不重新读取账号列表。 |

两阶段接入时先请求 `phase=accounts` 展示账号，再将返回的 ID 分批（每批 1–100 项）传给 `phase=diagnostics`，按 `id` 合并结果。第二阶段校验所有 ID 的格式、账号资源类型及所选订阅归属后才调度请求；它不依赖第一阶段缓存，资源读取权限仍由 ARM 校验。未查询的 `pending` 不代表诊断日志关闭；诊断设置读取返回 401/403 时仍为 `unknown`，429 原样传递，其余上游错误为 502，不返回上游敏感内容。

工作区发现、账号发现和诊断设置使用有界调度：单个请求的活动任务及待完成任务最多为 `min(max(1, max_workers), 8)`。每个工作线程独占并复用 HTTP Session，分页和后续任务沿用连接；完整模式在账号与诊断阶段间复用同一线程池。请求结束或失败后等待在途任务并关闭所有 Session，不在用户、请求之间共享认证会话。列表仍按资源 ID 排序，分页地址、版本和页数限制保持不变。当前 JS 未接入两阶段模式，继续使用默认完整响应。

### `POST /api/azure-diagnostics/query`

查询 `AzureDiagnostics`，保留时间范围及 `MICROSOFT.COGNITIVESERVICES` 提供商限制，不再限制 `Category` 为 `AzureOpenAIRequestUsage`，也不按 `RequestId` 执行 `summarize arg_max` 去重。同一 ID 的不同操作、不同 IP 以及重复日志均独立返回；原始字段的值、空字符串、`null` 和 `ResourceId` / `_ResourceId` 大小写不被资源过滤覆盖。

资源过滤使用内部 `_ResourceIdFilter`（优先读取 `_ResourceId`，列不存在时读取 `ResourceId`），以不区分大小写的方式限定所选订阅及可选资源。查询通过 `project-away p, _ResourceIdFilter` 移除内部字段，其余原始列及现有 Token、模型、操作、关联 ID、请求 ID 计算字段保留，计算口径不变。

有效 Microsoft 登录、当前订阅权限、工作区及资源类型/订阅归属校验保持不变；日期及时区校验、最多 31 天、未来结束时间限制及 UTC 时间过滤保持不变。明细按时间倒序返回，每个工作区最多 5000 条（查询 5001 条用于检测截断），截断及跨工作区重复提示保持不变。与 Portal 对照时仍需使用相同工作区、时间和资源范围，并注意明细截断。

离线回归（屏蔽 `.env` 读取及网络连接，认证和 ARM 响应均模拟；断言无 Category 过滤/RequestId 去重、资源过滤不覆盖原始 ID、内部字段不外泄，同 ID 不同操作/IP 及原值/空值全部保留，并覆盖计算字段、权限、时间和明细上限）：

```powershell
python tests/run_offline.py
```

包含独立进程中的入口路由注册烟雾测试：正常导入 `main`，不手动注册路由、不模拟认证函数；使用虚拟登录配置但不设置登录会话，验证页面及认证状态接口返回 `200`，诊断工作区、资源 GET 和查询 POST 返回 `401` JSON，而不是 `404`。测试屏蔽 `.env` 读取和网络连接，不验证线上部署状态。

### `POST /api/costs/query`

查询 Azure 成本。前端以 `authList` 传递多组认证参数；后端也兼容单个 `auth` 参数。

请求示例：

```json
{
  "startDate": "2026-06-01",
  "endDate": "2026-06-13",
  "authList": [
    {
      "azureClientId": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
      "azureClientSecret": "真实 Secret Value",
      "azureTenantId": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
      "tenantName": "Production Tenant",
      "subscriptionId": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
    }
  ]
}
```

响应示例：

```json
{
  "summary": {
    "currency": "USD",
    "totalCost": 1234.56
  },
  "bySubscription": [
    {
      "tenantName": "Production Tenant",
      "subscriptionId": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
      "subscriptionName": "Production",
      "cost": 500.12
    }
  ],
  "byResourceType": [
    {
      "tenantName": "Production Tenant",
      "subscription": "Production",
      "resourceName": "vm-prod-01",
      "location": "eastus",
      "serviceName": "Virtual Machines",
      "cost": 320.45
    }
  ],
  "byDate": [
    {
      "tenantName": "Production Tenant",
      "date": "2026-06-01",
      "subscription": "Production",
      "resourceName": "vm-prod-01",
      "product": "Virtual Machines D Series",
      "cost": 45.67
    }
  ],
  "warnings": [],
  "authWarnings": []
}
```

### `POST /api/costs/export`

根据当前结果导出 CSV。前端会将完整查询结果通过 `data` 传回后端。

请求示例：

```json
{
  "dimension": "subscription",
  "data": {
    "summary": {},
    "bySubscription": [],
    "byResourceType": [],
    "byDate": []
  }
}
```

`dimension` 支持：

- `subscription`
- `resourceType`
- `date`
- `all`

后端返回带 UTF-8 BOM 的 CSV，便于 Excel 打开中文内容。

## Azure 权限要求

应用注册和服务主体至少需要满足：

- 能通过 Client Credentials 获取 `https://management.azure.com/.default` 令牌。
- 对目标订阅具备读取订阅列表/订阅信息的权限。
- 对目标订阅具备 Azure Cost Management 查询权限。

常见可用角色组合取决于企业权限模型。通常需要在订阅或管理组范围授予可读取订阅和成本数据的角色，例如 Reader、Cost Management Reader，或具备等效权限的自定义角色。

## 常见问题

- `AADSTS7000215 Invalid client secret`：通常是填写了 Secret ID，而不是 Secret Value；也可能是本地保存了掩码值，需要重新输入真实 Secret Value。
- `未找到匹配的 Subscription ID`：当前服务主体不可见该订阅，或 `Subscription_ID` 填写错误。
- `查询订阅汇总成本触发限流` / `429`：Azure Cost Management API 限流。可降低 `max_workers`、`global_cost_api_max_inflight`、`tenant_cost_api_max_inflight`，缩短时间范围，或稍后重试。
- 页面提示部分认证/订阅异常：表示至少一组认证或部分时间段失败，但其它成功结果已正常返回。
- 查询结果为空：可能是目标日期范围无成本、服务主体无权限、订阅过滤不匹配，或 `Cost` 默认筛选了 `>0`。

## 安全说明

当前实现会把 `APP_Client_Secret` 保存在浏览器 `localStorage`，更适合本地工具、内部受控环境或 PoC。生产化建议：

- 不让 Secret 进入前端。
- 使用服务端安全存储或 Azure Key Vault 保存敏感配置。
- 前端仅保存非敏感配置和 UI 偏好。
- 为服务主体配置最小必要权限。
- 配合 HTTPS、访问控制、审计日志和密钥轮换策略。

## 验证命令

```powershell
python -m py_compile main.py
python tests/run_offline.py
```

期望结果：

- `/` 返回 `200`
- `/favicon.ico` 返回 `204`
- 无登录会话且登录配置完整时，`/api/openai-tier/auth/status` 返回 `200`、`signedIn=false`
- 同样条件下，`GET /api/azure-diagnostics/workspaces`、`GET /api/azure-diagnostics/resources` 和 `POST /api/azure-diagnostics/query` 均返回 `401` JSON，不是 `404`
