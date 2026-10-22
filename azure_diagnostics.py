from __future__ import annotations

import importlib
import re
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, urlsplit

import requests
from flask import Flask, jsonify, request

from azure_token_usage import build_timespan_range, parse_tz

ARM_ENDPOINT = "https://management.azure.com"
COGNITIVE_API_VERSION = "2024-10-01"
DIAGNOSTIC_SETTINGS_API_VERSION = "2021-05-01-preview"
USAGE_LOG_CATEGORY = "AzureOpenAIRequestUsage"
WORKSPACES_API_VERSION = "2023-09-01"
QUERY_API_VERSION = "2017-10-01"
MAX_QUERY_DAYS = 31
MAX_ITEMS = 100
MAX_PAGES = 100
MAX_LIST_WORKERS = 8
MAX_DETAIL_ROWS = 5000
SUBSCRIPTION_PATTERN = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")
RESOURCE_PATTERN = re.compile(
    r"/subscriptions/([^/]+)/resourceGroups/([\w.()-]+)/providers/"
    r"(Microsoft\.OperationalInsights/workspaces|Microsoft\.CognitiveServices/accounts)/([\w.()-]+)",
    re.IGNORECASE,
)


def normalize_ids(raw: Any, field: str, backend: Any) -> list[str]:
    if not isinstance(raw, list) or not raw or len(raw) > MAX_ITEMS:
        raise backend.HttpError(400, f"{field} 必须是包含 1 至 {MAX_ITEMS} 项的列表")
    if any(not isinstance(item, str) or not item.strip() for item in raw):
        raise backend.HttpError(400, f"{field} 包含无效值")
    return list(dict.fromkeys(item.strip().lower() for item in raw))


def validate_resource_id(value: str, subscriptions: list[str], kind: str, backend: Any) -> str:
    match = RESOURCE_PATTERN.fullmatch(value)
    if not match or match.group(3).lower() != kind.lower():
        raise backend.HttpError(400, f"资源 ID 格式或资源类型无效：应为 {kind}")
    if match.group(1).lower() not in subscriptions:
        raise backend.HttpError(403, "资源不属于所选订阅")
    return value.lower()


def validate_timespan(payload: dict[str, Any], backend: Any) -> tuple[str, str, str]:
    start = payload.get("startDate")
    end = payload.get("endDate")
    tz_text = payload.get("tz", "+8")
    if not isinstance(tz_text, str) or not re.fullmatch(r"[+-]?\d{1,2}(?::[0-5]\d)?", tz_text):
        raise backend.HttpError(400, "tz 必须是有效的 UTC 偏移，如 +8 或 +05:30")
    if any(not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) for value in (start, end)):
        raise backend.HttpError(400, "startDate 和 endDate 必须为 YYYY-MM-DD")
    try:
        tz = parse_tz(tz_text)
        timespan, start_utc, end_utc = build_timespan_range(start, end, tz)
    except (ValueError, OverflowError) as exc:
        raise backend.HttpError(400, "日期范围或时区无效") from exc
    if end_utc - start_utc > timedelta(days=MAX_QUERY_DAYS):
        raise backend.HttpError(400, f"查询范围不能超过 {MAX_QUERY_DAYS} 天")
    now = datetime.now(timezone.utc)
    if datetime.strptime(end, "%Y-%m-%d").date() > now.astimezone(tz).date():
        raise backend.HttpError(400, "结束日期不能晚于当前日期")
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return timespan, start_utc.strftime(fmt), min(end_utc, now).strftime(fmt)


def build_query(resource_ids: list[str], start_utc: str, end_utc: str, subscriptions: list[str]) -> str:
    resources = ", ".join(f"'{item}'" for item in resource_ids)
    subscription_filter = ", ".join(f"'{item}'" for item in subscriptions)
    resource_filter = f"| where _ResourceIdFilter in~ ({resources})" if resources else ""
    return f"""AzureDiagnostics
| where TimeGenerated >= datetime({start_utc}) and TimeGenerated < datetime({end_utc})
| where ResourceProvider =~ 'MICROSOFT.COGNITIVESERVICES'
| extend _ResourceIdFilter = tolower(tostring(column_ifexists('_ResourceId', column_ifexists('ResourceId', ''))))
| where tostring(split(_ResourceIdFilter, '/')[2]) in~ ({subscription_filter})
{resource_filter}
| extend p = parse_json(tostring(column_ifexists('properties_s', '{{}}')))
| extend PromptTokens = coalesce(tolong(p.promptTokens[0]), long(0)),
         CachedTokens = coalesce(tolong(p.cachedTokens[0]), long(0)),
         CacheWriteTokens = coalesce(tolong(p.cacheWriteTokens[0]), long(0)),
         GeneratedTokens = coalesce(tolong(p.generatedTokens[0]), long(0)),
         LongContextPromptTokens = coalesce(tolong(p.longContextPromptTokens[0]), long(0)),
         LongContextCachedTokens = coalesce(tolong(p.longContextCachedTokens[0]), long(0)),
         LongContextCacheWriteTokens = coalesce(tolong(p.longContextCacheWriteTokens[0]), long(0)),
         LongContextGeneratedTokens = coalesce(tolong(p.longContextGeneratedTokens[0]), long(0)),
         CorrelationId = tostring(column_ifexists('CorrelationId', '')),
         OperationName = tostring(column_ifexists('OperationName', '')),
         Deployment = tostring(p.modelDeploymentName),
         Model = tostring(p.modelName),
         ModelVersion = tostring(p.modelVersion)
| extend InputTokens = PromptTokens,
         OutputTokens = GeneratedTokens,
         CachedInputTokens = CachedTokens,
         RequestId = coalesce(CorrelationId, tostring(p.requestId))
| extend UncachedInputTokens = InputTokens - CachedInputTokens
| extend RequestId = iff(isnotempty(RequestId), RequestId, hash_sha256(tostring(pack_array(_ResourceIdFilter, TimeGenerated, OperationName, Deployment, Model, ModelVersion, PromptTokens, CachedTokens, CacheWriteTokens, GeneratedTokens, LongContextPromptTokens, LongContextCachedTokens, LongContextCacheWriteTokens, LongContextGeneratedTokens))))
| project-away p, _ResourceIdFilter
| order by TimeGenerated desc, RequestId asc
| take {MAX_DETAIL_ROWS + 1}"""


def arm_request(http_session: Any, method: str, url: str, token: str, timeout: int, backend: Any, **kwargs: Any) -> dict[str, Any]:
    try:
        response = getattr(http_session, method)(
            url, headers={"Authorization": f"Bearer {token}"}, timeout=timeout,
            allow_redirects=False, **kwargs,
        )
    except requests.RequestException as exc:
        raise backend.HttpError(502, "ARM 诊断接口连接失败") from exc
    if response.status_code != 200:
        status = response.status_code if response.status_code in (401, 403, 429) else 502
        raise backend.HttpError(status, f"ARM 诊断接口请求失败（HTTP {response.status_code}）")
    try:
        body = response.json()
    except ValueError as exc:
        raise backend.HttpError(502, "ARM 诊断接口返回无效 JSON") from exc
    if not isinstance(body, dict):
        raise backend.HttpError(502, "ARM 诊断接口返回无效数据")
    return body


class BoundedSessionPool:
    def __init__(self, config: Any, backend: Any):
        self.config = config
        self.backend = backend
        self.workers = min(MAX_LIST_WORKERS, max(1, config.max_workers))
        self.local = threading.local()
        self.lock = threading.Lock()
        self.sessions = ExitStack()

    def __enter__(self):
        self.executor = ThreadPoolExecutor(max_workers=self.workers)
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any):
        try:
            self.executor.shutdown(wait=True, cancel_futures=True)
        finally:
            self.sessions.__exit__(exc_type, exc, traceback)

    def execute(self, operation: Any, item: Any) -> Any:
        if not hasattr(self.local, "session"):
            with self.lock:
                self.local.session = self.sessions.enter_context(self.backend.build_http_session(self.config))
        return operation(self.local.session, item)

    def map(self, items: list[Any], operation: Any) -> list[Any]:
        results: list[Any] = [None] * len(items)
        iterator = iter(enumerate(items))
        pending = {}
        for _ in range(min(len(items), self.workers)):
            index, item = next(iterator)
            pending[self.executor.submit(self.execute, operation, item)] = index
        try:
            while pending:
                completed, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in completed:
                    results[pending.pop(future)] = future.result()
                for _ in completed:
                    entry = next(iterator, None)
                    if entry is not None:
                        index, item = entry
                        pending[self.executor.submit(self.execute, operation, item)] = index
        except BaseException:
            for future in pending:
                future.cancel()
            raise
        return results


def bounded_session_map(items: list[Any], operation: Any, config: Any, backend: Any) -> list[Any]:
    if not items:
        return []
    with BoundedSessionPool(config, backend) as pool:
        return pool.map(items, operation)


def discover_workspaces(token: str, subscriptions: list[str], config: Any, backend: Any) -> list[dict[str, Any]]:
    batches = bounded_session_map(
        subscriptions,
        lambda http_session, subscription: discover_subscription_workspaces(
            http_session, token, [subscription], config.subscription_list_timeout_seconds, backend,
        ),
        config, backend,
    )
    return sorted((item for batch in batches for item in batch), key=lambda item: item["id"])


def discover_subscription_workspaces(http_session: Any, token: str, subscriptions: list[str], timeout: int, backend: Any) -> list[dict[str, Any]]:
    workspaces: dict[str, dict[str, Any]] = {}
    for subscription in subscriptions:
        path = f"/subscriptions/{subscription}/providers/Microsoft.OperationalInsights/workspaces"
        url = f"{ARM_ENDPOINT}{path}"
        visited: set[str] = set()
        params = {"api-version": WORKSPACES_API_VERSION}
        while url:
            try:
                parsed = urlsplit(url)
            except ValueError as exc:
                raise backend.HttpError(502, "ARM 工作区分页地址无效") from exc
            query = parse_qs(parsed.query)
            if (parsed.scheme != "https" or parsed.netloc.lower() != "management.azure.com"
                    or parsed.path.lower() != path.lower() or parsed.fragment
                    or (params is None and query.get("api-version") != [WORKSPACES_API_VERSION])
                    or url in visited or len(visited) >= MAX_PAGES):
                raise backend.HttpError(502, "ARM 工作区分页地址无效或超过限制")
            visited.add(url)
            body = arm_request(http_session, "get", url, token, timeout, backend, params=params)
            values = body.get("value")
            if not isinstance(values, list):
                raise backend.HttpError(502, "ARM 工作区列表格式无效")
            for item in values:
                if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                    raise backend.HttpError(502, "ARM 工作区格式无效")
                resource_id = validate_resource_id(item["id"], [subscription], "Microsoft.OperationalInsights/workspaces", backend)
                properties = item.get("properties") or {}
                if not isinstance(properties, dict):
                    raise backend.HttpError(502, "ARM 工作区属性格式无效")
                workspaces[resource_id] = {
                    "id": resource_id, "name": item.get("name", resource_id.rsplit("/", 1)[-1]),
                    "subscriptionId": subscription, "location": item.get("location", ""),
                    "customerId": properties.get("customerId", ""),
                }
            next_link = body.get("nextLink")
            if next_link is not None and not isinstance(next_link, str):
                raise backend.HttpError(502, "ARM 工作区分页地址无效")
            url = next_link or ""
            params = None
    return sorted(workspaces.values(), key=lambda item: item["id"])


def list_arm_values(http_session: Any, token: str, path: str, version: str, timeout: int, backend: Any) -> list[dict[str, Any]]:
    url = f"{ARM_ENDPOINT}{path}"
    params = {"api-version": version}
    visited: set[str] = set()
    items: list[dict[str, Any]] = []
    while url:
        try:
            parsed = urlsplit(url)
        except ValueError as exc:
            raise backend.HttpError(502, "ARM 资源分页地址无效") from exc
        if (parsed.scheme != "https" or parsed.netloc.lower() != "management.azure.com"
                or parsed.path.lower() != path.lower() or parsed.fragment
                or (params is None and parse_qs(parsed.query).get("api-version") != [version])
                or url in visited or len(visited) >= MAX_PAGES):
            raise backend.HttpError(502, "ARM 资源分页地址无效或超过限制")
        visited.add(url)
        body = arm_request(http_session, "get", url, token, timeout, backend, params=params)
        values = body.get("value")
        if not isinstance(values, list) or any(not isinstance(item, dict) for item in values):
            raise backend.HttpError(502, "ARM 资源列表格式无效")
        items.extend(values)
        next_link = body.get("nextLink")
        if next_link is not None and not isinstance(next_link, str):
            raise backend.HttpError(502, "ARM 资源分页地址无效")
        url = next_link or ""
        params = None
    return items


def discover_diagnostic_settings(http_session: Any, token: str, resource_id: str, timeout: int, backend: Any) -> dict[str, Any]:
    path = f"{resource_id}/providers/Microsoft.Insights/diagnosticSettings"
    try:
        settings = list_arm_values(http_session, token, path, DIAGNOSTIC_SETTINGS_API_VERSION, timeout, backend)
    except backend.HttpError as exc:
        if exc.status_code not in (401, 403):
            raise
        return {"diagnosticStatus": "unknown", "logsEnabled": None, "usageLogsEnabled": None,
                "enabledLogCategories": [], "enabledLogCategoryGroups": [], "workspaceIds": None, "usageWorkspaceIds": None}
    categories: set[str] = set()
    groups: set[str] = set()
    workspaces: set[str] = set()
    usage_workspaces: set[str] = set()
    logs_enabled = False
    usage_logs_enabled = False
    for setting in settings:
        properties = setting.get("properties")
        if not isinstance(properties, dict):
            raise backend.HttpError(502, "ARM 诊断设置属性格式无效")
        logs = properties.get("logs", [])
        if not isinstance(logs, list):
            raise backend.HttpError(502, "ARM 诊断日志分类格式无效")
        setting_logs_enabled = False
        setting_usage_enabled = False
        for log in logs:
            if not isinstance(log, dict) or not isinstance(log.get("enabled"), bool):
                raise backend.HttpError(502, "ARM 诊断日志分类格式无效")
            category = log.get("category")
            group = log.get("categoryGroup")
            if any(value is not None and (not isinstance(value, str) or not value.strip()) for value in (category, group)):
                raise backend.HttpError(502, "ARM 诊断日志分类格式无效")
            if log["enabled"]:
                if not category and not group:
                    raise backend.HttpError(502, "ARM 诊断日志缺少分类")
                setting_logs_enabled = True
                if (category and category.lower() == USAGE_LOG_CATEGORY.lower()) or (group and group.lower() == "alllogs"):
                    setting_usage_enabled = True
                if category:
                    categories.add(category)
                if group:
                    groups.add(group)
        logs_enabled = logs_enabled or setting_logs_enabled
        usage_logs_enabled = usage_logs_enabled or setting_usage_enabled
        workspace = properties.get("workspaceId")
        if workspace is not None:
            match = RESOURCE_PATTERN.fullmatch(workspace) if isinstance(workspace, str) else None
            if not match or match.group(3).lower() != "microsoft.operationalinsights/workspaces" or not SUBSCRIPTION_PATTERN.fullmatch(match.group(1)):
                raise backend.HttpError(502, "ARM 诊断工作区 ID 格式无效")
            if setting_logs_enabled:
                workspaces.add(workspace.lower())
            if setting_usage_enabled:
                usage_workspaces.add(workspace.lower())
    return {"diagnosticStatus": "known", "logsEnabled": logs_enabled,
            "usageLogsEnabled": usage_logs_enabled,
            "enabledLogCategories": sorted(categories), "enabledLogCategoryGroups": sorted(groups),
            "workspaceIds": sorted(workspaces), "usageWorkspaceIds": sorted(usage_workspaces)}


def discover_resource_statuses(token: str, resource_ids: list[str], config: Any, backend: Any, pool: Any = None) -> list[dict[str, Any]]:
    def fetch(http_session: Any, resource_id: str) -> dict[str, Any]:
        return {"id": resource_id, **discover_diagnostic_settings(
            http_session, token, resource_id, config.subscription_list_timeout_seconds, backend,
        )}

    rows = pool.map(resource_ids, fetch) if pool is not None else bounded_session_map(resource_ids, fetch, config, backend)
    return sorted(rows, key=lambda item: item["id"])


def discover_resources(token: str, subscriptions: list[str], config: Any, backend: Any, include_diagnostics: bool = True) -> list[dict[str, Any]]:
    resources: dict[str, dict[str, Any]] = {}

    def fetch(http_session: Any, subscription: str) -> list[dict[str, Any]]:
        path = f"/subscriptions/{subscription}/providers/Microsoft.CognitiveServices/accounts"
        rows = []
        for item in list_arm_values(http_session, token, path, COGNITIVE_API_VERSION, config.subscription_list_timeout_seconds, backend):
            kind = item.get("kind")
            if not isinstance(kind, str) or kind.lower() not in ("openai", "aiservices"):
                continue
            if not isinstance(item.get("id"), str):
                raise backend.HttpError(502, "ARM 账号 ID 格式无效")
            resource_id = validate_resource_id(item["id"], [subscription], "Microsoft.CognitiveServices/accounts", backend)
            rows.append({
                "id": resource_id, "name": item.get("name", resource_id.rsplit("/", 1)[-1]),
                "subscriptionId": subscription, "resourceGroup": resource_id.split("/")[4],
                "location": item.get("location", ""), "kind": kind,
            })
        return rows

    with BoundedSessionPool(config, backend) as pool:
        for rows in pool.map(subscriptions, fetch):
            resources.update((item["id"], item) for item in rows)
        if include_diagnostics:
            for setting in discover_resource_statuses(token, list(resources), config, backend, pool):
                resources[setting["id"]].update(setting)
        else:
            for item in resources.values():
                item.update({"diagnosticStatus": "pending", "logsEnabled": None, "usageLogsEnabled": None,
                             "enabledLogCategories": [], "enabledLogCategoryGroups": [], "workspaceIds": None, "usageWorkspaceIds": None})
    return sorted(resources.values(), key=lambda item: item["id"])


def parse_query_result(body: dict[str, Any], backend: Any) -> tuple[list[dict[str, Any]], list[str]]:
    if body.get("error"):
        raise backend.HttpError(502, "ARM 诊断查询返回错误或部分结果，未计入统计")
    tables = body.get("tables")
    if not isinstance(tables, list):
        raise backend.HttpError(502, "ARM 查询结果缺少 tables")
    rows: list[dict[str, Any]] = []
    field_names: dict[str, None] = {}
    for table in tables:
        if not isinstance(table, dict) or not isinstance(table.get("columns"), list) or not isinstance(table.get("rows"), list):
            raise backend.HttpError(502, "ARM 查询表格格式无效")
        columns = table["columns"]
        if any(not isinstance(column, dict) or not isinstance(column.get("name"), str) for column in columns):
            raise backend.HttpError(502, "ARM 查询列格式无效")
        names = [column["name"] for column in columns]
        if len(set(names)) != len(names):
            raise backend.HttpError(502, "ARM 查询包含重复列")
        field_names.update(dict.fromkeys(name for name in names if name not in ("p", "_ResourceIdFilter")))
        for values in table["rows"]:
            if not isinstance(values, list) or len(values) != len(names):
                raise backend.HttpError(502, "ARM 查询行格式无效")
            rows.append({name: value for name, value in zip(names, values) if name not in ("p", "_ResourceIdFilter")})
    return rows, list(field_names)


def register_routes(app: Flask, backend: Any = None) -> None:
    backend = backend if backend is not None else importlib.import_module("main")
    if "azure_diagnostics" in app.extensions:
        return

    def authorize(payload: dict[str, Any], auth_state: dict[str, Any], http_session: Any, config: Any) -> list[str]:
        raw = payload.get("subscriptionIds", [payload.get("subscriptionId")])
        subscriptions = normalize_ids(raw, "subscriptionIds", backend)
        if any(not SUBSCRIPTION_PATTERN.fullmatch(item) for item in subscriptions):
            raise backend.HttpError(400, "subscriptionIds 必须是有效的订阅 UUID")
        available = backend.get_cached_user_subscriptions(auth_state, http_session, config)
        allowed = {str(item.get("subscriptionId", "")).lower() for item in available}
        if not set(subscriptions).issubset(allowed):
            raise backend.HttpError(403, "所选订阅不存在、已失效或当前账号无访问权限")
        return subscriptions

    def list_workspaces():
        auth_state = backend.require_openai_tier_auth_state()
        payload = {"subscriptionIds": request.args.getlist("subscriptionIds") or request.args.getlist("subscriptionId")}
        config = backend.load_sync_config()
        with backend.build_http_session(config) as http_session:
            subscriptions = authorize(payload, auth_state, http_session, config)
        workspaces = discover_workspaces(auth_state["accessToken"], subscriptions, config, backend)
        return jsonify({"workspaces": workspaces})

    def list_resources():
        auth_state = backend.require_openai_tier_auth_state()
        payload = {"subscriptionIds": request.args.getlist("subscriptionIds") or request.args.getlist("subscriptionId")}
        phases = request.args.getlist("phase")
        phase = phases[0] if phases else "full"
        if len(phases) > 1 or phase not in ("full", "accounts", "diagnostics"):
            raise backend.HttpError(400, "phase 必须为 full、accounts 或 diagnostics")
        raw_resources = request.args.getlist("resourceIds")
        if phase != "diagnostics" and raw_resources:
            raise backend.HttpError(400, "resourceIds 仅用于 diagnostics 阶段")
        resource_ids = normalize_ids(raw_resources, "resourceIds", backend) if phase == "diagnostics" else []
        config = backend.load_sync_config()
        with backend.build_http_session(config) as http_session:
            subscriptions = authorize(payload, auth_state, http_session, config)
            for resource_id in resource_ids:
                validate_resource_id(resource_id, subscriptions, "Microsoft.CognitiveServices/accounts", backend)
        if phase == "diagnostics":
            resources = discover_resource_statuses(auth_state["accessToken"], resource_ids, config, backend)
        else:
            resources = discover_resources(auth_state["accessToken"], subscriptions, config, backend, include_diagnostics=phase == "full")
        return jsonify({"resources": resources})

    def query_diagnostics():
        auth_state = backend.require_openai_tier_auth_state()
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            raise backend.HttpError(400, "请求体必须是 JSON 对象")
        timespan, start_utc, end_utc = validate_timespan(payload, backend)
        workspace_ids = normalize_ids(payload.get("workspaceIds", [payload.get("workspaceId")]), "workspaceIds", backend)
        raw_resources = payload.get("resourceIds", [])
        resource_ids = [] if raw_resources == [] else normalize_ids(raw_resources, "resourceIds", backend)
        config = backend.load_sync_config()
        with backend.build_http_session(config) as http_session:
            subscriptions = authorize(payload, auth_state, http_session, config)
            for workspace in workspace_ids:
                validate_resource_id(workspace, subscriptions, "Microsoft.OperationalInsights/workspaces", backend)
            for resource in resource_ids:
                validate_resource_id(resource, subscriptions, "Microsoft.CognitiveServices/accounts", backend)
            query = build_query(resource_ids, start_utc, end_utc, subscriptions)
            rows = []
            columns: dict[str, None] = {}
            truncated_workspaces = []
            for workspace in workspace_ids:
                body = arm_request(
                    http_session, "post", f"{ARM_ENDPOINT}{workspace}/query", auth_state["accessToken"],
                    config.cost_query_timeout_seconds, backend, params={"api-version": QUERY_API_VERSION},
                    json={"query": query, "timespan": f"{start_utc}/{end_utc}"},
                )
                details, field_names = parse_query_result(body, backend)
                columns.update(dict.fromkeys(field_names))
                if len(details) > MAX_DETAIL_ROWS:
                    truncated_workspaces.append(workspace)
                rows.extend({**row, "workspaceId": workspace} for row in details[:MAX_DETAIL_ROWS])
        columns["workspaceId"] = None
        warnings = ["多个工作区可能包含相同日志；结果按工作区独立统计，请勿直接跨工作区累加。"] if len(workspace_ids) > 1 else []
        for workspace in truncated_workspaces:
            warnings.append(f"工作区 {workspace} 的明细已截断，仅返回最新 {MAX_DETAIL_ROWS} 条；请缩小时间或资源范围，结果不代表完整用量。")
        rows.sort(key=lambda row: str(row.get("TimeGenerated") or ""), reverse=True)
        return jsonify({"subscriptionIds": subscriptions, "timespan": timespan, "queryTimespan": f"{start_utc}/{end_utc}", "query": query, "columns": list(columns), "rows": rows, "truncated": bool(truncated_workspaces), "truncatedWorkspaceIds": truncated_workspaces, "warnings": warnings})

    app.add_url_rule("/api/azure-diagnostics/workspaces", "azure_diagnostics_workspaces", list_workspaces, methods=["GET"])
    app.add_url_rule("/api/azure-diagnostics/resources", "azure_diagnostics_resources", list_resources, methods=["GET"])
    app.add_url_rule("/api/azure-diagnostics/query", "azure_diagnostics_query", query_diagnostics, methods=["POST"])
    app.extensions["azure_diagnostics"] = backend


def create_app() -> Flask:
    application = importlib.import_module("main")
    register_routes(application.app, application)
    return application.app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=5100, debug=False)
