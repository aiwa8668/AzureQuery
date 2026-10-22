import unittest
import time
from urllib.parse import parse_qs, urlsplit
from unittest.mock import MagicMock, Mock, patch

import requests
from flask import Flask

import main
import azure_diagnostics as diagnostics


SUBSCRIPTION = "11111111-1111-1111-1111-111111111111"
OTHER_SUBSCRIPTION = "22222222-2222-2222-2222-222222222222"
WORKSPACE = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg/providers/Microsoft.OperationalInsights/workspaces/logs"
RESOURCE = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/openai"


def response(body, status=200):
    return Mock(status_code=status, json=Mock(return_value=body))


class AzureDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True)
        self.app.register_error_handler(main.HttpError, main.handle_http_error)
        diagnostics.register_routes(self.app, main)
        self.client = self.app.test_client()
        self.session = MagicMock()
        self.session.__enter__.return_value = self.session
        self.session.__exit__.return_value = False
        self.auth = {"accessToken": "secret-test-token", "account": {"tenantId": "tenant"}}
        self.patches = [
            patch.object(main, "require_openai_tier_auth_state", return_value=self.auth),
            patch.object(main, "load_sync_config", return_value=main.SyncConfig(max_workers=1)),
            patch.object(main, "build_http_session", return_value=self.session),
            patch.object(main, "get_cached_user_subscriptions", return_value=[{"subscriptionId": SUBSCRIPTION}]),
        ]
        self.mocks = [item.start() for item in self.patches]
        for item in self.patches:
            self.addCleanup(item.stop)
        self.payload = {
            "subscriptionIds": [SUBSCRIPTION], "workspaceIds": [WORKSPACE],
            "resourceIds": [RESOURCE], "startDate": "2026-01-01", "endDate": "2026-01-02", "tz": "+8",
        }
        self.session.post.return_value = response({"tables": [{
            "columns": [{"name": name} for name in (
                "TimeGenerated", "ResourceId", "RequestId", "CorrelationId", "OperationName", "ModelVersion",
                "PromptTokens", "CachedTokens", "CacheWriteTokens", "GeneratedTokens",
                "LongContextPromptTokens", "LongContextCachedTokens", "LongContextCacheWriteTokens", "LongContextGeneratedTokens",
                "InputTokens", "UncachedInputTokens", "CachedInputTokens", "OutputTokens",
            )],
            "rows": [["2026-01-01T00:00:00Z", RESOURCE.lower(), "request-1", "request-1", "ChatCompletions", "2026-01",
                      10, 2, 1, 5, 20, 4, 2, 7, 10, 8, 2, 5]],
        }]})
        self.session.get.return_value = response({"value": [{"id": WORKSPACE, "name": "logs", "properties": {"customerId": "customer"}}]})

    def query(self, **changes):
        return self.client.post("/api/azure-diagnostics/query", json={**self.payload, **changes})

    def test_registration_is_idempotent(self):
        diagnostics.register_routes(self.app, main)
        self.assertEqual(len([rule for rule in self.app.url_map.iter_rules() if rule.rule.startswith("/api/azure-diagnostics")]), 3)

    def test_factory_reuses_main_app(self):
        with patch.object(diagnostics, "register_routes") as register:
            self.assertIs(diagnostics.create_app(), main.app)
        register.assert_called_once_with(main.app, main)

    def test_all_routes_require_shared_authentication(self):
        self.mocks[0].side_effect = main.HttpError(401, "登录失效")
        self.assertEqual(self.query().status_code, 401)
        self.assertEqual(self.client.get("/api/azure-diagnostics/workspaces").status_code, 401)
        self.assertEqual(self.client.get("/api/azure-diagnostics/resources").status_code, 401)
        self.mocks[2].assert_not_called()

    def test_query_uses_arm_version_shared_session_and_utc(self):
        result = self.query()
        self.assertEqual(result.status_code, 200)
        row = result.get_json()["rows"][0]
        self.assertEqual(row["InputTokens"], 10)
        self.assertEqual(row["CachedInputTokens"], 2)
        self.assertEqual(row["UncachedInputTokens"], 8)
        self.assertEqual(row["OutputTokens"], 5)
        self.assertEqual(row["CacheWriteTokens"], 1)
        self.assertEqual([row[field] for field in ("LongContextPromptTokens", "LongContextCachedTokens", "LongContextCacheWriteTokens", "LongContextGeneratedTokens")], [20, 4, 2, 7])
        for field in ("TotalTokens", "CompletionTokens", "CacheWriteInputTokens"):
            self.assertNotIn(field, row)
        call = self.session.post.call_args
        self.assertEqual(call.args[0], f"{diagnostics.ARM_ENDPOINT}{WORKSPACE.lower()}/query")
        self.assertEqual(call.kwargs["params"], {"api-version": "2017-10-01"})
        self.assertEqual(call.kwargs["json"]["timespan"], "2025-12-31T16:00:00Z/2026-01-02T16:00:00Z")
        self.assertEqual(call.kwargs["headers"], {"Authorization": "Bearer secret-test-token"})
        self.assertFalse(call.kwargs["allow_redirects"])
        self.assertIs(self.mocks[3].call_args.args[1], self.session)
        self.session.__exit__.assert_called_once()

    def test_kql_extracts_usage_arrays_and_returns_exact_query(self):
        result = self.query().get_json()
        query = self.session.post.call_args.kwargs["json"]["query"]
        self.assertEqual(result["query"], query)
        self.assertNotIn("| where Category", query)
        for field in ("promptTokens", "cachedTokens", "cacheWriteTokens", "generatedTokens",
                      "longContextPromptTokens", "longContextCachedTokens", "longContextCacheWriteTokens", "longContextGeneratedTokens"):
            self.assertEqual(query.count(f"coalesce(tolong(p.{field}[0]), long(0))"), 1)
        for field in ("CorrelationId", "OperationName", "p.modelVersion"):
            self.assertIn(field, query)
        for legacy_field in ("promptTokens_d", "completionTokens_d", "totalTokens_d", "Properties.usage", "ReportedTotal", "DedupKey"):
            self.assertNotIn(legacy_field, query)
        self.assertIn(RESOURCE.lower(), query)
        self.assertIn(f"in~ ('{SUBSCRIPTION}')", query)

    def test_kql_token_calculation_preserves_original_fields(self):
        self.query()
        query = self.session.post.call_args.kwargs["json"]["query"]
        for expression in (
            "InputTokens = PromptTokens,",
            "OutputTokens = GeneratedTokens,",
            "CachedInputTokens = CachedTokens,",
            "| extend UncachedInputTokens = InputTokens - CachedInputTokens\n",
        ):
            self.assertIn(expression, query)
        for legacy_expression in ("+ LongContext", "CacheWriteInputTokens", "CompletionTokens", "TotalTokens", "max_of(", "- CacheWriteTokens"):
            self.assertNotIn(legacy_expression, query)
        self.assertNotIn("| project ", query)
        self.assertEqual(query.count("| project-away"), 1)
        self.assertIn("| project-away p, _ResourceIdFilter\n| order by", query)
        self.assertNotIn("| where PromptTokens", query)

    def test_query_preserves_raw_and_calculated_fields_without_internal_fields(self):
        table = self.session.post.return_value.json.return_value["tables"][0]
        raw_fields = {
            "Category": "AzureOpenAIRequestUsage", "_ResourceId": RESOURCE,
            "properties_s": '{"promptTokens":[10],"modelName":"gpt-4o"}',
            "AdditionalFields": {"custom": [1, 2]}, "custom_s": None,
            "Deployment": "chat", "Model": "gpt-4o", "p": {"promptTokens": [10]},
            "_ResourceIdFilter": RESOURCE.lower(),
        }
        table["columns"].extend({"name": name} for name in raw_fields)
        table["rows"][0].extend(raw_fields.values())
        result = self.query()
        self.assertEqual(result.status_code, 200)
        body = result.get_json()
        expected = dict(zip((column["name"] for column in table["columns"]), table["rows"][0]))
        expected.pop("p")
        expected.pop("_ResourceIdFilter")
        expected["workspaceId"] = WORKSPACE.lower()
        self.assertEqual(body["rows"], [expected])
        self.assertEqual(body["columns"], list(expected))
        self.assertNotIn("p", body["columns"])
        self.assertNotIn("_ResourceIdFilter", body["columns"])

    def test_empty_result_retains_complete_schema_without_internal_fields(self):
        names = ["TimeGenerated", "Category", "properties_s", "custom_s", "InputTokens", "p", "_ResourceIdFilter"]
        self.session.post.return_value = response({"tables": [{
            "columns": [{"name": name} for name in names], "rows": [],
        }]})
        result = self.query()
        self.assertEqual(result.status_code, 200)
        body = result.get_json()
        self.assertEqual(body["rows"], [])
        self.assertEqual(body["columns"], names[:-2] + ["workspaceId"])

    def test_columns_union_deduplicates_tables_and_workspaces_including_empty_schema(self):
        self.session.post.side_effect = [
            response({"tables": [
                {"columns": [{"name": "RequestId"}, {"name": "first_s"}, {"name": "p"}],
                 "rows": [["same-request", "first", {}]]},
                {"columns": [{"name": "RequestId"}, {"name": "empty_s"}], "rows": []},
            ]}),
            response({"tables": [{
                "columns": [{"name": "RequestId"}, {"name": "second_s"}, {"name": "workspaceId"}],
                "rows": [["same-request", "second", "original-workspace"]],
            }]}),
            response({"tables": [{
                "columns": [{"name": "empty_s"}, {"name": "last_s"}, {"name": "p"}], "rows": [],
            }]}),
        ]
        result = self.query(workspaceIds=[WORKSPACE, WORKSPACE + "2", WORKSPACE + "3"])
        self.assertEqual(result.status_code, 200)
        body = result.get_json()
        self.assertEqual(body["columns"], ["RequestId", "first_s", "empty_s", "second_s", "workspaceId", "last_s"])
        self.assertEqual(body["rows"], [
            {"RequestId": "same-request", "first_s": "first", "workspaceId": WORKSPACE.lower()},
            {"RequestId": "same-request", "second_s": "second", "workspaceId": (WORKSPACE + "2").lower()},
        ])
        self.assertEqual(self.session.post.call_count, 3)
        self.assertEqual(len(body["warnings"]), 1)

    def test_kql_keeps_all_requests_before_descending_detail_limit(self):
        self.query()
        query = self.session.post.call_args.kwargs["json"]["query"]
        self.assertIn("RequestId = coalesce(CorrelationId, tostring(p.requestId))", query)
        self.assertIn("RequestId = iff(isnotempty(RequestId), RequestId, hash_sha256", query)
        for expression in ("| summarize", "arg_max", "| distinct", "| where Category"):
            self.assertNotIn(expression, query)
        self.assertIn("pack_array(_ResourceIdFilter, TimeGenerated, OperationName", query)
        self.assertLess(query.index("| project-away p"), query.index("| order by"))
        self.assertTrue(query.endswith("| order by TimeGenerated desc, RequestId asc\n| take 5001"))

    def test_kql_filters_internal_resource_id_without_overwriting_raw_id(self):
        for resource_ids in ([RESOURCE.upper()], []):
            with self.subTest(resource_ids=resource_ids):
                result = self.query(resourceIds=resource_ids).get_json()
                query = result["query"]
                self.assertIn("| extend _ResourceIdFilter = tolower(tostring(column_ifexists('_ResourceId', column_ifexists('ResourceId', ''))))", query)
                self.assertNotIn("| extend ResourceId =", query)
                expected_filters = [
                    "| where TimeGenerated >= datetime(2025-12-31T16:00:00Z) and TimeGenerated < datetime(2026-01-02T16:00:00Z)",
                    "| where ResourceProvider =~ 'MICROSOFT.COGNITIVESERVICES'",
                    f"| where tostring(split(_ResourceIdFilter, '/')[2]) in~ ('{SUBSCRIPTION}')",
                ]
                if resource_ids:
                    expected_filters.append(f"| where _ResourceIdFilter in~ ('{RESOURCE.lower()}')")
                self.assertEqual([line for line in query.splitlines() if line.startswith("| where")], expected_filters)
                self.assertIn("| project-away p, _ResourceIdFilter", query)

    def test_same_request_different_operations_ips_and_raw_empty_values_all_survive(self):
        names = ["TimeGenerated", "RequestId", "CorrelationId", "Category", "OperationName",
                 "CallerIPAddress", "ResourceId", "_ResourceId", "properties_s", "custom_s",
                 "InputTokens", "CachedInputTokens", "UncachedInputTokens", "OutputTokens", "p", "_ResourceIdFilter"]
        raw_rows = []
        for operation in ("ChatCompletions", "Embeddings"):
            for ip in ("192.0.2.10", "192.0.2.20", "", None):
                for raw_value in ("OriginalValue", "", None):
                    raw_rows.append([
                        "2026-01-01T00:00:00Z", "same-request", "same-request",
                        "AzureOpenAIRequestUsage" if operation == "ChatCompletions" else "Audit",
                        operation, ip, RESOURCE, RESOURCE.upper(), raw_value, raw_value,
                        10, 2, 8, 5, {}, RESOURCE.lower(),
                    ])
        raw_rows.append(raw_rows[0][:])
        raw_rows.append(["2026-01-01T00:00:00Z", "same-request", "same-request", "", "ChatCompletions",
                         None, "", None, "", None, 0, 0, 0, 0, {}, RESOURCE.lower()])
        self.session.post.return_value = response({"tables": [{
            "columns": [{"name": name} for name in names], "rows": raw_rows,
        }]})
        result = self.query()
        self.assertEqual(result.status_code, 200)
        body = result.get_json()
        expected = [{**dict(zip(names[:-2], values[:-2])), "workspaceId": WORKSPACE.lower()} for values in raw_rows]
        self.assertEqual(body["rows"], expected)
        self.assertEqual(body["columns"], names[:-2] + ["workspaceId"])
        self.assertEqual(len(body["rows"]), 26)
        self.assertFalse(body["truncated"])
        self.assertNotIn("arg_max", body["query"])
        self.assertNotIn("| where Category", body["query"])

    def test_discovery_uses_arm_and_deduplicates_pages(self):
        path = f"{diagnostics.ARM_ENDPOINT}/subscriptions/{SUBSCRIPTION}/providers/Microsoft.OperationalInsights/workspaces"
        next_link = f"{path}?api-version=2023-09-01&$skiptoken=abc"
        item = {"id": WORKSPACE, "name": "logs"}
        self.session.get.side_effect = [response({"value": [item], "nextLink": next_link}), response({"value": [item]})]
        result = self.client.get("/api/azure-diagnostics/workspaces", query_string={"subscriptionId": SUBSCRIPTION})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(result.get_json()["workspaces"]), 1)
        self.assertEqual(self.session.get.call_count, 2)
        self.assertEqual(self.session.get.call_args_list[0].args[0], path)
        self.assertEqual(self.session.get.call_args_list[0].kwargs["params"], {"api-version": "2023-09-01"})
        self.assertEqual(self.session.get.call_args.args[0], next_link)
        self.assertIsNone(self.session.get.call_args.kwargs["params"])
        self.assertFalse(self.session.get.call_args.kwargs["allow_redirects"])

    def test_discovery_version_is_independent_of_query_version(self):
        def list_response(url, **kwargs):
            version = kwargs["params"]["api-version"]
            return response({"value": [{"id": WORKSPACE}]}, 200 if version == "2023-09-01" else 404)

        self.session.get.side_effect = list_response
        result = self.client.get("/api/azure-diagnostics/workspaces", query_string={"subscriptionId": SUBSCRIPTION})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.get_json()["workspaces"][0]["id"], WORKSPACE.lower())
        self.assertEqual(self.query().status_code, 200)
        self.assertEqual(self.session.post.call_args.kwargs["params"], {"api-version": "2017-10-01"})

    def resources(self, subscription_ids=None, **options):
        return self.client.get("/api/azure-diagnostics/resources", query_string={
            "subscriptionIds": subscription_ids if subscription_ids is not None else [SUBSCRIPTION],
            **options,
        })

    def mock_resource_settings(self, settings):
        self.session.get.side_effect = [
            response({"value": [{"id": RESOURCE, "name": "openai", "kind": "OpenAI", "location": "eastus"}]}),
            response({"value": settings}),
        ]

    def test_resources_filter_kinds_and_preserve_authentication_and_versions(self):
        ai_resource = RESOURCE + "-ai"
        self.session.get.side_effect = [
            response({"value": [
                {"id": RESOURCE, "name": "openai", "kind": "OpenAI", "location": "eastus"},
                {"id": ai_resource, "name": "foundry", "kind": "AIServices", "location": "westus"},
                {"id": RESOURCE + "-speech", "kind": "SpeechServices"},
                {"id": RESOURCE + "-legacy", "kind": "CognitiveServices"},
            ]}),
            response({"value": [{"properties": {"workspaceId": WORKSPACE, "logs": [
                {"category": "AzureOpenAIRequestUsage", "enabled": True},
            ]}}]}),
            response({"value": []}),
        ]
        result = self.resources()
        self.assertEqual(result.status_code, 200)
        resources = result.get_json()["resources"]
        self.assertEqual([item["id"] for item in resources], [RESOURCE.lower(), ai_resource.lower()])
        self.assertEqual(resources[0]["subscriptionId"], SUBSCRIPTION)
        self.assertEqual(resources[0]["resourceGroup"], "rg")
        self.assertEqual(resources[0]["location"], "eastus")
        self.assertEqual(resources[0]["kind"], "OpenAI")
        self.assertEqual(resources[1]["kind"], "AIServices")
        self.assertEqual(resources[0]["diagnosticStatus"], "known")
        self.assertTrue(resources[0]["logsEnabled"])
        self.assertTrue(resources[0]["usageLogsEnabled"])
        self.assertEqual(resources[0]["enabledLogCategories"], ["AzureOpenAIRequestUsage"])
        self.assertEqual(resources[0]["workspaceIds"], [WORKSPACE.lower()])
        self.assertEqual(resources[0]["usageWorkspaceIds"], [WORKSPACE.lower()])
        calls = self.session.get.call_args_list
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0].args[0], f"{diagnostics.ARM_ENDPOINT}/subscriptions/{SUBSCRIPTION}/providers/Microsoft.CognitiveServices/accounts")
        self.assertEqual(calls[0].kwargs["params"], {"api-version": "2024-10-01"})
        for call, resource_id in zip(calls[1:], (RESOURCE.lower(), ai_resource.lower())):
            self.assertEqual(call.args[0], f"{diagnostics.ARM_ENDPOINT}{resource_id}/providers/Microsoft.Insights/diagnosticSettings")
            self.assertEqual(call.kwargs["params"], {"api-version": "2021-05-01-preview"})
        for call in calls:
            self.assertEqual(call.kwargs["headers"], {"Authorization": "Bearer secret-test-token"})
            self.assertEqual(call.kwargs["timeout"], main.SyncConfig().subscription_list_timeout_seconds)
            self.assertFalse(call.kwargs["allow_redirects"])
        self.mocks[3].assert_called_once_with(self.auth, self.session, self.mocks[1].return_value)
        self.assertEqual(self.mocks[2].call_count, 2)
        self.assertEqual(self.session.__exit__.call_count, 2)

    def test_two_stages_merge_to_exact_default_response(self):
        settings = [{"properties": {"workspaceId": WORKSPACE, "logs": [{"categoryGroup": "allLogs", "enabled": True}]}}]
        self.mock_resource_settings(settings)
        full = self.resources().get_json()
        self.mock_resource_settings(settings)
        self.assertEqual(self.resources(phase="full").get_json(), full)
        self.session.get.reset_mock()
        self.mock_resource_settings(settings)
        accounts = self.resources(phase="accounts").get_json()
        self.session.get.assert_called_once()
        item = accounts["resources"][0]
        self.assertEqual(item["diagnosticStatus"], "pending")
        for field in ("logsEnabled", "usageLogsEnabled", "workspaceIds", "usageWorkspaceIds"):
            self.assertIsNone(item[field])
        self.session.get.reset_mock()
        self.session.get.side_effect = [response({"value": settings})]
        statuses = self.resources(phase="diagnostics", resourceIds=[RESOURCE, RESOURCE.upper()]).get_json()
        self.session.get.assert_called_once()
        self.assertEqual(set(statuses["resources"][0]), {"id", "diagnosticStatus", "logsEnabled", "usageLogsEnabled", "enabledLogCategories", "enabledLogCategoryGroups", "workspaceIds", "usageWorkspaceIds"})
        item.update(statuses["resources"][0])
        self.assertEqual(accounts, full)
        self.assertEqual(self.mocks[3].call_count, 4)

    def test_staged_requests_reject_invalid_inputs_before_resource_calls(self):
        cases = (
            ({"phase": "invalid"}, 400), ({"phase": ""}, 400),
            ({"phase": ["accounts", "diagnostics"]}, 400),
            ({"phase": "accounts", "resourceIds": [RESOURCE]}, 400),
            ({"resourceIds": [RESOURCE]}, 400),
            ({"phase": "diagnostics"}, 400),
            ({"phase": "diagnostics", "resourceIds": [""]}, 400),
            ({"phase": "diagnostics", "resourceIds": [RESOURCE] * 101}, 400),
            ({"phase": "diagnostics", "resourceIds": [WORKSPACE]}, 400),
            ({"phase": "diagnostics", "resourceIds": [RESOURCE + "?x=1"]}, 400),
            ({"phase": "diagnostics", "resourceIds": [RESOURCE, RESOURCE.replace(SUBSCRIPTION, OTHER_SUBSCRIPTION)]}, 403),
        )
        for options, status in cases:
            with self.subTest(options=options):
                self.assertEqual(self.resources(**options).status_code, status)
        self.session.get.assert_not_called()

    def test_both_stages_recheck_login_and_current_subscription_permissions(self):
        for phase in ("accounts", "diagnostics"):
            options = {"phase": phase, **({"resourceIds": [RESOURCE]} if phase == "diagnostics" else {})}
            with self.subTest(phase=phase):
                self.mocks[3].return_value = []
                self.assertEqual(self.resources(**options).status_code, 403)
                self.mocks[0].side_effect = main.HttpError(401, "登录失效")
                self.assertEqual(self.resources(**options).status_code, 401)
                self.mocks[0].side_effect = None
        self.session.get.assert_not_called()

    def test_diagnostics_stage_retains_unknown_and_sanitized_upstream_errors(self):
        for upstream, expected in ((401, 200), (403, 200), (429, 429), (500, 502), (302, 502)):
            with self.subTest(upstream=upstream):
                self.session.get.return_value = response({"error": self.auth["accessToken"]}, upstream)
                result = self.resources(phase="diagnostics", resourceIds=[RESOURCE])
                self.assertEqual(result.status_code, expected)
                self.assertNotIn(self.auth["accessToken"], result.get_data(as_text=True))
                if expected == 200:
                    self.assertEqual(result.get_json()["resources"][0]["diagnosticStatus"], "unknown")

    def test_resource_logs_categories_and_groups_are_not_confused_with_metrics(self):
        for logs, metrics, enabled, usage in (
            ([], [{"category": "AllMetrics", "enabled": True}], False, False),
            ([{"category": "AzureOpenAIRequestUsage", "enabled": False}], [], False, False),
            ([{"category": "RequestResponse", "enabled": True}], [], True, False),
            ([{"categoryGroup": "audit", "enabled": True}], [], True, False),
            ([{"categoryGroup": "allLogs", "enabled": True}], [], True, True),
            ([{"categoryGroup": "allLogs", "enabled": False}], [], False, False),
        ):
            with self.subTest(logs=logs):
                self.mock_resource_settings([{"properties": {"logs": logs, "metrics": metrics}}])
                item = self.resources().get_json()["resources"][0]
                self.assertEqual(item["logsEnabled"], enabled)
                self.assertEqual(item["usageLogsEnabled"], usage)
                self.assertEqual(item["workspaceIds"], [])
                self.assertEqual(item["usageWorkspaceIds"], [])
                self.assertEqual(item["enabledLogCategoryGroups"], [logs[0]["categoryGroup"]] if logs and "categoryGroup" in logs[0] and enabled else [])

    def test_resource_mixed_settings_scope_workspaces_to_enabled_logs_and_usage(self):
        audit_workspace = WORKSPACE + "-audit"
        all_logs_workspace = WORKSPACE + "-all"
        settings = [
            {"properties": {"workspaceId": WORKSPACE, "logs": [{"category": "AzureOpenAIRequestUsage", "enabled": True}]}},
            {"properties": {"workspaceId": WORKSPACE.upper(), "logs": [{"categoryGroup": "allLogs", "enabled": True}]}},
            {"properties": {"workspaceId": audit_workspace, "logs": [
                {"category": "Audit", "enabled": True}, {"category": "AzureOpenAIRequestUsage", "enabled": False},
            ]}},
            {"properties": {"workspaceId": all_logs_workspace, "logs": [{"categoryGroup": "ALLLOGS", "enabled": True}]}},
            {"properties": {"workspaceId": WORKSPACE + "-disabled", "logs": [
                {"category": "AzureOpenAIRequestUsage", "enabled": False}, {"categoryGroup": "allLogs", "enabled": False},
            ]}},
            {"properties": {"workspaceId": WORKSPACE + "-metrics", "metrics": [{"category": "AllMetrics", "enabled": True}]}},
            {"properties": {"workspaceId": WORKSPACE + "-empty", "logs": []}},
            {"properties": {"logs": [{"category": "AzureOpenAIRequestUsage", "enabled": True}]}},
        ]
        for values in (settings, list(reversed(settings))):
            with self.subTest(reversed=values is not settings):
                self.mock_resource_settings(values)
                result = self.resources()
                self.assertEqual(result.status_code, 200)
                item = result.get_json()["resources"][0]
                self.assertTrue(item["logsEnabled"])
                self.assertTrue(item["usageLogsEnabled"])
                self.assertEqual(item["workspaceIds"], sorted([WORKSPACE.lower(), audit_workspace.lower(), all_logs_workspace.lower()]))
                self.assertEqual(item["usageWorkspaceIds"], sorted([WORKSPACE.lower(), all_logs_workspace.lower()]))

    def test_resource_metrics_only_settings_do_not_include_workspaces(self):
        self.mock_resource_settings([
            {"properties": {"workspaceId": WORKSPACE, "metrics": [{"category": "AllMetrics", "enabled": True}]}},
            {"properties": {"workspaceId": WORKSPACE + "2", "logs": [], "metrics": [{"category": "AllMetrics", "enabled": True}]}},
        ])
        result = self.resources()
        self.assertEqual(result.status_code, 200)
        item = result.get_json()["resources"][0]
        self.assertEqual(item["diagnosticStatus"], "known")
        self.assertFalse(item["logsEnabled"])
        self.assertFalse(item["usageLogsEnabled"])
        self.assertEqual(item["workspaceIds"], [])
        self.assertEqual(item["usageWorkspaceIds"], [])

    def test_resource_workspaces_are_deduplicated_including_cross_subscription_destinations(self):
        other_workspace = WORKSPACE.replace(SUBSCRIPTION, OTHER_SUBSCRIPTION)
        self.mock_resource_settings([
            {"properties": {"workspaceId": WORKSPACE, "logs": [{"category": "Audit", "enabled": True}]}},
            {"properties": {"workspaceId": WORKSPACE.upper(), "logs": [{"category": "AzureOpenAIRequestUsage", "enabled": True}]}},
            {"properties": {"workspaceId": other_workspace, "logs": [{"category": "Audit", "enabled": True}]}},
        ])
        item = self.resources().get_json()["resources"][0]
        self.assertEqual(item["workspaceIds"], [WORKSPACE.lower(), other_workspace.lower()])
        self.assertEqual(item["usageWorkspaceIds"], [WORKSPACE.lower()])
        self.assertEqual(item["enabledLogCategories"], ["Audit", "AzureOpenAIRequestUsage"])
        self.assertTrue(item["usageLogsEnabled"])
        self.assertEqual(self.query(workspaceIds=[other_workspace]).status_code, 403)

    def test_resource_permission_failures_are_unknown_and_do_not_hide_other_resources(self):
        for status in (401, 403):
            with self.subTest(status=status):
                self.session.get.side_effect = [
                    response({"value": [{"id": RESOURCE, "kind": "OpenAI"}, {"id": RESOURCE + "2", "kind": "AIServices"}]}),
                    response({"error": {"message": self.auth["accessToken"]}}, status),
                    response({"value": []}),
                ]
                result = self.resources()
                self.assertEqual(result.status_code, 200)
                unknown, known = result.get_json()["resources"]
                self.assertEqual(unknown["diagnosticStatus"], "unknown")
                for field in ("logsEnabled", "usageLogsEnabled", "workspaceIds", "usageWorkspaceIds"):
                    self.assertIsNone(unknown[field])
                self.assertEqual(known["diagnosticStatus"], "known")
                self.assertFalse(known["logsEnabled"])
                self.assertFalse(known["usageLogsEnabled"])
                self.assertEqual(known["workspaceIds"], [])
                self.assertEqual(known["usageWorkspaceIds"], [])
                self.assertNotIn(self.auth["accessToken"], result.get_data(as_text=True))

    def test_resource_listing_supports_multiple_normalized_subscriptions(self):
        self.mocks[3].return_value.append({"subscriptionId": OTHER_SUBSCRIPTION})
        self.session.get.side_effect = [
            response({"value": [{"id": RESOURCE.replace(SUBSCRIPTION, OTHER_SUBSCRIPTION), "kind": "aiservices"}]}),
            response({"value": [{"id": RESOURCE, "kind": "openai"}]}),
            response({"value": []}), response({"value": []}),
        ]
        result = self.resources([OTHER_SUBSCRIPTION, SUBSCRIPTION, SUBSCRIPTION.upper()])
        self.assertEqual(result.status_code, 200)
        self.assertEqual([item["subscriptionId"] for item in result.get_json()["resources"]], [SUBSCRIPTION, OTHER_SUBSCRIPTION])
        self.assertEqual(self.session.get.call_count, 4)

    def test_resource_listing_accepts_singular_subscription_and_empty_lists(self):
        self.session.get.return_value = response({"value": []})
        result = self.client.get("/api/azure-diagnostics/resources", query_string={"subscriptionId": SUBSCRIPTION})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.get_json(), {"resources": []})
        self.assertEqual(self.session.get.call_count, 1)

    def test_resource_listing_rejects_invalid_and_forbidden_subscriptions_before_arm(self):
        for subscriptions, status in (([], 400), (["invalid"], 400), ([SUBSCRIPTION] * 101, 400), ([OTHER_SUBSCRIPTION], 403)):
            with self.subTest(subscriptions=subscriptions):
                self.assertEqual(self.resources(subscriptions).status_code, status)
        self.session.get.assert_not_called()

    def test_resource_account_and_settings_pagination_deduplicate_and_preserve_versions(self):
        account_path = f"{diagnostics.ARM_ENDPOINT}/subscriptions/{SUBSCRIPTION}/providers/Microsoft.CognitiveServices/accounts"
        settings_path = f"{diagnostics.ARM_ENDPOINT}{RESOURCE.lower()}/providers/Microsoft.Insights/diagnosticSettings"
        account_next = account_path + "?api-version=2024-10-01&$skiptoken=accounts"
        settings_next = settings_path + "?api-version=2021-05-01-preview&$skiptoken=settings"
        self.session.get.side_effect = [
            response({"value": [{"id": RESOURCE, "kind": "OpenAI"}], "nextLink": account_next}),
            response({"value": [{"id": RESOURCE.upper(), "kind": "OpenAI"}]}),
            response({"value": [{"properties": {"workspaceId": WORKSPACE}}], "nextLink": settings_next}),
            response({"value": [{"properties": {"workspaceId": WORKSPACE, "logs": [{"categoryGroup": "allLogs", "enabled": True}]}}]}),
        ]
        result = self.resources()
        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(result.get_json()["resources"]), 1)
        self.assertTrue(result.get_json()["resources"][0]["usageLogsEnabled"])
        self.assertEqual(result.get_json()["resources"][0]["workspaceIds"], [WORKSPACE.lower()])
        self.assertEqual(result.get_json()["resources"][0]["usageWorkspaceIds"], [WORKSPACE.lower()])
        for index, url in ((1, account_next), (3, settings_next)):
            call = self.session.get.call_args_list[index]
            self.assertEqual(call.args[0], url)
            self.assertIsNone(call.kwargs["params"])

    def test_resource_account_and_settings_unsafe_pagination_is_blocked(self):
        paths = (
            f"{diagnostics.ARM_ENDPOINT}/subscriptions/{SUBSCRIPTION}/providers/Microsoft.CognitiveServices/accounts",
            f"{diagnostics.ARM_ENDPOINT}{RESOURCE.lower()}/providers/Microsoft.Insights/diagnosticSettings",
        )
        for settings, path in enumerate(paths):
            for next_link in ("https://evil.example/token", "https://[invalid", path, path + "?api-version=wrong",
                              path.replace(SUBSCRIPTION, OTHER_SUBSCRIPTION), path + "?api-version=2024-10-01#fragment", 42):
                with self.subTest(settings=settings, next_link=next_link):
                    self.session.get.reset_mock()
                    self.session.get.side_effect = ([response({"value": [{"id": RESOURCE, "kind": "OpenAI"}]})] if settings else []) + [
                        response({"value": [], "nextLink": next_link}),
                    ]
                    self.assertEqual(self.resources().status_code, 502)
                    self.assertEqual(self.session.get.call_count, settings + 1)

    def test_resource_pagination_limit_is_enforced(self):
        path = f"{diagnostics.ARM_ENDPOINT}/subscriptions/{SUBSCRIPTION}/providers/Microsoft.CognitiveServices/accounts"
        self.session.get.side_effect = [
            response({"value": [], "nextLink": path + "?api-version=2024-10-01&$skiptoken=1"}),
            response({"value": [], "nextLink": path + "?api-version=2024-10-01&$skiptoken=2"}),
        ]
        with patch.object(diagnostics, "MAX_PAGES", 2):
            self.assertEqual(self.resources().status_code, 502)
        self.assertEqual(self.session.get.call_count, 2)

    def test_resource_listing_upstream_failures_are_not_reported_as_disabled(self):
        for settings in (False, True):
            for status, expected in ((401, 401), (403, 403), (429, 429), (500, 502), (302, 502)):
                if settings and status in (401, 403):
                    continue
                with self.subTest(settings=settings, status=status):
                    self.session.get.side_effect = ([response({"value": [{"id": RESOURCE, "kind": "OpenAI"}]})] if settings else []) + [
                        response({"error": {"message": self.auth["accessToken"]}}, status),
                    ]
                    result = self.resources()
                    self.assertEqual(result.status_code, expected)
                    self.assertNotIn(self.auth["accessToken"], result.get_data(as_text=True))

    def test_resource_settings_reject_malformed_values_without_claiming_disabled(self):
        for body in ({}, {"value": [None]}, {"value": [{}]}, {"value": [{"properties": []}]},
                     {"value": [{"properties": {"logs": {}}}]},
                     {"value": [{"properties": {"logs": [{"enabled": "true", "category": "Audit"}]}}]},
                     {"value": [{"properties": {"logs": [{"enabled": True}]}}]},
                     {"value": [{"properties": {"logs": [{"enabled": True, "category": []}]}}]},
                     {"value": [{"properties": {"workspaceId": "https://evil.example/workspace"}}]}):
            with self.subTest(body=body):
                self.session.get.side_effect = [response({"value": [{"id": RESOURCE, "kind": "OpenAI"}]}), response(body)]
                self.assertEqual(self.resources().status_code, 502)

    def test_resource_account_ids_cannot_redirect_diagnostics_requests(self):
        for resource_id, expected in ((None, 502), (WORKSPACE, 400), (RESOURCE + "?query=x", 400),
                                      (RESOURCE.replace(SUBSCRIPTION, OTHER_SUBSCRIPTION), 403)):
            with self.subTest(resource_id=resource_id):
                self.session.get.reset_mock()
                self.session.get.return_value = response({"value": [{"id": resource_id, "kind": "OpenAI"}]})
                self.assertEqual(self.resources().status_code, expected)
                self.assertEqual(self.session.get.call_count, 1)

    def test_resource_network_failure_and_invalid_json_return_502_without_secrets(self):
        for failure in (requests.Timeout(self.auth["accessToken"]), Mock(status_code=200, json=Mock(side_effect=ValueError()))):
            with self.subTest(failure=failure):
                self.session.get.side_effect = [response({"value": [{"id": RESOURCE, "kind": "OpenAI"}]}), failure]
                result = self.resources()
                self.assertEqual(result.status_code, 502)
                self.assertNotIn(self.auth["accessToken"], result.get_data(as_text=True))

    def test_forbidden_subscription_does_not_call_arm(self):
        self.assertEqual(self.query(subscriptionIds=[OTHER_SUBSCRIPTION]).status_code, 403)
        self.session.post.assert_not_called()

    def test_invalid_subscription_ids(self):
        for value in (None, "abc", [], [1], ["../other"], [SUBSCRIPTION] * 101):
            with self.subTest(value=value):
                self.assertEqual(self.query(subscriptionIds=value).status_code, 400)
        self.session.post.assert_not_called()

    def test_resources_must_match_selected_subscription(self):
        for field, value in (("workspaceIds", WORKSPACE), ("resourceIds", RESOURCE)):
            with self.subTest(field=field):
                self.assertEqual(self.query(**{field: [value.replace(SUBSCRIPTION, OTHER_SUBSCRIPTION)]}).status_code, 403)
        self.session.post.assert_not_called()

    def test_rejects_resource_injection_and_wrong_type(self):
        for value in (WORKSPACE + "?api-version=x", WORKSPACE + "/../query", WORKSPACE + "';take 1", "https://evil.example/workspace", RESOURCE):
            with self.subTest(value=value):
                self.assertEqual(self.query(workspaceIds=[value]).status_code, 400)
        self.session.post.assert_not_called()

    def test_dates_timezone_and_range_are_validated(self):
        for changes in (
            {"startDate": "2026-02-30"}, {"startDate": "2026-1-1"}, {"startDate": 1},
            {"startDate": "2026-01-03"}, {"endDate": "2099-01-01"},
            {"endDate": "2026-02-01"}, {"tz": "+15"}, {"tz": "+8:99"}, {"tz": []},
        ):
            with self.subTest(changes=changes):
                self.assertEqual(self.query(**changes).status_code, 400)
        self.session.post.assert_not_called()

    def test_rejects_non_object_json(self):
        for value in ([], "text", None):
            self.assertEqual(self.client.post("/api/azure-diagnostics/query", json=value).status_code, 400)

    def test_duplicate_workspace_and_resource_ids_do_not_repeat_query(self):
        self.assertEqual(self.query(workspaceIds=[WORKSPACE, WORKSPACE.upper()], resourceIds=[RESOURCE, RESOURCE.upper()]).status_code, 200)
        self.assertEqual(self.session.post.call_count, 1)

    def test_optional_resource_filter_still_limits_subscriptions(self):
        self.assertEqual(self.query(resourceIds=[]).status_code, 200)
        query = self.session.post.call_args.kwargs["json"]["query"]
        self.assertIn(f"in~ ('{SUBSCRIPTION}')", query)

    def test_external_cross_subscription_and_cyclic_next_links_are_blocked(self):
        path = f"{diagnostics.ARM_ENDPOINT}/subscriptions/{SUBSCRIPTION}/providers/Microsoft.OperationalInsights/workspaces"
        for next_link in ("https://evil.example/steal", path.replace(SUBSCRIPTION, OTHER_SUBSCRIPTION), path, path + "?api-version=2020-01-01", path + "?api-version=2017-10-01", "https://[invalid"):
            with self.subTest(next_link=next_link):
                self.session.get.reset_mock()
                self.session.get.return_value = response({"value": [], "nextLink": next_link})
                result = self.client.get("/api/azure-diagnostics/workspaces", query_string={"subscriptionId": SUBSCRIPTION})
                self.assertEqual(result.status_code, 502)
                self.assertEqual(self.session.get.call_count, 1)

    def test_arm_errors_preserve_auth_and_throttle_status_without_secrets(self):
        for upstream, expected in ((401, 401), (403, 403), (429, 429), (500, 502), (302, 502)):
            with self.subTest(upstream=upstream):
                self.session.post.return_value = response({"error": {"message": self.auth["accessToken"]}}, upstream)
                result = self.query()
                self.assertEqual(result.status_code, expected)
                self.assertNotIn(self.auth["accessToken"], result.get_data(as_text=True))

    def test_network_failure_and_invalid_json_return_502(self):
        self.session.post.side_effect = requests.Timeout("secret-test-token")
        self.assertEqual(self.query().status_code, 502)
        self.session.post.side_effect = None
        self.session.post.return_value = Mock(status_code=200, json=Mock(side_effect=ValueError()))
        self.assertEqual(self.query().status_code, 502)

    def test_partial_and_malformed_results_are_not_silently_counted(self):
        for body in ({"error": {"code": "PartialError"}, "tables": []}, {}, {"tables": [{}]}, {"tables": [{"columns": [{"name": "x"}, {"name": "x"}], "rows": []}]}, {"tables": [{"columns": [{"name": "x"}], "rows": [[]]}]}):
            with self.subTest(body=body):
                self.session.post.return_value = response(body)
                self.assertEqual(self.query().status_code, 502)

    def test_empty_result_is_successful(self):
        self.session.post.return_value = response({"tables": []})
        result = self.query()
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.get_json()["rows"], [])
        self.assertFalse(result.get_json()["truncated"])
        self.assertEqual(result.get_json()["truncatedWorkspaceIds"], [])
        self.assertEqual(result.get_json()["warnings"], [])
        self.assertEqual(result.get_json()["query"], self.session.post.call_args.kwargs["json"]["query"])

    def test_detail_limit_boundary_and_truncation_warning(self):
        for count, truncated in ((4999, False), (5000, False), (5001, True)):
            with self.subTest(count=count):
                self.session.post.return_value = response({"tables": [{
                    "columns": [{"name": "RequestId"}], "rows": [[str(index)] for index in range(count)],
                }]})
                result = self.query().get_json()
                self.assertEqual(len(result["rows"]), min(count, 5000))
                self.assertEqual(result["rows"][-1]["RequestId"], str(min(count, 5000) - 1))
                self.assertEqual(result["truncated"], truncated)
                self.assertEqual(result["truncatedWorkspaceIds"], [WORKSPACE.lower()] if truncated else [])
                if truncated:
                    self.assertIn("已截断", result["warnings"][0])
                    self.assertIn("最新 5000 条", result["warnings"][0])
                    self.assertIn("不代表完整用量", result["warnings"][0])
                else:
                    self.assertEqual(result["warnings"], [])

    def test_multiple_workspace_truncation_is_scoped_and_globally_sorted(self):
        self.session.post.side_effect = [
            response({"tables": [{"columns": [{"name": "TimeGenerated"}], "rows": [["2026-01-01T00:00:00Z"]] * 5001}]}),
            response({"tables": [{"columns": [{"name": "TimeGenerated"}], "rows": [["2026-01-02T00:00:00Z"]]}]}),
        ]
        result = self.query(workspaceIds=[WORKSPACE, WORKSPACE + "2"]).get_json()
        self.assertEqual(len(result["rows"]), 5001)
        self.assertEqual(result["rows"][0]["workspaceId"], (WORKSPACE + "2").lower())
        self.assertEqual(result["truncatedWorkspaceIds"], [WORKSPACE.lower()])
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["warnings"]), 2)
        self.assertTrue(all(call.kwargs["json"]["query"] == result["query"] for call in self.session.post.call_args_list))

    def test_multiple_workspaces_warn_about_cross_workspace_duplicates(self):
        result = self.query(workspaceIds=[WORKSPACE, WORKSPACE + "2"])
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.session.post.call_count, 2)
        self.assertTrue(result.get_json()["warnings"])


class AzureDiagnosticsMainIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.client = main.app.test_client()
        self.config = {"configured": True, "scopes": [main.OPENAI_TIER_SCOPE]}
        self.msal_app = Mock()
        self.msal_app.acquire_token_by_auth_code_flow.return_value = {
            "access_token": "mock-arm-token", "expires_in": 3600,
            "id_token_claims": {"name": "测试用户", "preferred_username": "test@example.com", "tid": "tenant"},
        }
        for target, value in (("get_openai_tier_auth_config", self.config), ("build_openai_tier_msal_app", self.msal_app)):
            patcher = patch.object(main, target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def assert_diagnostics_redirect(self, result):
        self.assertEqual(result.status_code, 302)
        location = urlsplit(result.headers["Location"])
        self.assertEqual(location.path, "/")
        self.assertEqual(parse_qs(location.query), {"view": ["azure-diagnostics"]})

    def seed_flow(self, flow_source=None, session_source=None):
        with self.client.session_transaction() as browser_session:
            browser_session.clear()
            flow = {"state": "copilot-azure-diagnostics-test"}
            if flow_source:
                flow["copilot_source"] = flow_source
            browser_session[main.OPENAI_TIER_AUTH_FLOW_KEY] = flow
            if session_source:
                browser_session[main.OPENAI_TIER_AUTH_SOURCE_SESSION_KEY] = session_source

    def test_main_automatically_registers_diagnostics_routes_without_factory(self):
        self.assertIs(main.app.extensions["azure_diagnostics"], main)
        rules = {rule.rule: rule for rule in main.app.url_map.iter_rules() if rule.rule.startswith("/api/azure-diagnostics/")}
        self.assertEqual(set(rules), {"/api/azure-diagnostics/workspaces", "/api/azure-diagnostics/resources", "/api/azure-diagnostics/query"})
        self.assertIn("GET", rules["/api/azure-diagnostics/workspaces"].methods)
        self.assertIn("GET", rules["/api/azure-diagnostics/resources"].methods)
        self.assertIn("POST", rules["/api/azure-diagnostics/query"].methods)
        self.assertEqual(rules["/api/azure-diagnostics/query"].endpoint, "azure_diagnostics_query")

    def test_main_registered_routes_require_real_session_authentication(self):
        for result in (self.client.get("/api/azure-diagnostics/workspaces"), self.client.get("/api/azure-diagnostics/resources"), self.client.post("/api/azure-diagnostics/query", json={})):
            self.assertEqual(result.status_code, 401)
            self.assertIn("登录", result.get_json()["message"])

    def test_login_url_preserves_diagnostics_source_in_state_and_session(self):
        self.msal_app.initiate_auth_code_flow.return_value = {"auth_uri": "https://login.microsoftonline.com/mock", "state": "mock-state"}
        result = self.client.get("/api/openai-tier/auth/login-url", query_string={"source": "azure-diagnostics"})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.get_json()["authUrl"], "https://login.microsoftonline.com/mock")
        kwargs = self.msal_app.initiate_auth_code_flow.call_args.kwargs
        self.assertTrue(kwargs["state"].startswith("copilot-azure-diagnostics-"))
        self.assertEqual(main.extract_openai_auth_source_from_state(kwargs["state"]), "azure-diagnostics")
        self.assertEqual(kwargs["scopes"], self.config["scopes"])
        with self.client.session_transaction() as browser_session:
            self.assertEqual(browser_session[main.OPENAI_TIER_AUTH_SOURCE_SESSION_KEY], "azure-diagnostics")
            self.assertEqual(browser_session[main.OPENAI_TIER_AUTH_FLOW_KEY]["copilot_source"], "azure-diagnostics")

    def test_success_callback_returns_to_diagnostics_and_saves_shared_auth(self):
        self.seed_flow(flow_source="azure-diagnostics")
        result = self.client.get("/auth/openai/callback", query_string={"code": "mock-code"})
        self.assert_diagnostics_redirect(result)
        self.msal_app.acquire_token_by_auth_code_flow.assert_called_once()
        with self.client.session_transaction() as browser_session:
            auth = browser_session[main.OPENAI_TIER_AUTH_SESSION_KEY]
            self.assertEqual(auth["accessToken"], "mock-arm-token")
            self.assertEqual(auth["tenantId"], "tenant")
            self.assertGreater(auth["expiresAt"], int(time.time()))
            self.assertNotIn(main.OPENAI_TIER_AUTH_FLOW_KEY, browser_session)
            self.assertNotIn(main.OPENAI_TIER_AUTH_SOURCE_SESSION_KEY, browser_session)
            self.assertFalse(browser_session[main.OPENAI_TIER_NOTICE_SESSION_KEY]["isError"])

    def test_callback_resolves_diagnostics_source_from_query_state_flow_and_session(self):
        cases = (
            ({"source": "azure-diagnostics"}, "openai-tier", "openai-tier"),
            ({"state": "copilot-azure-diagnostics-test"}, "openai-tier", "openai-tier"),
            ({}, "azure-diagnostics", "openai-tier"),
            ({}, None, "azure-diagnostics"),
        )
        for args, flow_source, session_source in cases:
            with self.subTest(args=args, flow_source=flow_source, session_source=session_source):
                self.seed_flow(flow_source, session_source)
                self.assert_diagnostics_redirect(self.client.get("/auth/openai/callback", query_string=args))

    def test_expired_callback_returns_to_diagnostics_without_msal(self):
        result = self.client.get("/auth/openai/callback", query_string={"state": "copilot-azure-diagnostics-test"})
        self.assert_diagnostics_redirect(result)
        self.msal_app.acquire_token_by_auth_code_flow.assert_not_called()
        with self.client.session_transaction() as browser_session:
            self.assertTrue(browser_session[main.OPENAI_TIER_NOTICE_SESSION_KEY]["isError"])
            self.assertNotIn(main.OPENAI_TIER_AUTH_SESSION_KEY, browser_session)

    def test_failed_callbacks_return_to_diagnostics_without_auth(self):
        for outcome in ({"error": "access_denied", "error_description": "cancelled"}, {}, ValueError("mock failure")):
            with self.subTest(outcome=outcome):
                self.seed_flow(flow_source="azure-diagnostics")
                if isinstance(outcome, Exception):
                    self.msal_app.acquire_token_by_auth_code_flow.side_effect = outcome
                else:
                    self.msal_app.acquire_token_by_auth_code_flow.side_effect = None
                    self.msal_app.acquire_token_by_auth_code_flow.return_value = outcome
                self.assert_diagnostics_redirect(self.client.get("/auth/openai/callback"))
                with self.client.session_transaction() as browser_session:
                    self.assertTrue(browser_session[main.OPENAI_TIER_NOTICE_SESSION_KEY]["isError"])
                    self.assertNotIn(main.OPENAI_TIER_AUTH_SESSION_KEY, browser_session)


if __name__ == "__main__":
    unittest.main()
