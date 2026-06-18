# Azure 成本统计分析网站

一个基于 Flask + 原生 HTML/CSS/JavaScript 的 Azure 成本查询与分析工具。应用通过 Azure Client Credentials 获取访问令牌，由后端代理调用 Azure Cost Management API，前端负责配置保存、查询条件、结果展示、筛选和 CSV 导出。

## 功能概览

- 单页查询界面：`/` 和 `/query` 均进入主页面，`/settings` 兼容重定向到 `/query`。
- 设置弹窗：配置 Azure 认证参数、可选单个 Subscription ID、查询并发和超时参数。
- 成本查询：支持“上月”“本月”“昨天”“自定义范围”。
- 查询优化：先查询订阅汇总成本；当订阅汇总为 `0` 时跳过明细查询，减少 Azure Cost Management API 调用。
- 统计展示：统计概览、订阅维度、按天维度、资源/产品维度、详单维度。
- 表格筛选：所有表格的 `Cost` 列默认筛选 `>0`，支持列筛选、搜索、全选和清除。
- CSV 导出：支持按当前页签下载订阅、按天、资源/产品、详单 CSV。
- 本地缓存：查询结果缓存在浏览器 localStorage，刷新后自动恢复最近一次结果。
- 主题切换：支持浅色/深色主题，选择保存在 localStorage。
- 本地数据删除：可一键删除本地认证参数和查询缓存。

## 技术栈

- 后端：Python、Flask、requests
- 前端：Jinja2 模板、原生 HTML/CSS/JavaScript
- 配置：服务器侧 `config/sync.config`，浏览器侧 localStorage

## 目录结构

```text
subcost/
├── app.py                       # Flask 应用、Azure API 代理、查询聚合、CSV 导出
├── requirements.txt             # Python 依赖
├── README_AzureCost.md          # 项目说明
├── config/
│   └── sync.config              # 并发、连接池、超时、重试参数
├── public/
│   ├── css/
│   │   └── styles.css           # 页面样式与主题
│   └── js/
│       ├── query.js             # 查询页交互、筛选、缓存、设置弹窗
│       └── storage.js           # 浏览器配置存取
└── templates/
    └── index.html               # 单页应用模板
```

## 环境要求

- Python 3.10+ 建议使用。当前代码使用现代类型注解写法。
- 可访问 Azure 登录端点和 Azure Management API。
- Azure 应用注册需要具备读取订阅及 Cost Management 查询所需权限。

## 安装与启动

1. 创建并激活虚拟环境：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

1. 安装依赖：

```powershell
pip install -r requirements.txt
```

1. 启动服务：

```powershell
python app.py
```

1. 打开页面：

- 主页面：[http://localhost:5100/](http://localhost:5100/)
- 查询页：[http://localhost:5100/query](http://localhost:5100/query)
- `/settings` 会重定向到 `/query`

查询过程日志会同时输出到控制台和本地日志文件：

```text
logs/azure-cost-query.log
```

日志文件采用滚动写入，单文件最大约 5 MB，最多保留 5 个备份文件。

## 使用方式

### 1. 配置认证参数

点击页面左上角设置按钮，填写认证参数：

- `AZURE_CLIENT_ID`：Azure 应用注册的 Application client ID。
- `AZURE_CLIENT_SECRET`：客户端密码的 Secret Value，不是 Secret ID。
- `AZURE_TENANT_ID`：租户 ID。
- `Subscription ID`：可选。填写后只查询该订阅；不填则查询当前身份可见的订阅列表。

`AZURE_CLIENT_SECRET` 在输入框中会以“前 3 位 + 中间 * + 后 3 位”的方式显示。保存时会保存真实值；如果检测到保存的是掩码值，会阻止查询并提示重新输入真实 Secret Value。

### 2. 配置并发控制

设置弹窗中的并发控制参数会保存到服务器侧 `config/sync.config`，查询时立即生效：

| 参数 | 说明 |
| --- | --- |
| `max_workers` | 订阅查询并发数。时间范围较长时后端会自动降低有效并发，减少 429 限流。 |
| `http_pool_connections` | HTTP 连接池分组数。 |
| `http_pool_maxsize` | HTTP 连接池最大连接数，应大于等于并发数。 |
| `subscription_list_timeout_seconds` | 获取订阅列表接口超时秒数。 |
| `cost_query_timeout_seconds` | 单个成本查询接口超时秒数。 |
| `cost_query_max_retries` | 429 限流最大重试次数。总尝试次数为 `1 + cost_query_max_retries`。 |
| `cost_query_retry_base_seconds` | 未返回 `Retry-After` 时的退避基数秒数。 |

### 3. 查询成本

选择时间范围后点击“查询”。支持：

- 上月
- 本月
- 昨天
- 自定义范围

查询成功后会展示：

- 统计概览：时间范围、总费用、订阅数
- 订阅：按订阅汇总成本
- 按天：按日期/订阅汇总成本
- 资源：按订阅、服务、产品汇总成本
- 详单：按日期、订阅、服务、产品展示明细成本

查询状态、缓存恢复提示和错误信息显示在“Azure成本统计”标题下方，并会自动换行，不会撑出页面。

### 4. 筛选与导出

- 所有表格的 `Cost` 列默认选择 `>0`。
- 每个筛选列支持下拉、搜索、全选、清除。
- 当前页签右侧“下载”按钮会导出对应维度 CSV。
- 按天 CSV 由前端根据当前结果生成；其它维度通过 `/api/costs/export` 导出。

### 5. 删除本地数据

设置弹窗底部的“删除数据”按钮会在确认后删除：

- 本地认证参数设置：`azure-cost-analyzer-config-v1`
- 本地查询缓存：`azure-cost-analyzer-query-cache-v1`
- 当前页面结果和筛选状态

## 查询逻辑

后端查询入口为 `POST /api/costs/query`。

整体流程：

1. 校验日期范围和认证参数。
2. 使用 `AZURE_CLIENT_ID`、`AZURE_CLIENT_SECRET`、`AZURE_TENANT_ID` 获取 Azure 访问令牌。
3. 拉取当前身份可见订阅列表；如填写了 Subscription ID，则只保留该订阅。
4. 对每个订阅先查询整个时间范围内的汇总成本。
5. 若汇总成本为 `0`，跳过该订阅明细查询。
6. 若汇总成本大于 `0`，查询 Daily 粒度并按 `ResourceId`、`ResourceLocation`、`ServiceName`、`Product` 分组的明细，并从 `ResourceId` 解析资源名称。
7. 对明细结果聚合生成订阅、资源/产品、日期详单等维度。
8. 如遇 Azure 429，按配置重试；明细查询支持按日期范围递归拆分降级。

订阅汇总查询时间范围来自页面选择：

```text
from: YYYY-MM-DDT00:00:00Z
to:   YYYY-MM-DDT23:59:59Z
```

成本口径为 `ActualCost` + `PreTaxCost` 的 `Sum`。

## API 说明

### `POST /api/costs/query`

查询 Azure 成本。

请求示例：

```json
{
  "startDate": "2026-06-01",
  "endDate": "2026-06-13",
  "auth": {
    "azureClientId": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
    "azureClientSecret": "真实 Secret Value",
    "azureTenantId": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
    "subscriptionId": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
  }
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
      "subscriptionId": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
      "subscriptionName": "Production",
      "cost": 500.12
    }
  ],
  "byResourceType": [
    {
      "subscription": "Production",
      "resourceName": "vm-prod-01",
      "location": "eastus",
      "serviceName": "Virtual Machines",
      "cost": 320.45
    }
  ],
  "byDate": [
    {
      "date": "2026-06-01",
      "subscription": "Production",
      "resourceName": "vm-prod-01",
      "product": "Virtual Machines D Series",
      "cost": 45.67
    }
  ]
}
```

如果部分日期范围因限流失败，响应中可能包含 `warnings` 字段。

### `POST /api/costs/export`

导出 CSV。

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

## 浏览器本地存储

| Key | 用途 |
| --- | --- |
| `azure-cost-analyzer-config-v1` | Azure 认证参数和可选 Subscription ID。 |
| `azure-cost-analyzer-query-cache-v1` | 最近一次查询结果、时间范围和缓存时间。 |
| `color-theme` | 主题偏好，取值为 `light` 或 `dark`。 |

这些数据只保存在当前浏览器、当前用户 Profile、当前站点来源下。更换浏览器、清理站点数据或修改访问端口后需要重新配置。

## Azure 权限要求

应用注册和服务主体至少需要满足：

- 能通过 Client Credentials 获取 `https://management.azure.com/.default` 令牌。
- 对目标订阅具备读取订阅信息的权限。
- 对目标订阅具备 Cost Management 查询权限。

常见问题：

- `AADSTS7000215 Invalid client secret`：通常是填写了 Secret ID，而不是 Secret Value；也可能是本地保存了掩码值，需要重新输入真实 Secret Value。
- `未找到匹配的 Subscription ID`：当前身份不可见该订阅，或 Subscription ID 填写错误。
- `429`：Azure Cost Management API 限流，可降低并发或稍后重试。

## 安全说明

当前实现会把 `AZURE_CLIENT_SECRET` 保存在浏览器 localStorage，适合本地工具、内部工具或 PoC。生产环境建议改为：

- Secret 不进入前端。
- 使用后端安全存储或 Azure Key Vault。
- 前端只保存非敏感配置和 UI 偏好。

## 验证命令

```powershell
python -m py_compile app.py
python -c "from app import app; c=app.test_client(); print('/', c.get('/').status_code); print('/query', c.get('/query').status_code); r=c.get('/settings'); print('/settings', r.status_code, r.headers.get('Location'))"
```

期望结果：

- `/` 返回 `200`
- `/query` 返回 `200`
- `/settings` 返回 `302 /query`
