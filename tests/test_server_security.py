import importlib
import os
import unittest
from unittest.mock import patch

os.environ["API_TOKEN"] = "test-token"
os.environ.setdefault("CACHE_DIR", "/tmp/aria-test-cache")

server = importlib.import_module("server")


class ServerSecurityTest(unittest.TestCase):
    def setUp(self):
        self.client = server.app.test_client()

    @staticmethod
    def auth_headers():
        return {"Authorization": "Bearer test-token"}

    def test_protected_endpoint_requires_auth(self):
        resp = self.client.get("/search?q=Beatles")
        self.assertEqual(resp.status_code, 401)

    def test_protected_endpoint_requires_server_token_config(self):
        original = server.API_TOKEN
        server.API_TOKEN = None
        try:
            resp = self.client.get("/search?q=Beatles")
        finally:
            server.API_TOKEN = original
        self.assertEqual(resp.status_code, 503)

    def test_bearer_auth_reaches_validation_before_network(self):
        with patch.object(server, "_apple_get", side_effect=AssertionError("network")):
            resp = self.client.get(
                "/search?q=Beatles&limit=not-an-int",
                headers=self.auth_headers(),
            )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("limit", resp.get_json()["error"])

    def test_batch_rejects_path_traversal_ids(self):
        resp = self.client.post(
            "/batch",
            json={"ids": ["../secret"], "fmt": "aac"},
            headers=self.auth_headers(),
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("ids[]", resp.get_json()["error"])

    def test_download_rejects_unknown_format(self):
        resp = self.client.get(
            "/download/1440841263?fmt=../../x",
            headers=self.auth_headers(),
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("fmt", resp.get_json()["error"])

    def test_cache_path_validates_components(self):
        with self.assertRaises(ValueError):
            server._get_cached_path("../secret", "aac")
        with self.assertRaises(ValueError):
            server._get_cached_path("1440841263", "../aac")


if __name__ == "__main__":
    unittest.main()
