import base64
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from async_image_sidecar import (
    AsyncImageServer,
    Config,
    QueueFullError,
    TaskStore,
    WorkerPool,
    delete_expired_files,
    extract_outputs,
)


def make_config(
    root: str,
    *,
    max_queue_size: int = 2,
    task_retention_seconds: int = 86400,
    sub2api_base_url: str = "http://sub2api:8080",
) -> Config:
    return Config(
        bind_host="127.0.0.1",
        port=0,
        db_path=os.path.join(root, "tasks.sqlite3"),
        sub2api_base_url=sub2api_base_url,
        token_secret="test-token-secret",
        callback_secret="",
        image_dir=os.path.join(root, "images"),
        image_url_prefix="/v1/task/images",
        upload_dir=os.path.join(root, "uploads"),
        upload_url_prefix="/v1/task/uploads",
        public_base_url="",
        default_generation_model="gpt-image-2.6-flare",
        default_edit_model="gpt-image-2.6-sunburst",
        worker_count=1,
        max_queue_size=max_queue_size,
        request_timeout_seconds=5,
        callback_timeout_seconds=5,
        max_body_bytes=1024 * 1024,
        max_upload_bytes=50 * 1024 * 1024,
        task_retention_seconds=task_retention_seconds,
        cleanup_interval_seconds=600,
        allow_http_callback=False,
    )


class TaskStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config = make_config(self.temp_dir.name)
        self.store = TaskStore(self.config.db_path, self.config.token_secret)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def create_task(self, task_id: str, request_payload: dict | None = None) -> None:
        self.store.create_task(
            task_id=task_id,
            model="gpt-image-2",
            endpoint="/v1/images/generations",
            request_payload=request_payload or {"model": "gpt-image-2", "prompt": "test"},
            api_key="sub2api-test-key",
            callback_url=None,
        )

    def raw_row(self, task_id: str) -> sqlite3.Row:
        with self.store.connect() as conn:
            row = conn.execute("SELECT * FROM image_tasks WHERE id = ?", (task_id,)).fetchone()
        self.assertIsNotNone(row)
        return row

    def test_complete_task_removes_base64_request_and_api_key_from_storage(self) -> None:
        raw_image = b"large-image-payload"
        encoded = base64.b64encode(raw_image).decode("ascii")
        self.create_task(
            "task_complete",
            {"model": "gpt-image-2", "prompt": "test", "image": "data:image/png;base64," + encoded},
        )

        task = self.store.complete_task(
            "task_complete",
            {"data": [{"b64_json": encoded, "revised_prompt": "revised"}], "usage": {"total_tokens": 1}},
            [
                {
                    "url": "/v1/task/images/task_complete_0.png",
                    "file": "task_complete_0.png",
                    "b64_json": encoded,
                }
            ],
        )

        row = self.raw_row("task_complete")
        self.assertEqual(row["request_json"], "{}")
        self.assertIsNone(row["api_key_ciphertext"])
        self.assertNotIn("b64_json", row["response_json"])
        self.assertNotIn(encoded, row["response_json"])
        self.assertNotIn("b64_json", row["outputs_json"])
        self.assertNotIn(encoded, row["outputs_json"])
        self.assertEqual(task["response"]["data"][0]["revised_prompt"], "revised")
        self.assertEqual(task["response"]["usage"]["total_tokens"], 1)

    def test_complete_responses_task_removes_image_result_from_storage(self) -> None:
        encoded = base64.b64encode(b"responses-image").decode("ascii")
        self.create_task("task_responses")

        task = self.store.complete_task(
            "task_responses",
            {
                "id": "resp_123",
                "output": [
                    {
                        "id": "ig_123",
                        "type": "image_generation_call",
                        "result": encoded,
                        "output_format": "png",
                    }
                ],
            },
            [{"url": "/v1/task/images/task_responses_0.png"}],
        )

        row = self.raw_row("task_responses")
        self.assertNotIn(encoded, row["response_json"])
        self.assertNotIn("result", row["response_json"])
        self.assertEqual(task["response"]["output"][0]["id"], "ig_123")

    def test_fail_task_removes_request_and_api_key_from_storage(self) -> None:
        self.create_task("task_failed", {"model": "gpt-image-2", "prompt": "sensitive prompt"})
        self.store.fail_task("task_failed", "upstream failed")

        row = self.raw_row("task_failed")
        self.assertEqual(row["request_json"], "{}")
        self.assertIsNone(row["api_key_ciphertext"])

    def test_complete_task_with_only_save_error_is_partial(self) -> None:
        self.create_task("task_save_error")

        task = self.store.complete_task(
            "task_save_error",
            {"data": []},
            [{"save_error": "disk full"}],
        )

        self.assertEqual(task["status"], "partial_completed")

    def test_compact_terminal_tasks_sanitizes_existing_rows(self) -> None:
        self.create_task("task_legacy")
        encoded = base64.b64encode(b"legacy-image").decode("ascii")
        with self.store.connect() as conn:
            conn.execute(
                """
                UPDATE image_tasks
                SET status = 'completed', response_json = ?, outputs_json = ?, completed_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    json.dumps({"data": [{"b64_json": encoded}]}),
                    json.dumps([{"b64_json": encoded, "save_error": "legacy failure"}]),
                    int(time.time()),
                    int(time.time()),
                    "task_legacy",
                ),
            )

        compacted = self.store.compact_terminal_tasks()

        row = self.raw_row("task_legacy")
        self.assertEqual(compacted, 1)
        self.assertEqual(row["request_json"], "{}")
        self.assertIsNone(row["api_key_ciphertext"])
        self.assertNotIn("b64_json", row["response_json"])
        self.assertNotIn("b64_json", row["outputs_json"])

    def test_compaction_reclaims_sqlite_file_space(self) -> None:
        self.create_task("task_large_legacy")
        encoded = base64.b64encode(b"x" * (2 * 1024 * 1024)).decode("ascii")
        with self.store.connect() as conn:
            conn.execute(
                """
                UPDATE image_tasks
                SET status = 'completed', response_json = ?, completed_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    json.dumps({"data": [{"b64_json": encoded}]}),
                    int(time.time()),
                    int(time.time()),
                    "task_large_legacy",
                ),
            )
        self.store.reclaim_space()
        size_before = os.path.getsize(self.config.db_path)

        self.store.compact_terminal_tasks()

        size_after = os.path.getsize(self.config.db_path)
        self.assertGreater(size_before, 2 * 1024 * 1024)
        self.assertLess(size_after, size_before // 4)

    def test_expiry_only_deletes_old_terminal_tasks(self) -> None:
        for task_id in ("task_old", "task_recent", "task_queued"):
            self.create_task(task_id)
        self.store.complete_task("task_old", {"data": []}, [])
        self.store.complete_task("task_recent", {"data": []}, [])
        with self.store.connect() as conn:
            conn.execute("UPDATE image_tasks SET completed_at = 100, updated_at = 100 WHERE id = 'task_old'")
            conn.execute("UPDATE image_tasks SET completed_at = 300, updated_at = 300 WHERE id = 'task_recent'")
            conn.execute("UPDATE image_tasks SET created_at = 50, updated_at = 50 WHERE id = 'task_queued'")

        deleted = self.store.delete_expired_terminal_tasks(cutoff=200)

        self.assertEqual(deleted, 1)
        self.assertIsNone(self.store.get_task("task_old"))
        self.assertIsNotNone(self.store.get_task("task_recent"))
        self.assertIsNotNone(self.store.get_task("task_queued"))


class FileCleanupTest(unittest.TestCase):
    def test_extract_outputs_never_falls_back_to_persisting_base64(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            config = make_config(root)
            Path(config.image_dir).write_bytes(b"not-a-directory")
            encoded = base64.b64encode(b"image-payload").decode("ascii")

            outputs = extract_outputs({"data": [{"b64_json": encoded}]}, "task_save_error", config)

            self.assertEqual(len(outputs), 1)
            self.assertIn("save_error", outputs[0])
            self.assertNotIn("b64_json", outputs[0])
            self.assertNotIn(encoded, json.dumps(outputs))

    def test_extract_outputs_saves_responses_image_generation_calls(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            config = make_config(root)
            image_bytes = b"\x89PNG\r\n\x1a\nresponses-image"
            encoded = base64.b64encode(image_bytes).decode("ascii")

            outputs = extract_outputs(
                {
                    "output": [
                        {
                            "id": "ig_123",
                            "type": "image_generation_call",
                            "result": encoded,
                            "output_format": "png",
                            "revised_prompt": "keep the subject",
                        }
                    ]
                },
                "task_responses",
                config,
            )

            self.assertEqual(outputs[0]["generation_id"], "ig_123")
            self.assertEqual(outputs[0]["revised_prompt"], "keep the subject")
            self.assertEqual(Path(config.image_dir, outputs[0]["file"]).read_bytes(), image_bytes)
            self.assertNotIn(encoded, json.dumps(outputs))

    def test_delete_expired_files_uses_mtime_and_ignores_directories(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            old_file = Path(root, "old.png")
            recent_file = Path(root, "recent.png")
            nested_dir = Path(root, "nested")
            old_file.write_bytes(b"old")
            recent_file.write_bytes(b"recent")
            nested_dir.mkdir()
            os.utime(old_file, (100, 100))
            os.utime(recent_file, (300, 300))
            os.utime(nested_dir, (100, 100))

            deleted = delete_expired_files(root, cutoff=200)

            self.assertEqual(deleted, 1)
            self.assertFalse(old_file.exists())
            self.assertTrue(recent_file.exists())
            self.assertTrue(nested_dir.exists())


class QueueLimitTest(unittest.TestCase):
    def test_enqueue_rejects_when_queue_is_full(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            config = make_config(root, max_queue_size=1)
            store = TaskStore(config.db_path, config.token_secret)
            pool = WorkerPool(config, store)

            self.assertTrue(pool.enqueue("task_one"))
            self.assertFalse(pool.enqueue("task_two"))

    def test_http_submit_returns_503_and_removes_rejected_task(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            config = make_config(root, max_queue_size=1)
            store = TaskStore(config.db_path, config.token_secret)
            pool = WorkerPool(config, store)
            self.assertTrue(pool.enqueue("occupied"))
            server = AsyncImageServer(("127.0.0.1", 0), config, store, pool)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                body = json.dumps({"model": "gpt-image-2", "prompt": "test"}).encode("utf-8")
                request = urllib.request.Request(
                    f"http://127.0.0.1:{server.server_address[1]}/v1/images/generations",
                    data=body,
                    method="POST",
                    headers={"Authorization": "Bearer sub2api-test-key", "Content-Type": "application/json"},
                )
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(request, timeout=5)
                self.assertEqual(raised.exception.code, 503)
                error = json.loads(raised.exception.read().decode("utf-8"))
                self.assertEqual(error["error"]["code"], "server_overloaded")
                with store.connect() as conn:
                    task_count = conn.execute("SELECT COUNT(*) FROM image_tasks").fetchone()[0]
                self.assertEqual(task_count, 0)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_startup_recovery_does_not_drop_tasks_over_queue_capacity(self) -> None:
        class RecordingWorkerPool(WorkerPool):
            def __init__(self, config: Config, store: TaskStore) -> None:
                super().__init__(config, store)
                self.processed: list[str] = []
                self.all_processed = threading.Event()

            def _process(self, task_id: str) -> None:
                self.processed.append(task_id)
                if len(self.processed) == 3:
                    self.all_processed.set()

        with tempfile.TemporaryDirectory() as root:
            config = make_config(root, max_queue_size=1)
            store = TaskStore(config.db_path, config.token_secret)
            for task_id in ("task_one", "task_two", "task_three"):
                store.create_task(
                    task_id,
                    "gpt-image-2",
                    "/v1/images/generations",
                    {"model": "gpt-image-2", "prompt": task_id},
                    "sub2api-test-key",
                    None,
                )
            pool = RecordingWorkerPool(config, store)
            pool.start()
            try:
                self.assertTrue(pool.all_processed.wait(timeout=5))
                self.assertCountEqual(pool.processed, ["task_one", "task_two", "task_three"])
            finally:
                pool.stop()
            self.assertTrue(all(not worker.is_alive() for worker in pool.threads))


class CleanupIntegrationTest(unittest.TestCase):
    def test_cleanup_expired_removes_terminal_rows_and_old_files(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            config = make_config(root, task_retention_seconds=100)
            store = TaskStore(config.db_path, config.token_secret)
            store.create_task(
                "task_expired",
                "gpt-image-2",
                "/v1/images/generations",
                {"model": "gpt-image-2", "prompt": "test"},
                "sub2api-test-key",
                None,
            )
            store.complete_task("task_expired", {"data": []}, [])
            with store.connect() as conn:
                conn.execute(
                    "UPDATE image_tasks SET completed_at = 1, updated_at = 1 WHERE id = 'task_expired'"
                )
            for directory in (config.image_dir, config.upload_dir):
                os.makedirs(directory, exist_ok=True)
                old_file = Path(directory, "expired.bin")
                old_file.write_bytes(b"expired")
                os.utime(old_file, (1, 1))

            stats = WorkerPool(config, store).cleanup_expired()

            self.assertEqual(stats, {"tasks": 1, "images": 1, "uploads": 1})
            self.assertIsNone(store.get_task("task_expired"))


class EndToEndTest(unittest.TestCase):
    def test_multipart_edit_accepts_multiple_image_array_fields(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            config = make_config(root)
            store = TaskStore(config.db_path, config.token_secret)
            pool = WorkerPool(config, store)
            sidecar = AsyncImageServer(("127.0.0.1", 0), config, store, pool)
            sidecar_thread = threading.Thread(target=sidecar.serve_forever, daemon=True)
            sidecar_thread.start()
            boundary = "sidecar-test-boundary"
            parts = [
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"provider\"\r\n\r\nsub2api\r\n".encode(),
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"prompt\"\r\n\r\npreserve subject\r\n".encode(),
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"quality\"\r\n\r\nxhigh\r\n".encode(),
            ]
            for key, value in {"complexity": "medium", "output_format": "gif", "frame_count": "4", "frame_duration_ms": "200"}.items():
                parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode())
            for index, content in enumerate((b"first-image", b"second-image")):
                parts.append(
                    (
                        f"--{boundary}\r\n"
                        f"Content-Disposition: form-data; name=\"image[]\"; filename=\"source-{index}.png\"\r\n"
                        "Content-Type: image/png\r\n\r\n"
                    ).encode()
                    + content
                    + b"\r\n"
                )
            parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"markup\"; filename=\"overlay.png\"\r\nContent-Type: image/png\r\n\r\n".encode()
                + b"transparent-overlay\r\n"
            )
            parts.append(f"--{boundary}--\r\n".encode())
            body = b"".join(parts)
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{sidecar.server_address[1]}/v1/images/edits",
                    data=body,
                    method="POST",
                    headers={
                        "Authorization": "Bearer sub2api-test-key",
                        "Content-Type": f"multipart/form-data; boundary={boundary}",
                    },
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    self.assertEqual(response.status, 202)
                    submitted = json.loads(response.read().decode("utf-8"))

                self.assertEqual(submitted["model"], "gpt-image-2.6-sunburst")
                with store.connect() as conn:
                    row = conn.execute(
                        "SELECT request_json FROM image_tasks WHERE id = ?",
                        (submitted["id"],),
                    ).fetchone()
                payload = json.loads(row["request_json"])
                self.assertNotIn("provider", payload)
                self.assertEqual(payload["model"], "gpt-image-2.6-sunburst")
                self.assertEqual(payload["quality"], "xhigh")
                self.assertEqual(payload["complexity"], "medium")
                self.assertEqual(payload["output_format"], "gif")
                self.assertEqual(payload["frame_count"], 4)
                self.assertEqual(payload["frame_duration_ms"], 200)
                markup = payload["markup"]["image_url"]
                self.assertEqual(base64.b64decode(markup.split(",", 1)[1]), b"transparent-overlay")
                self.assertEqual(len(payload["images"]), 2)
                self.assertTrue(all(image["image_url"].startswith("data:image/png;base64,") for image in payload["images"]))
            finally:
                sidecar.shutdown()
                sidecar.server_close()
                sidecar_thread.join(timeout=5)

    def test_submit_process_poll_and_fetch_image(self) -> None:
        self._submit_process_poll_and_fetch_image(b"\x89PNG\r\n\x1a\nimage-payload", "png", "image/png")

    def test_image26_svg_and_gif_outputs(self) -> None:
        svg = b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg" width="64" height="64"><circle cx="32" cy="32" r="20"/></svg>'
        gif = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==")
        for output_format, image_bytes, mime in (("svg", svg, "image/svg+xml"), ("gif", gif, "image/gif")):
            for include_response_format in (False, True):
                with self.subTest(format=output_format, response_metadata=include_response_format):
                    self._submit_process_poll_and_fetch_image(
                        image_bytes, output_format, mime, include_response_format=include_response_format
                    )

    def test_explicit_image25_model_is_preserved(self) -> None:
        self._submit_process_poll_and_fetch_image(
            b"\x89PNG\r\n\x1a\nlegacy-image", "png", "image/png", model="gpt-image-2.5-flare"
        )

    def _submit_process_poll_and_fetch_image(
        self, image_bytes: bytes, output_format: str, mime: str,
        *, include_response_format: bool = False, model: str | None = None,
    ) -> None:
        encoded = base64.b64encode(image_bytes).decode("ascii")

        class FakeUpstreamHandler(BaseHTTPRequestHandler):
            requests: list[dict] = []

            def do_POST(self) -> None:  # noqa: N802
                content_length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(content_length))
                self.__class__.requests.append(body)
                result = {"data": [{"b64_json": encoded, "revised_prompt": "revised"}], "usage": {"total_tokens": 3}}
                if include_response_format:
                    result["output_format"] = output_format
                response = json.dumps(result).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

            def log_message(self, _fmt: str, *_args: object) -> None:
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstreamHandler)
        upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        upstream_thread.start()
        self.addCleanup(upstream_thread.join, 5)
        self.addCleanup(upstream.server_close)
        self.addCleanup(upstream.shutdown)
        with tempfile.TemporaryDirectory() as root:
            config = make_config(
                root,
                sub2api_base_url=f"http://127.0.0.1:{upstream.server_address[1]}",
            )
            store = TaskStore(config.db_path, config.token_secret)
            pool = WorkerPool(config, store)
            sidecar = AsyncImageServer(("127.0.0.1", 0), config, store, pool)
            sidecar_thread = threading.Thread(target=sidecar.serve_forever, daemon=True)
            pool.start()
            sidecar_thread.start()
            base_url = f"http://127.0.0.1:{sidecar.server_address[1]}"
            headers = {
                "Authorization": "Bearer sub2api-test-key",
                "Content-Type": "application/json",
            }
            try:
                payload = {"provider": "sub2api", "prompt": "test", "quality": "max", "size": "1536x864", "complexity": "low", "output_format": output_format}
                if model:
                    payload["model"] = model
                if output_format == "gif":
                    payload.update(frame_count=4, frame_duration_ms=200)
                submit_request = urllib.request.Request(
                    base_url + "/v1/images/generations",
                    data=json.dumps(payload).encode("utf-8"),
                    method="POST",
                    headers=headers,
                )
                with urllib.request.urlopen(submit_request, timeout=5) as response:
                    self.assertEqual(response.status, 202)
                    submitted = json.loads(response.read().decode("utf-8"))

                task = submitted
                deadline = time.time() + 5
                while task["status"] not in {"completed", "partial_completed", "failed"}:
                    self.assertLess(time.time(), deadline, "task did not complete before timeout")
                    time.sleep(0.05)
                    poll_request = urllib.request.Request(
                        base_url + "/v1/task/" + submitted["id"],
                        headers={"Authorization": "Bearer sub2api-test-key"},
                    )
                    with urllib.request.urlopen(poll_request, timeout=5) as response:
                        task = json.loads(response.read().decode("utf-8"))

                self.assertEqual(task["status"], "completed")
                self.assertEqual(task["model"], model or "gpt-image-2.6-flare")
                self.assertEqual(task["response"]["usage"]["total_tokens"], 3)
                upstream_payload = {key: value for key, value in payload.items() if key != "provider"}
                self.assertEqual(FakeUpstreamHandler.requests[0], {**upstream_payload, "model": model or "gpt-image-2.6-flare"})
                self.assertEqual(FakeUpstreamHandler.requests[0]["quality"], "max")
                self.assertEqual(FakeUpstreamHandler.requests[0]["size"], "1536x864")
                self.assertNotIn("b64_json", json.dumps(task))
                self.assertTrue(task["outputs"][0]["file"].endswith("." + output_format))
                with urllib.request.urlopen(base_url + task["outputs"][0]["url"], timeout=5) as response:
                    self.assertEqual(response.headers.get_content_type(), mime)
                    self.assertEqual(response.read(), image_bytes)

                with store.connect() as conn:
                    row = conn.execute(
                        "SELECT request_json, api_key_ciphertext, response_json, outputs_json FROM image_tasks"
                    ).fetchone()
                self.assertEqual(row["request_json"], "{}")
                self.assertIsNone(row["api_key_ciphertext"])
                self.assertNotIn(encoded, row["response_json"])
                self.assertNotIn(encoded, row["outputs_json"])
            finally:
                sidecar.shutdown()
                sidecar.server_close()
                sidecar_thread.join(timeout=5)
                pool.stop()

    def test_responses_thinking_edit_processes_image_output(self) -> None:
        image_bytes = b"\x89PNG\r\n\x1a\nthinking-edit"
        encoded = base64.b64encode(image_bytes).decode("ascii")

        class FakeUpstreamHandler(BaseHTTPRequestHandler):
            path_seen = ""
            body_seen: dict = {}

            def do_POST(self) -> None:  # noqa: N802
                content_length = int(self.headers.get("Content-Length", "0"))
                self.__class__.path_seen = self.path
                self.__class__.body_seen = json.loads(self.rfile.read(content_length))
                response = json.dumps(
                    {
                        "id": "resp_123",
                        "output": [
                            {
                                "id": "ig_123",
                                "type": "image_generation_call",
                                "result": encoded,
                                "output_format": "png",
                            }
                        ],
                        "usage": {"input_tokens": 11, "output_tokens": 22},
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

            def log_message(self, _fmt: str, *_args: object) -> None:
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstreamHandler)
        upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        upstream_thread.start()
        self.addCleanup(upstream_thread.join, 5)
        self.addCleanup(upstream.server_close)
        self.addCleanup(upstream.shutdown)
        with tempfile.TemporaryDirectory() as root:
            config = make_config(
                root,
                sub2api_base_url=f"http://127.0.0.1:{upstream.server_address[1]}",
            )
            store = TaskStore(config.db_path, config.token_secret)
            pool = WorkerPool(config, store)
            sidecar = AsyncImageServer(("127.0.0.1", 0), config, store, pool)
            sidecar_thread = threading.Thread(target=sidecar.serve_forever, daemon=True)
            pool.start()
            sidecar_thread.start()
            base_url = f"http://127.0.0.1:{sidecar.server_address[1]}"
            payload = {
                "model": "gpt-6-astra",
                "reasoning": {"effort": "high"},
                "input": "Preserve the subject and change the background",
                "tools": [
                    {
                        "type": "image_generation",
                        "model": "gpt-image-2.5-sunburst",
                        "action": "edit",
                        "quality": "xhigh",
                    }
                ],
            }
            try:
                submit_request = urllib.request.Request(
                    base_url + "/v1/images/responses",
                    data=json.dumps(payload).encode("utf-8"),
                    method="POST",
                    headers={"Authorization": "Bearer sub2api-test-key", "Content-Type": "application/json"},
                )
                with urllib.request.urlopen(submit_request, timeout=5) as response:
                    self.assertEqual(response.status, 202)
                    submitted = json.loads(response.read().decode("utf-8"))

                task = submitted
                deadline = time.time() + 5
                while task["status"] not in {"completed", "partial_completed", "failed"}:
                    self.assertLess(time.time(), deadline, "task did not complete before timeout")
                    time.sleep(0.05)
                    poll_request = urllib.request.Request(
                        base_url + "/v1/task/" + submitted["id"],
                        headers={"Authorization": "Bearer sub2api-test-key"},
                    )
                    with urllib.request.urlopen(poll_request, timeout=5) as response:
                        task = json.loads(response.read().decode("utf-8"))

                self.assertEqual(task["status"], "completed")
                self.assertEqual(task["model"], "gpt-6-astra")
                self.assertEqual(FakeUpstreamHandler.path_seen, "/v1/responses")
                self.assertEqual(FakeUpstreamHandler.body_seen, payload)
                self.assertNotIn(encoded, json.dumps(task))
                with urllib.request.urlopen(base_url + task["outputs"][0]["url"], timeout=5) as response:
                    self.assertEqual(response.read(), image_bytes)
            finally:
                sidecar.shutdown()
                sidecar.server_close()
                sidecar_thread.join(timeout=5)
                pool.stop()


class GracefulShutdownTest(unittest.TestCase):
    def test_sigterm_exits_without_docker_force_kill(self) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        with tempfile.TemporaryDirectory() as root:
            env = os.environ.copy()
            env["ASYNC_IMAGE_TOKEN_SECRET"] = "test-token-secret"
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).resolve().parents[1] / "async_image_sidecar.py"),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--db",
                    os.path.join(root, "tasks.sqlite3"),
                    "--image-dir",
                    os.path.join(root, "images"),
                    "--upload-dir",
                    os.path.join(root, "uploads"),
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            try:
                deadline = time.time() + 5
                while True:
                    try:
                        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as response:
                            self.assertEqual(response.status, 200)
                        break
                    except (urllib.error.URLError, ConnectionError):
                        if time.time() >= deadline:
                            self.fail("sidecar did not become healthy before timeout")
                        time.sleep(0.05)
                process.terminate()
                output, _ = process.communicate(timeout=3)
                self.assertEqual(process.returncode, 0, output)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=3)


if __name__ == "__main__":
    unittest.main()
