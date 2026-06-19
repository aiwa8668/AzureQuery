# Azure 费用查询

一个面向本地/内部使用的 Azure 成本查询 Web 工具。项目使用 Flask 提供后端代理接口，前端使用原生 HTML/CSS/JavaScript 实现单页查询界面。用户在浏览器中维护一组或多组 Azure 应用凭据，后端通过 Client Credentials 获取 Azure Management API 令牌，并调用 Azure Cost Management Query API 汇总订阅、资源和每日成本数据。

## 当前实现的主要功能

- 单页 Web 界面：`/` 为查询页面入口。
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
- 日志记录：后端日志输出到控制台和 `logs/azure-cost-query.log`，日志文件滚动保留。

## 技术栈

- 后端：Python、Flask、requests
- 前端：Jinja2 模板、原生 HTML/CSS/JavaScript
- 配置：服务端 `config/sync.config`，浏览器端 `localStorage`
- Azure API：Microsoft Entra ID OAuth 2.0 Client Credentials、Azure Subscriptions API、Azure Cost Management Query API

## 目录结构

```text
AzureSubCost/
├── app.py                       # Flask 应用、Azure API 调用、成本聚合、CSV 导出
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

- 启动服务：

```powershell
python app.py
```

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
python -m py_compile app.py
python -c "from app import app; c=app.test_client(); print('/', c.get('/').status_code); print('/favicon.ico', c.get('/favicon.ico').status_code)"
```

期望结果：

- `/` 返回 `200`
- `/favicon.ico` 返回 `204`
