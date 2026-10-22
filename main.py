from __future__ import annotations

import csv
import contextvars
import hashlib
import io
import logging
import sqlite3
import sys
from email.utils import parsedate_to_datetime
from logging.handlers import RotatingFileHandler
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import random
import secrets
import threading
import time
import uuid
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import msal
import requests
from requests.adapters import HTTPAdapter
from azure_token_usage import collect as collect_azure_token_usage
from flask import Flask, g, has_request_context, jsonify, redirect, render_template, request, Response, session, url_for
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
DEFAULT_SCOPE_COST_API_MAX_INFLIGHT = 1
DEFAULT_TENANT_COST_API_REQUESTS_PER_MINUTE = 15
DEFAULT_SCOPE_COST_API_REQUESTS_PER_MINUTE = 3
DEFAULT_SCOPE_COST_API_MIN_INTERVAL_SECONDS = 20
DEFAULT_RETRY_JITTER_MAX_SECONDS = 3
DEFAULT_RETRY_TOTAL_WAIT_SECONDS = 600
DEFAULT_MAX_SUBSCRIPTIONS_PER_QUERY = 0
DEFAULT_AUDIT_LOG_RETENTION_DAYS = 5
BASE_DIR = Path(__file__).resolve().parent
SYNC_CONFIG_PATH = BASE_DIR / "config" / "sync.config"
LOG_DIR = BASE_DIR / "logs"
LOG_FILE_PATH = LOG_DIR / "azure-cost-query.log"
AUDIT_LOG_DB_PATH = LOG_DIR / "azure-cost-query-audit.db"
DOTENV_PATH = BASE_DIR / ".env"
OPENAI_TIER_SCOPE = "https://management.azure.com/user_impersonation"
OPENAI_TIER_DEFAULT_API_VERSIONS = ("2026-01-15-preview", "2025-10-01-preview")
OPENAI_TIER_QUERY_TIMEOUT_SECONDS = 30
OPENAI_TIER_DEFAULT_MAX_WORKERS = 6
OPENAI_TIER_SUBSCRIPTIONS_API_VERSION = "2022-12-01"
OPENAI_TIER_AUTH_FLOW_KEY = "openai_tier_auth_flow"
OPENAI_TIER_AUTH_SESSION_KEY = "openai_tier_auth"
OPENAI_TIER_NOTICE_SESSION_KEY = "openai_tier_notice"
OPENAI_TIER_AUTH_SOURCE_SESSION_KEY = "openai_tier_auth_source"
SINGLE_TENANT_SUBSCRIPTION_CACHE_TTL_SECONDS = 300
OPENAI_TIER_ADAPTIVE_MIN_WORKERS = 1
OPENAI_TIER_ADAPTIVE_MAX_WORKERS = 16
OPENAI_TIER_ADAPTIVE_FAILURE_RATIO_THRESHOLD = 0.2
OPENAI_TIER_ADAPTIVE_THROTTLE_RATIO_THRESHOLD = 0.1
OPENAI_TIER_ADAPTIVE_LATENCY_MS_THRESHOLD = 1500

_cost_api_limit_lock = threading.Lock()
_global_cost_api_semaphore: threading.BoundedSemaphore | None = None
_global_cost_api_limit = 0
_tenant_cost_api_semaphores: dict[str, threading.BoundedSemaphore] = {}
_tenant_cost_api_limit = 0
_cost_query_governor: "CostQueryGovernor | None" = None
_single_tenant_subscription_cache_lock = threading.Lock()
_single_tenant_subscription_cache: dict[str, tuple[float, list[dict[str, str]]]] = {}
_audit_log_lock = threading.Lock()
_audit_context: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar("audit_context", default={})
_active_audit_contexts: dict[str, dict[str, Any]] = {}
_openai_tier_concurrency_controller_lock = threading.Lock()
_openai_tier_concurrency_controller: "OpenAITierConcurrencyController | None" = None
_multi_cost_jobs_lock = threading.Lock()
_multi_cost_jobs: dict[str, dict[str, Any]] = {}


def load_dotenv_file() -> None:
    if not DOTENV_PATH.exists():
        return

    for raw_line in DOTENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        env_key = key.strip()
        if not env_key:
            continue

        env_value = value.strip()
        if len(env_value) >= 2 and env_value[0] == env_value[-1] and env_value[0] in ("'", '"'):
            env_value = env_value[1:-1]

        os.environ.setdefault(env_key, env_value)


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


load_dotenv_file()
configure_logging()
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder="public", static_url_path="/static")
app.logger.setLevel(logging.INFO)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-only-change-me")
app.config.update(
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_HTTPONLY=True,
)


class HttpError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class ThrottledError(Exception):
    pass


def normalize_subscription_ids(raw_subscription_ids: Any, max_subscriptions_per_query: int) -> list[str]:
    if not isinstance(raw_subscription_ids, list):
        return []

    subscription_ids: list[str] = []
    for raw_subscription_id in raw_subscription_ids:
        subscription_id = str(raw_subscription_id or "").strip()
        if subscription_id and subscription_id not in subscription_ids:
            subscription_ids.append(subscription_id)

    if max_subscriptions_per_query > 0 and len(subscription_ids) > max_subscriptions_per_query:
        raise HttpError(400, f"最多只能选择 {max_subscriptions_per_query} 个订阅")
    return subscription_ids


@app.errorhandler(HttpError)
def handle_http_error(exc: HttpError):
    if request.path.startswith("/api/"):
        return jsonify({"message": exc.message}), exc.status_code
    return exc.message, exc.status_code


@app.errorhandler(HTTPException)
def handle_http_exception(exc: HTTPException):
    if request.path.startswith("/api/"):
        return jsonify({"message": exc.description}), exc.code or 500
    return exc


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
    scope_cost_api_max_inflight: int = DEFAULT_SCOPE_COST_API_MAX_INFLIGHT
    cost_query_rate_limit_enabled: bool = True
    tenant_cost_api_requests_per_minute: int = DEFAULT_TENANT_COST_API_REQUESTS_PER_MINUTE
    scope_cost_api_requests_per_minute: int = DEFAULT_SCOPE_COST_API_REQUESTS_PER_MINUTE
    scope_cost_api_min_interval_seconds: int = DEFAULT_SCOPE_COST_API_MIN_INTERVAL_SECONDS
    cost_query_retry_jitter_max_seconds: int = DEFAULT_RETRY_JITTER_MAX_SECONDS
    cost_query_retry_total_wait_seconds: int = DEFAULT_RETRY_TOTAL_WAIT_SECONDS
    cost_query_client_type: str = "AzureCost-FinOpsPortal"
    max_subscriptions_per_query: int = DEFAULT_MAX_SUBSCRIPTIONS_PER_QUERY
    audit_log_retention_days: int = DEFAULT_AUDIT_LOG_RETENTION_DAYS


SYNC_CONFIG_FIELD_ORDER = [
    "max_workers",
    "http_pool_connections",
    "http_pool_maxsize",
    "subscription_list_timeout_seconds",
    "cost_query_timeout_seconds",
    "cost_query_max_retries",
    "cost_query_retry_base_seconds",
]


def get_static_asset_version() -> str:
    asset_paths = [
        BASE_DIR / "public" / "css" / "styles.css",
        BASE_DIR / "public" / "js" / "storage.js",
        BASE_DIR / "public" / "js" / "query.js",
        BASE_DIR / "public" / "js" / "openai-tier.js",
        BASE_DIR / "public" / "js" / "azure-token-usage.js",
        BASE_DIR / "public" / "js" / "app-shell.js",
    ]
    latest_mtime = 0
    for asset_path in asset_paths:
        try:
            latest_mtime = max(latest_mtime, int(asset_path.stat().st_mtime))
        except OSError:
            continue
    return str(latest_mtime or int(time.time()))


def get_audit_actor() -> str:
    auth_state = session.get(OPENAI_TIER_AUTH_SESSION_KEY, {})
    if not isinstance(auth_state, dict):
        return "anonymous"
    return str(auth_state.get("preferredUsername") or auth_state.get("accountName") or "anonymous").strip() or "anonymous"


def get_request_client_ip() -> str:
    return str(request.remote_addr or "unknown")


def get_audit_connection() -> sqlite3.Connection:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(AUDIT_LOG_DB_PATH, timeout=5)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_audit_log_store() -> None:
    with _audit_log_lock, get_audit_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS api_audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                occurred_at TEXT NOT NULL,
                request_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                login_account TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                subscription_id TEXT NOT NULL,
                client_ip TEXT NOT NULL,
                request_method TEXT NOT NULL,
                request_path TEXT NOT NULL,
                status_code INTEGER NOT NULL,
                request_started_at TEXT NOT NULL DEFAULT '',
                request_finished_at TEXT NOT NULL DEFAULT '',
                elapsed_seconds REAL NOT NULL,
                throttle_count INTEGER NOT NULL DEFAULT 0,
                throttle_at TEXT NOT NULL DEFAULT '',
                throttle_rule TEXT NOT NULL DEFAULT '',
                throttle_request_path TEXT NOT NULL DEFAULT '',
                retry_wait_seconds REAL NOT NULL DEFAULT 0
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS api_audit_dimensions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                log_id INTEGER NOT NULL,
                login_account TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                subscription_id TEXT NOT NULL,
                FOREIGN KEY(log_id) REFERENCES api_audit_logs(id) ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS api_throttle_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                occurred_at TEXT NOT NULL,
                request_id TEXT NOT NULL,
                login_account TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                subscription_id TEXT NOT NULL,
                source_ip TEXT NOT NULL,
                request_path TEXT NOT NULL,
                throttle_rule TEXT NOT NULL,
                retry_wait_seconds REAL NOT NULL,
                status_code INTEGER NOT NULL DEFAULT 429
            )
            """
        )
        existing_columns = {row[1] for row in connection.execute("PRAGMA table_info(api_audit_logs)").fetchall()}
        if "request_started_at" not in existing_columns:
            connection.execute("ALTER TABLE api_audit_logs ADD COLUMN request_started_at TEXT NOT NULL DEFAULT ''")
        if "request_finished_at" not in existing_columns:
            connection.execute("ALTER TABLE api_audit_logs ADD COLUMN request_finished_at TEXT NOT NULL DEFAULT ''")
        if "throttle_request_path" not in existing_columns:
            connection.execute("ALTER TABLE api_audit_logs ADD COLUMN throttle_request_path TEXT NOT NULL DEFAULT ''")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_audit_occurred_at ON api_audit_logs(occurred_at DESC, id DESC)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_audit_login_account ON api_audit_logs(login_account, occurred_at DESC)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_audit_tenant ON api_audit_logs(tenant_id, occurred_at DESC)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_audit_subscription ON api_audit_logs(subscription_id, occurred_at DESC)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_audit_throttle ON api_audit_logs(throttle_count, occurred_at DESC)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_audit_status_path ON api_audit_logs(status_code, request_path, occurred_at DESC)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_audit_dimensions_filter ON api_audit_dimensions(login_account, tenant_id, subscription_id, log_id DESC)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_throttle_events_occurred_at ON api_throttle_events(occurred_at DESC, id DESC)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_throttle_events_filter ON api_throttle_events(login_account, tenant_id, subscription_id, request_path, status_code, occurred_at DESC)")


def normalize_dimension_pairs(raw_pairs: Any) -> list[tuple[str, str]]:
    if not isinstance(raw_pairs, list):
        return []
    pairs: list[tuple[str, str]] = []
    for pair in raw_pairs:
        tenant_id = "unknown"
        subscription_id = "unknown"
        if isinstance(pair, dict):
            tenant_id = str(pair.get("tenant_id") or "unknown").strip() or "unknown"
            subscription_id = str(pair.get("subscription_id") or "unknown").strip() or "unknown"
        elif isinstance(pair, (tuple, list)) and len(pair) >= 2:
            tenant_id = str(pair[0] or "unknown").strip() or "unknown"
            subscription_id = str(pair[1] or "unknown").strip() or "unknown"
        else:
            continue
        candidate = (tenant_id, subscription_id)
        if candidate not in pairs:
            pairs.append(candidate)
    return pairs


def build_audit_dimension_pairs(payload: dict[str, Any], context: dict[str, Any]) -> list[tuple[str, str]]:
    pairs = normalize_dimension_pairs(context.get("dimension_pairs"))
    if pairs:
        return pairs

    tenant_id = str(context.get("tenant_id") or payload.get("tenantId") or payload.get("tenantName") or "unknown").strip() or "unknown"
    subscription_ids = payload.get("subscriptionIds") if isinstance(payload.get("subscriptionIds"), list) else []
    if subscription_ids:
        normalized: list[tuple[str, str]] = []
        for raw_subscription_id in subscription_ids:
            subscription_id = str(raw_subscription_id or "").strip()
            if not subscription_id:
                continue
            candidate = (tenant_id, subscription_id)
            if candidate not in normalized:
                normalized.append(candidate)
        if normalized:
            return normalized

    fallback_subscription = str(context.get("subscription_id") or "unknown").strip() or "unknown"
    return [(tenant_id, fallback_subscription)]


def write_throttle_event(
    *,
    request_id: str,
    tenant_id: str,
    subscription_id: str,
    request_path: str,
    retry_wait_seconds: float,
    throttle_rule: str,
    occurred_at: str,
) -> None:
    with _audit_log_lock, get_audit_connection() as connection:
        connection.execute(
            """
            INSERT INTO api_throttle_events (
                occurred_at, request_id, login_account, tenant_id, subscription_id,
                source_ip, request_path, throttle_rule, retry_wait_seconds, status_code
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                occurred_at,
                request_id,
                get_audit_actor(),
                tenant_id,
                subscription_id,
                get_request_client_ip(),
                request_path,
                throttle_rule or "unknown",
                float(retry_wait_seconds or 0),
                429,
            ),
        )


def write_api_audit_log(status_code: int, elapsed_seconds: float) -> None:
    if not request.path.startswith("/api/") or bool(getattr(g, "audit_log_written", False)):
        return
    context = _audit_context.get()
    payload = request.get_json(silent=True) if request.is_json else {}
    payload = payload if isinstance(payload, dict) else {}
    request_started_at_iso = str(context.get("request_started_at") or "")
    request_finished_at_iso = datetime.now(timezone.utc).isoformat()
    raw_subscription_ids = payload.get("subscriptionIds")
    subscription_ids: list[Any] = raw_subscription_ids if isinstance(raw_subscription_ids, list) else []
    subscription_id = ",".join(str(item).strip() for item in subscription_ids if str(item).strip())
    tenant_id = str(context.get("tenant_id") or payload.get("tenantId") or payload.get("tenantName") or "unknown")
    throttle_count = int(context.get("throttle_count", 0))
    login_account = get_audit_actor()
    dimension_pairs = build_audit_dimension_pairs(payload, context)
    with _audit_log_lock, get_audit_connection() as connection:
        cursor = connection.execute(
            """
            INSERT INTO api_audit_logs (
                occurred_at, request_id, event_type, login_account, tenant_id, subscription_id,
                client_ip, request_method, request_path, status_code, request_started_at, request_finished_at, elapsed_seconds,
                throttle_count, throttle_at, throttle_rule, throttle_request_path, retry_wait_seconds
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                request_finished_at_iso,
                str(getattr(g, "request_id", "")),
                "api_request",
                login_account,
                tenant_id,
                subscription_id or str(context.get("subscription_id") or "unknown"),
                get_request_client_ip(),
                request.method,
                request.path,
                status_code,
                request_started_at_iso,
                request_finished_at_iso,
                round(elapsed_seconds, 3),
                throttle_count,
                str(context.get("throttle_at") or ""),
                str(context.get("throttle_rule") or ""),
                str(context.get("throttle_request_path") or ""),
                float(context.get("retry_wait_seconds", 0)),
            ),
        )
        log_id = int(cursor.lastrowid or 0)
        for tenant_value, subscription_value in dimension_pairs:
            connection.execute(
                """
                INSERT INTO api_audit_dimensions (log_id, login_account, tenant_id, subscription_id)
                VALUES (?, ?, ?, ?)
                """,
                (log_id, login_account, tenant_value, subscription_value),
            )
    g.audit_log_written = True


def purge_expired_audit_logs(retention_days: int) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, retention_days))).isoformat()
    with _audit_log_lock, get_audit_connection() as connection:
        connection.execute("DELETE FROM api_audit_logs WHERE occurred_at < ?", (cutoff,))


def update_audit_context(**kwargs: Any) -> None:
    current = dict(_audit_context.get() or {})
    current.update({key: value for key, value in kwargs.items() if value is not None})
    _audit_context.set(current)
    request_id = str(current.get("request_id") or (getattr(g, "request_id", "") if has_request_context() else ""))
    if request_id:
        with _audit_log_lock:
            _active_audit_contexts[request_id] = current


def record_throttle_audit(tenant_id: str, subscription_id: str, request_path: str, retry_wait_seconds: float, throttle_rule: str) -> None:
    context = dict(_audit_context.get() or {})
    request_id = str(context.get("request_id") or (getattr(g, "request_id", "") if has_request_context() else ""))
    if request_id:
        with _audit_log_lock:
            context = dict(_active_audit_contexts.get(request_id, context))
    context["tenant_id"] = tenant_id or context.get("tenant_id") or "unknown"
    context["subscription_id"] = subscription_id or context.get("subscription_id") or "unknown"
    context["dimension_pairs"] = normalize_dimension_pairs(context.get("dimension_pairs")) or [
        {"tenant_id": context["tenant_id"], "subscription_id": context["subscription_id"]}
    ]
    context["throttle_count"] = int(context.get("throttle_count", 0)) + 1
    occurred_at = datetime.now(timezone.utc).isoformat()
    context["throttle_at"] = occurred_at
    context["throttle_rule"] = throttle_rule or "unknown"
    context["retry_wait_seconds"] = float(context.get("retry_wait_seconds", 0)) + float(retry_wait_seconds or 0)
    context["throttle_request_path"] = request_path
    _audit_context.set(context)
    try:
        write_throttle_event(
            request_id=request_id,
            tenant_id=context["tenant_id"],
            subscription_id=context["subscription_id"],
            request_path=request_path,
            retry_wait_seconds=retry_wait_seconds,
            throttle_rule=throttle_rule,
            occurred_at=occurred_at,
        )
    except Exception:
        logger.exception("throttle event write failed: request_id=%s", request_id)
    if request_id:
        with _audit_log_lock:
            _active_audit_contexts[request_id] = context


def parse_audit_limit(raw_limit: Any) -> int:
    try:
        limit = int(str(raw_limit or "100"))
    except ValueError:
        limit = 100
    return max(1, min(500, limit))


def query_audit_logs(filters: dict[str, str], limit: int) -> list[dict[str, Any]]:
    log_clauses: list[str] = []
    log_params: list[Any] = []
    dim_clauses: list[str] = []
    dim_params: list[Any] = []

    login_account = filters.get("loginAccount") or ""
    tenant_id = filters.get("tenantId") or ""
    subscription_id = filters.get("subscriptionId") or ""
    if login_account:
        dim_clauses.append("d.login_account = ?")
        dim_params.append(login_account)
    if tenant_id:
        dim_clauses.append("d.tenant_id = ?")
        dim_params.append(tenant_id)
    if subscription_id:
        dim_clauses.append("d.subscription_id = ?")
        dim_params.append(subscription_id)

    mapping = {
        "requestPath": "l.request_path",
        "statusCode": "l.status_code",
        "sourceIp": "l.client_ip",
        "throttleRule": "l.throttle_rule",
        "throttleRequestPath": "l.throttle_request_path",
    }
    for filter_key, column_name in mapping.items():
        value = filters.get(filter_key)
        if value:
            log_clauses.append(f"{column_name} = ?")
            log_params.append(value)

    if filters.get("throttled") == "true":
        log_clauses.append("l.throttle_count > 0")
    elif filters.get("throttled") == "false":
        log_clauses.append("l.throttle_count = 0")

    clauses = list(dim_clauses) + list(log_clauses)
    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = (
        "SELECT DISTINCT l.* "
        "FROM api_audit_logs l "
        "LEFT JOIN api_audit_dimensions d ON d.log_id = l.id "
        f"{where_sql} "
        "ORDER BY l.occurred_at DESC, l.id DESC LIMIT ?"
    )

    with _audit_log_lock, get_audit_connection() as connection:
        rows = connection.execute(sql, (*dim_params, *log_params, limit)).fetchall()
        request_ids = [str(row["request_id"]) for row in rows if str(row["request_id"]).strip()]
        throttle_events_by_request: dict[str, list[dict[str, Any]]] = {}
        if request_ids:
            placeholders = ",".join("?" for _ in request_ids)
            event_rows = connection.execute(
                f"SELECT * FROM api_throttle_events WHERE request_id IN ({placeholders}) ORDER BY occurred_at ASC, id ASC",
                tuple(request_ids),
            ).fetchall()
            for event_row in event_rows:
                event_data = dict(event_row)
                throttle_events_by_request.setdefault(str(event_data.get("request_id") or ""), []).append(event_data)

    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["throttle_events"] = throttle_events_by_request.get(str(item.get("request_id") or ""), [])
        result.append(item)
    return result


def parse_throttle_group_by(raw_group_by: Any) -> str:
    normalized = str(raw_group_by or "hour").strip().lower()
    allowed = {"hour", "day", "requestpath", "rule", "tenant", "subscription"}
    if normalized in allowed:
        return normalized
    return "hour"


def query_throttle_trends(filters: dict[str, str], limit: int, group_by: str) -> list[dict[str, Any]]:
    group_mapping = {
        "hour": "substr(occurred_at, 1, 13)",
        "day": "substr(occurred_at, 1, 10)",
        "requestpath": "request_path",
        "rule": "throttle_rule",
        "tenant": "tenant_id",
        "subscription": "subscription_id",
    }
    group_expr = group_mapping.get(group_by, group_mapping["hour"])
    clauses: list[str] = []
    params: list[Any] = []

    mapping = {
        "loginAccount": "login_account",
        "tenantId": "tenant_id",
        "subscriptionId": "subscription_id",
        "requestPath": "request_path",
        "sourceIp": "source_ip",
        "throttleRule": "throttle_rule",
    }
    for filter_key, column_name in mapping.items():
        value = filters.get(filter_key)
        if value:
            clauses.append(f"{column_name} = ?")
            params.append(value)

    start_at = filters.get("startAt")
    end_at = filters.get("endAt")
    if start_at:
        clauses.append("occurred_at >= ?")
        params.append(start_at)
    if end_at:
        clauses.append("occurred_at <= ?")
        params.append(end_at)

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = (
        "SELECT "
        f"{group_expr} AS bucket, "
        "COUNT(*) AS throttle_count, "
        "MIN(occurred_at) AS first_throttle_at, "
        "MAX(occurred_at) AS last_throttle_at, "
        "ROUND(AVG(retry_wait_seconds), 3) AS avg_retry_wait_seconds, "
        "ROUND(MAX(retry_wait_seconds), 3) AS max_retry_wait_seconds "
        "FROM api_throttle_events "
        f"{where_sql} "
        "GROUP BY bucket "
        "ORDER BY last_throttle_at DESC "
        "LIMIT ?"
    )

    with _audit_log_lock, get_audit_connection() as connection:
        rows = connection.execute(sql, (*params, limit)).fetchall()
    return [dict(row) for row in rows]


def extract_throttle_rule(headers: Any) -> str:
    lower_headers = {str(key).lower(): value for key, value in (headers or {}).items()}
    matched_rules = [
        header_name
        for header_name in (
            "retry-after",
            "x-ms-ratelimit-microsoft.costmanagement-qpu-retry-after",
            "x-ms-ratelimit-microsoft.costmanagement-entity-retry-after",
            "x-ms-ratelimit-microsoft.costmanagement-tenant-retry-after",
            "x-ms-ratelimit-microsoft.costmanagement-client-retry-after",
            "x-ms-ratelimit-microsoft.costmanagement-clienttype-retry-after",
        )
        if header_name in lower_headers
    ]
    return ",".join(matched_rules) or "http-429"


def normalize_throttle_request_path(raw_url: str) -> str:
    parsed = urlparse(raw_url)
    if parsed.path:
        return parsed.path
    return raw_url


initialize_audit_log_store()


@app.get("/")
def index():
    return render_template("index.html", static_version=get_static_asset_version())


@app.get("/favicon.ico")
def favicon():
    return Response(status=204)


@app.after_request
def add_no_cache_headers(resp: Response):
    if request.method == "GET" and request.path == "/":
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    if request.path.startswith("/api/"):
        elapsed_seconds = max(0, time.perf_counter() - getattr(g, "request_started_at", time.perf_counter()))
        try:
            write_api_audit_log(resp.status_code, elapsed_seconds)
        except Exception:
            logger.exception("api audit log write failed: request_id=%s path=%s", getattr(g, "request_id", ""), request.path)
        resp.headers["X-Request-ID"] = str(getattr(g, "request_id", ""))
        with _audit_log_lock:
            _active_audit_contexts.pop(str(getattr(g, "request_id", "")), None)
    return resp


@app.before_request
def start_api_audit_request() -> None:
    g.request_id = str(uuid.uuid4())
    g.request_started_at = time.perf_counter()
    g.audit_log_written = False
    _audit_context.set(
        {
            "request_id": g.request_id,
            "request_started_at": datetime.now(timezone.utc).isoformat(),
            "dimension_pairs": [],
        }
    )
    with _audit_log_lock:
        _active_audit_contexts[g.request_id] = {"request_id": g.request_id}
    if request.path.startswith("/api/"):
        sync_config = load_sync_config()
        try:
            purge_expired_audit_logs(sync_config.audit_log_retention_days)
        except Exception:
            logger.exception("api audit retention cleanup failed")


@app.teardown_request
def audit_unhandled_api_exception(exc: BaseException | None) -> None:
    if not request.path.startswith("/api/"):
        return
    try:
        if exc is not None:
            elapsed_seconds = max(0, time.perf_counter() - getattr(g, "request_started_at", time.perf_counter()))
            try:
                write_api_audit_log(500, elapsed_seconds)
            except Exception:
                logger.exception("unhandled api audit log write failed: request_id=%s", getattr(g, "request_id", ""))
    finally:
        with _audit_log_lock:
            _active_audit_contexts.pop(str(getattr(g, "request_id", "")), None)


@app.get("/api/audit-logs")
def list_api_audit_logs():
    started_at = time.perf_counter()
    filters = {
        "loginAccount": str(request.args.get("loginAccount") or "").strip(),
        "tenantId": str(request.args.get("tenantId") or "").strip(),
        "subscriptionId": str(request.args.get("subscriptionId") or "").strip(),
        "requestPath": str(request.args.get("requestPath") or "").strip(),
        "statusCode": str(request.args.get("statusCode") or "").strip(),
        "sourceIp": str(request.args.get("sourceIp") or "").strip(),
        "throttleRule": str(request.args.get("throttleRule") or "").strip(),
        "throttleRequestPath": str(request.args.get("throttleRequestPath") or "").strip(),
        "throttled": str(request.args.get("throttled") or "").strip().lower(),
    }
    limit = parse_audit_limit(request.args.get("limit"))
    rows = query_audit_logs(filters, limit)
    elapsed_ms = round((time.perf_counter() - started_at) * 1000, 3)
    active_filter_count = sum(1 for value in filters.values() if value)
    performance_target_ms = 200 if active_filter_count <= 1 else 500
    return jsonify(
        {
            "items": rows,
            "summary": {
                "count": len(rows),
                "limit": limit,
                "elapsedSeconds": round(elapsed_ms / 1000.0, 3),
                "elapsedMs": elapsed_ms,
                "activeFilterCount": active_filter_count,
                "performanceTargetMs": performance_target_ms,
                "withinPerformanceTarget": elapsed_ms <= performance_target_ms,
            },
        }
    )


@app.get("/api/audit-logs/throttle-trends")
def get_api_audit_throttle_trends():
    started_at = time.perf_counter()
    filters = {
        "loginAccount": str(request.args.get("loginAccount") or "").strip(),
        "tenantId": str(request.args.get("tenantId") or "").strip(),
        "subscriptionId": str(request.args.get("subscriptionId") or "").strip(),
        "requestPath": str(request.args.get("requestPath") or "").strip(),
        "sourceIp": str(request.args.get("sourceIp") or "").strip(),
        "throttleRule": str(request.args.get("throttleRule") or "").strip(),
        "startAt": str(request.args.get("startAt") or "").strip(),
        "endAt": str(request.args.get("endAt") or "").strip(),
    }
    limit = parse_audit_limit(request.args.get("limit"))
    group_by = parse_throttle_group_by(request.args.get("groupBy"))
    rows = query_throttle_trends(filters, limit, group_by)
    elapsed_ms = round((time.perf_counter() - started_at) * 1000, 3)
    active_filter_count = sum(1 for value in filters.values() if value)
    performance_target_ms = 200 if active_filter_count <= 1 else 500
    return jsonify(
        {
            "items": rows,
            "summary": {
                "count": len(rows),
                "groupBy": group_by,
                "limit": limit,
                "elapsedSeconds": round(elapsed_ms / 1000.0, 3),
                "elapsedMs": elapsed_ms,
                "activeFilterCount": active_filter_count,
                "performanceTargetMs": performance_target_ms,
                "withinPerformanceTarget": elapsed_ms <= performance_target_ms,
            },
        }
    )


@app.get("/api/openai-tier/auth/login-url")
def get_openai_tier_login_url():
    config = get_openai_tier_auth_config()
    auth_source = normalize_openai_auth_source(request.args.get("source"))
    auth_flow = build_openai_tier_msal_app(config).initiate_auth_code_flow(
        scopes=config["scopes"],
        redirect_uri=get_openai_tier_redirect_uri(),
        state=build_openai_auth_flow_state(auth_source),
        prompt="select_account",
    )
    auth_flow["copilot_source"] = auth_source
    session[OPENAI_TIER_AUTH_FLOW_KEY] = auth_flow
    session[OPENAI_TIER_AUTH_SOURCE_SESSION_KEY] = auth_source
    return jsonify({"authUrl": auth_flow["auth_uri"]})


@app.get("/auth/openai/callback")
def openai_tier_auth_callback():
    config = get_openai_tier_auth_config()
    auth_flow = session.pop(OPENAI_TIER_AUTH_FLOW_KEY, None)
    source_from_request = resolve_openai_auth_source_from_request_args(request.args)
    source_from_session = normalize_openai_auth_source(session.pop(OPENAI_TIER_AUTH_SOURCE_SESSION_KEY, None), fallback="")
    auth_source = source_from_request or source_from_session or "openai-tier"

    if isinstance(auth_flow, dict):
        source_from_flow = normalize_openai_auth_source(auth_flow.get("copilot_source"), fallback="")
        auth_source = source_from_request or source_from_flow or source_from_session or "openai-tier"

    if not isinstance(auth_flow, dict):
        set_openai_tier_notice("登录会话已过期，请重新登录。", is_error=True)
        return redirect_for_openai_auth_source(auth_source)

    try:
        result = build_openai_tier_msal_app(config).acquire_token_by_auth_code_flow(auth_flow, request.args)
    except Exception as exc:
        set_openai_tier_notice(f"Microsoft 登录失败：{str(exc).strip()}", is_error=True)
        return redirect_for_openai_auth_source(auth_source)

    error = str(result.get("error") or "").strip()
    if error:
        message = str(result.get("error_description") or error).strip()
        set_openai_tier_notice(f"Microsoft 登录失败：{message}", is_error=True)
        return redirect_for_openai_auth_source(auth_source)

    id_claims = result.get("id_token_claims", {}) if isinstance(result, dict) else {}
    access_token = str(result.get("access_token") or "").strip()
    if not access_token:
        set_openai_tier_notice("Microsoft 登录失败：未获取到访问令牌。", is_error=True)
        return redirect_for_openai_auth_source(auth_source)

    expires_in = max(60, int(result.get("expires_in") or 3600))
    session[OPENAI_TIER_AUTH_SESSION_KEY] = {
        "accessToken": access_token,
        "expiresAt": int(time.time()) + expires_in - 60,
        "accountName": str(id_claims.get("name") or "").strip(),
        "preferredUsername": str(
            id_claims.get("preferred_username")
            or id_claims.get("upn")
            or id_claims.get("email")
            or ""
        ).strip(),
        "tenantId": str(id_claims.get("tid") or "").strip(),
    }
    set_openai_tier_notice("Microsoft 登录成功。")
    return redirect_for_openai_auth_source(auth_source)


@app.get("/auth/openai/logout")
def openai_tier_logout():
    session.pop(OPENAI_TIER_AUTH_FLOW_KEY, None)
    session.pop(OPENAI_TIER_AUTH_SOURCE_SESSION_KEY, None)
    session.pop(OPENAI_TIER_AUTH_SESSION_KEY, None)
    set_openai_tier_notice("已退出 OpenAI Tier 查询登录状态。")
    return redirect_for_openai_auth_source(request.args.get("source"))


def normalize_openai_auth_source(raw_source: Any, fallback: str = "openai-tier") -> str:
    source = str(raw_source or "").strip().lower()
    if source in ("single-tenant-cost", "single", "cost-single"):
        return "single-tenant-cost"
    if source in ("azure-token-usage", "azure-token", "token", "model-token"):
        return "azure-token-usage"
    if source == "azure-diagnostics":
        return "azure-diagnostics"
    if source in ("openai-tier", "tier", "openai"):
        return "openai-tier"
    return fallback


def build_openai_auth_flow_state(auth_source: str) -> str:
    normalized = normalize_openai_auth_source(auth_source)
    random_suffix = secrets.token_urlsafe(18)
    return f"copilot-{normalized}-{random_suffix}"


def extract_openai_auth_source_from_state(raw_state: Any) -> str:
    state = str(raw_state or "").strip()
    if not state.startswith("copilot-"):
        return ""

    if state.startswith("copilot-single-tenant-cost-"):
        return "single-tenant-cost"
    if state.startswith("copilot-azure-token-usage-"):
        return "azure-token-usage"
    if state.startswith("copilot-azure-diagnostics-"):
        return "azure-diagnostics"
    if state.startswith("copilot-openai-tier-"):
        return "openai-tier"
    return ""


def resolve_openai_auth_source_from_request_args(args: Any) -> str:
    source_from_query = normalize_openai_auth_source((args or {}).get("source"), fallback="")
    if source_from_query:
        return source_from_query

    source_from_state = extract_openai_auth_source_from_state((args or {}).get("state"))
    return normalize_openai_auth_source(source_from_state, fallback="")


def redirect_for_openai_auth_source(source: str):
    normalized = normalize_openai_auth_source(source)
    if normalized == "single-tenant-cost":
        return redirect(url_for("index", view="costs", costMode="single"))
    if normalized == "azure-token-usage":
        return redirect(url_for("index", view="azure-token-usage"))
    if normalized == "azure-diagnostics":
        return redirect(url_for("index", view="azure-diagnostics"))
    return redirect(url_for("index", view="openai-tier"))


def get_openai_tier_redirect_uri() -> str:
    custom_redirect_uri = str(os.getenv("OPENAI_TIER_REDIRECT_URI", "")).strip()
    if custom_redirect_uri:
        return custom_redirect_uri
    return url_for("openai_tier_auth_callback", _external=True)


@app.get("/api/openai-tier/auth/status")
def get_openai_tier_auth_status():
    config = get_openai_tier_auth_config(optional=True)
    auth_state = get_openai_tier_auth_state()
    notice = session.pop(OPENAI_TIER_NOTICE_SESSION_KEY, None)
    return jsonify(
        {
            "configured": config["configured"],
            "signedIn": auth_state is not None,
            "account": auth_state.get("account") if auth_state else None,
            "loginUrlApi": url_for("get_openai_tier_login_url"),
            "logoutUrl": url_for("openai_tier_logout"),
            "notice": notice,
        }
    )


@app.get("/api/openai-tier/subscriptions")
def list_openai_tier_subscriptions():
    auth_state = require_openai_tier_auth_state()
    sync_config = load_sync_config()
    with build_http_session(sync_config) as http_session:
        subscriptions = get_cached_user_subscriptions(auth_state, http_session, sync_config)
    return jsonify({"subscriptions": subscriptions})


@app.get("/api/azure-token/subscriptions")
def list_azure_token_subscriptions():
    """令牌统计复用 OpenAI Tier 的 Entra 登录会话和订阅列表。"""
    auth_state = require_openai_tier_auth_state()
    sync_config = load_sync_config()
    with build_http_session(sync_config) as http_session:
        subscriptions = get_cached_user_subscriptions(auth_state, http_session, sync_config)
    return jsonify({"subscriptions": subscriptions})


@app.post("/api/azure-token/query")
def query_azure_token_usage():
    payload = request.get_json(silent=True) or {}
    auth_state = require_openai_tier_auth_state()
    raw_subscription_ids = payload.get("subscriptionIds")
    if not isinstance(raw_subscription_ids, list):
        raw_subscription_ids = [payload.get("subscriptionId")]
    subscription_ids = list(dict.fromkeys(str(item or "").strip() for item in raw_subscription_ids if str(item or "").strip()))
    start_date = str(payload.get("startDate") or "").strip()
    end_date = str(payload.get("endDate") or start_date).strip()
    timezone_text = str(payload.get("tz") or "+8").strip()
    interval = str(payload.get("interval") or "FULL").strip()
    scope = str(payload.get("scope") or "resource").strip()

    if not subscription_ids or not start_date or not end_date:
        raise HttpError(400, "订阅和日期范围为必填项")
    try:
        start = datetime.strptime(start_date, "%Y-%m-%d")
        end = datetime.strptime(end_date, "%Y-%m-%d")
    except ValueError as exc:
        raise HttpError(400, "日期格式不正确，应为 YYYY-MM-DD") from exc
    if start > end:
        raise HttpError(400, "结束日期不能早于开始日期")
    if end.date() > datetime.now(timezone.utc).date():
        raise HttpError(400, "结束日期不能晚于当前日期")

    update_audit_context(
        tenant_id=str(auth_state.get("account", {}).get("tenantId") or "current-tenant"),
        subscription_id=",".join(subscription_ids),
    )
    try:
        sync_config = load_sync_config()
        with build_http_session(sync_config) as http_session:
            available = get_cached_user_subscriptions(auth_state, http_session, sync_config)
        names = {str(item.get("subscriptionId")): str(item.get("subscriptionName") or item.get("subscriptionId")) for item in available}
        unauthorized = [item for item in subscription_ids if item not in names]
        if unauthorized:
            raise HttpError(403, "所选订阅不存在、已失效或当前账号无访问权限")

        max_workers = min(len(subscription_ids), max(1, int(os.getenv("AZURE_TOKEN_MAX_WORKERS", "6"))))
        results: list[dict[str, Any]] = []
        errors: list[str] = []
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    collect_azure_token_usage,
                    access_token=auth_state["accessToken"],
                    subscription_id=subscription_id,
                    subscription_name=names[subscription_id],
                    start_date=start_date,
                    end_date=end_date,
                    timezone_text=timezone_text,
                    interval=interval,
                    scope=scope,
                ): subscription_id
                for subscription_id in subscription_ids
            }
            for future in as_completed(futures):
                subscription_id = futures[future]
                try:
                    results.append(future.result())
                except Exception as exc:
                    logger.exception("azure token usage query failed: subscription=%s", subscription_id)
                    errors.append(f"{names.get(subscription_id, subscription_id)}：{exc}")

        if not results:
            raise HttpError(502, "所有订阅的 Azure 模型令牌统计均失败：" + "；".join(errors))
        result = merge_azure_token_usage_results(results, errors, subscription_ids)
    except ValueError as exc:
        raise HttpError(400, str(exc)) from exc
    except HttpError:
        raise
    except Exception as exc:
        logger.exception("azure token usage query failed: subscription=%s", subscription_id)
        raise HttpError(502, f"Azure 模型令牌统计失败：{exc}") from exc
    return jsonify(result)


def merge_azure_token_usage_results(results: list[dict[str, Any]], errors: list[str], subscription_ids: list[str]) -> dict[str, Any]:
    rows = [row for result in results for row in result.get("rows", [])]
    summary = [item for result in results for item in result.get("summary", [])]
    regions = sorted({region for result in results for region in result.get("regions", [])})
    warnings = [warning for result in results for warning in result.get("warnings", [])] + errors
    totals = {metric: sum(float(row.get(metric, 0) or 0) for row in rows) for metric in ("ProcessedPromptTokens", "GeneratedTokens", "TokenTransaction", "Total")}
    first = results[0]
    return {
        "subscriptionIds": subscription_ids,
        "startDate": first["startDate"],
        "endDate": first["endDate"],
        "tz": first["tz"],
        "timespan": first["timespan"],
        "startUtc": first["startUtc"],
        "endUtc": first["endUtc"],
        "regions": regions,
        "accountCount": sum(int(result.get("accountCount", 0)) for result in results),
        "metrics": first.get("metrics", []),
        "rows": rows,
        "summary": summary,
        "grandTotal": totals["Total"],
        "warnings": warnings,
    }


@app.post("/api/single-tenant/query")
def query_single_tenant_costs():
    payload = request.get_json(silent=True) or {}
    if request.args.get("async") == "1":
        auth_state = require_openai_tier_auth_state()
        sync_config = load_sync_config()
        subscription_ids = normalize_subscription_ids(payload.get("subscriptionIds"), sync_config.max_subscriptions_per_query)
        if not subscription_ids:
            raise HttpError(400, "subscriptionIds、startDate 和 endDate 为必填项")
        job_id = uuid.uuid4().hex
        with _multi_cost_jobs_lock:
            _multi_cost_jobs[job_id] = {"status": "queued", "result": None, "error": ""}
        threading.Thread(target=run_single_tenant_cost_job, args=(job_id, payload, auth_state), daemon=True).start()
        return jsonify({"jobId": job_id, "status": "queued"}), 202

    start_date = payload.get("startDate")
    end_date = payload.get("endDate")
    auth_state = require_openai_tier_auth_state()
    sync_config = load_sync_config()
    subscription_ids = normalize_subscription_ids(payload.get("subscriptionIds"), sync_config.max_subscriptions_per_query)
    started_at = time.perf_counter()

    if not start_date or not end_date or not subscription_ids:
        raise HttpError(400, "subscriptionIds、startDate 和 endDate 为必填项")

    try:
        start = datetime.strptime(str(start_date), "%Y-%m-%d")
        end = datetime.strptime(str(end_date), "%Y-%m-%d")
    except ValueError as exc:
        raise HttpError(400, "日期格式不正确，应为 YYYY-MM-DD") from exc

    if start > end:
        raise HttpError(400, "开始时间不能晚于结束时间")

    tenant_label = "当前租户"
    account = auth_state.get("account", {})
    update_audit_context(
        tenant_id=str(account.get("tenantId") or tenant_label),
        subscription_id=",".join(subscription_ids),
        dimension_pairs=[
            {"tenant_id": str(account.get("tenantId") or tenant_label), "subscription_id": sub_id}
            for sub_id in subscription_ids
            if str(sub_id or "").strip()
        ],
    )
    logger.info(
        "cost_query_monitor event=single_tenant_started start_date=%s end_date=%s requested_subscriptions=%s subscription_limit=%s account=%s",
        start_date,
        end_date,
        len(subscription_ids),
        sync_config.max_subscriptions_per_query or "unlimited",
        auth_state.get("account", {}).get("preferredUsername") or auth_state.get("account", {}).get("name") or "unknown",
    )

    with build_http_session(sync_config) as http_session:
        subscriptions = get_cached_user_subscriptions(auth_state, http_session, sync_config)

        if not subscriptions:
            raise HttpError(400, "当前租户下未找到任何订阅")

        allowed_subscriptions = {
            item["subscriptionId"]: item
            for item in subscriptions
            if item.get("subscriptionId")
        }
        unauthorized_subscription_ids = [subscription_id for subscription_id in subscription_ids if subscription_id not in allowed_subscriptions]
        if unauthorized_subscription_ids:
            raise HttpError(403, "所选订阅不存在、已失效或当前账号无访问权限")

        subscription_objects = [
            Subscription(
                subscription_id=allowed_subscriptions[subscription_id]["subscriptionId"],
                subscription_name=allowed_subscriptions[subscription_id]["subscriptionName"],
                tenant_name=tenant_label,
            )
            for subscription_id in subscription_ids
        ]

        result = query_and_aggregate_costs(
            token=auth_state["accessToken"],
            subscriptions=subscription_objects,
            start_date=str(start_date),
            end_date=str(end_date),
            session=http_session,
            sync_config=sync_config,
        )

    logger.info(
        "cost_query_monitor event=single_tenant_finished subscriptions=%s total_cost=%s elapsed_ms=%s warnings=%s",
        len(subscription_objects),
        result.get("summary", {}).get("totalCost"),
        int((time.perf_counter() - started_at) * 1000),
        len(result.get("warnings", [])),
    )

    return jsonify(result)


def run_single_tenant_cost_job(job_id: str, payload: dict[str, Any], auth_state: dict[str, Any]) -> None:
    with _multi_cost_jobs_lock:
        _multi_cost_jobs[job_id]["status"] = "running"
    try:
        with app.test_request_context("/api/single-tenant/query?async-worker=1", method="POST", json=payload):
            session[OPENAI_TIER_AUTH_SESSION_KEY] = auth_state
            response = query_single_tenant_costs()
            result = response[0] if isinstance(response, tuple) else response
            result_json = result.get_json() if hasattr(result, "get_json") else {}
            status_code = response[1] if isinstance(response, tuple) else 200
        with _multi_cost_jobs_lock:
            _multi_cost_jobs[job_id].update({
                "status": "succeeded" if status_code < 400 else "failed",
                "result": result_json if status_code < 400 else None,
                "error": result_json.get("message") if status_code >= 400 else "",
            })
    except Exception as exc:
        logger.exception("async single-tenant cost job failed: job_id=%s", job_id)
        with _multi_cost_jobs_lock:
            _multi_cost_jobs[job_id].update({"status": "failed", "error": str(exc)})


@app.get("/api/single-tenant/query/jobs/<job_id>")
def get_single_tenant_cost_job(job_id: str):
    return get_multi_cost_job(job_id)


@app.post("/api/openai-tier/query")
def query_openai_tiers():
    payload = request.get_json(silent=True) or {}
    selected_ids = payload.get("subscriptionIds")
    auth_state = require_openai_tier_auth_state()
    sync_config = load_sync_config()
    started_at = time.perf_counter()

    with build_http_session(sync_config) as http_session:
        subscriptions = fetch_enabled_user_subscriptions(
            token=auth_state["accessToken"],
            http_session=http_session,
            timeout_seconds=sync_config.subscription_list_timeout_seconds,
        )
        if isinstance(selected_ids, list) and selected_ids:
            selected_set = {str(item).strip() for item in selected_ids if str(item).strip()}
            subscriptions = [item for item in subscriptions if item["subscriptionId"] in selected_set]

        update_audit_context(
            tenant_id=str(auth_state.get("account", {}).get("tenantId") or "current-tenant"),
            subscription_id=",".join(subscription["subscriptionId"] for subscription in subscriptions),
            dimension_pairs=[
                {
                    "tenant_id": str(auth_state.get("account", {}).get("tenantId") or "current-tenant"),
                    "subscription_id": str(subscription.get("subscriptionId") or "unknown"),
                }
                for subscription in subscriptions
                if str(subscription.get("subscriptionId") or "").strip()
            ],
        )

        logger.info(
            "openai tier query started: requested=%s matched=%s",
            len(selected_ids) if isinstance(selected_ids, list) else 0,
            len(subscriptions),
        )
        rows, warnings = query_openai_tier_rows(
            token=auth_state["accessToken"],
            subscriptions=subscriptions,
            sync_config=sync_config,
        )

    ok_count = sum(1 for row in rows if row.get("queryStatus") == "OK")
    failed_count = max(0, len(rows) - ok_count)
    elapsed_ms = int((time.perf_counter() - started_at) * 1000)
    logger.info(
        "openai tier query finished: subscriptions=%s ok=%s failed=%s elapsed_ms=%s",
        len(subscriptions),
        ok_count,
        failed_count,
        elapsed_ms,
    )

    response_payload = {
        "results": rows,
        "summary": {
            "subscriptionCount": len(subscriptions),
            "okCount": ok_count,
            "failedCount": failed_count,
            "elapsedMs": elapsed_ms,
            "cachedAt": datetime.now(timezone.utc).isoformat(),
        },
    }
    if warnings:
        response_payload["warnings"] = warnings
    return jsonify(response_payload)


@app.post("/api/costs/query")
def query_costs():
    payload = request.get_json(silent=True) or {}
    requested_auth_list = payload.get("authList", [])
    if not requested_auth_list and payload.get("auth"):
        requested_auth_list = [payload.get("auth")]
    if not requested_auth_list:
        return jsonify({"error": "missing auth"}), 400

    if request.args.get("async") == "1":
        job_id = uuid.uuid4().hex
        with _multi_cost_jobs_lock:
            _multi_cost_jobs[job_id] = {"status": "queued", "result": None, "error": ""}
        threading.Thread(target=run_multi_cost_job, args=(job_id, payload), daemon=True).start()
        return jsonify({"jobId": job_id, "status": "queued"}), 202

    start_date = payload.get("startDate")
    end_date = payload.get("endDate")
    auth_list = requested_auth_list
    auth = payload.get("auth")
    sync_config = load_sync_config()

    if not auth_list and auth:
        auth_list = [auth]

    update_audit_context(
        tenant_id=",".join(str(item.get("tenantName") or item.get("tenantId") or "unknown") for item in auth_list if isinstance(item, dict)),
        subscription_id=",".join(str(item.get("subscriptionId") or "all") for item in auth_list if isinstance(item, dict)),
        dimension_pairs=[
            {
                "tenant_id": str(item.get("tenantName") or item.get("tenantId") or "unknown").strip() or "unknown",
                "subscription_id": str(item.get("subscriptionId") or "all").strip() or "all",
            }
            for item in auth_list
            if isinstance(item, dict)
        ],
    )

    logger.info(
        "cost query started: start_date=%s end_date=%s auth_count=%s",
        start_date,
        end_date,
        len(auth_list),
    )

    def query_auth_costs(idx: int, auth: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
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
                    return None, f"[{tenant_label}] 未找到任何订阅"

                result = query_and_aggregate_costs(
                    token=token,
                    subscriptions=subscriptions,
                    start_date=start_date,
                    end_date=end_date,
                    session=session,
                    sync_config=sync_config,
                )
                return result, None
        except HttpError as e:
            logger.error("cost query for auth failed: tenant=%s status=%s message=%s", tenant_label, e.status_code, e.message)
            return None, f"[{tenant_label}] {e.message}"
        except Exception as e:
            logger.error("cost query for auth failed: tenant=%s error=%s", tenant_label, str(e))
            return None, f"[{tenant_label}] {str(e)}"

    results = []
    auth_errors: list[str] = []
    auth_workers = max(1, min(sync_config.max_workers, len(auth_list)))
    with ThreadPoolExecutor(max_workers=auth_workers) as executor:
        futures = [executor.submit(query_auth_costs, idx, auth) for idx, auth in enumerate(auth_list)]
        for future in as_completed(futures):
            result, auth_error = future.result()
            if result is not None:
                results.append(result)
            if auth_error:
                auth_errors.append(auth_error)

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


def run_multi_cost_job(job_id: str, payload: dict[str, Any]) -> None:
    with _multi_cost_jobs_lock:
        _multi_cost_jobs[job_id]["status"] = "running"
    try:
        with app.test_request_context("/api/costs/query?async-worker=1", method="POST", json=payload):
            response = query_costs()
            result = response[0] if isinstance(response, tuple) else response
            result_json = result.get_json() if hasattr(result, "get_json") else {}
            status_code = response[1] if isinstance(response, tuple) else 200
        with _multi_cost_jobs_lock:
            _multi_cost_jobs[job_id].update({
                "status": "succeeded" if status_code < 400 else "failed",
                "result": result_json if status_code < 400 else None,
                "error": result_json.get("message") if status_code >= 400 else "",
            })
    except Exception as exc:
        logger.exception("async multi-tenant cost job failed: job_id=%s", job_id)
        with _multi_cost_jobs_lock:
            _multi_cost_jobs[job_id].update({"status": "failed", "error": str(exc)})


@app.get("/api/costs/query/jobs/<job_id>")
def get_multi_cost_job(job_id: str):
    with _multi_cost_jobs_lock:
        job = _multi_cost_jobs.get(job_id)
        if not job:
            return jsonify({"message": "查询任务不存在或已过期"}), 404
        return jsonify({"status": job["status"], "result": job.get("result"), "error": job.get("error", "")})


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


def get_openai_tier_auth_config(optional: bool = False) -> dict[str, Any]:
    client_id = str(os.getenv("OPENAI_TIER_CLIENT_ID", "")).strip()
    client_secret = str(os.getenv("OPENAI_TIER_CLIENT_SECRET", "")).strip()
    authority = str(
        os.getenv("OPENAI_TIER_AUTHORITY", "https://login.microsoftonline.com/organizations")
    ).strip()
    configured = bool(client_id and client_secret and authority)

    if not configured and not optional:
        raise HttpError(
            500,
            "未配置 OpenAI Tier 查询登录参数，请设置 OPENAI_TIER_CLIENT_ID、OPENAI_TIER_CLIENT_SECRET 和 OPENAI_TIER_AUTHORITY。",
        )

    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "authority": authority,
        "configured": configured,
        "scopes": [OPENAI_TIER_SCOPE],
    }


def build_openai_tier_msal_app(config: dict[str, Any]) -> msal.ConfidentialClientApplication:
    return msal.ConfidentialClientApplication(
        client_id=config["client_id"],
        client_credential=config["client_secret"],
        authority=config["authority"],
    )


def set_openai_tier_notice(message: str, *, is_error: bool = False) -> None:
    session[OPENAI_TIER_NOTICE_SESSION_KEY] = {
        "message": message,
        "isError": is_error,
    }


def get_openai_tier_auth_state() -> dict[str, Any] | None:
    raw = session.get(OPENAI_TIER_AUTH_SESSION_KEY)
    if not isinstance(raw, dict):
        return None

    access_token = str(raw.get("accessToken") or "").strip()
    expires_at = int(raw.get("expiresAt") or 0)
    if not access_token or expires_at <= int(time.time()):
        session.pop(OPENAI_TIER_AUTH_SESSION_KEY, None)
        return None

    return {
        "accessToken": access_token,
        "expiresAt": expires_at,
        "account": {
            "name": str(raw.get("accountName") or "").strip(),
            "preferredUsername": str(raw.get("preferredUsername") or "").strip(),
            "tenantId": str(raw.get("tenantId") or "").strip(),
        },
    }


def require_openai_tier_auth_state() -> dict[str, Any]:
    config = get_openai_tier_auth_config()
    if not config["configured"]:
        raise HttpError(500, "OpenAI Tier 查询登录尚未配置完成。")

    auth_state = get_openai_tier_auth_state()
    if auth_state is None:
        raise HttpError(401, "登录状态已失效，请先通过 Microsoft 账号登录后再查询。")
    return auth_state


def fetch_enabled_user_subscriptions(
    token: str,
    http_session: requests.Session,
    timeout_seconds: int,
) -> list[dict[str, str]]:
    url = (
        "https://management.azure.com/subscriptions"
        f"?api-version={OPENAI_TIER_SUBSCRIPTIONS_API_VERSION}"
    )
    subscriptions: dict[str, dict[str, str]] = {}
    visited: set[str] = set()

    while url:
        try:
            parsed = urlparse(url)
        except ValueError as exc:
            raise HttpError(502, "ARM 订阅分页地址无效") from exc
        if (parsed.scheme != "https" or parsed.netloc.lower() != "management.azure.com"
                or parsed.path.lower() != "/subscriptions" or parsed.fragment
                or parse_qs(parsed.query).get("api-version") != [OPENAI_TIER_SUBSCRIPTIONS_API_VERSION]
                or url in visited or len(visited) >= 100):
            raise HttpError(502, "ARM 订阅分页地址无效或超过限制")
        visited.add(url)
        try:
            resp = http_session.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=timeout_seconds,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise HttpError(502, "获取订阅列表连接失败") from exc

        if resp.status_code == 401:
            raise HttpError(401, "Microsoft 登录状态已失效，请重新登录。")
        if resp.status_code != 200:
            status = resp.status_code if resp.status_code in (403, 429) else 502
            raise HttpError(status, f"获取订阅列表失败（HTTP {resp.status_code}）")

        try:
            body = resp.json()
        except ValueError as exc:
            raise HttpError(502, "ARM 订阅接口返回无效 JSON") from exc
        values = body.get("value") if isinstance(body, dict) else None
        if not isinstance(values, list):
            raise HttpError(502, "ARM 订阅列表格式无效")
        for item in values:
            if not isinstance(item, dict):
                continue
            if str(item.get("state") or "").strip() != "Enabled":
                continue
            subscription_id = str(item.get("subscriptionId") or "").strip()
            if not subscription_id:
                continue
            subscriptions[subscription_id.lower()] = {
                "subscriptionId": subscription_id,
                "subscriptionName": str(item.get("displayName") or subscription_id).strip(),
            }

        next_link = body.get("nextLink") if isinstance(body, dict) else None
        if next_link is not None and not isinstance(next_link, str):
            raise HttpError(502, "ARM 订阅分页地址无效")
        url = next_link or ""

    return sorted(subscriptions.values(), key=lambda item: (item["subscriptionName"].lower(), item["subscriptionId"]))


def get_cached_user_subscriptions(
    auth_state: dict[str, Any],
    http_session: requests.Session,
    sync_config: SyncConfig,
) -> list[dict[str, str]]:
    cache_key = hashlib.sha256(auth_state["accessToken"].encode("utf-8")).hexdigest()
    now = time.monotonic()
    with _single_tenant_subscription_cache_lock:
        cached = _single_tenant_subscription_cache.get(cache_key)
        if cached and cached[0] > now:
            return cached[1]

    subscriptions = fetch_enabled_user_subscriptions(
        token=auth_state["accessToken"],
        http_session=http_session,
        timeout_seconds=sync_config.subscription_list_timeout_seconds,
    )
    with _single_tenant_subscription_cache_lock:
        _single_tenant_subscription_cache[cache_key] = (
            now + SINGLE_TENANT_SUBSCRIPTION_CACHE_TTL_SECONDS,
            subscriptions,
        )
        expired_keys = [key for key, value in _single_tenant_subscription_cache.items() if value[0] <= now]
        for expired_key in expired_keys:
            _single_tenant_subscription_cache.pop(expired_key, None)
    return subscriptions


def query_openai_tier_rows(
    token: str,
    subscriptions: list[dict[str, str]],
    sync_config: SyncConfig,
) -> tuple[list[dict[str, str]], list[str]]:
    if not subscriptions:
        return [], []

    warnings: list[str] = []
    max_workers = resolve_openai_tier_workers(sync_config, len(subscriptions))
    logger.info(
        "openai tier batch dispatch: subscriptions=%s max_workers=%s",
        len(subscriptions),
        max_workers,
    )

    started_at = time.perf_counter()
    rows: list[dict[str, str]] = []
    with build_http_session(sync_config) as shared_http_session:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    fetch_openai_tier_row_isolated,
                    token,
                    subscription,
                    shared_http_session,
                ): subscription
                for subscription in subscriptions
            }

            for future in as_completed(futures):
                row, warning = future.result()
                rows.append(row)
                if warning:
                    warnings.append(warning)

    rows.sort(key=lambda item: (item["subscriptionName"].lower(), item["subscriptionId"]))
    elapsed_ms = int((time.perf_counter() - started_at) * 1000)
    ok_count = sum(1 for row in rows if row.get("queryStatus") == "OK")
    throttle_count = sum(1 for warning in warnings if "429" in str(warning))
    get_openai_tier_concurrency_controller().observe_batch(
        total_subscriptions=len(subscriptions),
        ok_count=ok_count,
        throttle_count=throttle_count,
        elapsed_ms=elapsed_ms,
    )
    return rows, warnings


def fetch_openai_tier_row_isolated(
    token: str,
    subscription: dict[str, str],
    http_session: requests.Session,
) -> tuple[dict[str, str], str | None]:
    subscription_id = str(subscription.get("subscriptionId") or "").strip()
    subscription_name = str(subscription.get("subscriptionName") or subscription_id).strip()
    started_at = time.perf_counter()

    try:
        row = fetch_openai_tier_row(
            token=token,
            subscription=subscription,
            http_session=http_session,
        )
        logger.info(
            "openai tier subscription finished: subscription_id=%s status=%s elapsed_ms=%s",
            subscription_id,
            row.get("queryStatus") or "unknown",
            int((time.perf_counter() - started_at) * 1000),
        )
        return row, None
    except HttpError as exc:
        warning = f"订阅 {subscription_name} ({subscription_id}) 查询失败：{exc.message}"
        logger.warning("openai tier subscription failed: subscription_id=%s error=%s", subscription_id, exc.message)
        return build_openai_tier_error_row(subscription, exc.message), warning
    except Exception as exc:
        warning = f"订阅 {subscription_name} ({subscription_id}) 查询失败：{str(exc)}"
        logger.exception("openai tier subscription failed with unexpected error: subscription_id=%s", subscription_id)
        return build_openai_tier_error_row(subscription, str(exc)), warning


def build_openai_tier_error_row(subscription: dict[str, str], message: str) -> dict[str, str]:
    subscription_id = str(subscription.get("subscriptionId") or "").strip()
    subscription_name = str(subscription.get("subscriptionName") or subscription_id).strip()
    normalized_message = str(message or "查询失败").strip()
    return {
        "subscriptionName": subscription_name,
        "subscriptionId": subscription_id,
        "currentTierName": "",
        "assignmentDate": "",
        "tierUpgradePolicy": "",
        "nextTierName": "",
        "nextTierStatus": "",
        "nextTierDate": "",
        "queryStatus": normalized_message,
    }


def fetch_openai_tier_row(
    token: str,
    subscription: dict[str, str],
    http_session: requests.Session,
) -> dict[str, str]:
    quota = get_subscription_quota_tier(
        token=token,
        subscription_id=subscription["subscriptionId"],
        http_session=http_session,
    )
    properties = quota.get("properties", {}) if isinstance(quota, dict) else {}
    eligibility = properties.get("tierUpgradeEligibilityInfo", {}) if isinstance(properties, dict) else {}

    return {
        "subscriptionName": subscription["subscriptionName"],
        "subscriptionId": subscription["subscriptionId"],
        "currentTierName": str(properties.get("currentTierName") or "").strip(),
        "assignmentDate": format_utc_to_china_display(properties.get("assignmentDate")),
        "tierUpgradePolicy": str(properties.get("tierUpgradePolicy") or "").strip(),
        "nextTierName": str(eligibility.get("nextTierName") or "").strip(),
        "nextTierStatus": str(eligibility.get("upgradeAvailabilityStatus") or "").strip(),
        "nextTierDate": format_utc_to_china_display(eligibility.get("upgradeApplicableDate")),
        "queryStatus": str(quota.get("queryStatus") or "未使用").strip(),
    }


def get_subscription_quota_tier(
    token: str,
    subscription_id: str,
    http_session: requests.Session,
) -> dict[str, Any]:
    last_error_message = ""
    encountered_hard_failure = False

    for api_version in get_openai_tier_api_versions():
        for suffix in ("", "/default"):
            url = (
                "https://management.azure.com/subscriptions/"
                f"{subscription_id}/providers/Microsoft.CognitiveServices/quotaTiers"
                f"{suffix}?api-version={api_version}"
            )
            try:
                resp = http_session.get(
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=OPENAI_TIER_QUERY_TIMEOUT_SECONDS,
                )
            except requests.RequestException as exc:
                last_error_message = str(exc)
                encountered_hard_failure = True
                continue

            if resp.status_code == 401:
                raise HttpError(401, "Microsoft 登录状态已失效，请重新登录。")
            if resp.status_code >= 400:
                last_error_message = extract_http_error(resp)
                if resp.status_code not in (400, 404):
                    encountered_hard_failure = True
                continue

            body = resp.json() if resp.content else {}
            quota = extract_quota_object(body)
            if quota:
                merged = quota.copy()
                merged["apiVersionUsed"] = api_version
                merged["queryStatus"] = "OK"
                return merged

    if encountered_hard_failure:
        raise HttpError(502, f"调用 quotaTiers 接口失败: {last_error_message or '未知错误'}")

    return {
        "properties": {},
        "apiVersionUsed": "",
        "queryStatus": "未使用",
    }


class OpenAITierConcurrencyController:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._recommended_workers = OPENAI_TIER_DEFAULT_MAX_WORKERS
        self._latest_failure_ratio = 0.0
        self._latest_throttle_ratio = 0.0
        self._latest_latency_ms = 0.0

    def recommend(self, subscription_count: int, configured_cap: int) -> int:
        if subscription_count <= 0:
            return 1

        cap = max(OPENAI_TIER_ADAPTIVE_MIN_WORKERS, min(configured_cap, OPENAI_TIER_ADAPTIVE_MAX_WORKERS))
        with self._lock:
            recommended = max(OPENAI_TIER_ADAPTIVE_MIN_WORKERS, min(int(self._recommended_workers), cap))

            if self._latest_throttle_ratio > OPENAI_TIER_ADAPTIVE_THROTTLE_RATIO_THRESHOLD:
                recommended = max(OPENAI_TIER_ADAPTIVE_MIN_WORKERS, recommended // 2)
            elif self._latest_failure_ratio > OPENAI_TIER_ADAPTIVE_FAILURE_RATIO_THRESHOLD:
                recommended = max(OPENAI_TIER_ADAPTIVE_MIN_WORKERS, recommended - 1)
            elif self._latest_latency_ms > OPENAI_TIER_ADAPTIVE_LATENCY_MS_THRESHOLD:
                recommended = max(OPENAI_TIER_ADAPTIVE_MIN_WORKERS, recommended - 1)
            elif self._latest_failure_ratio == 0 and self._latest_throttle_ratio == 0:
                recommended = min(cap, recommended + 1)

        return max(1, min(subscription_count, recommended))

    def observe_batch(self, total_subscriptions: int, ok_count: int, throttle_count: int, elapsed_ms: int) -> None:
        total = max(1, int(total_subscriptions))
        failures = max(0, total - max(0, int(ok_count)))
        failure_ratio = failures / total
        throttle_ratio = max(0, int(throttle_count)) / total
        latency_ms = max(0, int(elapsed_ms))

        with self._lock:
            self._latest_failure_ratio = failure_ratio
            self._latest_throttle_ratio = throttle_ratio
            self._latest_latency_ms = float(latency_ms)

            current = max(OPENAI_TIER_ADAPTIVE_MIN_WORKERS, int(self._recommended_workers))
            if throttle_ratio > OPENAI_TIER_ADAPTIVE_THROTTLE_RATIO_THRESHOLD:
                current = max(OPENAI_TIER_ADAPTIVE_MIN_WORKERS, current // 2)
            elif failure_ratio > OPENAI_TIER_ADAPTIVE_FAILURE_RATIO_THRESHOLD:
                current = max(OPENAI_TIER_ADAPTIVE_MIN_WORKERS, current - 1)
            elif latency_ms > OPENAI_TIER_ADAPTIVE_LATENCY_MS_THRESHOLD:
                current = max(OPENAI_TIER_ADAPTIVE_MIN_WORKERS, current - 1)
            elif failure_ratio == 0 and throttle_ratio == 0:
                current = min(OPENAI_TIER_ADAPTIVE_MAX_WORKERS, current + 1)

            self._recommended_workers = current


def get_openai_tier_concurrency_controller() -> OpenAITierConcurrencyController:
    global _openai_tier_concurrency_controller
    with _openai_tier_concurrency_controller_lock:
        if _openai_tier_concurrency_controller is None:
            _openai_tier_concurrency_controller = OpenAITierConcurrencyController()
        return _openai_tier_concurrency_controller


def is_openai_tier_adaptive_concurrency_enabled() -> bool:
    return parse_bool(os.getenv("OPENAI_TIER_ADAPTIVE_CONCURRENCY_ENABLED"), True)


def resolve_openai_tier_workers(sync_config: SyncConfig, subscription_count: int) -> int:
    requested_workers = OPENAI_TIER_DEFAULT_MAX_WORKERS
    raw_env_value = str(os.getenv("OPENAI_TIER_MAX_WORKERS", "")).strip()
    if raw_env_value:
        try:
            requested_workers = max(1, int(raw_env_value))
        except ValueError:
            requested_workers = OPENAI_TIER_DEFAULT_MAX_WORKERS
    else:
        requested_workers = max(1, max(sync_config.max_workers, OPENAI_TIER_DEFAULT_MAX_WORKERS))
    requested_workers = max(OPENAI_TIER_ADAPTIVE_MIN_WORKERS, min(requested_workers, OPENAI_TIER_ADAPTIVE_MAX_WORKERS))

    if is_openai_tier_adaptive_concurrency_enabled():
        adaptive_workers = get_openai_tier_concurrency_controller().recommend(subscription_count, requested_workers)
        logger.info(
            "openai tier adaptive workers selected: subscription_count=%s configured_cap=%s adaptive_workers=%s",
            subscription_count,
            requested_workers,
            adaptive_workers,
        )
        return adaptive_workers

    return max(1, min(subscription_count, requested_workers))


def get_openai_tier_api_versions() -> list[str]:
    versions: list[str] = []
    custom_version = str(os.getenv("OPENAI_TIER_API_VERSION", "")).strip()
    if custom_version:
        versions.append(custom_version)

    for version in OPENAI_TIER_DEFAULT_API_VERSIONS:
        if version not in versions:
            versions.append(version)
    return versions


def extract_quota_object(body: Any) -> dict[str, Any] | None:
    if isinstance(body, dict) and isinstance(body.get("value"), list):
        values = [item for item in body["value"] if isinstance(item, dict)]
        for item in values:
            if str(item.get("name") or "").strip() == "default":
                return item
        return values[0] if values else None

    if isinstance(body, dict):
        return body

    return None


def format_utc_to_china_display(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw or raw == "null":
        return ""

    normalized = raw
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"

    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return raw

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    china_dt = parsed.astimezone(timezone(timedelta(hours=8)))
    return china_dt.strftime("%Y-%m-%d")


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
            try:
                rows, row_currency, warning, sub_total = future.result()
            except HttpError as exc:
                warning = f"订阅 {subscription.subscription_id} 成本查询失败，已自动跳过：{normalize_cost_query_error_message(exc.message)}"
                logger.warning(
                    "subscription cost query failed with http error: subscription_id=%s subscription_name=%s error=%s",
                    subscription.subscription_id,
                    subscription.subscription_name,
                    exc.message,
                )
                warnings.append(warning)
                by_subscription[subscription.subscription_id] = {
                    "subscriptionId": subscription.subscription_id,
                    "subscriptionName": subscription.subscription_name,
                    "tenantName": subscription.tenant_name,
                    "cost": 0.0,
                }
                continue
            except Exception as exc:
                warning = f"订阅 {subscription.subscription_id} 成本查询失败，已自动跳过：{str(exc)}"
                logger.exception(
                    "subscription cost query failed with unexpected error: subscription_id=%s subscription_name=%s",
                    subscription.subscription_id,
                    subscription.subscription_name,
                )
                warnings.append(warning)
                by_subscription[subscription.subscription_id] = {
                    "subscriptionId": subscription.subscription_id,
                    "subscriptionName": subscription.subscription_name,
                    "tenantName": subscription.tenant_name,
                    "cost": 0.0,
                }
                continue

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

    try:
        summary_cost, summary_currency = fetch_subscription_cost_summary(
            token=token,
            tenant_key=tenant_key,
            subscription_id=subscription_id,
            url=url,
            start_dt=start_dt,
            end_dt=end_dt,
            session=session,
            sync_config=sync_config,
        )
    except HttpError as exc:
        warning = f"订阅 {subscription_id} 成本查询失败，已自动跳过：{normalize_cost_query_error_message(exc.message)}"
        logger.warning("subscription summary query failed and skipped: subscription_id=%s error=%s", subscription_id, exc.message)
        return [], "", warning, 0.0
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
    try:
        rows, currency, failed_ranges = query_cost_rows_with_split_fallback(
            token=token,
            tenant_key=tenant_key,
            subscription_id=subscription_id,
            url=url,
            start_dt=start_dt,
            end_dt=end_dt,
            session=session,
            sync_config=sync_config,
        )
    except HttpError as exc:
        warning = f"订阅 {subscription_id} 明细成本查询失败，已自动跳过：{normalize_cost_query_error_message(exc.message)}"
        logger.warning("subscription detail query failed and skipped: subscription_id=%s error=%s", subscription_id, exc.message)
        return [], summary_currency, warning, summary_cost

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
    subscription_id: str,
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
            subscription_id=subscription_id,
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
    subscription_id: str,
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
            subscription_id=subscription_id,
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
            subscription_id,
            url,
            start_dt,
            mid_dt,
            session,
            sync_config,
        )
        right_rows, right_currency, right_failed = query_cost_rows_with_split_fallback(
            token,
            tenant_key,
            subscription_id,
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
    subscription_id: str,
    url: str,
    payload: dict[str, Any],
    session: requests.Session,
    sync_config: SyncConfig,
) -> tuple[float, str]:
    resp = post_cost_query_with_retry(
        token=token,
        tenant_key=tenant_key,
        subscription_id=subscription_id,
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
    subscription_id: str,
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
            subscription_id=subscription_id,
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
    subscription_id: str,
    url: str,
    payload: dict[str, Any],
    session: requests.Session,
    sync_config: SyncConfig,
) -> requests.Response:
    total_wait_seconds = 0
    for attempt in range(1, sync_config.cost_query_max_retries + 2):
        request_started_at = time.perf_counter()
        with limit_cost_api_request(sync_config, tenant_key, subscription_id):
            try:
                resp = session.post(
                    url,
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                        "ClientType": sync_config.cost_query_client_type,
                    },
                    timeout=sync_config.cost_query_timeout_seconds,
                )
            except requests.RequestException as exc:
                elapsed_ms = int((time.perf_counter() - request_started_at) * 1000)
                logger.warning(
                    "cost_query_monitor event=api_request_failed tenant=%s subscription=%s attempt=%s elapsed_ms=%s error_type=%s",
                    tenant_key,
                    subscription_id,
                    attempt,
                    elapsed_ms,
                    type(exc).__name__,
                )
                if attempt > sync_config.cost_query_max_retries:
                    raise
                delay_seconds = get_retry_delay_seconds(None, attempt, sync_config)
                if total_wait_seconds + delay_seconds > sync_config.cost_query_retry_total_wait_seconds:
                    raise
                total_wait_seconds += delay_seconds
                time.sleep(delay_seconds)
                continue

        elapsed_ms = int((time.perf_counter() - request_started_at) * 1000)
        logger.info(
            "cost_query_monitor event=api_request_completed tenant=%s subscription=%s attempt=%s status=%s elapsed_ms=%s",
            tenant_key,
            subscription_id,
            attempt,
            resp.status_code,
            elapsed_ms,
        )

        if resp.status_code == 403:
            return resp

        if resp.status_code == 429:
            get_cost_query_governor(sync_config).observe_throttle(tenant_key, subscription_id, resp.headers)
            if attempt <= sync_config.cost_query_max_retries:
                delay_seconds = get_retry_delay_seconds(resp, attempt, sync_config)
                record_throttle_audit(
                    tenant_key,
                    subscription_id,
                    normalize_throttle_request_path(url),
                    delay_seconds,
                    extract_throttle_rule(resp.headers),
                )
                if total_wait_seconds + delay_seconds > sync_config.cost_query_retry_total_wait_seconds:
                    raise ThrottledError("Azure Cost API retry wait budget exhausted")
                total_wait_seconds += delay_seconds
                logger.warning(
                    "cost_query_monitor event=throttled tenant=%s subscription=%s attempt=%s max_retries=%s delay_seconds=%s cumulative_wait_seconds=%s",
                    tenant_key,
                    subscription_id,
                    attempt,
                    sync_config.cost_query_max_retries,
                    delay_seconds,
                    total_wait_seconds,
                )
                time.sleep(delay_seconds)
                continue
            raise ThrottledError("Azure Cost API throttled")

        if resp.status_code in {408, 500, 502, 503, 504} and attempt <= sync_config.cost_query_max_retries:
            delay_seconds = get_retry_delay_seconds(resp, attempt, sync_config)
            if total_wait_seconds + delay_seconds > sync_config.cost_query_retry_total_wait_seconds:
                raise HttpError(502, "查询订阅成本失败：重试等待时间已达到上限")
            total_wait_seconds += delay_seconds
            logger.warning(
                "cost_query_monitor event=transient_retry tenant=%s subscription=%s status=%s attempt=%s max_retries=%s delay_seconds=%s cumulative_wait_seconds=%s",
                tenant_key,
                subscription_id,
                resp.status_code,
                attempt,
                sync_config.cost_query_max_retries,
                delay_seconds,
                total_wait_seconds,
            )
            time.sleep(delay_seconds)
            continue

        if resp.status_code >= 400:
            error_message = normalize_cost_query_error_message(extract_http_error(resp))
            raise HttpError(502, f"查询订阅成本失败: {error_message}")

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
    resp: requests.Response | None,
    attempt: int,
    sync_config: SyncConfig,
) -> int:
    retry_after_seconds = parse_cost_retry_after(resp.headers if resp is not None else {})

    base_delay = max(1, sync_config.cost_query_retry_base_seconds)
    max_delay = max(base_delay, sync_config.cost_query_retry_max_delay_seconds)

    exp_delay = base_delay * (2 ** max(0, attempt - 1))
    jitter = random.uniform(0, max(0, sync_config.cost_query_retry_jitter_max_seconds))
    computed_delay = max(retry_after_seconds, exp_delay + jitter)
    return max(1, min(max_delay, int(round(computed_delay))))


def parse_cost_retry_after(headers: Any) -> float:
    lower_headers = {str(key).lower(): value for key, value in (headers or {}).items()}
    header_names = (
        "retry-after",
        "x-ms-ratelimit-microsoft.costmanagement-qpu-retry-after",
        "x-ms-ratelimit-microsoft.costmanagement-entity-retry-after",
        "x-ms-ratelimit-microsoft.costmanagement-tenant-retry-after",
        "x-ms-ratelimit-microsoft.costmanagement-client-retry-after",
        "x-ms-ratelimit-microsoft.costmanagement-clienttype-retry-after",
    )
    delays: list[float] = []
    now = datetime.now(timezone.utc)
    for header_name in header_names:
        raw_value = lower_headers.get(header_name)
        try:
            delay = float(raw_value)
        except (TypeError, ValueError):
            delay = -1
        if delay < 0 and header_name == "retry-after" and raw_value:
            try:
                parsed_date = parsedate_to_datetime(str(raw_value))
                if parsed_date.tzinfo is None:
                    parsed_date = parsed_date.replace(tzinfo=timezone.utc)
                delay = (parsed_date - now).total_seconds()
            except (TypeError, ValueError, OverflowError):
                delay = -1
        if delay >= 0:
            delays.append(delay)
    return max(delays, default=0.0)


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
        scope_cost_api_max_inflight=parse_positive_int(
            values.get("scope_cost_api_max_inflight"),
            DEFAULT_SCOPE_COST_API_MAX_INFLIGHT,
        ),
        cost_query_rate_limit_enabled=parse_bool(values.get("cost_query_rate_limit_enabled"), True),
        tenant_cost_api_requests_per_minute=parse_positive_int(
            values.get("tenant_cost_api_requests_per_minute"),
            DEFAULT_TENANT_COST_API_REQUESTS_PER_MINUTE,
        ),
        scope_cost_api_requests_per_minute=parse_positive_int(
            values.get("scope_cost_api_requests_per_minute"),
            DEFAULT_SCOPE_COST_API_REQUESTS_PER_MINUTE,
        ),
        scope_cost_api_min_interval_seconds=parse_non_negative_int(
            values.get("scope_cost_api_min_interval_seconds"),
            DEFAULT_SCOPE_COST_API_MIN_INTERVAL_SECONDS,
        ),
        cost_query_retry_jitter_max_seconds=parse_non_negative_int(
            values.get("cost_query_retry_jitter_max_seconds"),
            DEFAULT_RETRY_JITTER_MAX_SECONDS,
        ),
        cost_query_retry_total_wait_seconds=parse_positive_int(
            values.get("cost_query_retry_total_wait_seconds"),
            DEFAULT_RETRY_TOTAL_WAIT_SECONDS,
        ),
        cost_query_client_type=str(values.get("cost_query_client_type") or "AzureCost-FinOpsPortal").strip() or "AzureCost-FinOpsPortal",
        max_subscriptions_per_query=parse_non_negative_int(
            values.get("max_subscriptions_per_query"),
            DEFAULT_MAX_SUBSCRIPTIONS_PER_QUERY,
        ),
        audit_log_retention_days=parse_positive_int(
            values.get("audit_log_retention_days"),
            DEFAULT_AUDIT_LOG_RETENTION_DAYS,
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
        pool_block=True,
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


class CostQueryGovernor:
    def __init__(self):
        self._condition = threading.Condition(threading.RLock())
        self._active_global = 0
        self._active_tenants: dict[str, int] = {}
        self._active_scopes: dict[str, int] = {}
        self._last_request: dict[tuple[str, str], float] = {}
        self._cooldowns: dict[tuple[str, str], float] = {}

    @contextmanager
    def limit(self, sync_config: SyncConfig, tenant_key: str, subscription_id: str):
        tenant_id = str(tenant_key or "unknown").strip() or "unknown"
        scope_id = str(subscription_id or "unknown").strip() or "unknown"
        self._wait_for_turn(sync_config, tenant_id, scope_id)
        try:
            yield
        finally:
            with self._condition:
                self._active_global = max(0, self._active_global - 1)
                self._active_tenants[tenant_id] = max(0, self._active_tenants.get(tenant_id, 1) - 1)
                self._active_scopes[scope_id] = max(0, self._active_scopes.get(scope_id, 1) - 1)
                self._condition.notify_all()

    def _wait_for_turn(self, sync_config: SyncConfig, tenant_id: str, scope_id: str) -> None:
        tenant_interval = 60.0 / max(1, sync_config.tenant_cost_api_requests_per_minute)
        scope_interval = max(
            60.0 / max(1, sync_config.scope_cost_api_requests_per_minute),
            float(sync_config.scope_cost_api_min_interval_seconds),
        )
        wait_started_at = time.perf_counter()
        was_delayed = False
        while True:
            with self._condition:
                now = time.monotonic()
                within_limits = (
                    self._active_global < max(1, sync_config.global_cost_api_max_inflight)
                    and self._active_tenants.get(tenant_id, 0) < max(1, sync_config.tenant_cost_api_max_inflight)
                    and self._active_scopes.get(scope_id, 0) < max(1, sync_config.scope_cost_api_max_inflight)
                )
                wait_until = now
                if sync_config.cost_query_rate_limit_enabled:
                    wait_until = max(
                        self._last_request.get(("tenant", tenant_id), 0) + tenant_interval,
                        self._last_request.get(("scope", scope_id), 0) + scope_interval,
                        self._cooldowns.get(("tenant", tenant_id), 0),
                        self._cooldowns.get(("scope", scope_id), 0),
                        self._cooldowns.get(("client", "*"), 0),
                    )
                if within_limits and wait_until <= now:
                    self._active_global += 1
                    self._active_tenants[tenant_id] = self._active_tenants.get(tenant_id, 0) + 1
                    self._active_scopes[scope_id] = self._active_scopes.get(scope_id, 0) + 1
                    if sync_config.cost_query_rate_limit_enabled:
                        self._last_request[("tenant", tenant_id)] = now
                        self._last_request[("scope", scope_id)] = now
                    if was_delayed:
                        logger.info(
                            "cost_query_monitor event=governor_wait_completed tenant=%s subscription=%s wait_ms=%s",
                            tenant_id,
                            scope_id,
                            int((time.perf_counter() - wait_started_at) * 1000),
                        )
                    return
                was_delayed = True
                self._condition.wait(timeout=max(0.05, min(0.25, wait_until - now)))

    def observe_throttle(self, tenant_id: str, scope_id: str, headers: Any) -> None:
        delay_seconds = parse_cost_retry_after(headers)
        if delay_seconds <= 0:
            return
        lower_headers = {str(key).lower(): value for key, value in (headers or {}).items()}
        targets: list[tuple[str, str]] = []
        if "x-ms-ratelimit-microsoft.costmanagement-entity-retry-after" in lower_headers:
            targets.append(("scope", scope_id))
        if "x-ms-ratelimit-microsoft.costmanagement-tenant-retry-after" in lower_headers:
            targets.append(("tenant", tenant_id))
        if (
            "x-ms-ratelimit-microsoft.costmanagement-client-retry-after" in lower_headers
            or "x-ms-ratelimit-microsoft.costmanagement-clienttype-retry-after" in lower_headers
        ):
            targets.append(("client", "*"))
        if not targets:
            targets.append(("tenant", tenant_id))
        with self._condition:
            cooldown_until = time.monotonic() + delay_seconds
            for target in targets:
                self._cooldowns[target] = max(self._cooldowns.get(target, 0), cooldown_until)
            logger.warning(
                "cost_query_monitor event=cooldown_applied tenant=%s subscription=%s delay_seconds=%s targets=%s",
                tenant_id,
                scope_id,
                delay_seconds,
                ",".join(f"{kind}:{identifier}" for kind, identifier in targets),
            )
            self._condition.notify_all()


def get_cost_query_governor(sync_config: SyncConfig) -> CostQueryGovernor:
    global _cost_query_governor
    with _cost_api_limit_lock:
        if _cost_query_governor is None:
            _cost_query_governor = CostQueryGovernor()
        return _cost_query_governor


@contextmanager
def limit_cost_api_request(sync_config: SyncConfig, tenant_key: str, subscription_id: str):
    with get_cost_query_governor(sync_config).limit(sync_config, tenant_key, subscription_id):
        yield


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


def parse_bool(value: str | None, default: bool) -> bool:
    normalized = str(value or "").strip().lower()
    if normalized in {"true", "1", "yes", "on"}:
        return True
    if normalized in {"false", "0", "no", "off"}:
        return False
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


def normalize_cost_query_error_message(message: Any) -> str:
    text = str(message or "").strip()
    if not text:
        return "未知错误"

    if "GtmDimensionDataProvider.GetAzureSubscriptionsById returns null or empty list" in text:
        return "Cost Management 未识别该订阅（可能无成本查询权限、订阅状态异常或数据尚未同步）"

    return text


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


from azure_diagnostics import register_routes as register_azure_diagnostics_routes

register_azure_diagnostics_routes(app, sys.modules[__name__])


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5100, debug=False)

