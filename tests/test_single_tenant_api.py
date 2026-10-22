import unittest
import threading
import time
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import main as application


class SingleTenantCostApiTests(unittest.TestCase):
    def setUp(self):
        self.client = application.app.test_client()
        application.app.config.update(TESTING=True)
        application._openai_tier_concurrency_controller = None
        self.audit_db_path = application.AUDIT_LOG_DB_PATH
        self.temp_dir = tempfile.TemporaryDirectory()
        application.AUDIT_LOG_DB_PATH = Path(self.temp_dir.name) / "audit.db"
        application.initialize_audit_log_store()
        self.auth_state = {
            "accessToken": "test-access-token",
            "account": {
                "name": "测试用户",
                "preferredUsername": "test@example.com",
                "tenantId": "tenant-a",
            },
        }

    def tearDown(self):
        application.AUDIT_LOG_DB_PATH = self.audit_db_path
        application._active_audit_contexts.clear()
        application._openai_tier_concurrency_controller = None
        for _ in range(3):
            try:
                self.temp_dir.cleanup()
                break
            except PermissionError:
                time.sleep(0.05)

    @patch("main.fetch_openai_tier_row_isolated")
    @patch("main.resolve_openai_tier_workers")
    @patch("main.build_http_session")
    def test_openai_tier_query_reuses_shared_http_session(self, mock_build_session, mock_resolve_workers, mock_fetch_row):
        session_context = Mock()
        shared_session = Mock()
        session_context.__enter__ = Mock(return_value=shared_session)
        session_context.__exit__ = Mock(return_value=False)
        mock_build_session.return_value = session_context
        mock_resolve_workers.return_value = 2

        def _fake_fetch(_token, subscription, http_session):
            return {
                "subscriptionName": subscription["subscriptionName"],
                "subscriptionId": subscription["subscriptionId"],
                "queryStatus": "OK",
            }, None

        mock_fetch_row.side_effect = _fake_fetch

        subscriptions = [
            {"subscriptionId": "sub-a", "subscriptionName": "A"},
            {"subscriptionId": "sub-b", "subscriptionName": "B"},
            {"subscriptionId": "sub-c", "subscriptionName": "C"},
        ]
        rows, warnings = application.query_openai_tier_rows("token", subscriptions, application.SyncConfig())

        self.assertEqual(len(rows), 3)
        self.assertEqual(warnings, [])
        self.assertEqual(mock_build_session.call_count, 1)
        self.assertTrue(all(call.args[2] is shared_session for call in mock_fetch_row.call_args_list))

    def test_openai_tier_adaptive_workers_reduce_after_throttle(self):
        controller = application.OpenAITierConcurrencyController()
        # 假设上一个批次存在明显限流，下一批次应下调并发。
        controller.observe_batch(total_subscriptions=10, ok_count=6, throttle_count=4, elapsed_ms=2000)
        application._openai_tier_concurrency_controller = controller

        with patch.dict("os.environ", {"OPENAI_TIER_ADAPTIVE_CONCURRENCY_ENABLED": "true", "OPENAI_TIER_MAX_WORKERS": "10"}, clear=False):
            workers = application.resolve_openai_tier_workers(application.SyncConfig(max_workers=10), subscription_count=10)

        self.assertLessEqual(workers, 5)

    @patch("main.get_openai_tier_auth_config", return_value={"configured": True})
    def test_query_requires_login(self, mock_auth_config):
        response = self.client.post(
            "/api/single-tenant/query",
            json={"subscriptionIds": ["subscription-a"], "startDate": "2026-01-01", "endDate": "2026-01-02"},
        )

        self.assertEqual(response.status_code, 401)

    @patch("main.require_openai_tier_auth_state")
    def test_query_rejects_missing_subscription(self, mock_auth_state):
        mock_auth_state.return_value = self.auth_state

        response = self.client.post(
            "/api/single-tenant/query",
            json={"startDate": "2026-01-01", "endDate": "2026-01-02"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("subscriptionIds", response.get_json()["message"])

    @patch("main.require_openai_tier_auth_state")
    def test_query_rejects_invalid_date_range(self, mock_auth_state):
        mock_auth_state.return_value = self.auth_state

        response = self.client.post(
            "/api/single-tenant/query",
            json={"subscriptionIds": ["subscription-a"], "startDate": "2026-01-03", "endDate": "2026-01-02"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("开始时间", response.get_json()["message"])

    @patch("main.get_cached_user_subscriptions")
    @patch("main.require_openai_tier_auth_state")
    def test_query_rejects_subscription_outside_current_permission_scope(self, mock_auth_state, mock_subscriptions):
        mock_auth_state.return_value = self.auth_state
        mock_subscriptions.return_value = [{"subscriptionId": "subscription-a", "subscriptionName": "订阅 A"}]

        response = self.client.post(
            "/api/single-tenant/query",
            json={"subscriptionIds": ["subscription-b"], "startDate": "2026-01-01", "endDate": "2026-01-02"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertIn("无访问权限", response.get_json()["message"])

    @patch("main.query_and_aggregate_costs")
    @patch("main.get_cached_user_subscriptions")
    @patch("main.require_openai_tier_auth_state")
    def test_query_passes_selected_subscription_and_dates(self, mock_auth_state, mock_subscriptions, mock_query):
        mock_auth_state.return_value = self.auth_state
        mock_subscriptions.return_value = [{"subscriptionId": "subscription-a", "subscriptionName": "订阅 A"}]
        mock_query.return_value = {"summary": {"totalCost": 0}, "warnings": []}

        response = self.client.post(
            "/api/single-tenant/query",
            json={"subscriptionIds": ["subscription-a"], "startDate": "2026-01-01", "endDate": "2026-01-02"},
        )

        self.assertEqual(response.status_code, 200)
        query_kwargs = mock_query.call_args.kwargs
        self.assertEqual(query_kwargs["start_date"], "2026-01-01")
        self.assertEqual(query_kwargs["end_date"], "2026-01-02")
        self.assertEqual(query_kwargs["subscriptions"][0].subscription_id, "subscription-a")

    @patch("main.require_openai_tier_auth_state")
    def test_query_allows_unlimited_subscriptions_by_default(self, mock_auth_state):
        mock_auth_state.return_value = self.auth_state

        subscription_ids = [f"subscription-{index}" for index in range(6)]

        self.assertEqual(application.normalize_subscription_ids(subscription_ids, 0), subscription_ids)

    def test_query_rejects_subscriptions_over_configured_limit(self):
        with self.assertRaises(application.HttpError) as context:
            application.normalize_subscription_ids([f"subscription-{index}" for index in range(6)], 5)

        self.assertEqual(context.exception.status_code, 400)
        self.assertIn("5", context.exception.message)

    @patch("main.fetch_enabled_user_subscriptions")
    def test_subscription_cache_prevents_duplicate_arm_request(self, mock_fetch_subscriptions):
        mock_fetch_subscriptions.return_value = [{"subscriptionId": "subscription-a", "subscriptionName": "订阅 A"}]
        application._single_tenant_subscription_cache.clear()

        with application.build_http_session(application.SyncConfig()) as http_session:
            first = application.get_cached_user_subscriptions(self.auth_state, http_session, application.SyncConfig())
            second = application.get_cached_user_subscriptions(self.auth_state, http_session, application.SyncConfig())

        self.assertEqual(first, second)
        self.assertEqual(mock_fetch_subscriptions.call_count, 1)

    def test_cost_retry_after_supports_azure_throttle_headers(self):
        delay = application.parse_cost_retry_after(
            {
                "Retry-After": "2",
                "x-ms-ratelimit-microsoft.costmanagement-tenant-retry-after": "7",
            }
        )

        self.assertEqual(delay, 7)

    def test_cost_query_governor_limits_same_scope_concurrency(self):
        sync_config = application.SyncConfig(
            global_cost_api_max_inflight=4,
            tenant_cost_api_max_inflight=2,
            scope_cost_api_max_inflight=1,
            cost_query_rate_limit_enabled=False,
        )
        governor = application.CostQueryGovernor()
        active_count = 0
        max_active_count = 0
        count_lock = threading.Lock()

        def acquire_scope():
            nonlocal active_count, max_active_count
            with governor.limit(sync_config, "tenant-a", "subscription-a"):
                with count_lock:
                    active_count += 1
                    max_active_count = max(max_active_count, active_count)
                time.sleep(0.01)
                with count_lock:
                    active_count -= 1

        threads = [threading.Thread(target=acquire_scope) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(max_active_count, 1)

    def test_cost_query_retries_temporary_server_error(self):
        class Response:
            status_code = 503
            headers = {}
            content = b""

            @staticmethod
            def json():
                return {"error": {"message": "temporary failure"}}

        sync_config = application.SyncConfig(
            cost_query_max_retries=1,
            cost_query_retry_base_seconds=1,
            cost_query_retry_max_delay_seconds=1,
            cost_query_retry_jitter_max_seconds=0,
            cost_query_retry_total_wait_seconds=2,
            cost_query_rate_limit_enabled=False,
        )
        session = Mock()
        session.post.return_value = Response()

        with patch("main.time.sleep"):
            with self.assertRaises(application.HttpError):
                application.post_cost_query_with_retry(
                    token="token",
                    tenant_key="tenant-a",
                    subscription_id="subscription-a",
                    url="https://management.azure.com/example",
                    payload={},
                    session=session,
                    sync_config=sync_config,
                )

        self.assertEqual(session.post.call_count, 2)

    @patch("main.query_and_aggregate_costs")
    @patch("main.get_cached_user_subscriptions")
    @patch("main.require_openai_tier_auth_state")
    def test_api_audit_log_contains_required_fields_and_filters(self, mock_auth_state, mock_subscriptions, mock_query):
        mock_auth_state.return_value = self.auth_state
        mock_subscriptions.return_value = [{"subscriptionId": "subscription-a", "subscriptionName": "订阅 A"}]
        mock_query.return_value = {"summary": {"totalCost": 0}, "warnings": []}
        with self.client.session_transaction() as browser_session:
            browser_session[application.OPENAI_TIER_AUTH_SESSION_KEY] = {
                "accessToken": "token",
                "expiresAt": int(time.time()) + 300,
                "accountName": "测试用户",
                "preferredUsername": "test@example.com",
                "tenantId": "tenant-a",
            }

        response = self.client.post(
            "/api/single-tenant/query",
            json={"subscriptionIds": ["subscription-a"], "startDate": "2026-01-01", "endDate": "2026-01-02"},
        )

        self.assertEqual(response.status_code, 200)
        logs_response = self.client.get(
            "/api/audit-logs",
            query_string={"loginAccount": "test@example.com", "tenantId": "tenant-a", "subscriptionId": "subscription-a"},
        )
        self.assertEqual(logs_response.status_code, 200)
        logs_payload = logs_response.get_json()
        item = logs_payload["items"][0]
        for field in (
            "login_account",
            "tenant_id",
            "subscription_id",
            "elapsed_seconds",
            "throttle_count",
            "request_path",
            "client_ip",
            "request_started_at",
            "request_finished_at",
            "throttle_events",
        ):
            self.assertIn(field, item)
        self.assertEqual(item["login_account"], "test@example.com")
        self.assertEqual(item["tenant_id"], "tenant-a")
        self.assertEqual(item["subscription_id"], "subscription-a")
        self.assertIsInstance(item["throttle_events"], list)
        self.assertIn("withinPerformanceTarget", logs_payload["summary"])

    @patch("main.query_and_aggregate_costs")
    @patch("main.get_cached_user_subscriptions")
    @patch("main.require_openai_tier_auth_state")
    def test_audit_dimension_filter_supports_multi_subscription_request(self, mock_auth_state, mock_subscriptions, mock_query):
        mock_auth_state.return_value = self.auth_state
        mock_subscriptions.return_value = [
            {"subscriptionId": "subscription-a", "subscriptionName": "订阅 A"},
            {"subscriptionId": "subscription-b", "subscriptionName": "订阅 B"},
        ]
        mock_query.return_value = {"summary": {"totalCost": 0}, "warnings": []}
        with self.client.session_transaction() as browser_session:
            browser_session[application.OPENAI_TIER_AUTH_SESSION_KEY] = {
                "accessToken": "token",
                "expiresAt": int(time.time()) + 300,
                "accountName": "测试用户",
                "preferredUsername": "test@example.com",
                "tenantId": "tenant-a",
            }

        response = self.client.post(
            "/api/single-tenant/query",
            json={"subscriptionIds": ["subscription-a", "subscription-b"], "startDate": "2026-01-01", "endDate": "2026-01-02"},
        )
        self.assertEqual(response.status_code, 200)

        filtered = self.client.get(
            "/api/audit-logs",
            query_string={"loginAccount": "test@example.com", "tenantId": "tenant-a", "subscriptionId": "subscription-b"},
        )
        self.assertEqual(filtered.status_code, 200)
        payload = filtered.get_json()
        self.assertGreaterEqual(payload["summary"]["count"], 1)

    def test_throttle_audit_records_are_filterable(self):
        class Response:
            status_code = 429
            headers = {"x-ms-ratelimit-microsoft.costmanagement-tenant-retry-after": "1"}
            content = b""

            @staticmethod
            def json():
                return {"error": {"message": "throttled"}}

        sync_config = application.SyncConfig(
            cost_query_max_retries=1,
            cost_query_retry_base_seconds=1,
            cost_query_retry_max_delay_seconds=1,
            cost_query_retry_jitter_max_seconds=0,
        )
        with application.app.test_request_context("/api/single-tenant/query", method="POST", json={"subscriptionIds": ["subscription-a"]}):
            application.start_api_audit_request()
            with patch("main.time.sleep"):
                with self.assertRaises(application.ThrottledError):
                    application.post_cost_query_with_retry(
                        token="token",
                        tenant_key="tenant-a",
                        subscription_id="subscription-a",
                        url="https://management.azure.com/subscriptions/subscription-a/providers/Microsoft.CostManagement/query",
                        payload={},
                        session=Mock(post=Mock(return_value=Response())),
                        sync_config=sync_config,
                    )
            application.write_api_audit_log(429, 1.0)

        with application.get_audit_connection() as connection:
            item = dict(connection.execute("SELECT * FROM api_audit_logs WHERE throttle_count > 0").fetchone())
            throttle_event = dict(connection.execute("SELECT * FROM api_throttle_events ORDER BY id DESC LIMIT 1").fetchone())
        self.assertEqual(item["throttle_count"], 1)
        self.assertEqual(item["throttle_request_path"], "/subscriptions/subscription-a/providers/Microsoft.CostManagement/query")
        self.assertIn("tenant-retry-after", item["throttle_rule"])
        self.assertEqual(throttle_event["status_code"], 429)
        self.assertEqual(throttle_event["request_path"], "/subscriptions/subscription-a/providers/Microsoft.CostManagement/query")

    def test_throttle_trend_endpoint_returns_grouped_data(self):
        with application.app.test_request_context("/api/single-tenant/query", method="POST", json={"subscriptionIds": ["subscription-a"]}):
            application.start_api_audit_request()
            application.record_throttle_audit(
                tenant_id="tenant-a",
                subscription_id="subscription-a",
                request_path="/subscriptions/subscription-a/providers/Microsoft.CostManagement/query",
                retry_wait_seconds=2,
                throttle_rule="retry-after",
            )
            application.write_api_audit_log(429, 1.0)

        response = self.client.get(
            "/api/audit-logs/throttle-trends",
            query_string={"groupBy": "rule", "tenantId": "tenant-a", "subscriptionId": "subscription-a"},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertGreaterEqual(payload["summary"]["count"], 1)
        self.assertTrue(any(item.get("bucket") == "retry-after" for item in payload["items"]))


if __name__ == "__main__":
    unittest.main()
