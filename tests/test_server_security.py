import importlib
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
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

    def test_apple_headers_use_configured_bearer_token(self):
        with patch.object(
            server,
            "_load_aria_config",
            return_value={
                "accessToken": "Bearer configured-token",
                "mediaUserToken": "configured-mut",
            },
        ), patch.object(server.apple_api, "get_web_token", side_effect=AssertionError):
            headers = server._get_apple_headers()
        self.assertEqual(headers["Authorization"], "Bearer configured-token")
        self.assertEqual(headers["Media-User-Token"], "configured-mut")

    def test_alac_download_passes_configured_apple_credentials(self):
        captured = {}
        old_cache_dir = server.CACHE_DIR
        with tempfile.TemporaryDirectory() as tmpdir:
            server.CACHE_DIR = tmpdir
            source_path = Path(tmpdir) / "source.m4a"

            def fake_decrypt_one_track(**kwargs):
                captured.update(kwargs)
                source_path.write_bytes(b"fake m4a")
                return SimpleNamespace(out_path=str(source_path), elapsed_seconds=0.1)

            try:
                with patch.object(
                    server,
                    "_load_aria_config",
                    return_value={
                        "accessToken": "Bearer configured-token",
                        "mediaUserToken": "configured-mut",
                    },
                ), patch.object(
                    server.decryptor,
                    "decrypt_one_track",
                    side_effect=fake_decrypt_one_track,
                ), patch.object(
                    server.apple_api,
                    "AppleMusicClient",
                    side_effect=RuntimeError("skip filename lookup"),
                ):
                    resp = self.client.get(
                        "/download/1440841263?fmt=alac",
                        headers=self.auth_headers(),
                    )
            finally:
                server.CACHE_DIR = old_cache_dir

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(captured["authorization_token"], "configured-token")
        self.assertEqual(captured["media_user_token"], "configured-mut")

    def test_web_token_regex_accepts_typ_first_jwt(self):
        token = (
            b"eyJ0eXAiOiJKV1QiLCJhbGciOiJFUzI1NiIsImtpZCI6IldlYlBsYXlLaWQifQ."
            b"eyJpc3MiOiJBTVBXZWJQbGF5IiwiaWF0IjoxNzg1Nzg3MjkyLCJleHAiOjE3OTE4MzUyOTJ9."
            b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
        )
        self.assertEqual(server.apple_api._TOKEN_RE.search(token).group(0), token)


if __name__ == "__main__":
    unittest.main()
