"""Azure Cognitive Services model token usage collection using a caller-provided ARM token."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

ARM_ENDPOINT = "https://management.azure.com"
METRICS_API_VERSION = "2023-10-01"
COGNITIVE_API_VERSION = "2024-10-01"
METRIC_NAMESPACE = "microsoft.cognitiveservices/accounts"
DEFAULT_METRICS = ["ProcessedPromptTokens", "GeneratedTokens", "TokenTransaction"]
DIMENSION_FILTER = (
    "Microsoft.ResourceId eq '*' and ModelDeploymentName eq '*' "
    "and ModelName eq '*' and ModelVersion eq '*'"
)
RESOURCE_DIMENSION_FILTER = "ModelDeploymentName eq '*' and ModelName eq '*' and ModelVersion eq '*'"


def parse_tz(value: str) -> timezone:
    text = str(value or "+0").strip().lower().replace("utc", "").replace("gmt", "") or "+0"
    match = re.fullmatch(r"([+-]?)(\d{1,2})(?::?(\d{2})|\.(\d+))?", text)
    if not match:
        raise ValueError(f"无法解析时区偏移: {value}")
    sign = -1 if match.group(1) == "-" else 1
    hours = int(match.group(2))
    minutes = int(match.group(3)) if match.group(3) else 0
    if match.group(4):
        minutes = int(round(float("0." + match.group(4)) * 60))
    offset = timedelta(hours=hours, minutes=minutes) * sign
    if abs(offset) > timedelta(hours=14):
        raise ValueError(f"时区偏移超出范围: {value}")
    return timezone(offset)


def build_timespan_range(start_date: str, end_date: str, tz: timezone) -> tuple[str, datetime, datetime]:
    local_start = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=tz)
    local_end = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=tz) + timedelta(days=1)
    if local_end <= local_start:
        raise ValueError("结束日期不能早于开始日期")
    start_utc = local_start.astimezone(timezone.utc)
    end_utc = local_end.astimezone(timezone.utc)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return f"{start_utc.strftime(fmt)}/{end_utc.strftime(fmt)}", start_utc, end_utc


class ArmClient:
    def __init__(self, access_token: str, subscription_id: str):
        self.access_token = access_token
        self.subscription_id = subscription_id
        self.session = requests.Session()

    def get(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self.session.get(
            url,
            params=params,
            headers={"Authorization": f"Bearer {self.access_token}"},
            timeout=120,
        )
        if response.status_code != 200:
            raise RuntimeError(f"请求失败 {response.status_code}: {response.text[:800]}")
        return response.json()

    def list_cognitive_accounts(self) -> list[dict[str, Any]]:
        url = f"{ARM_ENDPOINT}/subscriptions/{self.subscription_id}/providers/Microsoft.CognitiveServices/accounts"
        params: dict[str, Any] | None = {"api-version": COGNITIVE_API_VERSION}
        accounts: list[dict[str, Any]] = []
        while url:
            payload = self.get(url, params)
            accounts.extend(payload.get("value", []))
            url = payload.get("nextLink")
            params = None
        return accounts

    def list_metrics_at_subscription_scope(self, region: str, timespan: str, metric_names: list[str], interval: str) -> dict[str, Any]:
        url = f"{ARM_ENDPOINT}/subscriptions/{self.subscription_id}/providers/Microsoft.Insights/metrics"
        params: dict[str, Any] = {
            "api-version": METRICS_API_VERSION,
            "region": region,
            "timespan": timespan,
            "interval": interval,
            "metricnames": ",".join(metric_names),
            "metricnamespace": METRIC_NAMESPACE,
            "aggregation": "total",
            "$filter": DIMENSION_FILTER,
            "top": 1000,
            "ValidateDimensions": "false",
        }
        if interval.upper() != "FULL":
            params["AutoAdjustTimegrain"] = "true"
        return self.get(url, params)

    def list_metrics_for_resource(self, resource_id: str, timespan: str, metric_names: list[str], interval: str) -> dict[str, Any]:
        url = f"{ARM_ENDPOINT}{resource_id}/providers/Microsoft.Insights/metrics"
        params: dict[str, Any] = {
            "api-version": METRICS_API_VERSION,
            "timespan": timespan,
            "interval": interval,
            "metricnames": ",".join(metric_names),
            "metricnamespace": METRIC_NAMESPACE,
            "aggregation": "total",
            "$filter": RESOURCE_DIMENSION_FILTER,
            "top": 1000,
            "ValidateDimensions": "false",
        }
        if interval.upper() != "FULL":
            params["AutoAdjustTimegrain"] = "true"
        return self.get(url, params)


def metadata_map(series: dict[str, Any]) -> dict[str, str]:
    return {item["name"]["value"].lower(): item.get("value", "") for item in series.get("metadatavalues", [])}


def accumulate(rows: dict[tuple[str, ...], dict[str, Any]], warnings: list[str], payload: dict[str, Any], scope_label: str, default_region: str = "", default_resource_id: str = "") -> None:
    for metric in payload.get("value", []):
        metric_name = metric["name"]["value"]
        if metric.get("errorCode") not in (None, "Success"):
            warnings.append(f"[{scope_label}] 指标 {metric_name}: {metric.get('errorCode')} {metric.get('errorMessage', '')}")
            continue
        for series in metric.get("timeseries", []):
            meta = metadata_map(series)
            resource_id = meta.get("microsoft.resourceid", default_resource_id)
            key = (resource_id, meta.get("modeldeploymentname", ""), meta.get("modelname", ""), meta.get("modelversion", ""))
            row = rows.setdefault(key, {
                "region": default_region,
                "account": resource_id.rsplit("/", 1)[-1] if resource_id else "",
                "resourceId": resource_id,
                "deployment": key[1],
                "model": key[2] or key[1],
                "modelVersion": key[3],
            })
            row[metric_name] = row.get(metric_name, 0) + sum(point.get("total") or 0 for point in series.get("data", []))


def collect(access_token: str, subscription_id: str, start_date: str, end_date: str, timezone_text: str = "+8", interval: str = "FULL", scope: str = "resource", regions_filter: list[str] | None = None, subscription_name: str = "") -> dict[str, Any]:
    tz = parse_tz(timezone_text)
    timespan, start_utc, end_utc = build_timespan_range(start_date, end_date, tz)
    client = ArmClient(access_token, subscription_id)
    accounts = client.list_cognitive_accounts()
    regions = regions_filter or sorted({item.get("location", "") for item in accounts if item.get("location")})
    if regions_filter:
        accounts = [item for item in accounts if item.get("location") in regions_filter]
    if not accounts:
        raise ValueError("订阅下未找到 Microsoft.CognitiveServices 账户")

    rows: dict[tuple[str, ...], dict[str, Any]] = {}
    warnings: list[str] = []
    use_subscription_scope = scope == "subscription"
    if use_subscription_scope:
        for region in regions:
            try:
                accumulate(rows, warnings, client.list_metrics_at_subscription_scope(region, timespan, DEFAULT_METRICS, interval), region, default_region=region)
            except RuntimeError as exc:
                if "Cross Resource metrics" in str(exc):
                    warnings.append(f"[{region}] 订阅级跨资源指标不受支持，已改为逐资源查询")
                    use_subscription_scope = False
                    rows.clear()
                    break
                warnings.append(f"[{region}] {exc}")
    if not use_subscription_scope:
        for account in accounts:
            resource_id = account["id"]
            label = account.get("name", resource_id)
            try:
                payload = client.list_metrics_for_resource(resource_id, timespan, DEFAULT_METRICS, interval)
                accumulate(rows, warnings, payload, label, default_region=account.get("location", ""), default_resource_id=resource_id)
            except RuntimeError as exc:
                warnings.append(f"[{label}] {exc}")

    for row in rows.values():
        for metric in DEFAULT_METRICS:
            row.setdefault(metric, 0)
        row["subscriptionId"] = subscription_id
        row["subscriptionName"] = subscription_name or subscription_id
        row["Total"] = row["TokenTransaction"]
    ordered = sorted(rows.values(), key=lambda item: (-item["Total"], item["region"], item["model"]))
    summary: dict[str, dict[str, float]] = {}
    for row in ordered:
        key = f"{row['subscriptionId']}|{row['subscriptionName']}|{row['model'] or row['deployment'] or '(unknown)'}"
        bucket = summary.setdefault(key, {metric: 0 for metric in [*DEFAULT_METRICS, "Total"]})
        for metric in [*DEFAULT_METRICS, "Total"]:
            bucket[metric] += row.get(metric, 0)
    return {
        "subscription": subscription_id,
        "startDate": start_date,
        "endDate": end_date,
        "tz": timezone_text,
        "timespan": timespan,
        "startUtc": start_utc.strftime("%Y-%m-%d %H:%M UTC"),
        "endUtc": end_utc.strftime("%Y-%m-%d %H:%M UTC"),
        "regions": regions,
        "accountCount": len(accounts),
        "metrics": DEFAULT_METRICS,
        "rows": ordered,
        "summary": [
            {
                "subscriptionId": key.split("|", 2)[0],
                "subscriptionName": key.split("|", 2)[1],
                "model": key.split("|", 2)[2],
                **values,
            }
            for key, values in sorted(summary.items(), key=lambda item: -item[1]["Total"])
        ],
        "grandTotal": sum(row["Total"] for row in ordered),
        "warnings": warnings,
    }
