import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def deny_network(*args, **kwargs):
    raise AssertionError("Network access forbidden in route registration smoke test")


def run_smoke():
    sys.path.insert(0, str(ROOT))
    original_exists = Path.exists

    def safe_exists(path):
        return False if path.name == ".env" else original_exists(path)

    with patch.object(Path, "exists", safe_exists), patch.object(socket.socket, "connect", deny_network), patch.object(socket.socket, "connect_ex", deny_network), patch.object(socket, "create_connection", deny_network):
        import main

        main.app.config.update(TESTING=True)
        client = main.app.test_client()
        check = unittest.TestCase()
        routes = {
            "/api/azure-diagnostics/workspaces": "GET",
            "/api/azure-diagnostics/resources": "GET",
            "/api/azure-diagnostics/query": "POST",
        }
        for path, method in routes.items():
            registered = [rule for rule in main.app.url_map.iter_rules() if rule.rule == path]
            check.assertEqual(len(registered), 1, path)
            check.assertIn(method, registered[0].methods, path)

        statuses = {}
        for path, expected in (("/", 200), ("/favicon.ico", 204), ("/api/openai-tier/auth/status", 200)):
            result = client.get(path)
            check.assertEqual(result.status_code, expected, path)
            statuses[path] = result.status_code
            if path == "/api/openai-tier/auth/status":
                check.assertTrue(result.is_json)
                check.assertTrue(result.get_json()["configured"])
                check.assertIs(result.get_json()["signedIn"], False)

        for path, method in routes.items():
            result = client.open(path, method=method)
            check.assertEqual(result.status_code, 401, path)
            check.assertTrue(result.is_json, path)
            check.assertEqual(result.get_json(), {"message": "登录状态已失效，请先通过 Microsoft 账号登录后再查询。"}, path)
            statuses[path] = result.status_code
        print(json.dumps(statuses))


class MainRouteRegistrationTests(unittest.TestCase):
    def test_normal_import_registers_routes_and_requires_login_offline(self):
        env = {
            **os.environ,
            "FLASK_SECRET_KEY": "offline-smoke-session-key",
            "OPENAI_TIER_CLIENT_ID": "offline-smoke-client",
            "OPENAI_TIER_CLIENT_SECRET": "offline-smoke-not-a-real-secret",
            "OPENAI_TIER_AUTHORITY": "https://login.microsoftonline.com/organizations",
        }
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--smoke"],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(json.loads(result.stdout)), 6)


if __name__ == "__main__":
    if "--smoke" in sys.argv:
        run_smoke()
    else:
        unittest.main()
