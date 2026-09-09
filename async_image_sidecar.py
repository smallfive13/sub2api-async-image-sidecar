#!/usr/bin/env python3
"""
Minimal async image task sidecar for Sub2API.

It exposes:
  POST /v1/images/generations
  POST /v1/images/edits
  POST /v1/images/responses
  POST /v1/task/submit
  GET  /v1/task/{task_id}
  GET  /health

The sidecar stores task state in SQLite and calls the existing Sub2API
OpenAI-compatible image endpoint in a background worker thread.
"""

from __future__ import annotations

import argparse
import cgi
import hmac
import hashlib
import base64
import json
import mimetypes
import os
import queue
import secrets
import signal
import sqlite3
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse


TERMINAL_STATUSES = {"completed", "partial_completed", "failed", "cancelled"}
DEFAULT_GENERATION_MODEL = "gpt-image-2.5-flare"
DEFAULT_EDIT_MODEL = "gpt-image-2.5-sunburst"


class QueueFullError(RuntimeError):
    pass


def now_ts() -> int:
    return int(time.time())


def json_dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json_dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def error_response(handler: BaseHTTPRequestHandler, status: int, code: str, message: str) -> None:
    json_response(handler, status, {"error": {"code": code, "message": message}})


def read_json_body(handler: BaseHTTPRequestHandler, max_bytes: int) -> dict[str, Any]:
    length_header = handler.headers.get("Content-Length")
    if not length_header:
        raise ValueError("request body is required")
    try:
        length = int(length_header)
    except ValueError as exc:
        raise ValueError("invalid Content-Length") from exc
    if length <= 0:
        raise ValueError("request body is required")
    if length > max_bytes:
        raise ValueError(f"request body too large; max {max_bytes} bytes")
    raw = handler.rfile.read(length)
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON body: {exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("request body must be a JSON object")
    return parsed


def bearer_token(headers: Any) -> str:
    auth = headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return ""
    return auth.removeprefix("Bearer ").strip()


def redact_secret(value: str) -> str:
    if len(value) <= 8:
        return "***"
    return value[:4] + "..." + value[-4:]


def is_public_callback_url(url: str, allow_http: bool) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme == "https":
        return bool(parsed.netloc)
    if allow_http and parsed.scheme == "http":
        return bool(parsed.netloc)
    return False


def fingerprint_token(token: str, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), token.encode("utf-8"), hashlib.sha256).hexdigest()


def token_keystream(secret: str, nonce: bytes, length: int) -> bytes:
    chunks: list[bytes] = []
    counter = 0
    while sum(len(chunk) for chunk in chunks) < length:
        counter_bytes = counter.to_bytes(4, "big")
        chunks.append(hmac.new(secret.encode("utf-8"), nonce + counter_bytes, hashlib.sha256).digest())
        counter += 1
    return b"".join(chunks)[:length]


def protect_token(token: str, secret: str) -> str:
    nonce = secrets.token_bytes(16)
    raw = token.encode("utf-8")
    stream = token_keystream(secret, nonce, len(raw))
    encrypted = bytes(a ^ b for a, b in zip(raw, stream))
    return base64.urlsafe_b64encode(nonce + encrypted).decode("ascii")


def unprotect_token(encoded: str, secret: str) -> str:
    data = base64.urlsafe_b64decode(encoded.encode("ascii"))
    if len(data) < 17:
        raise ValueError("invalid stored API key")
    nonce, encrypted = data[:16], data[16:]
    stream = token_keystream(secret, nonce, len(encrypted))
    raw = bytes(a ^ b for a, b in zip(encrypted, stream))
    return raw.decode("utf-8")


@dataclass(frozen=True)
class Config:
    bind_host: str
    port: int
    db_path: str
    sub2api_base_url: str
    token_secret: str
    callback_secret: str
    image_dir: str
    image_url_prefix: str
    upload_dir: str
    upload_url_prefix: str
    public_base_url: str
    default_generation_model: str
    default_edit_model: str
    worker_count: int
    max_queue_size: int
    request_timeout_seconds: int
    callback_timeout_seconds: int
    max_body_bytes: int
    max_upload_bytes: int
    task_retention_seconds: int
    cleanup_interval_seconds: int
    allow_http_callback: bool


def sanitize_persisted_response(value: Any) -> Any:
    if isinstance(value, dict):
        image_call = value.get("type") == "image_generation_call"
        return {
            key: sanitize_persisted_response(item)
            for key, item in value.items()
            if key != "b64_json" and not (image_call and key == "result")
        }
    if isinstance(value, list):
        return [sanitize_persisted_response(item) for item in value]
    return value


class TaskStore:
    def __init__(self, db_path: str, token_secret: str) -> None:
        self.db_path = db_path
        self.token_secret = token_secret
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_db(self) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS image_tasks (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    model TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    api_key_fingerprint TEXT,
                    api_key_ciphertext TEXT,
                    response_json TEXT,
                    outputs_json TEXT,
                    error TEXT,
                    callback_url TEXT,
                    callback_status TEXT,
                    callback_attempts INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    started_at INTEGER,
                    completed_at INTEGER
                )
                """
            )
            existing_columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(image_tasks)").fetchall()
            }
            if "api_key_fingerprint" not in existing_columns:
                conn.execute("ALTER TABLE image_tasks ADD COLUMN api_key_fingerprint TEXT")
            if "api_key_ciphertext" not in existing_columns:
                conn.execute("ALTER TABLE image_tasks ADD COLUMN api_key_ciphertext TEXT")
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS image_tasks_status_created_idx
                ON image_tasks(status, created_at)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS image_tasks_status_completed_idx
                ON image_tasks(status, completed_at)
                """
            )

    def create_task(
        self,
        task_id: str,
        model: str,
        endpoint: str,
        request_payload: dict[str, Any],
        api_key: str,
        callback_url: str | None,
    ) -> dict[str, Any]:
        ts = now_ts()
        api_key_fingerprint = fingerprint_token(api_key, self.token_secret)
        api_key_ciphertext = protect_token(api_key, self.token_secret)
        with self._lock, self.connect() as conn:
            conn.execute(
                """
                INSERT INTO image_tasks (
                    id, status, model, endpoint, request_json,
                    api_key_fingerprint, api_key_ciphertext, callback_url,
                    created_at, updated_at
                ) VALUES (?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    model,
                    endpoint,
                    json_dumps(request_payload),
                    api_key_fingerprint,
                    api_key_ciphertext,
                    callback_url,
                    ts,
                    ts,
                ),
            )
        return self.get_task(task_id) or {}

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM image_tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_public_task(row)

    def get_task_for_api_key(self, task_id: str, api_key: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM image_tasks WHERE id = ? AND api_key_fingerprint = ?",
                (task_id, fingerprint_token(api_key, self.token_secret)),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_public_task(row)

    def mark_in_progress(self, task_id: str) -> None:
        ts = now_ts()
        with self._lock, self.connect() as conn:
            conn.execute(
                """
                UPDATE image_tasks
                SET status = 'in_progress', started_at = COALESCE(started_at, ?), updated_at = ?
                WHERE id = ? AND status = 'queued'
                """,
                (ts, ts, task_id),
            )

    def complete_task(
        self,
        task_id: str,
        response_payload: dict[str, Any],
        outputs: list[dict[str, Any]],
    ) -> dict[str, Any]:
        ts = now_ts()
        persisted_response = sanitize_persisted_response(response_payload)
        persisted_outputs = sanitize_persisted_response(outputs)
        has_usable_output = any(
            isinstance(output, dict) and bool(output.get("url"))
            for output in persisted_outputs
        )
        status = "completed" if has_usable_output else "partial_completed"
        with self._lock, self.connect() as conn:
            conn.execute(
                """
                UPDATE image_tasks
                SET status = ?, request_json = '{}', api_key_ciphertext = NULL,
                    response_json = ?, outputs_json = ?, updated_at = ?, completed_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    json_dumps(persisted_response),
                    json_dumps(persisted_outputs),
                    ts,
                    ts,
                    task_id,
                ),
            )
        return self.get_task(task_id) or {}

    def fail_task(self, task_id: str, error: str) -> dict[str, Any]:
        ts = now_ts()
        with self._lock, self.connect() as conn:
            conn.execute(
                """
                UPDATE image_tasks
                SET status = 'failed', request_json = '{}', api_key_ciphertext = NULL,
                    error = ?, updated_at = ?, completed_at = ?
                WHERE id = ?
                """,
                (error, ts, ts, task_id),
            )
        return self.get_task(task_id) or {}

    def delete_task(self, task_id: str) -> bool:
        with self._lock, self.connect() as conn:
            cursor = conn.execute("DELETE FROM image_tasks WHERE id = ?", (task_id,))
        return cursor.rowcount > 0

    def compact_terminal_tasks(self) -> int:
        compacted = 0
        with self._lock:
            with self.connect() as conn:
                task_ids = [
                    str(row["id"])
                    for row in conn.execute(
                        """
                        SELECT id
                        FROM image_tasks
                        WHERE status IN ('completed', 'partial_completed', 'failed', 'cancelled')
                        """
                    ).fetchall()
                ]
                for task_id in task_ids:
                    row = conn.execute(
                        """
                        SELECT id, request_json, api_key_ciphertext, response_json, outputs_json
                        FROM image_tasks
                        WHERE id = ?
                        """,
                        (task_id,),
                    ).fetchone()
                    if row is None:
                        continue
                    response_json = row["response_json"]
                    sanitized_json = response_json
                    outputs_json = row["outputs_json"]
                    sanitized_outputs_json = outputs_json
                    if response_json:
                        try:
                            sanitized_json = json_dumps(sanitize_persisted_response(json.loads(response_json)))
                        except (TypeError, ValueError):
                            pass
                    if outputs_json:
                        try:
                            sanitized_outputs_json = json_dumps(
                                sanitize_persisted_response(json.loads(outputs_json))
                            )
                        except (TypeError, ValueError):
                            pass
                    if (
                        row["request_json"] == "{}"
                        and row["api_key_ciphertext"] is None
                        and sanitized_json == response_json
                        and sanitized_outputs_json == outputs_json
                    ):
                        continue
                    conn.execute(
                        """
                        UPDATE image_tasks
                        SET request_json = '{}', api_key_ciphertext = NULL,
                            response_json = ?, outputs_json = ?
                        WHERE id = ?
                        """,
                        (sanitized_json, sanitized_outputs_json, row["id"]),
                    )
                    compacted += 1
            if compacted:
                self._vacuum_locked()
        return compacted

    def delete_expired_terminal_tasks(self, cutoff: int) -> int:
        with self._lock:
            with self.connect() as conn:
                cursor = conn.execute(
                    """
                    DELETE FROM image_tasks
                    WHERE status IN ('completed', 'partial_completed', 'failed', 'cancelled')
                      AND COALESCE(completed_at, updated_at) < ?
                    """,
                    (cutoff,),
                )
                deleted = cursor.rowcount
            if deleted:
                self._checkpoint_wal_locked()
        return deleted

    def reclaim_space(self) -> None:
        with self._lock:
            self._vacuum_locked()

    def _checkpoint_wal_locked(self) -> None:
        with self.connect() as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def _vacuum_locked(self) -> None:
        with self.connect() as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("VACUUM")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def set_callback_result(self, task_id: str, callback_status: str) -> None:
        with self._lock, self.connect() as conn:
            conn.execute(
                """
                UPDATE image_tasks
                SET callback_status = ?,
                    callback_attempts = callback_attempts + 1,
                    updated_at = ?
                WHERE id = ?
                """,
                (callback_status, now_ts(), task_id),
            )

    def requeue_unfinished(self) -> list[str]:
        with self._lock, self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id FROM image_tasks
                WHERE status IN ('queued', 'in_progress')
                ORDER BY created_at ASC
                """
            ).fetchall()
            conn.execute(
                """
                UPDATE image_tasks
                SET status = 'queued', updated_at = ?
                WHERE status = 'in_progress'
                """,
                (now_ts(),),
            )
        return [str(row["id"]) for row in rows]

    def _row_to_public_task(self, row: sqlite3.Row) -> dict[str, Any]:
        response = json.loads(row["response_json"]) if row["response_json"] else None
        outputs = json.loads(row["outputs_json"]) if row["outputs_json"] else None
        task: dict[str, Any] = {
            "id": row["id"],
            "status": row["status"],
            "model": row["model"],
            "endpoint": row["endpoint"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        for field in ("started_at", "completed_at"):
            if row[field] is not None:
                task[field] = row[field]
        if outputs is not None:
            task["outputs"] = outputs
        if response is not None:
            task["response"] = response
        if row["error"]:
            task["error"] = row["error"]
        if row["callback_url"]:
            task["callback_url"] = row["callback_url"]
            task["callback_attempts"] = row["callback_attempts"]
            if row["callback_status"]:
                task["callback_status"] = row["callback_status"]
        return task


def normalize_output_format(output_format: str) -> str:
    output_format = output_format.lower().strip()
    if "/" in output_format:
        output_format = output_format.rsplit("/", 1)[-1]
    if output_format == "jpeg":
        return "jpg"
    if output_format in {"png", "jpg", "webp"}:
        return output_format
    return "png"


def detect_image_extension(data: bytes, output_format: str) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp"
    return normalize_output_format(output_format)


def strip_data_url_prefix(value: str) -> str:
    if value.startswith("data:") and "," in value:
        return value.split(",", 1)[1]
    return value


def public_image_url(config: Config, filename: str) -> str:
    return public_file_url(config, config.image_url_prefix, filename)


def public_upload_url(config: Config, filename: str) -> str:
    return public_file_url(config, config.upload_url_prefix, filename)


def public_file_url(config: Config, url_prefix: str, filename: str) -> str:
    prefix = "/" + url_prefix.strip("/")
    path = f"{prefix}/{filename}"
    if not config.public_base_url:
        return path
    return config.public_base_url.rstrip("/") + path


def save_b64_image(config: Config, task_id: str, index: int, b64_value: str, output_format: str) -> dict[str, str]:
    raw = base64.b64decode(strip_data_url_prefix(b64_value), validate=True)
    ext = detect_image_extension(raw, output_format)
    filename = f"{task_id}_{index}.{ext}"
    os.makedirs(config.image_dir, exist_ok=True)
    path = os.path.join(config.image_dir, filename)
    with open(path, "wb") as image_file:
        image_file.write(raw)
    return {
        "url": public_image_url(config, filename),
        "file": filename,
        "bytes": str(len(raw)),
    }


def normalize_image_ref(value: Any) -> Any:
    if isinstance(value, str):
        trimmed = value.strip()
        if not trimmed:
            return value
        if trimmed.startswith("data:") or trimmed.startswith("http://") or trimmed.startswith("https://"):
            return trimmed
        return trimmed
    return value


def normalize_edit_images(value: Any) -> list[dict[str, str]]:
    items = value if isinstance(value, list) else [value]
    images: list[dict[str, str]] = []
    for item in items:
        if isinstance(item, str):
            ref = normalize_image_ref(item)
            if isinstance(ref, str) and ref:
                images.append({"image_url": ref})
            continue
        if isinstance(item, dict):
            image_url = normalize_image_ref(item.get("image_url"))
            if isinstance(image_url, str) and image_url:
                images.append({"image_url": image_url})
    return images


def normalize_edit_request_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    if "images" in normalized:
        images = normalize_edit_images(normalized["images"])
        if images:
            normalized["images"] = images
    elif "image" in normalized:
        images = normalize_edit_images(normalized.pop("image"))
        if images:
            normalized["images"] = images

    if "mask" in normalized:
        mask = normalized["mask"]
        if isinstance(mask, str):
            ref = normalize_image_ref(mask)
            if isinstance(ref, str) and ref:
                normalized["mask"] = {"image_url": ref}
        elif isinstance(mask, dict):
            mask_url = normalize_image_ref(mask.get("image_url"))
            if isinstance(mask_url, str) and mask_url:
                normalized["mask"] = {"image_url": mask_url}
    return normalized


def extract_outputs(response_payload: dict[str, Any], task_id: str, config: Config) -> list[dict[str, Any]]:
    outputs: list[dict[str, Any]] = []
    data = response_payload.get("data")
    if isinstance(data, list):
        for index, item in enumerate(data):
            if not isinstance(item, dict):
                continue
            output: dict[str, Any] = {}
            if isinstance(item.get("url"), str):
                output["url"] = item["url"]
            if isinstance(item.get("b64_json"), str):
                try:
                    saved = save_b64_image(
                        config,
                        task_id,
                        index,
                        item["b64_json"],
                        str(item.get("output_format") or item.get("format") or "png"),
                    )
                    output.update(saved)
                except Exception as exc:  # noqa: BLE001 - preserve result if local save fails.
                    output["save_error"] = str(exc)
            if isinstance(item.get("revised_prompt"), str):
                output["revised_prompt"] = item["revised_prompt"]
            if output:
                outputs.append(output)

    response_output = response_payload.get("output")
    if isinstance(response_output, list):
        for item in response_output:
            if not isinstance(item, dict) or item.get("type") != "image_generation_call":
                continue
            result = item.get("result")
            if not isinstance(result, str) or not result:
                continue
            output: dict[str, Any] = {}
            try:
                saved = save_b64_image(
                    config,
                    task_id,
                    len(outputs),
                    result,
                    str(item.get("output_format") or item.get("format") or "png"),
                )
                output.update(saved)
            except Exception as exc:  # noqa: BLE001 - preserve result metadata if local save fails.
                output["save_error"] = str(exc)
            if isinstance(item.get("id"), str):
                output["generation_id"] = item["id"]
            if isinstance(item.get("revised_prompt"), str):
                output["revised_prompt"] = item["revised_prompt"]
            outputs.append(output)
    return outputs


def delete_expired_files(directory: str, cutoff: int) -> int:
    if not os.path.isdir(directory):
        return 0
    deleted = 0
    with os.scandir(directory) as entries:
        for entry in entries:
            try:
                if not entry.is_file(follow_symlinks=False):
                    continue
                if entry.stat(follow_symlinks=False).st_mtime >= cutoff:
                    continue
                os.unlink(entry.path)
                deleted += 1
            except FileNotFoundError:
                continue
    return deleted


class WorkerPool:
    def __init__(self, config: Config, store: TaskStore) -> None:
        self.config = config
        self.store = store
        self.tasks: queue.Queue[str] = queue.Queue(maxsize=config.max_queue_size)
        self.shutdown_event = threading.Event()
        self.threads: list[threading.Thread] = []
        self.recovery_thread: threading.Thread | None = None
        self.cleanup_thread: threading.Thread | None = None

    def start(self) -> None:
        cleanup_stats = self.cleanup_expired()
        compacted = self.store.compact_terminal_tasks()
        if cleanup_stats["tasks"] and not compacted:
            self.store.reclaim_space()
        if compacted or any(cleanup_stats.values()):
            print(f"startup maintenance compacted_tasks={compacted} cleanup={cleanup_stats}", flush=True)
        for idx in range(self.config.worker_count):
            thread = threading.Thread(target=self._run, name=f"async-image-worker-{idx + 1}", daemon=True)
            thread.start()
            self.threads.append(thread)
        unfinished = self.store.requeue_unfinished()
        if unfinished:
            self.recovery_thread = threading.Thread(
                target=self._recover_tasks,
                args=(unfinished,),
                name="async-image-recovery",
                daemon=True,
            )
            self.recovery_thread.start()
        self.cleanup_thread = threading.Thread(
            target=self._cleanup_loop,
            name="async-image-cleanup",
            daemon=True,
        )
        self.cleanup_thread.start()

    def stop(self) -> None:
        self.shutdown_event.set()
        for _ in self.threads:
            try:
                self.tasks.put("", timeout=1)
            except queue.Full:
                break
        for thread in self.threads:
            thread.join(timeout=5)
        if self.recovery_thread:
            self.recovery_thread.join(timeout=5)
        if self.cleanup_thread:
            self.cleanup_thread.join(timeout=5)

    def enqueue(self, task_id: str) -> bool:
        try:
            self.tasks.put_nowait(task_id)
            return True
        except queue.Full:
            return False

    def _recover_tasks(self, task_ids: list[str]) -> None:
        for task_id in task_ids:
            while not self.shutdown_event.is_set():
                try:
                    self.tasks.put(task_id, timeout=1)
                    break
                except queue.Full:
                    continue

    def _cleanup_loop(self) -> None:
        while not self.shutdown_event.wait(self.config.cleanup_interval_seconds):
            try:
                stats = self.cleanup_expired()
                if any(stats.values()):
                    print(f"expired data cleanup={stats}", flush=True)
            except Exception as exc:  # noqa: BLE001 - maintenance failure must not stop workers.
                print(f"cleanup error: {exc}", file=sys.stderr)
                traceback.print_exc()

    def cleanup_expired(self) -> dict[str, int]:
        if self.config.task_retention_seconds <= 0:
            return {"tasks": 0, "images": 0, "uploads": 0}
        cutoff = now_ts() - self.config.task_retention_seconds
        return {
            "tasks": self.store.delete_expired_terminal_tasks(cutoff),
            "images": delete_expired_files(self.config.image_dir, cutoff),
            "uploads": delete_expired_files(self.config.upload_dir, cutoff),
        }

    def _run(self) -> None:
        while True:
            task_id = self.tasks.get()
            try:
                if not task_id or self.shutdown_event.is_set():
                    return
                self._process(task_id)
            except Exception as exc:  # noqa: BLE001 - keep worker alive for MVP reliability.
                print(f"worker error task_id={task_id}: {exc}", file=sys.stderr)
                traceback.print_exc()
                self.store.fail_task(task_id, str(exc))
            finally:
                self.tasks.task_done()

    def _process(self, task_id: str) -> None:
        task = self.store.get_task(task_id)
        if task is None or task["status"] in TERMINAL_STATUSES:
            return
        self.store.mark_in_progress(task_id)
        payload, api_key = self._load_internal_request(task_id)
        try:
            response_payload = self._call_sub2api(task["endpoint"], payload, api_key)
            outputs = extract_outputs(response_payload, task_id, self.config)
            final_task = self.store.complete_task(task_id, response_payload, outputs)
        except Exception as exc:  # noqa: BLE001 - persist upstream failure as task failure.
            final_task = self.store.fail_task(task_id, str(exc))
        self._send_callback_if_needed(final_task)

    def _load_internal_request(self, task_id: str) -> tuple[dict[str, Any], str]:
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT request_json, api_key_ciphertext FROM image_tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError(f"task not found: {task_id}")
        api_key_ciphertext = row["api_key_ciphertext"]
        if not api_key_ciphertext:
            raise RuntimeError("task has no stored API key; resubmit it with Authorization: Bearer <Sub2API key>")
        return json.loads(row["request_json"]), unprotect_token(api_key_ciphertext, self.store.token_secret)

    def _call_sub2api(self, endpoint: str, payload: dict[str, Any], api_key: str) -> dict[str, Any]:
        base = self.config.sub2api_base_url.rstrip("/")
        url = f"{base}{endpoint}"
        raw = json_dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=raw,
            method="POST",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.config.request_timeout_seconds) as resp:
                body = resp.read()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"sub2api HTTP {exc.code}: {body[:1000]}") from exc
        parsed = json.loads(body.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise RuntimeError("sub2api response must be a JSON object")
        return parsed

    def _send_callback_if_needed(self, task: dict[str, Any]) -> None:
        callback_url = task.get("callback_url")
        if not callback_url:
            return
        body = json_dumps(task).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "X-Async-Image-Task-Id": task["id"],
        }
        if self.config.callback_secret:
            signature = hmac.new(
                self.config.callback_secret.encode("utf-8"),
                body,
                hashlib.sha256,
            ).hexdigest()
            headers["X-Async-Image-Signature"] = f"sha256={signature}"
        req = urllib.request.Request(
            callback_url,
            data=body,
            method="POST",
            headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=self.config.callback_timeout_seconds) as resp:
                self.store.set_callback_result(task["id"], f"http_{resp.status}")
        except Exception as exc:  # noqa: BLE001 - callback failure should not fail the task.
            self.store.set_callback_result(task["id"], f"failed:{exc}")


class AsyncImageHandler(BaseHTTPRequestHandler):
    server_version = "AsyncImageSidecar/0.1"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API.
        if self.path == "/health":
            json_response(self, 200, {"status": "ok"})
            return
        parsed = urlparse(self.path)
        image_prefix = "/" + self.server.config.image_url_prefix.strip("/") + "/"  # type: ignore[attr-defined]
        if parsed.path.startswith(image_prefix):
            filename = parsed.path.removeprefix(image_prefix)
            self._serve_file(self.server.config.image_dir, filename)  # type: ignore[attr-defined]
            return
        upload_prefix = "/" + self.server.config.upload_url_prefix.strip("/") + "/"  # type: ignore[attr-defined]
        if parsed.path.startswith(upload_prefix):
            filename = parsed.path.removeprefix(upload_prefix)
            self._serve_file(self.server.config.upload_dir, filename)  # type: ignore[attr-defined]
            return
        if parsed.path.startswith("/v1/task/"):
            api_key = self._require_bearer_token()
            if not api_key:
                return
            task_id = parsed.path.removeprefix("/v1/task/").strip("/")
            if not task_id:
                error_response(self, 404, "not_found", "task id is required")
                return
            task = self.server.store.get_task_for_api_key(task_id, api_key)  # type: ignore[attr-defined]
            if task is None:
                error_response(self, 404, "not_found", "task not found")
                return
            json_response(self, 200, task)
            return
        error_response(self, 404, "not_found", "route not found")

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API.
        parsed = urlparse(self.path)
        if parsed.path in {"/v1/images/generations", "/images/generations"}:
            api_key = self._require_bearer_token()
            if not api_key:
                return
            try:
                body = read_json_body(self, self.server.config.max_body_bytes)  # type: ignore[attr-defined]
                task = self._submit_image_request("/v1/images/generations", body, api_key, None)
            except QueueFullError as exc:
                error_response(self, 503, "server_overloaded", str(exc))
                return
            except ValueError as exc:
                error_response(self, 400, "invalid_request", str(exc))
                return
            json_response(self, 202, task)
            return
        if parsed.path in {"/v1/images/edits", "/images/edits"}:
            api_key = self._require_bearer_token()
            if not api_key:
                return
            try:
                task = self._submit_multipart_edit(api_key)
            except QueueFullError as exc:
                error_response(self, 503, "server_overloaded", str(exc))
                return
            except ValueError as exc:
                error_response(self, 400, "invalid_request", str(exc))
                return
            json_response(self, 202, task)
            return
        if parsed.path in {"/v1/images/responses", "/images/responses"}:
            api_key = self._require_bearer_token()
            if not api_key:
                return
            try:
                body = read_json_body(self, self.server.config.max_body_bytes)  # type: ignore[attr-defined]
                task = self._submit_responses_image_request(body, api_key, None)
            except QueueFullError as exc:
                error_response(self, 503, "server_overloaded", str(exc))
                return
            except ValueError as exc:
                error_response(self, 400, "invalid_request", str(exc))
                return
            json_response(self, 202, task)
            return
        if parsed.path == "/v1/task/upload":
            api_key = self._require_bearer_token()
            if not api_key:
                return
            try:
                upload = self._upload_file()
            except ValueError as exc:
                error_response(self, 400, "invalid_request", str(exc))
                return
            json_response(self, 201, upload)
            return
        if parsed.path != "/v1/task/submit":
            error_response(self, 404, "not_found", "route not found")
            return
        api_key = self._require_bearer_token()
        if not api_key:
            return
        try:
            body = read_json_body(self, self.server.config.max_body_bytes)  # type: ignore[attr-defined]
            task = self._submit_task(body, api_key)
        except QueueFullError as exc:
            error_response(self, 503, "server_overloaded", str(exc))
            return
        except ValueError as exc:
            error_response(self, 400, "invalid_request", str(exc))
            return
        json_response(self, 202, task)

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), fmt % args))

    def _serve_file(self, base_dir: str, filename: str) -> None:
        if not filename or "/" in filename or "\\" in filename or filename.startswith("."):
            error_response(self, 404, "not_found", "file not found")
            return
        path = os.path.join(base_dir, filename)
        if not os.path.isfile(path):
            error_response(self, 404, "not_found", "file not found")
            return
        content_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
        size = os.path.getsize(path)
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        self.end_headers()
        with open(path, "rb") as image_file:
            while True:
                chunk = image_file.read(1024 * 64)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def _upload_file(self) -> dict[str, Any]:
        form = self._parse_multipart_form(self.server.config.max_upload_bytes)  # type: ignore[attr-defined]
        field = None
        for name in ("file", "image", "mask"):
            if name in form:
                field = form[name]
                if isinstance(field, list):
                    field = field[0] if field else None
                break
        if field is None:
            raise ValueError("multipart field file, image, or mask is required")
        return self._save_uploaded_field(field)

    def _parse_multipart_form(self, max_bytes: int) -> cgi.FieldStorage:
        content_type = self.headers.get("Content-Type", "")
        if not content_type.lower().startswith("multipart/form-data"):
            raise ValueError("Content-Type must be multipart/form-data")
        length_header = self.headers.get("Content-Length")
        if not length_header:
            raise ValueError("Content-Length is required")
        try:
            length = int(length_header)
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if length <= 0:
            raise ValueError("upload body is required")
        if length > max_bytes:
            raise ValueError(f"upload too large; max {max_bytes} bytes")

        return cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={
                "REQUEST_METHOD": "POST",
                "CONTENT_TYPE": content_type,
                "CONTENT_LENGTH": str(length),
            },
            keep_blank_values=True,
        )

    def _save_uploaded_field(self, field: Any, include_data_url: bool = False) -> dict[str, Any]:
        if field is None or not getattr(field, "file", None):
            raise ValueError("multipart file field is required")
        data = field.file.read()
        if not data:
            raise ValueError("uploaded file is empty")
        if len(data) > self.server.config.max_upload_bytes:  # type: ignore[attr-defined]
            raise ValueError(f"upload too large; max {self.server.config.max_upload_bytes} bytes")  # type: ignore[attr-defined]

        content_type = getattr(field, "type", "") or "application/octet-stream"
        if not content_type.lower().startswith("image/"):
            detected = mimetypes.guess_type(getattr(field, "filename", "") or "")[0] or ""
            if not detected.lower().startswith("image/"):
                raise ValueError("uploaded file must be an image")
            content_type = detected

        filename_hint = getattr(field, "filename", "") or ""
        fallback_ext = os.path.splitext(filename_hint)[1].lstrip(".") or content_type.rsplit("/", 1)[-1]
        ext = detect_image_extension(data, fallback_ext)
        filename = "upload_" + secrets.token_urlsafe(18).replace("-", "").replace("_", "")[:24] + f".{ext}"
        os.makedirs(self.server.config.upload_dir, exist_ok=True)  # type: ignore[attr-defined]
        path = os.path.join(self.server.config.upload_dir, filename)  # type: ignore[attr-defined]
        with open(path, "wb") as uploaded:
            uploaded.write(data)
        result = {
            "url": public_upload_url(self.server.config, filename),  # type: ignore[attr-defined]
            "file": filename,
            "bytes": len(data),
            "content_type": mimetypes.guess_type(path)[0] or content_type,
        }
        if include_data_url:
            result["data_url"] = "data:" + result["content_type"] + ";base64," + base64.b64encode(data).decode("ascii")
        return result

    def _require_bearer_token(self) -> str:
        got = bearer_token(self.headers)
        if not got:
            error_response(self, 401, "authentication_error", "Authorization: Bearer <Sub2API API key> is required")
            return ""
        return got

    def _create_task_from_payload(
        self,
        endpoint: str,
        payload: dict[str, Any],
        api_key: str,
        callback_url: str | None,
    ) -> dict[str, Any]:
        model = payload.get("model")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model is required")
        model = model.strip()
        request_payload = dict(payload)
        request_payload["model"] = model
        if endpoint == "/v1/images/edits":
            request_payload = normalize_edit_request_payload(request_payload)

        task_id = "task_" + secrets.token_urlsafe(18).replace("-", "").replace("_", "")[:24]
        task = self.server.store.create_task(  # type: ignore[attr-defined]
            task_id=task_id,
            model=model,
            endpoint=endpoint,
            request_payload=request_payload,
            api_key=api_key,
            callback_url=callback_url,
        )
        if not self.server.worker_pool.enqueue(task_id):  # type: ignore[attr-defined]
            self.server.store.delete_task(task_id)  # type: ignore[attr-defined]
            raise QueueFullError("task queue is full; retry later")
        return task

    def _submit_image_request(
        self,
        endpoint: str,
        body: dict[str, Any],
        api_key: str,
        callback_url: str | None,
    ) -> dict[str, Any]:
        payload = dict(body)
        if "model" not in payload:
            if endpoint == "/v1/images/edits":
                payload["model"] = self.server.config.default_edit_model  # type: ignore[attr-defined]
            else:
                payload["model"] = self.server.config.default_generation_model  # type: ignore[attr-defined]
        return self._create_task_from_payload(endpoint, payload, api_key, callback_url)

    def _submit_responses_image_request(
        self,
        body: dict[str, Any],
        api_key: str,
        callback_url: str | None,
    ) -> dict[str, Any]:
        tools = body.get("tools")
        if not isinstance(tools, list) or not any(
            isinstance(tool, dict) and tool.get("type") == "image_generation"
            for tool in tools
        ):
            raise ValueError("tools must include an image_generation tool")
        if "input" not in body:
            raise ValueError("input is required")
        return self._create_task_from_payload("/v1/responses", dict(body), api_key, callback_url)

    def _submit_task(self, body: dict[str, Any], api_key: str) -> dict[str, Any]:
        endpoint = body.get("endpoint", "/v1/images/generations")
        endpoint_aliases = {
            "/v1/images/generations": "/v1/images/generations",
            "/images/generations": "/v1/images/generations",
            "generations": "/v1/images/generations",
            "generate": "/v1/images/generations",
            "/v1/images/edits": "/v1/images/edits",
            "/images/edits": "/v1/images/edits",
            "edits": "/v1/images/edits",
            "edit": "/v1/images/edits",
            "/v1/responses": "/v1/responses",
            "responses": "/v1/responses",
            "thinking": "/v1/responses",
        }
        if not isinstance(endpoint, str) or endpoint not in endpoint_aliases:
            raise ValueError("endpoint must be generations, edits, or responses")
        endpoint = endpoint_aliases[endpoint]

        callback_url = body.get("callback_url")
        if callback_url is not None:
            if not isinstance(callback_url, str) or not callback_url.strip():
                raise ValueError("callback_url must be a non-empty string")
            callback_url = callback_url.strip()
            if not is_public_callback_url(callback_url, self.server.config.allow_http_callback):  # type: ignore[attr-defined]
                raise ValueError("callback_url must be https URL")

        input_payload = body.get("input")
        if not isinstance(input_payload, dict):
            raise ValueError("input must be a JSON object")

        model = body.get("model") or input_payload.get("model")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model is required")
        model = model.strip()

        request_payload = dict(input_payload)
        request_payload["model"] = model
        if endpoint == "/v1/responses":
            return self._submit_responses_image_request(request_payload, api_key, callback_url)
        return self._create_task_from_payload(endpoint, request_payload, api_key, callback_url)

    def _submit_multipart_edit(self, api_key: str) -> dict[str, Any]:
        form = self._parse_multipart_form(self.server.config.max_upload_bytes)  # type: ignore[attr-defined]
        payload: dict[str, Any] = {}
        image_urls: list[dict[str, str]] = []
        mask_url = ""
        callback_url = ""

        for key in form.keys():
            values = form[key]
            fields = values if isinstance(values, list) else [values]
            for field in fields:
                filename = getattr(field, "filename", "") or ""
                if filename:
                    if key != "mask" and key != "image" and not key.startswith("image["):
                        continue
                    upload = self._save_uploaded_field(field, include_data_url=True)
                    if key == "image" or key.startswith("image["):
                        image_urls.append({"image_url": upload["data_url"]})
                    else:
                        mask_url = upload["data_url"]
                    continue
                raw_value = field.value
                if isinstance(raw_value, bytes):
                    value = raw_value.decode("utf-8", errors="replace")
                else:
                    value = str(raw_value)
                value = value.strip()
                if not value:
                    continue
                if key == "callback_url":
                    callback_url = value
                elif key == "image" or key.startswith("image["):
                    image_urls.append({"image_url": value})
                elif key == "mask":
                    mask_url = value
                elif key in {
                    "model",
                    "prompt",
                    "size",
                    "quality",
                    "response_format",
                    "background",
                    "output_format",
                    "style",
                    "moderation",
                    "input_fidelity",
                }:
                    payload[key] = value
                elif key in {"n", "output_compression", "partial_images"}:
                    try:
                        payload[key] = int(value)
                    except ValueError as exc:
                        raise ValueError(f"{key} must be a number") from exc

        if callback_url and not is_public_callback_url(callback_url, self.server.config.allow_http_callback):  # type: ignore[attr-defined]
            raise ValueError("callback_url must be https URL")
        if "model" not in payload:
            payload["model"] = self.server.config.default_edit_model  # type: ignore[attr-defined]
        if not isinstance(payload.get("prompt"), str) or not str(payload.get("prompt")).strip():
            raise ValueError("prompt is required")
        if not image_urls:
            raise ValueError("multipart field image is required")
        payload["images"] = image_urls
        if mask_url:
            payload["mask"] = {"image_url": mask_url}
        return self._create_task_from_payload("/v1/images/edits", payload, api_key, callback_url or None)


class AsyncImageServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], config: Config, store: TaskStore, worker_pool: WorkerPool) -> None:
        super().__init__(server_address, AsyncImageHandler)
        self.config = config
        self.store = store
        self.worker_pool = worker_pool


def load_config() -> Config:
    parser = argparse.ArgumentParser(description="Sub2API async image MVP sidecar")
    parser.add_argument("--host", default=os.getenv("ASYNC_IMAGE_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("ASYNC_IMAGE_PORT", "8090")))
    parser.add_argument("--db", default=os.getenv("ASYNC_IMAGE_DB", "/data/async-image-tasks.sqlite3"))
    parser.add_argument("--sub2api-base-url", default=os.getenv("SUB2API_BASE_URL", "http://sub2api:8080"))
    parser.add_argument("--token-secret", default=os.getenv("ASYNC_IMAGE_TOKEN_SECRET", ""))
    parser.add_argument("--callback-secret", default=os.getenv("ASYNC_IMAGE_CALLBACK_SECRET", ""))
    parser.add_argument("--image-dir", default=os.getenv("ASYNC_IMAGE_DIR", "/data/images"))
    parser.add_argument("--image-url-prefix", default=os.getenv("ASYNC_IMAGE_URL_PREFIX", "/v1/task/images"))
    parser.add_argument("--upload-dir", default=os.getenv("ASYNC_IMAGE_UPLOAD_DIR", "/data/uploads"))
    parser.add_argument("--upload-url-prefix", default=os.getenv("ASYNC_IMAGE_UPLOAD_URL_PREFIX", "/v1/task/uploads"))
    parser.add_argument("--public-base-url", default=os.getenv("ASYNC_IMAGE_PUBLIC_BASE_URL", ""))
    parser.add_argument(
        "--default-generation-model",
        default=os.getenv("ASYNC_IMAGE_DEFAULT_GENERATION_MODEL", DEFAULT_GENERATION_MODEL),
    )
    parser.add_argument(
        "--default-edit-model",
        default=os.getenv("ASYNC_IMAGE_DEFAULT_EDIT_MODEL", DEFAULT_EDIT_MODEL),
    )
    parser.add_argument("--workers", type=int, default=int(os.getenv("ASYNC_IMAGE_WORKERS", "2")))
    parser.add_argument("--max-queue-size", type=int, default=int(os.getenv("ASYNC_IMAGE_MAX_QUEUE_SIZE", "100")))
    parser.add_argument("--request-timeout", type=int, default=int(os.getenv("ASYNC_IMAGE_REQUEST_TIMEOUT_SECONDS", "900")))
    parser.add_argument("--callback-timeout", type=int, default=int(os.getenv("ASYNC_IMAGE_CALLBACK_TIMEOUT_SECONDS", "15")))
    parser.add_argument("--max-body-bytes", type=int, default=int(os.getenv("ASYNC_IMAGE_MAX_BODY_BYTES", str(70 << 20))))
    parser.add_argument("--max-upload-bytes", type=int, default=int(os.getenv("ASYNC_IMAGE_MAX_UPLOAD_BYTES", str(50 << 20))))
    parser.add_argument(
        "--task-retention-seconds",
        type=int,
        default=int(os.getenv("ASYNC_IMAGE_TASK_RETENTION_SECONDS", "86400")),
    )
    parser.add_argument(
        "--cleanup-interval-seconds",
        type=int,
        default=int(os.getenv("ASYNC_IMAGE_CLEANUP_INTERVAL_SECONDS", "600")),
    )
    parser.add_argument(
        "--allow-http-callback",
        action="store_true",
        default=os.getenv("ASYNC_IMAGE_ALLOW_HTTP_CALLBACK", "false").lower() == "true",
    )
    args = parser.parse_args()

    if not args.token_secret:
        raise SystemExit("ASYNC_IMAGE_TOKEN_SECRET or --token-secret is required")
    if args.workers < 1:
        raise SystemExit("workers must be >= 1")
    if args.max_queue_size < 1:
        raise SystemExit("max queue size must be >= 1")
    if not args.default_generation_model.strip():
        raise SystemExit("default generation model must not be empty")
    if not args.default_edit_model.strip():
        raise SystemExit("default edit model must not be empty")
    if args.task_retention_seconds < 0:
        raise SystemExit("task retention seconds must be >= 0")
    if args.cleanup_interval_seconds < 1:
        raise SystemExit("cleanup interval seconds must be >= 1")

    return Config(
        bind_host=args.host,
        port=args.port,
        db_path=args.db,
        sub2api_base_url=args.sub2api_base_url,
        token_secret=args.token_secret,
        callback_secret=args.callback_secret,
        image_dir=args.image_dir,
        image_url_prefix=args.image_url_prefix,
        upload_dir=args.upload_dir,
        upload_url_prefix=args.upload_url_prefix,
        public_base_url=args.public_base_url,
        default_generation_model=args.default_generation_model.strip(),
        default_edit_model=args.default_edit_model.strip(),
        worker_count=args.workers,
        max_queue_size=args.max_queue_size,
        request_timeout_seconds=args.request_timeout,
        callback_timeout_seconds=args.callback_timeout,
        max_body_bytes=args.max_body_bytes,
        max_upload_bytes=args.max_upload_bytes,
        task_retention_seconds=args.task_retention_seconds,
        cleanup_interval_seconds=args.cleanup_interval_seconds,
        allow_http_callback=args.allow_http_callback,
    )


def main() -> None:
    config = load_config()
    store = TaskStore(config.db_path, config.token_secret)
    worker_pool = WorkerPool(config, store)
    worker_pool.start()
    server = AsyncImageServer((config.bind_host, config.port), config, store, worker_pool)
    shutdown_started = threading.Event()

    def handle_signal(_signum: int, _frame: Any) -> None:
        if shutdown_started.is_set():
            return
        shutdown_started.set()
        threading.Thread(
            target=server.shutdown,
            name="async-image-shutdown",
            daemon=True,
        ).start()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    print(
        "async image sidecar listening "
        f"host={config.bind_host} port={config.port} "
        f"sub2api={config.sub2api_base_url} "
        f"image_dir={config.image_dir}",
        flush=True,
    )
    try:
        server.serve_forever()
    finally:
        worker_pool.stop()
        server.server_close()


if __name__ == "__main__":
    main()
