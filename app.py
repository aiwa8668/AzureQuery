from __future__ import annotations

import csv
import io
import logging
from logging.handlers import RotatingFileHandler
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
import random
import threading
import time
from typing import Any
from urllib.parse import unquote

import requests
from requests.adapters import HTTPAdapter
from flask import Flask, jsonify, redirect, render_template, request, Response
from werkzeug.exceptions import HTTPException

COST_QUERY_API_VERSION = "2023-03-01"
SUBSCRIPTIONS_API_VERSION = "2020-01-01"
MAX_COST_QUERY_RETRIES = 3
DEFAULT_RETRY_DELAY_SECONDS = 2
DEFAULT_SUBSCRIPTION_LIST_TIMEOUT_SECONDS = 60
DEFAULT_COST_QUERY_TIMEOUT_SECONDS = 120
DEFAULT_HTTP_POOL_CONNECTIONS = 16
DEFAULT_HTTP_POOL_MAXSIZE = 64
DEFAULT_MAX_WORKERS = 2
DEFAULT_GLOBAL_COST_API_MAX_INFLIGHT = 12
DEFAULT_TENANT_COST_API_MAX_INFLIGHT = 3
DEFAULT_RETRY_MAX_DELAY_SECONDS = 45
BASE_DIR = Path(__file__).resolve().parent
SYNC_CONFIG_PATH = BASE_DIR / "config" / "sync.config"
LOG_DIR = BASE_DIR / "logs"
LOG_FILE_PATH = LOG_DIR / "azure-cost-query.log"

_cost_api_limit_lock = threading.Lock()
_global_cost_api_semaphore: threading.BoundedSemaphore | None = None
_global_cost_api_limit = 0
_tenant_cost_api_semaphores: dict[str, threading.BoundedSemaphore] = {}
_tenant_cost_api_limit = 0


def configure_logging() -> None:
    # In hosted environments, logging may already be configured before app import.
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    target_log_file = str(LOG_FILE_PATH.resolve()).lower()
    has_target_file_handler = False
    has_console_handler = False

    for handler in root_logger.handlers:
        if isinstance(handler, logging.FileHandler):
            base_filename = getattr(handler, "baseFilename", "")
            if base_filename and str(Path(base_filename).resolve()).lower() == target_log_file:
                has_target_file_handler = True
        if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
            has_console_handler = True

    if not has_console_handler:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        root_logger.addHandler(console_handler)

    if not has_target_file_handler:
        file_handler = RotatingFileHandler(
            LOG_FILE_PATH,
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)


configure_logging()
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder="public", static_url_path="/static")
app.logger.setLevel(logging.INFO)


class HttpError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class ThrottledError(Exception):
    pass


@dataclass
class Subscription:
    subscription_id: str
    subscription_name: str
    tenant_name: str


@dataclass
class SyncConfig:
    max_workers: int = DEFAULT_MAX_WORKERS
    http_pool_connections: int = DEFAULT_HTTP_POOL_CONNECTIONS
    http_pool_maxsize: int = DEFAULT_HTTP_POOL_MAXSIZE
    subscription_list_timeout_seconds: int = DEFAULT_SUBSCRIPTION_LIST_TIMEOUT_SECONDS
    cost_query_timeout_seconds: int = DEFAULT_COST_QUERY_TIMEOUT_SECONDS
    cost_query_max_retries: int = MAX_COST_QUERY_RETRIES
    cost_query_retry_base_seconds: int = DEFAULT_RETRY_DELAY_SECONDS
    global_cost_api_max_inflight: int = DEFAULT_GLOBAL_COST_API_MAX_INFLIGHT
    tenant_cost_api_max_inflight: int = DEFAULT_TENANT_COST_API_MAX_INFLIGHT
    cost_query_retry_max_delay_seconds: int = DEFAULT_RETRY_MAX_DELAY_SECONDS


SYNC_CONFIG_FIELD_ORDER = [
    "max_workers",
    "http_pool_connections",
    "http_pool_maxsize",
    "subscription_list_timeout_seconds",
    "cost_query_timeout_seconds",
    "cost_query_max_retries",
    "cost_query_retry_base_seconds",
]


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/favicon.ico")
def favicon():
    return Response(status=204)


@app.post("/api/costs/query")
def query_costs():
    payload = request.get_json(silent=True) or {}
    start_date = payload.get("startDate")
    end_date = payload.get("endDate")
    auth_list = payload.get("authList", [])
    auth = payload.get("auth")
    sync_config = load_sync_config()

    if not auth_list and auth:
        auth_list = [auth]

    if not auth_list:
        return jsonify({"error": "missing auth"}), 400

    logger.info(
        "cost query started: start_date=%s end_date=%s auth_count=%s",
        start_date,
        end_date,
        len(auth_list),
    )

    results = []
    auth_errors: list[str] = []
    for idx, auth in enumerate(auth_list):
        tenant_label = str(auth.get("tenantName", "")).strip() or f"认证{idx + 1}"
        try:
            validate_query_input(start_date, end_date, auth)
            subscription_filter = str(auth.get("subscriptionId", "")).strip()
            tenant_name = str(auth.get("tenantName", "")).strip()
            target_subscription_filters: list[str] = [subscription_filter] if subscription_filter else []

            logger.info(
                "cost query for auth: tenant=%s start_date=%s end_date=%s target_subscription=%s",
                tenant_label,
                start_date,
                end_date,
                subscription_filter or "all",
            )

            token = get_azure_access_token(auth)
            logger.info("azure token acquired for auth: tenant=%s", tenant_label)
            with build_http_session(sync_config) as session:
                subscriptions = fetch_subscriptions(
                    token,
                    session,
                    sync_config,
                    tenant_name,
                    target_subscription_filters,
                )
                logger.info("subscriptions loaded for auth: tenant=%s count=%s", tenant_label, len(subscriptions))

                if not subscriptions:
                    logger.info("cost query for auth finished: tenant=%s no subscriptions found", tenant_label)
                    auth_errors.append(f"[{tenant_label}] 未找到任何订阅")
                    continue

                result = query_and_aggregate_costs(
                    token=token,
                    subscriptions=subscriptions,
                    start_date=start_date,
                    end_date=end_date,
                    session=session,
                    sync_config=sync_config,
                )
                results.append(result)
        except HttpError as e:
            logger.error("cost query for auth failed: tenant=%s status=%s message=%s", tenant_label, e.status_code, e.message)
            auth_errors.append(f"[{tenant_label}] {e.message}")
        except Exception as e:
            logger.error("cost query for auth failed: tenant=%s error=%s", tenant_label, str(e))
            auth_errors.append(f"[{tenant_label}] {str(e)}")

    if not results:
        error_detail = "；".join(auth_errors) if auth_errors else "未返回任何数据"
        logger.info("cost query finished: no valid results from any auth. errors: %s", error_detail)
        return jsonify({"message": error_detail}), 400

    aggregated = aggregate_multi_auth_results(results)

    if auth_errors:
        aggregated.setdefault("authWarnings", []).extend(auth_errors)

    logger.info(
        "cost query finished: total_cost=%s currency=%s auth_errors=%s",
        aggregated.get("summary", {}).get("totalCost"),
        aggregated.get("summary", {}).get("currency"),
        len(auth_errors),
    )
    return jsonify(aggregated)


@app.post("/api/costs/export")
def export_costs():
    payload = request.get_json(silent=True) or {}
    dimension = payload.get("dimension")
    data = payload.get("data")

    if not isinstance(data, dict):
        raise HttpError(400, "缺少导出数据")

    csv_content = build_csv_by_dimension(dimension, data)
    file_name = f"azure-cost-{dimension or 'all'}-{int(datetime.utcnow().timestamp())}.csv"

    return Response(
        "\ufeff" + csv_content,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{file_name}"'},
    )


def validate_query_input(start_date: Any, end_date: Any, auth: Any) -> None:
    if not start_date or not end_date:
        raise HttpError(400, "startDate 和 endDate 为必填项")

    try:
        start = datetime.strptime(str(start_date), "%Y-%m-%d")
        end = datetime.strptime(str(end_date), "%Y-%m-%d")
    except ValueError as exc:
        raise HttpError(400, "日期格式不正确，应为 YYYY-MM-DD") from exc

    if start > end:
        raise HttpError(400, "开始时间不能晚于结束时间")

    if not isinstance(auth, dict):
        raise HttpError(400, "缺少认证参数")

    required_fields = [
        "azureClientId",
        "azureClientSecret",
        "azureTenantId",
        "tenantName",
    ]

    for field in required_fields:
        if not auth.get(field):
            raise HttpError(400, f"认证参数缺失: {field}")


def get_azure_access_token(auth: dict[str, str]) -> str:
    token_url = (
        f"https://login.microsoftonline.com/{auth['azureTenantId']}/oauth2/v2.0/token"
    )
    form = {
        "grant_type": "client_credentials",
        "client_id": auth["azureClientId"],
        "client_secret": auth["azureClientSecret"],
        "scope": "https://management.azure.com/.default",
    }

    try:
        resp = requests.post(
            token_url,
            data=form,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        if resp.status_code >= 400:
            raise HttpError(401, f"Azure 鉴权失败: {extract_http_error(resp)}")

        body = resp.json()
        token = body.get("access_token")
        if not token:
            raise HttpError(401, "未获取到 Azure 访问令牌")
        return token
    except requests.RequestException as exc:
        raise HttpError(401, f"Azure 鉴权失败: {exc}") from exc


def fetch_subscriptions(
    token: str,
    session: requests.Session,
    sync_config: SyncConfig,
    tenant_name: str,
    target_subscription_filters: list[str] | None = None,
) -> list[Subscription]:
    url = (
        "https://management.azure.com/subscriptions"
        f"?api-version={SUBSCRIPTIONS_API_VERSION}"
    )

    try:
        resp = session.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=sync_config.subscription_list_timeout_seconds,
        )
        if resp.status_code >= 400:
            raise HttpError(502, f"获取订阅列表失败: {extract_http_error(resp)}")

        body = resp.json() if resp.content else {}
        values = body.get("value") if isinstance(body, dict) else []
        if not isinstance(values, list):
            return []

        target_set = set(target_subscription_filters or [])
        subscriptions: list[Subscription] = []
        for item in values:
            if not isinstance(item, dict):
                continue
            sub_id = str(item.get("subscriptionId") or "").strip()
            if not sub_id:
                continue
            sub_name = str(item.get("displayName") or sub_id)
            if target_set and sub_id not in target_set:
                continue
            subscriptions.append(Subscription(sub_id, sub_name, tenant_name))

        if target_set and not subscriptions:
            raise HttpError(400, "未找到匹配的 Subscription ID，请检查配置是否正确")

        return subscriptions
    except requests.RequestException as exc:
        raise HttpError(502, f"获取订阅列表失败: {exc}") from exc


def aggregate_multi_auth_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate cost results from multiple authentication contexts."""
    if not results:
        return {
            "summary": {"currency": "USD", "totalCost": 0},
            "bySubscription": [],
            "byResourceType": [],
            "byDate": [],
        }

    if len(results) == 1:
        return results[0]

    by_subscription: dict[str, dict[str, Any]] = {}
    by_resource_type: dict[tuple[str, str, str, str, str], float] = {}
    by_date: dict[tuple[str, str, str, str, str], float] = {}
    warnings: list[str] = []
    total_cost = 0.0
    currency = "USD"

    for result in results:
        summary = result.get("summary", {})
        if summary.get("currency"):
            currency = summary["currency"]
        total_cost += to_float(summary.get("totalCost", 0))

        for sub in result.get("bySubscription", []):
            sub_id = sub.get("subscriptionId")
            tenant_name = str(sub.get("tenantName") or "-")
            if sub_id:
                sub_key = f"{tenant_name}::{sub_id}"
                if sub_key in by_subscription:
                    by_subscription[sub_key]["cost"] += to_float(sub.get("cost", 0))
                else:
                    by_subscription[sub_key] = sub.copy()

        for item in result.get("byResourceType", []):
            key = (
                item.get("tenantName", ""),
                item.get("subscription", ""),
                item.get("resourceName", ""),
                item.get("location", ""),
                item.get("serviceName", ""),
            )
            cost = to_float(item.get("cost", 0))
            by_resource_type[key] = by_resource_type.get(key, 0.0) + cost

        for item in result.get("byDate", []):
            key = (
                item.get("tenantName", ""),
                item.get("date", ""),
                item.get("subscription", ""),
                item.get("resourceName", ""),
                item.get("product", ""),
            )
            cost = to_float(item.get("cost", 0))
            by_date[key] = by_date.get(key, 0.0) + cost

        warnings.extend(result.get("warnings", []))

    aggregated = {
        "summary": {
            "currency": currency,
            "totalCost": round2(total_cost),
        },
        "bySubscription": sorted(
            by_subscription.values(),
            key=lambda x: x["cost"],
            reverse=True,
        ),
        "byResourceType": sorted(
            [
                {
                    "tenantName": key[0],
                    "subscription": key[1],
                    "resourceName": key[2],
                    "location": key[3],
                    "serviceName": key[4],
                    "cost": round2(value),
                }
                for key, value in by_resource_type.items()
            ],
            key=lambda x: x["cost"],
            reverse=True,
        ),
        "byDate": sorted(
            [
                {
                    "tenantName": key[0],
                    "date": key[1],
                    "subscription": key[2],
                    "resourceName": key[3],
                    "serviceName": key[3],
                    "product": key[4],
                    "cost": round2(value),
                }
                for key, value in by_date.items()
            ],
            key=lambda x: (x["date"], -x["cost"]),
        ),
    }

    if warnings:
        aggregated["warnings"] = warnings

    return aggregated


def query_and_aggregate_costs(
    token: str,
    subscriptions: list[Subscription],
    start_date: str,
    end_date: str,
    session: requests.Session,
    sync_config: SyncConfig,
) -> dict[str, Any]:
    by_subscription: dict[str, dict[str, Any]] = {}
    by_resource_type: dict[tuple[str, str, str, str, str], float] = {}
    by_date: dict[tuple[str, str, str, str, str], float] = {}
    warnings: list[str] = []

    total_cost = 0.0
    currency = "USD"

    max_workers = max(1, min(resolve_effective_workers(sync_config, start_date, end_date), len(subscriptions)))
    logger.info(
        "cost aggregation started: subscriptions=%s max_workers=%s date_range=%s~%s",
        len(subscriptions),
        max_workers,
        start_date,
        end_date,
    )
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                fetch_subscription_cost_rows,
                token,
                subscription.tenant_name,
                subscription.subscription_id,
                start_date,
                end_date,
                session,
                sync_config,
            ): subscription
            for subscription in subscriptions
        }

        for future in as_completed(futures):
            subscription = futures[future]
            rows, row_currency, warning, sub_total = future.result()

            if warning:
                logger.warning("subscription cost query warning: %s", warning)
                warnings.append(warning)

            if row_currency:
                currency = row_currency

            total_cost += sub_total
            for item in rows:
                date = normalize_usage_date(item.get("usageDate"))
                resource_name = str(item.get("resourceName") or "Unknown")
                location = str(item.get("location") or "Unknown")
                service_name = str(item.get("serviceName") or "Unknown")
                product = str(item.get("product") or "Unknown")
                cost = to_float(item.get("cost"))

                resource_key = (
                    subscription.tenant_name,
                    subscription.subscription_name,
                    resource_name,
                    location,
                    service_name,
                )
                date_key = (
                    subscription.tenant_name,
                    date,
                    subscription.subscription_name,
                    resource_name,
                    product,
                )
                by_resource_type[resource_key] = by_resource_type.get(resource_key, 0.0) + cost
                by_date[date_key] = by_date.get(date_key, 0.0) + cost

            by_subscription[subscription.subscription_id] = {
                "subscriptionId": subscription.subscription_id,
                "subscriptionName": subscription.subscription_name,
                "tenantName": subscription.tenant_name,
                "cost": round2(sub_total),
            }
            logger.info(
                "subscription aggregation finished: subscription_id=%s subscription_name=%s total_cost=%s rows=%s",
                subscription.subscription_id,
                subscription.subscription_name,
                round2(sub_total),
                len(rows),
            )

    result = {
        "summary": {
            "currency": currency,
            "totalCost": round2(total_cost),
        },
        "bySubscription": sorted(
            by_subscription.values(),
            key=lambda x: x["cost"],
            reverse=True,
        ),
        "byResourceType": sorted(
            [
                {
                    "tenantName": key[0],
                    "subscription": key[1],
                    "resourceName": key[2],
                    "location": key[3],
                    "serviceName": key[4],
                    "cost": round2(value),
                }
                for key, value in by_resource_type.items()
            ],
            key=lambda x: x["cost"],
            reverse=True,
        ),
        "byDate": sorted(
            [
                {
                    "tenantName": key[0],
                    "date": key[1],
                    "subscription": key[2],
                    "resourceName": key[3],
                    "serviceName": key[3],
                    "product": key[4],
                    "cost": round2(value),
                }
                for key, value in by_date.items()
            ],
            key=lambda x: (x["date"], -x["cost"]),
        ),
    }

    if warnings:
        result["warnings"] = warnings

    return result


def fetch_subscription_cost_rows(
    token: str,
    tenant_key: str,
    subscription_id: str,
    start_date: str,
    end_date: str,
    session: requests.Session,
    sync_config: SyncConfig,
) -> tuple[list[dict[str, Any]], str, str | None, float]:
    url = (
        "https://management.azure.com/subscriptions/"
        f"{subscription_id}/providers/Microsoft.CostManagement/query"
        f"?api-version={COST_QUERY_API_VERSION}"
    )

    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")

    summary_cost, summary_currency = fetch_subscription_cost_summary(
        token=token,
        tenant_key=tenant_key,
        url=url,
        start_dt=start_dt,
        end_dt=end_dt,
        session=session,
        sync_config=sync_config,
    )
    logger.info(
        "subscription summary queried: subscription_id=%s total_cost=%s currency=%s",
        subscription_id,
        round2(summary_cost),
        summary_currency or "unknown",
    )

    if summary_cost == 0:
        logger.info(
            "subscription detail skipped because summary cost is zero: subscription_id=%s",
            subscription_id,
        )
        return [], summary_currency, None, 0.0

    logger.info("subscription detail query started: subscription_id=%s", subscription_id)
    rows, currency, failed_ranges = query_cost_rows_with_split_fallback(
        token=token,
        tenant_key=tenant_key,
        url=url,
        start_dt=start_dt,
        end_dt=end_dt,
        session=session,
        sync_config=sync_config,
    )

    if failed_ranges:
        ranges_text = "，".join(f"{item[0]}~{item[1]}" for item in failed_ranges[:3])
        suffix = "..." if len(failed_ranges) > 3 else ""
        warning = (
            f"订阅 {subscription_id} 部分时间段成本查询触发限流并失败：{ranges_text}{suffix}，"
            "请稍后重试该时间段。"
        )
        return rows, currency or summary_currency, warning, summary_cost

    return rows, currency or summary_currency, None, summary_cost


def fetch_subscription_cost_summary(
    token: str,
    tenant_key: str,
    url: str,
    start_dt: datetime,
    end_dt: datetime,
    session: requests.Session,
    sync_config: SyncConfig,
) -> tuple[float, str]:
    try:
        return execute_cost_summary_query(
            token=token,
            tenant_key=tenant_key,
            url=url,
            payload=build_cost_summary_payload(start_dt, end_dt),
            session=session,
            sync_config=sync_config,
        )
    except ThrottledError as exc:
        raise HttpError(502, "查询订阅汇总成本触发限流，请稍后重试") from exc
    except requests.RequestException as exc:
        raise HttpError(502, f"查询订阅汇总成本失败: {exc}") from exc


def query_cost_rows_with_split_fallback(
    token: str,
    tenant_key: str,
    url: str,
    start_dt: datetime,
    end_dt: datetime,
    session: requests.Session,
    sync_config: SyncConfig,
) -> tuple[list[dict[str, Any]], str, list[tuple[str, str]]]:
    if start_dt > end_dt:
        return [], "", []

    payload = build_cost_query_payload(start_dt, end_dt)
    try:
        rows, currency = execute_cost_query_with_paging(
            token=token,
            tenant_key=tenant_key,
            url=url,
            payload=payload,
            session=session,
            sync_config=sync_config,
        )
        return rows, currency, []
    except ThrottledError:
        logger.warning(
            "cost detail query throttled, splitting range: date_range=%s~%s",
            start_dt.strftime("%Y-%m-%d"),
            end_dt.strftime("%Y-%m-%d"),
        )
        if start_dt == end_dt:
            return [], "", [(start_dt.strftime("%Y-%m-%d"), end_dt.strftime("%Y-%m-%d"))]

        mid_dt = start_dt + timedelta(days=((end_dt - start_dt).days // 2))
        left_rows, left_currency, left_failed = query_cost_rows_with_split_fallback(
            token,
            tenant_key,
            url,
            start_dt,
            mid_dt,
            session,
            sync_config,
        )
        right_rows, right_currency, right_failed = query_cost_rows_with_split_fallback(
            token,
            tenant_key,
            url,
            mid_dt + timedelta(days=1),
            end_dt,
            session,
            sync_config,
        )

        merged_rows = left_rows + right_rows
        merged_currency = left_currency or right_currency
        return merged_rows, merged_currency, left_failed + right_failed
    except requests.RequestException as exc:
        raise HttpError(502, f"查询订阅成本失败: {exc}") from exc


def build_cost_query_payload(start_dt: datetime, end_dt: datetime) -> dict[str, Any]:
    return {
        "type": "ActualCost",
        "timeframe": "Custom",
        "timePeriod": {
            "from": f"{start_dt.strftime('%Y-%m-%d')}T00:00:00Z",
            "to": f"{end_dt.strftime('%Y-%m-%d')}T23:59:59Z",
        },
        "dataset": {
            "granularity": "Daily",
            "aggregation": {
                "totalCost": {"name": "PreTaxCost", "function": "Sum"}
            },
            "grouping": [
                {"type": "Dimension", "name": "ResourceId"},
                {"type": "Dimension", "name": "ResourceLocation"},
                {"type": "Dimension", "name": "ServiceName"},
                {"type": "Dimension", "name": "Product"},
            ],
        },
    }


def build_cost_summary_payload(start_dt: datetime, end_dt: datetime) -> dict[str, Any]:
    return {
        "type": "ActualCost",
        "timeframe": "Custom",
        "timePeriod": {
            "from": f"{start_dt.strftime('%Y-%m-%d')}T00:00:00Z",
            "to": f"{end_dt.strftime('%Y-%m-%d')}T23:59:59Z",
        },
        "dataset": {
            "aggregation": {
                "totalCost": {"name": "PreTaxCost", "function": "Sum"}
            },
        },
    }


def execute_cost_summary_query(
    token: str,
    tenant_key: str,
    url: str,
    payload: dict[str, Any],
    session: requests.Session,
    sync_config: SyncConfig,
) -> tuple[float, str]:
    resp = post_cost_query_with_retry(
        token=token,
        tenant_key=tenant_key,
        url=url,
        payload=payload,
        session=session,
        sync_config=sync_config,
    )

    body = resp.json() if resp.content else {}
    props = body.get("properties") if isinstance(body, dict) else {}
    columns = props.get("columns") if isinstance(props, dict) else []
    rows = props.get("rows") if isinstance(props, dict) else []
    return parse_cost_summary(columns, rows)


def execute_cost_query_with_paging(
    token: str,
    tenant_key: str,
    url: str,
    payload: dict[str, Any],
    session: requests.Session,
    sync_config: SyncConfig,
) -> tuple[list[dict[str, Any]], str]:
    all_rows: list[dict[str, Any]] = []
    currency = ""

    next_url = url
    while next_url:
        resp = post_cost_query_with_retry(
            token=token,
            tenant_key=tenant_key,
            url=next_url,
            payload=payload,
            session=session,
            sync_config=sync_config,
        )

        body = resp.json() if resp.content else {}
        props = body.get("properties") if isinstance(body, dict) else {}
        columns = props.get("columns") if isinstance(props, dict) else []
        rows = props.get("rows") if isinstance(props, dict) else []

        parsed_rows, page_currency = parse_cost_rows(columns, rows)
        all_rows.extend(parsed_rows)
        if not currency and page_currency:
            currency = page_currency

        next_link = props.get("nextLink") if isinstance(props, dict) else None
        next_url = str(next_link).strip() if next_link else ""

    return all_rows, currency


def post_cost_query_with_retry(
    token: str,
    tenant_key: str,
    url: str,
    payload: dict[str, Any],
    session: requests.Session,
    sync_config: SyncConfig,
) -> requests.Response:
    for attempt in range(1, sync_config.cost_query_max_retries + 2):
        with limit_cost_api_request(sync_config, tenant_key):
            resp = session.post(
                url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                timeout=sync_config.cost_query_timeout_seconds,
            )

        if resp.status_code == 403:
            return resp

        if resp.status_code == 429:
            if attempt <= sync_config.cost_query_max_retries:
                delay_seconds = get_retry_delay_seconds(resp, attempt, sync_config)
                logger.warning(
                    "azure cost api throttled, retrying: attempt=%s max_retries=%s delay_seconds=%s",
                    attempt,
                    sync_config.cost_query_max_retries,
                    delay_seconds,
                )
                time.sleep(delay_seconds)
                continue
            raise ThrottledError("Azure Cost API throttled")

        if resp.status_code >= 400:
            raise HttpError(502, f"查询订阅成本失败: {extract_http_error(resp)}")

        return resp

    raise ThrottledError("Azure Cost API throttled")


def resolve_effective_workers(sync_config: SyncConfig, start_date: str, end_date: str) -> int:
    try:
        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    except ValueError:
        return max(1, sync_config.max_workers)

    days = (end_dt - start_dt).days + 1
    requested_workers = max(1, sync_config.max_workers)

    if days >= 31:
        return min(requested_workers, 1)
    if days >= 14:
        return min(requested_workers, 2)
    return requested_workers


def get_retry_delay_seconds(
    resp: requests.Response,
    attempt: int,
    sync_config: SyncConfig,
) -> int:
    retry_after = resp.headers.get("Retry-After")
    if retry_after:
        try:
            delay = int(retry_after)
            if delay > 0:
                return delay
        except ValueError:
            pass

    base_delay = max(1, sync_config.cost_query_retry_base_seconds)
    max_delay = max(base_delay, sync_config.cost_query_retry_max_delay_seconds)

    exp_delay = base_delay * (2 ** max(0, attempt - 1))
    jitter_ratio = random.uniform(0.6, 1.4)
    computed_delay = int(round(exp_delay * jitter_ratio))
    return max(1, min(max_delay, computed_delay))


def load_sync_config() -> SyncConfig:
    values: dict[str, str] = {}
    if SYNC_CONFIG_PATH.exists():
        for raw_line in SYNC_CONFIG_PATH.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()

    return SyncConfig(
        max_workers=parse_positive_int(values.get("max_workers"), DEFAULT_MAX_WORKERS),
        http_pool_connections=parse_positive_int(
            values.get("http_pool_connections"),
            DEFAULT_HTTP_POOL_CONNECTIONS,
        ),
        http_pool_maxsize=parse_positive_int(
            values.get("http_pool_maxsize"),
            DEFAULT_HTTP_POOL_MAXSIZE,
        ),
        subscription_list_timeout_seconds=parse_positive_int(
            values.get("subscription_list_timeout_seconds"),
            DEFAULT_SUBSCRIPTION_LIST_TIMEOUT_SECONDS,
        ),
        cost_query_timeout_seconds=parse_positive_int(
            values.get("cost_query_timeout_seconds"),
            DEFAULT_COST_QUERY_TIMEOUT_SECONDS,
        ),
        cost_query_max_retries=parse_non_negative_int(
            values.get("cost_query_max_retries"),
            MAX_COST_QUERY_RETRIES,
        ),
        cost_query_retry_base_seconds=parse_positive_int(
            values.get("cost_query_retry_base_seconds"),
            DEFAULT_RETRY_DELAY_SECONDS,
        ),
        global_cost_api_max_inflight=parse_positive_int(
            values.get("global_cost_api_max_inflight"),
            DEFAULT_GLOBAL_COST_API_MAX_INFLIGHT,
        ),
        tenant_cost_api_max_inflight=parse_positive_int(
            values.get("tenant_cost_api_max_inflight"),
            DEFAULT_TENANT_COST_API_MAX_INFLIGHT,
        ),
        cost_query_retry_max_delay_seconds=parse_positive_int(
            values.get("cost_query_retry_max_delay_seconds"),
            DEFAULT_RETRY_MAX_DELAY_SECONDS,
        ),
    )


def save_sync_config(sync_config: SyncConfig) -> None:
    SYNC_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# 查询与同步并发配置",
        "# 单租户订阅并发数；过高可能增加 Azure Cost Management 429 限流概率",
        f"max_workers={sync_config.max_workers}",
        "",
        "# HTTP 连接池",
        f"http_pool_connections={sync_config.http_pool_connections}",
        f"http_pool_maxsize={sync_config.http_pool_maxsize}",
        "",
        "# 请求超时（秒）",
        f"subscription_list_timeout_seconds={sync_config.subscription_list_timeout_seconds}",
        f"cost_query_timeout_seconds={sync_config.cost_query_timeout_seconds}",
        "",
        "# 429 限流重试",
        "# 表示首次失败后的最大重试次数；总尝试次数 = 1 + cost_query_max_retries",
        f"cost_query_max_retries={sync_config.cost_query_max_retries}",
        f"cost_query_retry_base_seconds={sync_config.cost_query_retry_base_seconds}",
        f"cost_query_retry_max_delay_seconds={sync_config.cost_query_retry_max_delay_seconds}",
        "",
        "# 全局与租户级 Cost API 并发闸门",
        f"global_cost_api_max_inflight={sync_config.global_cost_api_max_inflight}",
        f"tenant_cost_api_max_inflight={sync_config.tenant_cost_api_max_inflight}",
        "",
    ]
    SYNC_CONFIG_PATH.write_text("\n".join(lines), encoding="utf-8")


def build_http_session(sync_config: SyncConfig) -> requests.Session:
    session = requests.Session()
    adapter = HTTPAdapter(
        pool_connections=sync_config.http_pool_connections,
        pool_maxsize=sync_config.http_pool_maxsize,
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def get_global_cost_api_semaphore(sync_config: SyncConfig) -> threading.BoundedSemaphore:
    global _global_cost_api_semaphore
    global _global_cost_api_limit
    global _tenant_cost_api_limit
    global _tenant_cost_api_semaphores

    limit = max(1, sync_config.global_cost_api_max_inflight)
    with _cost_api_limit_lock:
        if _global_cost_api_semaphore is None or _global_cost_api_limit != limit:
            _global_cost_api_semaphore = threading.BoundedSemaphore(limit)
            _global_cost_api_limit = limit

        # Tenant limiter depends on config too; reset cache when limit changes.
        tenant_limit = max(1, sync_config.tenant_cost_api_max_inflight)
        if _tenant_cost_api_limit != tenant_limit:
            _tenant_cost_api_semaphores = {}
            _tenant_cost_api_limit = tenant_limit

        return _global_cost_api_semaphore


def get_tenant_cost_api_semaphore(tenant_key: str, sync_config: SyncConfig) -> threading.BoundedSemaphore:
    global _tenant_cost_api_limit
    global _tenant_cost_api_semaphores

    normalized_tenant_key = str(tenant_key or "unknown").strip() or "unknown"
    tenant_limit = max(1, sync_config.tenant_cost_api_max_inflight)

    with _cost_api_limit_lock:
        if _tenant_cost_api_limit != tenant_limit:
            _tenant_cost_api_semaphores = {}
            _tenant_cost_api_limit = tenant_limit

        semaphore = _tenant_cost_api_semaphores.get(normalized_tenant_key)
        if semaphore is None:
            semaphore = threading.BoundedSemaphore(tenant_limit)
            _tenant_cost_api_semaphores[normalized_tenant_key] = semaphore

        return semaphore


@contextmanager
def limit_cost_api_request(sync_config: SyncConfig, tenant_key: str):
    global_semaphore = get_global_cost_api_semaphore(sync_config)
    tenant_semaphore = get_tenant_cost_api_semaphore(tenant_key, sync_config)

    global_semaphore.acquire()
    tenant_semaphore.acquire()
    try:
        yield
    finally:
        tenant_semaphore.release()
        global_semaphore.release()


def parse_positive_int(value: str | None, default: int) -> int:
    try:
        parsed = int(str(value))
        return parsed if parsed > 0 else default
    except (TypeError, ValueError):
        return default


def parse_non_negative_int(value: str | None, default: int) -> int:
    try:
        parsed = int(str(value))
        return parsed if parsed >= 0 else default
    except (TypeError, ValueError):
        return default


def parse_cost_rows(columns: Any, rows: Any) -> tuple[list[dict[str, Any]], str]:
    if not isinstance(columns, list) or not isinstance(rows, list):
        return [], ""

    index_map: dict[str, int] = {}
    for idx, col in enumerate(columns):
        if isinstance(col, dict):
            name = str(col.get("name") or "").lower()
            if name:
                index_map[name] = idx

    usage_idx = index_map.get("usagedate")
    resource_id_idx = index_map.get("resourceid")
    location_idx = index_map.get("resourcelocation")
    service_idx = index_map.get("servicename")
    product_idx = index_map.get("product")
    cost_idx = index_map.get("pretaxcost")
    currency_idx = index_map.get("currency")

    normalized: list[dict[str, Any]] = []
    currency = ""

    for row in rows:
        if not isinstance(row, list):
            continue

        resource_id = get_row_value(row, resource_id_idx)
        resource_name = extract_resource_name(resource_id)

        normalized.append(
            {
                "usageDate": get_row_value(row, usage_idx),
                "resourceId": resource_id,
                "resourceName": resource_name,
                "location": get_row_value(row, location_idx),
                "serviceName": get_row_value(row, service_idx),
                "product": get_row_value(row, product_idx),
                "cost": get_row_value(row, cost_idx),
                "currency": get_row_value(row, currency_idx),
            }
        )

        if not currency:
            maybe_currency = get_row_value(row, currency_idx)
            if maybe_currency:
                currency = str(maybe_currency)

    return normalized, currency


def extract_resource_name(resource_id: Any) -> str:
    raw_resource_id = str(resource_id or "").strip()
    if not raw_resource_id:
        return "Unknown"

    parts = [part for part in raw_resource_id.replace("\\", "/").split("/") if part]
    if not parts:
        return raw_resource_id

    return unquote(parts[-1]) or raw_resource_id


def parse_cost_summary(columns: Any, rows: Any) -> tuple[float, str]:
    if not isinstance(columns, list) or not isinstance(rows, list):
        return 0.0, ""

    index_map: dict[str, int] = {}
    for idx, col in enumerate(columns):
        if isinstance(col, dict):
            name = str(col.get("name") or "").lower()
            if name:
                index_map[name] = idx

    cost_idx = index_map.get("pretaxcost")
    currency_idx = index_map.get("currency")

    total_cost = 0.0
    currency = ""
    for row in rows:
        if not isinstance(row, list):
            continue
        total_cost += to_float(get_row_value(row, cost_idx))
        if not currency:
            maybe_currency = get_row_value(row, currency_idx)
            if maybe_currency:
                currency = str(maybe_currency)

    return total_cost, currency


def get_row_value(row: list[Any], index: int | None) -> Any:
    if index is None:
        return None
    if index < 0 or index >= len(row):
        return None
    return row[index]


def build_csv_by_dimension(dimension: Any, data: dict[str, Any]) -> str:
    if dimension == "subscription":
        return build_csv(["tenantName", "subscriptionId", "subscriptionName", "cost"], data.get("bySubscription", []))
    if dimension == "resourceType":
        return build_csv(
            ["tenantName", "subscription", "resourceName", "location", "serviceName", "cost"],
            data.get("byResourceType", []),
        )
    if dimension == "date":
        return build_csv(
            ["tenantName", "date", "subscription", "resourceName", "product", "cost"],
            data.get("byDate", []),
        )
    if dimension in ("all", None):
        sections = [
            "[summary]",
            build_csv(["currency", "totalCost"], [data.get("summary", {})]),
            "",
            "[subscription]",
            build_csv(["tenantName", "subscriptionId", "subscriptionName", "cost"], data.get("bySubscription", [])),
            "",
            "[resourceType]",
            build_csv(
                ["tenantName", "subscription", "resourceName", "location", "serviceName", "cost"],
                data.get("byResourceType", []),
            ),
            "",
            "[date]",
            build_csv(
                ["tenantName", "date", "subscription", "resourceName", "product", "cost"],
                data.get("byDate", []),
            ),
        ]
        return "\n".join(sections)

    raise HttpError(400, "dimension 必须是 subscription/resourceType/date/all")


def build_csv(columns: list[str], rows: Any) -> str:
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()

    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict):
                writer.writerow({key: row.get(key, "") for key in columns})

    return output.getvalue().strip("\r\n")


def extract_http_error(resp: requests.Response) -> str:
    try:
        body = resp.json()
        if isinstance(body, dict):
            if isinstance(body.get("error"), dict):
                message = body["error"].get("message")
                if message:
                    return str(message)
            if body.get("message"):
                return str(body["message"])
        return str(body)
    except ValueError:
        return resp.text or f"HTTP {resp.status_code}"


def normalize_usage_date(raw: Any) -> str:
    text = str(raw or "")
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    if len(text) >= 10 and text[4:5] == "-" and text[7:8] == "-":
        return text[:10]
    return text or "unknown-date"


def to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def round2(value: float) -> float:
    return round(float(value), 2)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5100, debug=False)

