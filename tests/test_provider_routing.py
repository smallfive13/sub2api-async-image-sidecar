import base64
import json
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from async_image_sidecar import AsyncImageServer, TaskStore, WorkerPool, load_config
from test_async_image_sidecar import make_config


CALLER_KEY = "test-client-key"
OFFICIAL_KEY = "test-official-key"
PNG = b"\x89PNG\r\n\x1a\nprovider-test-image"
GIF = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==")
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64"><circle cx="32" cy="32" r="20"/></svg>'


class UpstreamHandler(BaseHTTPRequestHandler):
    def reply(self, status, payload, request_id=None):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if request_id:
            self.send_header("x-request-id", request_id)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.server.auth_requests.append(self.headers.get("Authorization"))
        status = self.server.auth_status
        self.reply(status, {"data": [{"id": "gpt-image-2.6-flare"}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append((self.path, self.headers.get("Authorization"), body))
        if self.path == "/callback":
            self.reply(200, {})
            return
        if self.server.error_status:
            self.reply(self.server.error_status, {"error": {"message": "rejected " + OFFICIAL_KEY}}, "req_failure")
            return
        output_format = self.server.forced_format or body.get("output_format", "png")
        image = {"png": PNG, "svg": SVG, "gif": GIF}[output_format]
        encoded = base64.b64encode(image).decode()
        result = {"data": [{"b64_json": encoded}], "usage": {"total_tokens": 123}, "output_format": output_format}
        if self.path.endswith("/responses"):
            result = {"id": "resp_test", "output": [{"type": "image_generation_call", "id": "ig_test", "result": encoded}]}
        self.reply(200, result, "req_success")

    def log_message(self, *args):
        pass


class ProviderRoutingTest(unittest.TestCase):
    def start_server(self, server):
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return "http://127.0.0.1:" + str(server.server_address[1])

    def upstream(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        server.requests = []
        server.auth_requests = []
        server.auth_status = 200
        server.error_status = 0
        server.forced_format = None
        return server, self.start_server(server)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.sub2api, sub2api_url = self.upstream()
        self.openai, openai_url = self.upstream()
        self.addCleanup(patch.stopall)
        patch("async_image_sidecar.OPENAI_API_BASE_URL", openai_url + "/v1").start()
        self.config = replace(
            make_config(self.temp.name, sub2api_base_url=sub2api_url),
            max_queue_size=20,
            openai_api_key=OFFICIAL_KEY,
            openai_allowed_api_keys=(CALLER_KEY,),
            allow_http_callback=True,
        )
        self.store = TaskStore(self.config.db_path, self.config.token_secret)
        self.pool = WorkerPool(self.config, self.store)
        self.server = AsyncImageServer(("127.0.0.1", 0), self.config, self.store, self.pool)
        self.url = self.start_server(self.server)

    def request(self, path, payload=None, *, key=CALLER_KEY, body=None, content_type="application/json"):
        data = body if body is not None else json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(self.url + path, data=data, headers={
            "Authorization": "Bearer " + key, "Content-Type": content_type,
        })
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    def submit(self, payload, path="/v1/images/generations", **kwargs):
        status, task = self.request(path, payload, **kwargs)
        self.assertEqual(status, 202, task)
        return task

    def process(self, task):
        self.pool._process(task["id"])
        status, result = self.request("/v1/task/" + task["id"])
        self.assertEqual(status, 200)
        return result

    def assert_no_task(self):
        with self.store.connect() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM image_tasks").fetchone()[0], 0)

    def test_default_png_and_explicit_legacy_model_use_sub2api(self):
        task = self.submit({"model": "gpt-image-2.5-flare", "prompt": "draw"})
        result = self.process(task)
        self.assertEqual(result["provider"], "sub2api")
        self.assertEqual(result["route_reason"], "default_sub2api")
        self.assertEqual(result["upstream_request_id"], "req_success")
        self.assertEqual(self.sub2api.requests[0][1], "Bearer " + CALLER_KEY)
        self.assertEqual(self.sub2api.requests[0][2]["model"], "gpt-image-2.5-flare")
        self.assertFalse(self.openai.requests)

    def test_auto_svg_uses_official_credentials_and_saves_route_usage_and_xml(self):
        task = self.submit({"prompt": "icon", "output_format": "svg"})
        self.assertEqual(task["provider"], "openai")
        self.assertEqual(task["route_reason"], "native_svg")
        # A reopened store and worker retain the selected provider; routing is not recomputed.
        self.pool = WorkerPool(self.config, TaskStore(self.config.db_path, self.config.token_secret))
        result = self.process(task)
        self.assertEqual(result["status"], "completed")
        path, authorization, body = self.openai.requests[0]
        self.assertEqual(path, "/v1/images/generations")
        self.assertEqual(authorization, "Bearer " + OFFICIAL_KEY)
        self.assertEqual(body["svg"], {"model": "gpt-6.1-sol", "reasoning": {"effort": "low"}})
        self.assertNotIn("provider", body)
        self.assertNotIn("response_format", body)
        self.assertEqual(self.sub2api.auth_requests, ["Bearer " + CALLER_KEY] * 2)
        self.assertFalse(self.sub2api.requests)
        self.assertEqual(result["response"]["usage"]["total_tokens"], 123)
        self.assertEqual(result["upstream_request_id"], "req_success")
        with urllib.request.urlopen(self.url + result["outputs"][0]["url"]) as response:
            self.assertEqual(response.headers.get_content_type(), "image/svg+xml")
            self.assertEqual(response.read(), SVG)
        with self.store.connect() as connection:
            row = connection.execute("SELECT * FROM image_tasks WHERE id=?", (task["id"],)).fetchone()
        self.assertEqual(row["request_json"], "{}")
        self.assertIsNone(row["api_key_ciphertext"])
        self.assertNotIn(OFFICIAL_KEY, json.dumps(dict(row)))
        self.assertNotIn(OFFICIAL_KEY, repr(self.config))

    def test_auto_gif_and_explicit_openai_png(self):
        for output_format, expected_reason in (("gif", "native_gif"), ("png", "explicit_provider")):
            payload = {"prompt": "ball", "output_format": output_format}
            if output_format == "png":
                payload["provider"] = "openai"
                payload["response_format"] = "b64_json"
            result = self.process(self.submit(payload))
            self.assertEqual(result["provider"], "openai")
            self.assertEqual(result["route_reason"], expected_reason)
            self.assertEqual(result["status"], "completed")
            self.assertTrue(result["outputs"][0]["file"].endswith("." + output_format))
        self.assertEqual(len(self.openai.requests), 2)
        self.assertFalse(self.sub2api.requests)

    def test_explicit_sub2api_overrides_auto_without_silent_svg_downgrade(self):
        self.sub2api.forced_format = "png"
        result = self.process(self.submit({"prompt": "icon", "output_format": "svg", "provider": "sub2api"}))
        self.assertEqual(result["provider"], "sub2api")
        self.assertEqual(result["route_reason"], "explicit_provider")
        self.assertEqual(result["status"], "partial_completed")
        self.assertIn("SVG", result["outputs"][0]["save_error"])
        self.assertNotIn("url", result["outputs"][0])
        self.assertFalse(self.openai.requests)
        self.assertNotIn("provider", self.sub2api.requests[0][2])

    def test_unknown_and_invalid_providers_are_rejected_before_queueing(self):
        for provider in ("grok", "gemini", "", None, 3, {}):
            with self.subTest(provider=provider):
                status, error = self.request("/v1/images/generations", {"provider": provider, "prompt": "draw"})
                self.assertEqual(status, 400)
                self.assertIn("provider must be", error["error"]["message"])
        self.assert_no_task()
        self.assertFalse(self.openai.requests)

    def test_no_official_key_returns_503_without_falling_back(self):
        self.server.config = replace(self.config, openai_api_key="")
        status, error = self.request("/v1/images/generations", {"output_format": "gif", "prompt": "draw"})
        self.assertEqual(status, 503)
        self.assertEqual(error["error"]["code"], "provider_unavailable")
        self.assert_no_task()
        self.assertFalse(self.sub2api.requests)
        self.assertFalse(self.openai.requests)

    def test_unlisted_callers_are_rejected_without_contacting_official_api(self):
        status, error = self.request("/v1/images/generations", {"provider": "openai", "prompt": "draw"}, key="unlisted-key")
        self.assertEqual(status, 403)
        self.assertEqual(error["error"]["code"], "provider_forbidden")
        self.server.config = replace(self.config, openai_allowed_api_keys=())
        self.assertEqual(self.request("/v1/images/generations", {"provider": "openai", "prompt": "draw"})[0], 403)
        self.assert_no_task()
        self.assertFalse(self.sub2api.auth_requests)
        self.assertFalse(self.openai.requests)

    def test_revoked_sub2api_keys_and_auth_outages_do_not_spend_official_credit(self):
        for upstream_status, expected_status in ((401, 401), (403, 403), (500, 503)):
            self.sub2api.auth_status = upstream_status
            status, _ = self.request("/v1/images/generations", {"provider": "openai", "prompt": "draw"})
            self.assertEqual(status, expected_status)
        self.assert_no_task()
        self.assertFalse(self.openai.requests)

    def test_worker_rechecks_permissions_for_a_queued_official_task(self):
        task = self.submit({"provider": "openai", "prompt": "draw"})
        self.sub2api.auth_status = 401
        result = self.process(task)
        self.assertEqual(result["status"], "failed")
        self.assertIn("rejected the caller", result["error"])
        self.assertFalse(self.openai.requests)
        self.assertFalse(self.sub2api.requests)

    def test_official_failure_keeps_request_id_redacts_key_and_never_switches_provider(self):
        self.openai.error_status = 429
        result = self.process(self.submit({"provider": "openai", "prompt": "draw"}))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["upstream_request_id"], "req_failure")
        self.assertIn("openai HTTP 429", result["error"])
        self.assertNotIn(OFFICIAL_KEY, json.dumps(result))
        self.assertEqual(len(self.openai.requests), 1)
        self.assertFalse(self.sub2api.requests)

    def test_unified_submit_top_level_provider_and_custom_svg_author(self):
        task = self.submit({
            "endpoint": "generations", "model": "gpt-image-2.6-flare", "provider": "openai",
            "input": {"provider": "sub2api", "prompt": "icon", "output_format": "svg", "svg": {"model": "custom-author"}},
            "callback_url": self.config.sub2api_base_url + "/callback",
        }, path="/v1/task/submit")
        result = self.process(task)
        self.assertEqual(self.openai.requests[0][2]["svg"], {"model": "custom-author"})
        self.assertNotIn("provider", self.openai.requests[0][2])
        callback = self.sub2api.requests[0]
        self.assertEqual(callback[0], "/callback")
        self.assertEqual(callback[2]["provider"], "openai")
        self.assertEqual(callback[2]["upstream_request_id"], "req_success")
        self.assertNotIn(OFFICIAL_KEY, json.dumps(callback))
        self.assertEqual(result["callback_status"], "http_200")

    def test_multipart_edit_preserves_provider_and_sends_official_json_images(self):
        boundary = "provider-test"
        fields = {"prompt": "edit", "provider": "openai", "complexity": "low", "output_format": "png"}
        parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode() for key, value in fields.items()]
        for key in ("image", "markup"):
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"; filename="image.png"\r\nContent-Type: image/png\r\n\r\n'.encode() + PNG + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        task = self.submit(None, path="/v1/images/edits", body=b"".join(parts), content_type="multipart/form-data; boundary=" + boundary)
        result = self.process(task)
        self.assertEqual(result["model"], "gpt-image-2.6-sunburst")
        path, _, body = self.openai.requests[0]
        self.assertEqual(path, "/v1/images/edits")
        self.assertNotIn("provider", body)
        self.assertEqual(body["complexity"], "low")
        self.assertEqual(len(body["images"]), 1)
        self.assertTrue(body["markup"]["image_url"].startswith("data:image/png;base64,"))

    def test_responses_default_and_explicit_provider_preserve_request_context(self):
        payload = {"model": "gpt-6-astra", "input": "edit", "previous_response_id": "resp_prior", "tools": [{"type": "image_generation", "model": "gpt-image-2.5-sunburst"}]}
        for provider in ("auto", "openai"):
            result = self.process(self.submit({**payload, "provider": provider}, path="/v1/images/responses"))
            self.assertEqual(result["provider"], "sub2api" if provider == "auto" else "openai")
            server = self.sub2api if provider == "auto" else self.openai
            self.assertEqual(server.requests[-1][0], "/v1/responses")
            self.assertEqual(server.requests[-1][2], payload)
            self.assertEqual(result["response"]["id"], "resp_test")

    def test_native_incompatible_endpoint_and_streaming_fail_before_any_spend(self):
        for payload, path in (
            ({"provider": "openai", "stream": True, "prompt": "draw"}, "/v1/images/generations"),
            ({"provider": "openai", "response_format": "url", "prompt": "draw"}, "/v1/images/generations"),
            ({"endpoint": "edits", "model": "gpt-image-2.6-flare", "input": {"output_format": "svg"}}, "/v1/task/submit"),
        ):
            self.assertEqual(self.request(path, payload)[0], 400)
        self.assert_no_task()
        self.assertFalse(self.openai.requests)


class ProviderMigrationTest(unittest.TestCase):
    def test_server_environment_configures_credentials_without_command_line_or_files(self):
        with patch.dict(os.environ, {
            "ASYNC_IMAGE_TOKEN_SECRET": "test-routing-token-secret",
            "OPENAI_API_KEY": OFFICIAL_KEY,
            "ASYNC_IMAGE_OPENAI_ALLOWED_API_KEYS": CALLER_KEY + ", another-test-client ",
            "ASYNC_IMAGE_DEFAULT_SVG_MODEL": "custom-author",
        }, clear=True), patch("sys.argv", ["async_image_sidecar.py"]):
            config = load_config()
        self.assertEqual(config.openai_api_key, OFFICIAL_KEY)
        self.assertEqual(config.openai_allowed_api_keys, (CALLER_KEY, "another-test-client"))
        self.assertEqual(config.default_svg_model, "custom-author")
        self.assertNotIn(OFFICIAL_KEY, repr(config))
        self.assertNotIn(CALLER_KEY, repr(config))

    def test_existing_tasks_keep_sub2api_route_and_owner_after_upgrade(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "legacy.sqlite3")
            store = TaskStore(path, "test-secret")
            store.create_task("legacy", "gpt-image-2", "/v1/images/generations", {"prompt": "draw"}, CALLER_KEY, None)
            # Recreate the pre-provider schema, preserving the old task contents.
            with sqlite3.connect(path) as connection:
                for column in ("provider", "route_reason", "upstream_request_id"):
                    connection.execute("ALTER TABLE image_tasks DROP COLUMN " + column)
            upgraded = TaskStore(path, "test-secret")
            task = upgraded.get_task_for_api_key("legacy", CALLER_KEY)
            self.assertEqual(task["provider"], "sub2api")
            self.assertEqual(task["route_reason"], "legacy_task")
            self.assertIsNone(upgraded.get_task_for_api_key("legacy", "wrong-key"))
            payload, key = WorkerPool(make_config(folder), upgraded)._load_internal_request("legacy")
            self.assertEqual(payload, {"prompt": "draw"})
            self.assertEqual(key, CALLER_KEY)
