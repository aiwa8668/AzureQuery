import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, Mock, patch
from urllib.parse import urlsplit

import requests
from flask import Flask, has_app_context, has_request_context

import azure_diagnostics as diagnostics
import main


SUBSCRIPTIONS = [f"{index:08d}-1111-1111-1111-111111111111" for index in range(1, 13)]


def resource(subscription, kind, name):
    return f"/subscriptions/{subscription}/resourceGroups/rg/providers/{kind}/{name}".lower()


def response(body, status=200):
    return Mock(status_code=status, json=Mock(return_value=body))


class DiagnosticsConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True)
        self.app.register_error_handler(main.HttpError, main.handle_http_error)
        diagnostics.register_routes(self.app, main)
        self.client = self.app.test_client()
        self.config = main.SyncConfig(max_workers=3)
        self.lock = threading.Lock()
        self.sessions = []
        self.calls = []
        self.active = 0
        self.peak = 0
        self.barriers = {}
        self.handler = lambda url, kwargs: response({"value": []})
        self.auth = {"accessToken": "private-mock-token"}
        self.main_thread = threading.get_ident()
        self.patchers = [
            patch.object(main, "require_openai_tier_auth_state", side_effect=self.authorize),
            patch.object(main, "load_sync_config", return_value=self.config),
            patch.object(main, "get_cached_user_subscriptions", side_effect=self.allowed),
            patch.object(main, "build_http_session", side_effect=self.build_session),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def authorize(self):
        self.assertTrue(has_request_context())
        self.assertEqual(threading.get_ident(), self.main_thread)
        return self.auth

    def allowed(self, auth, http_session, config):
        self.assertTrue(has_request_context())
        return [{"subscriptionId": item} for item in SUBSCRIPTIONS]

    def build_session(self, config):
        owner = threading.get_ident()
        http_session = MagicMock()
        http_session.__enter__.return_value = http_session
        http_session.__exit__.return_value = False

        def get(url, **kwargs):
            self.assertEqual(threading.get_ident(), owner)
            self.assertFalse(has_request_context())
            self.assertFalse(has_app_context())
            self.assertFalse(kwargs["allow_redirects"])
            self.assertEqual(kwargs["timeout"], config.subscription_list_timeout_seconds)
            self.assertEqual(kwargs["headers"], {"Authorization": f"Bearer {self.auth['accessToken']}"})
            with self.lock:
                self.active += 1
                self.peak = max(self.peak, self.active)
                self.calls.append((http_session, url, kwargs))
            try:
                stage = "settings" if "diagnosticsettings" in url.lower() else "list"
                if stage in self.barriers:
                    self.barriers[stage].wait(timeout=5)
                time.sleep(0.005)
                return self.handler(url, kwargs)
            finally:
                with self.lock:
                    self.active -= 1

        http_session.get.side_effect = get
        with self.lock:
            self.sessions.append(http_session)
        return http_session

    def listing(self, endpoint, subscriptions):
        return self.client.get(f"/api/azure-diagnostics/{endpoint}", query_string={"subscriptionIds": subscriptions})

    def assert_closed(self):
        self.assertEqual(len({id(item) for item in self.sessions}), len(self.sessions))
        for http_session in self.sessions:
            http_session.__exit__.assert_called_once()

    def test_cross_subscription_workspaces_are_parallel_sorted_and_page_isolated(self):
        subscriptions = list(reversed(SUBSCRIPTIONS[:6]))
        self.barriers["list"] = threading.Barrier(3)

        def handle(url, kwargs):
            subscription = urlsplit(url).path.split("/")[2]
            item = {"id": resource(subscription, "Microsoft.OperationalInsights/workspaces", "logs")}
            if not urlsplit(url).query:
                return response({"value": [item], "nextLink": url + "?api-version=2023-09-01&$skiptoken=next"})
            self.assertIsNone(kwargs["params"])
            return response({"value": [item]})

        self.handler = handle
        result = self.listing("workspaces", subscriptions)
        self.assertEqual(result.status_code, 200)
        items = result.get_json()["workspaces"]
        self.assertEqual([item["subscriptionId"] for item in items], sorted(subscriptions))
        self.assertEqual(self.peak, 3)
        self.assertEqual(len(self.calls), 12)
        self.assertEqual(len(self.sessions), 4)
        for http_session in self.sessions[1:]:
            self.assertEqual(http_session.get.call_count, 4)
        self.assert_closed()

    def test_accounts_and_diagnostics_are_parallel_with_permission_fallback(self):
        self.barriers = {stage: threading.Barrier(3) for stage in ("list", "settings")}

        def handle(url, kwargs):
            subscription = urlsplit(url).path.split("/")[2]
            if "diagnosticsettings" not in url.lower():
                return response({"value": [{"id": resource(subscription, "Microsoft.CognitiveServices/accounts", "ai"), "kind": "AIServices"}]})
            status = {SUBSCRIPTIONS[0]: 401, SUBSCRIPTIONS[1]: 403}.get(subscription, 200)
            return response({"value": []}, status)

        self.handler = handle
        result = self.listing("resources", list(reversed(SUBSCRIPTIONS[:6])))
        self.assertEqual(result.status_code, 200)
        items = result.get_json()["resources"]
        self.assertEqual([item["subscriptionId"] for item in items], SUBSCRIPTIONS[:6])
        self.assertEqual([item["diagnosticStatus"] for item in items], ["unknown", "unknown"] + ["known"] * 4)
        self.assertIsNone(items[0]["usageLogsEnabled"])
        self.assertFalse(items[-1]["usageLogsEnabled"])
        self.assertEqual(self.peak, 3)
        self.assertEqual(len(self.sessions), 4)
        for http_session in self.sessions[1:]:
            urls = [call.args[0].lower() for call in http_session.get.call_args_list]
            self.assertTrue(any("diagnosticsettings" in url for url in urls))
            self.assertTrue(any("diagnosticsettings" not in url for url in urls))
        self.assert_closed()

    def test_single_subscription_settings_expand_workers_and_reuse_listing_session(self):
        self.barriers["settings"] = threading.Barrier(3)
        ids = [resource(SUBSCRIPTIONS[0], "Microsoft.CognitiveServices/accounts", str(index)) for index in range(12)]
        self.handler = lambda url, kwargs: response({"value": [] if "diagnosticsettings" in url.lower() else [{"id": item, "kind": "OpenAI"} for item in ids]})
        result = self.listing("resources", SUBSCRIPTIONS[:1])
        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(result.get_json()["resources"]), 12)
        self.assertEqual(self.peak, 3)
        self.assertEqual(len(self.sessions), 4)
        self.assertGreater(self.sessions[1].get.call_count, 1)
        self.assert_closed()

    def test_accounts_stage_skips_diagnostics_and_requests_do_not_share_sessions(self):
        self.barriers["list"] = threading.Barrier(3)

        def handle(url, kwargs):
            self.assertNotIn("diagnosticsettings", url.lower())
            subscription = urlsplit(url).path.split("/")[2]
            return response({"value": [{"id": resource(subscription, "Microsoft.CognitiveServices/accounts", "ai"), "kind": "OpenAI"}]})

        self.handler = handle
        for _ in range(2):
            result = self.client.get("/api/azure-diagnostics/resources", query_string={"subscriptionIds": SUBSCRIPTIONS[:6], "phase": "accounts"})
            self.assertEqual(result.status_code, 200)
            self.assertTrue(all(item["diagnosticStatus"] == "pending" for item in result.get_json()["resources"]))
        self.assertEqual(len(self.sessions), 8)
        self.assertEqual(len(self.calls), 12)
        self.assert_closed()

    def test_diagnostics_stage_is_parallel_deduplicated_and_skips_accounts(self):
        self.barriers["settings"] = threading.Barrier(3)
        ids = [resource(SUBSCRIPTIONS[0], "Microsoft.CognitiveServices/accounts", str(index)) for index in range(12)]

        def handle(url, kwargs):
            self.assertIn("diagnosticsettings", url.lower())
            self.assertEqual(kwargs["params"], {"api-version": diagnostics.DIAGNOSTIC_SETTINGS_API_VERSION})
            return response({"value": []})

        self.handler = handle
        result = self.client.get("/api/azure-diagnostics/resources", query_string={"subscriptionIds": SUBSCRIPTIONS[:1], "phase": "diagnostics", "resourceIds": list(reversed(ids)) + [ids[0].upper()]})
        self.assertEqual(result.status_code, 200)
        self.assertEqual([item["id"] for item in result.get_json()["resources"]], sorted(ids))
        self.assertEqual(self.peak, 3)
        self.assertEqual(len(self.calls), 12)
        self.assertEqual(len(self.sessions), 4)
        self.assert_closed()

    def test_diagnostic_failures_propagate_and_sessions_close(self):
        for failure, status in ((429, 429), (500, 502), (302, 502), (requests.Timeout("private-mock-token"), 502)):
            with self.subTest(failure=failure):
                self.sessions.clear()

                def handle(url, kwargs):
                    if "diagnosticsettings" not in url.lower():
                        return response({"value": [{"id": resource(SUBSCRIPTIONS[0], "Microsoft.CognitiveServices/accounts", str(index)), "kind": "OpenAI"} for index in range(3)]})
                    if isinstance(failure, Exception):
                        raise failure
                    return response({"error": "private-mock-token"}, failure)

                self.handler = handle
                result = self.listing("resources", SUBSCRIPTIONS[:1])
                self.assertEqual(result.status_code, status)
                self.assertNotIn(self.auth["accessToken"], result.get_data(as_text=True))
                self.assert_closed()

    def test_parallel_lists_reject_cross_subscription_next_link(self):
        self.barriers["list"] = threading.Barrier(3)
        self.handler = lambda url, kwargs: response({"value": [], "nextLink": url.replace(SUBSCRIPTIONS[0], SUBSCRIPTIONS[4]) + "?api-version=2023-09-01"}) if SUBSCRIPTIONS[0] in url else response({"value": []})
        result = self.listing("workspaces", SUBSCRIPTIONS[:3])
        self.assertEqual(result.status_code, 502)
        self.assertEqual(len(self.calls), 3)
        self.assert_closed()

    def test_worker_cap_and_empty_input(self):
        self.config.max_workers = 1000
        self.barriers["list"] = threading.Barrier(diagnostics.MAX_LIST_WORKERS)
        result = self.listing("workspaces", SUBSCRIPTIONS[:8])
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.peak, diagnostics.MAX_LIST_WORKERS)
        self.assert_closed()
        backend = Mock()
        self.assertEqual(diagnostics.bounded_session_map([], Mock(), self.config, backend), [])
        backend.build_http_session.assert_not_called()

    def test_pending_submissions_are_bounded_and_failure_stops_dispatch(self):
        gate = threading.Event()
        submitted = threading.Event()
        count = 0

        class TrackingExecutor(ThreadPoolExecutor):
            def submit(inner, fn, *args, **kwargs):
                nonlocal count
                count += 1
                if count == 3:
                    submitted.set()
                return super().submit(fn, *args, **kwargs)

        def operation(http_session, item):
            self.assertTrue(gate.wait(5))
            raise main.HttpError(429, "throttled")

        with patch.object(diagnostics, "ThreadPoolExecutor", TrackingExecutor):
            with ThreadPoolExecutor(max_workers=1) as driver:
                future = driver.submit(diagnostics.bounded_session_map, list(range(100)), operation, self.config, main)
                try:
                    self.assertTrue(submitted.wait(5))
                    self.assertEqual(count, 3)
                finally:
                    gate.set()
                with self.assertRaises(main.HttpError):
                    future.result(timeout=5)
        self.assertEqual(count, 3)
        self.assert_closed()

    def test_out_of_order_completion_preserves_input_order_and_zero_workers_is_safe(self):
        gate = threading.Event()

        def operation(http_session, item):
            if item == 0:
                self.assertTrue(gate.wait(5))
            else:
                gate.set()
            return item

        result = diagnostics.bounded_session_map(list(range(12)), operation, self.config, main)
        self.assertEqual(result, list(range(12)))
        self.config.max_workers = 0
        self.assertEqual(diagnostics.bounded_session_map([1, 2], lambda http_session, item: item, self.config, main), [1, 2])
        self.assert_closed()


class SubscriptionPaginationTests(unittest.TestCase):
    def test_malformed_json_and_network_errors_are_sanitized(self):
        for failure in (response([]), response({}), Mock(status_code=200, json=Mock(side_effect=ValueError("private-mock-token"))), requests.Timeout("private-mock-token")):
            with self.subTest(failure=failure):
                http_session = Mock()
                http_session.get.side_effect = [failure]
                with self.assertRaises(main.HttpError) as error:
                    main.fetch_enabled_user_subscriptions("private-mock-token", http_session, 7)
                self.assertEqual(error.exception.status_code, 502)
                self.assertNotIn("private-mock-token", error.exception.message)

    def test_safe_pages_deduplicate_filter_and_sort(self):
        http_session = Mock()
        path = "https://management.azure.com/subscriptions"
        version = main.OPENAI_TIER_SUBSCRIPTIONS_API_VERSION
        item = {"subscriptionId": SUBSCRIPTIONS[0], "displayName": "Z", "state": "Enabled"}
        http_session.get.side_effect = [
            response({"value": [item], "nextLink": f"{path}?api-version={version}&$skiptoken=next"}),
            response({"value": [item, {"subscriptionId": SUBSCRIPTIONS[1], "displayName": "a", "state": "Enabled"}, {"subscriptionId": SUBSCRIPTIONS[2], "state": "Disabled"}]}),
        ]
        result = main.fetch_enabled_user_subscriptions("token", http_session, 7)
        self.assertEqual([item["subscriptionId"] for item in result], [SUBSCRIPTIONS[1], SUBSCRIPTIONS[0]])
        self.assertTrue(all(not call.kwargs["allow_redirects"] for call in http_session.get.call_args_list))

    def test_unsafe_pages_and_cycles_are_rejected_before_request(self):
        start = f"https://management.azure.com/subscriptions?api-version={main.OPENAI_TIER_SUBSCRIPTIONS_API_VERSION}"
        for link in ("https://evil.example/steal", start, start + "#fragment", start.replace("/subscriptions?", "/subscriptions/other?"), start.replace("api-version=", "wrong="), 42, "https://[invalid"):
            with self.subTest(link=link):
                http_session = Mock()
                http_session.get.return_value = response({"value": [], "nextLink": link})
                with self.assertRaises(main.HttpError) as error:
                    main.fetch_enabled_user_subscriptions("token", http_session, 7)
                self.assertEqual(error.exception.status_code, 502)
                http_session.get.assert_called_once()

    def test_page_limit_and_error_statuses_do_not_leak_secrets(self):
        http_session = Mock()
        version = main.OPENAI_TIER_SUBSCRIPTIONS_API_VERSION
        http_session.get.side_effect = lambda url, **kwargs: response({"value": [], "nextLink": f"https://management.azure.com/subscriptions?api-version={version}&page={http_session.get.call_count}"})
        with self.assertRaises(main.HttpError):
            main.fetch_enabled_user_subscriptions("token", http_session, 7)
        self.assertEqual(http_session.get.call_count, 100)
        for upstream, expected in ((401, 401), (403, 403), (429, 429), (500, 502), (302, 502)):
            http_session.get.side_effect = None
            http_session.get.return_value = response({"error": "private-mock-token"}, upstream)
            with self.assertRaises(main.HttpError) as error:
                main.fetch_enabled_user_subscriptions("private-mock-token", http_session, 7)
            self.assertEqual(error.exception.status_code, expected)
            self.assertNotIn("private-mock-token", error.exception.message)


if __name__ == "__main__":
    unittest.main()
