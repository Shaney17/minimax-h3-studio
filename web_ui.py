"""Runpod request UI for the MiniMax H3 Cinematic endpoint.

The server only listens on 127.0.0.1 and forwards requests to one fixed Runpod
endpoint. Other devices reach it privately through `tailscale serve`. Each
request (prompt, settings, reference images, workflow) and its generated video
are kept in a SQLite database under ``data/`` so jobs can be reviewed or copied
later, together with the Runpod API key saved in Settings.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import shutil
import sqlite3
import ssl
import threading
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ENDPOINT_ID = "xjh0wwhkto13ky"
RUNPOD_BASE = "https://api.runpod.ai/v2"
MAX_REQUEST_BYTES = 9_500_000
ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}
UI_WORKFLOW = ROOT / "workflow" / "cinematic_ui.json"
API_WORKFLOW = ROOT / "workflow" / "cinematic_api.json"
DATA_DIR = Path(os.environ.get("H3_DATA_DIR", ROOT / "data"))
DB_PATH = DATA_DIR / "studio.db"
FILES_DIR = DATA_DIR / "requests"
JOB_ID = re.compile(r"^[A-Za-z0-9_-]{1,120}$")
REQUEST_ID = re.compile(r"^[a-f0-9]{32}$")
DATA_URL = re.compile(r"^data:(image/(?:png|jpeg|webp));base64,(.*)$", re.S)
IMAGE_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
# LoadImage nodes by slot: three subject references, then the optional frame
# anchor ("300") that the UI adds when a shot continues from an image.
IMAGE_NODES = ("28", "29", "30", "300")
REQUIRED_SLOTS = {1, 2, 3}
TERMINAL_STATES = {"COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT", "SUBMIT_FAILED", "EXPIRED"}
POLL_SECONDS = 15
# Seconds the UI may request. MiniMax H3 is trained on 124-362 frames (~5-15 s at
# 24 fps); node 66 snaps seconds up to its 17k+5 frame grid. 3 s is a smoke test.
DURATIONS = (3, 5, 8, 10, 12, 15)
# Per-job limit sent to Runpod. A job policy overrides the endpoint timeout, so
# keep this equal to the endpoint's execution timeout (60 min on 2026-09-28).
EXECUTION_TIMEOUT_MS = int(os.environ.get("H3_EXECUTION_TIMEOUT_MS", "3600000"))
# The TTL also covers queueing and cold starts (pulling the ~43 GB image).
JOB_TTL_MS = EXECUTION_TIMEOUT_MS + 3_600_000
ALLOWED_HOSTS = {"127.0.0.1", "localhost"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS requests (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    job_id TEXT UNIQUE,
    status TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    prompt TEXT NOT NULL,
    duration REAL,
    crf INTEGER,
    resolution REAL,
    key_hint TEXT,
    workflow TEXT NOT NULL,
    error TEXT,
    video_file TEXT,
    video_url TEXT,
    delay_ms INTEGER,
    execution_ms INTEGER,
    continues_from TEXT
);
CREATE TABLE IF NOT EXISTS request_images (
    request_id TEXT NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
    slot INTEGER NOT NULL,
    file TEXT NOT NULL,
    original_name TEXT NOT NULL,
    mime TEXT NOT NULL,
    PRIMARY KEY (request_id, slot)
);
"""


def runpod_ssl_context() -> ssl.SSLContext:
    """Add the OS CA bundle when a Python.org macOS install lacks one."""
    context = ssl.create_default_context()
    for path in (Path("/etc/ssl/cert.pem"), Path("/etc/ssl/certs/ca-certificates.crt")):
        if path.is_file():
            context.load_verify_locations(cafile=str(path))
            break
    return context


RUNPOD_SSL_CONTEXT = runpod_ssl_context()
STATUS_LOCK = threading.Lock()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_title(prompt: str) -> str:
    """Use the first meaningful line of the prompt when no title is given."""
    for line in prompt.splitlines():
        line = line.strip()
        if line:
            return line[:80] + ("…" if len(line) > 80 else "")
    return "Request không tiêu đề"


def fold(text: str) -> str:
    """Case- and accent-insensitive form, so "rung" matches "Rừng"."""
    decomposed = unicodedata.normalize("NFD", text.strip().casefold().replace("đ", "d"))
    return "".join(char for char in decomposed if not unicodedata.combining(char))


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db() -> None:
    DATA_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    FILES_DIR.mkdir(mode=0o700, exist_ok=True)
    with connect() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(requests)")}
        if "title" not in columns:
            conn.execute("ALTER TABLE requests ADD COLUMN title TEXT NOT NULL DEFAULT ''")
        for column, kind in (("delay_ms", "INTEGER"), ("execution_ms", "INTEGER"), ("continues_from", "TEXT")):
            if column not in columns:
                conn.execute(f"ALTER TABLE requests ADD COLUMN {column} {kind}")
        for row in conn.execute("SELECT id, prompt FROM requests WHERE title = ''").fetchall():
            conn.execute("UPDATE requests SET title = ? WHERE id = ?", (default_title(row["prompt"]), row["id"]))
    # The database can hold the Runpod API key; keep it private to this user.
    os.chmod(DB_PATH, 0o600)


def get_saved_key() -> str:
    with connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = 'runpod_api_key'").fetchone()
    return row["value"] if row else ""


def runpod_call(method: str, path: str, key: str, body: bytes | None = None) -> tuple[int, bytes]:
    """Call the fixed endpoint. Network failures raise URLError/TimeoutError."""
    url = f"{RUNPOD_BASE}/{ENDPOINT_ID}{path}"
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=45, context=RUNPOD_SSL_CONTEXT) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def record_submission(job_input: dict, key: str, title: str, continues_from: str | None = None) -> str:
    """Store the prompt, settings, workflow, and reference images of a request."""
    graph = job_input["workflow"]
    prompt = graph.get("190", {}).get("inputs", {}).get("value")
    if not isinstance(prompt, str):
        raise ValueError("Workflow thiếu prompt ở nút 190.")
    megapixels = graph.get("112", {}).get("inputs", {}).get("megapixels")
    request_id = uuid.uuid4().hex
    folder = FILES_DIR / request_id
    folder.mkdir(mode=0o700)
    slots = {
        graph[node]["inputs"]["image"]: slot
        for slot, node in enumerate(IMAGE_NODES, 1)
        if isinstance(graph.get(node, {}).get("inputs", {}).get("image"), str)
    }
    if not REQUIRED_SLOTS <= set(slots.values()):
        raise ValueError("Workflow thiếu một trong các nút ảnh 28, 29, 30.")
    images = []
    try:
        for item in job_input["images"]:
            name = item.get("name") if isinstance(item, dict) else None
            slot = slots.get(name)
            match = DATA_URL.match(item.get("image", "")) if slot else None
            if not match:
                raise ValueError("Ảnh tham chiếu không đúng định dạng PNG, JPG hoặc WebP.")
            mime, encoded = match.groups()
            file = f"ref_{slot}{IMAGE_EXT[mime]}"
            (folder / file).write_bytes(base64.b64decode(encoded, validate=True))
            original = re.sub(r"^h3_ref_\d_\d+_", "", name)
            images.append((request_id, slot, file, original, mime))
        if {image[1] for image in images} != set(slots.values()):
            raise ValueError("Thiếu ảnh cho một nút LoadImage trong workflow.")
        with connect() as conn:
            conn.execute(
                "INSERT INTO requests (id, created_at, updated_at, status, title, prompt, duration, crf, resolution, key_hint, workflow, continues_from)"
                " VALUES (?, ?, ?, 'SUBMITTING', ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    request_id, now(), now(), title[:200] or default_title(prompt), prompt,
                    graph.get("111", {}).get("inputs", {}).get("value"),
                    graph.get("154", {}).get("inputs", {}).get("crf"),
                    0.2 if megapixels == 0.2 else 1,
                    f"…{key[-4:]}",
                    json.dumps(graph, ensure_ascii=False),
                    continues_from,
                ),
            )
            conn.executemany("INSERT INTO request_images VALUES (?, ?, ?, ?, ?)", images)
    except (ValueError, binascii.Error, OSError, sqlite3.Error):
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return request_id


def update_request(request_id: str, **fields) -> None:
    fields["updated_at"] = now()
    columns = ", ".join(f"{name} = ?" for name in fields)
    with connect() as conn:
        conn.execute(f"UPDATE requests SET {columns} WHERE id = ?", (*fields.values(), request_id))


def job_error(result: dict) -> str:
    output = result.get("output")
    details = output.get("details") if isinstance(output, dict) else None
    lines = [str(result.get("error") or (output or {}).get("error") or "Job xử lý thất bại.")]
    if isinstance(details, list):
        lines += [str(item) for item in details if str(item) not in lines[0]]
    return "\n".join(lines)


def record_status(job_id: str, result: dict) -> None:
    """Persist a Runpod status response, saving the final video when present."""
    status = str(result.get("status", "")).upper()
    if not status:
        return
    with STATUS_LOCK:
        with connect() as conn:
            row = conn.execute("SELECT id, status FROM requests WHERE job_id = ?", (job_id,)).fetchone()
        if not row or row["status"] in TERMINAL_STATES:
            return
        request_id = row["id"]
        # Runpod reports queue/cold-start and run time in milliseconds.
        timings = {
            column: int(result[field])
            for column, field in (("delay_ms", "delayTime"), ("execution_ms", "executionTime"))
            if isinstance(result.get(field), (int, float))
        }
        if timings:
            update_request(request_id, **timings)
        if status == "TIMED_OUT":
            update_request(request_id, status=status, error=(
                f"Job vượt giới hạn chạy {EXECUTION_TIMEOUT_MS // 60000} phút và bị Runpod dừng. "
                "Hãy giảm thời lượng hoặc độ phân giải."
            ))
            return
        if status != "COMPLETED":
            error = job_error(result) if status in TERMINAL_STATES else None
            update_request(request_id, status=status, error=error)
            return
        output = result.get("output")
        videos = output.get("videos") if isinstance(output, dict) else None
        if not isinstance(output, dict) or output.get("error") or not videos:
            message = job_error(result) if isinstance(output, dict) and output.get("error") else "Job hoàn tất nhưng worker không trả video MP4."
            update_request(request_id, status="FAILED", error=message)
            return
        video = videos[-1]
        filename = re.sub(r"[^A-Za-z0-9._-]", "_", Path(str(video.get("filename") or "video.mp4")).name)
        if not filename.lower().endswith((".mp4", ".webm", ".mov", ".mkv")):
            filename += ".mp4"
        try:
            if video.get("type") == "s3_url" and str(video.get("data", "")).startswith("https://"):
                update_request(request_id, status="COMPLETED", video_url=video["data"], error=None)
            elif video.get("type") == "base64" and isinstance(video.get("data"), str):
                data = base64.b64decode(re.sub(r"^data:[^,]+,", "", video["data"]))
                (FILES_DIR / request_id / filename).write_bytes(data)
                update_request(request_id, status="COMPLETED", video_file=filename, error=None)
            else:
                update_request(request_id, status="FAILED", error="Worker trả định dạng video chưa được hỗ trợ.")
        except (binascii.Error, OSError) as exc:
            update_request(request_id, status="FAILED", error=f"Không lưu được video: {exc}")


def poll_pending(stop: threading.Event) -> None:
    """Keep saving job results after the browser tab has been closed."""
    while not stop.wait(POLL_SECONDS):
        try:
            key = get_saved_key()
            if not key:
                continue
            placeholders = ", ".join("?" * len(TERMINAL_STATES))
            with connect() as conn:
                jobs = [row["job_id"] for row in conn.execute(
                    f"SELECT job_id FROM requests WHERE job_id IS NOT NULL AND status NOT IN ({placeholders})",
                    tuple(TERMINAL_STATES),
                )]
            for job_id in jobs:
                try:
                    status, data = runpod_call("GET", f"/status/{job_id}", key)
                except (urllib.error.URLError, TimeoutError):
                    break
                if status == 200:
                    record_status(job_id, json.loads(data))
                elif status == 404:
                    with connect() as conn:
                        conn.execute(
                            "UPDATE requests SET status = 'EXPIRED', error = ?, updated_at = ? WHERE job_id = ?",
                            ("Runpod không còn giữ kết quả của job này.", now(), job_id),
                        )
        except (sqlite3.Error, ValueError) as exc:
            print(f"Poller: {exc}", flush=True)


def request_summary(row: sqlite3.Row, images: list[sqlite3.Row], full: bool = True) -> dict:
    base = f"/api/requests/{row['id']}"
    video = f"{base}/video" if row["video_file"] else row["video_url"]
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "job_id": row["job_id"],
        "status": row["status"],
        "title": row["title"],
        "prompt": row["prompt"] if full else row["prompt"][:240],
        "duration": row["duration"],
        "crf": row["crf"],
        "resolution": row["resolution"],
        "key_hint": row["key_hint"],
        "error": row["error"],
        "video": video,
        "video_name": row["video_file"],
        "delay_ms": row["delay_ms"],
        "execution_ms": row["execution_ms"],
        "continues_from": row["continues_from"],
        "images": [
            {"slot": image["slot"], "name": image["original_name"], "mime": image["mime"], "url": f"{base}/images/{image['slot']}"}
            for image in images
        ],
    }


def request_chain(row: sqlite3.Row) -> list[dict]:
    """Scenes linked by continues_from: all ancestors, this request, then the newest
    completed (else newest) continuation at each step after it."""
    chain = [row]
    seen = {row["id"]}
    with connect() as conn:
        parent_id = row["continues_from"]
        while parent_id and parent_id not in seen:
            parent = conn.execute("SELECT * FROM requests WHERE id = ?", (parent_id,)).fetchone()
            if not parent:
                break
            chain.insert(0, parent)
            seen.add(parent["id"])
            parent_id = parent["continues_from"]
        current = row
        while True:
            child = conn.execute(
                "SELECT * FROM requests WHERE continues_from = ? AND id NOT IN (%s)"
                " ORDER BY status = 'COMPLETED' DESC, created_at DESC LIMIT 1" % ",".join("?" * len(seen)),
                (current["id"], *seen),
            ).fetchone()
            if not child:
                break
            chain.append(child)
            seen.add(child["id"])
            current = child
    return [request_summary(item, [], full=False) for item in chain]


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "MiniMaxH3LocalUI/1.1"

    def _send(self, status: int, payload: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' blob: data:; media-src 'self' blob: https:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'")
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _json(self, status: int, value: dict | list) -> None:
        self._send(status, json.dumps(value, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _allowed(self) -> bool:
        """Reject other sites, including DNS-rebinding hosts that resolve to 127.0.0.1.

        Only localhost and the names passed with --allow-host (the Tailscale
        name that `tailscale serve` forwards) may reach the server.
        """
        host = urllib.parse.urlsplit(f"//{self.headers.get('Host', '')}").hostname
        origin = self.headers.get("Origin")
        origin_host = urllib.parse.urlsplit(origin).hostname if origin else None
        if host not in ALLOWED_HOSTS or (origin and origin_host not in ALLOWED_HOSTS):
            self._json(403, {"error": "Nguồn yêu cầu không hợp lệ."})
            return False
        return True

    def _key(self) -> str | None:
        key = self.headers.get("X-Runpod-Key", "").strip() or get_saved_key()
        if not key or any(c in key for c in "\r\n"):
            self._json(401, {"error": "Nhập Runpod API key trước khi gửi."})
            return None
        return key

    def _read_json(self, limit: int) -> object:
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
            raise ValueError("Request body phải là JSON.")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > limit:
            raise OverflowError
        return json.loads(self.rfile.read(length))

    def _runpod(self, method: str, path: str, key: str, body: bytes | None = None) -> tuple[int, bytes] | None:
        try:
            return runpod_call(method, path, key, body)
        except (urllib.error.URLError, TimeoutError) as exc:
            self._json(502, {"error": f"Không kết nối được Runpod: {getattr(exc, 'reason', exc)}"})
            return None

    def _send_file(self, path: Path, content_type: str) -> None:
        """Serve a stored file, honouring single byte ranges so videos can seek."""
        try:
            data = path.read_bytes()
        except OSError:
            self._json(404, {"error": "Không tìm thấy tệp."})
            return
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if match and data and match.group(0) != "bytes=-":
            first, last = match.groups()
            start = int(first) if first else max(0, len(data) - int(last))
            end = min(int(last), len(data) - 1) if first and last else len(data) - 1
            if start >= len(data) or start > end:
                self._send(416, b"", content_type, {"Content-Range": f"bytes */{len(data)}"})
                return
            self._send(206, data[start:end + 1], content_type, {"Accept-Ranges": "bytes", "Content-Range": f"bytes {start}-{end}/{len(data)}"})
            return
        self._send(200, data, content_type, {"Accept-Ranges": "bytes"})

    def _request_row(self, request_id: str) -> sqlite3.Row | None:
        if REQUEST_ID.fullmatch(request_id):
            with connect() as conn:
                row = conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
            if row:
                return row
        self._json(404, {"error": "Không tìm thấy request."})
        return None

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if not self._allowed():
            return
        if path in STATIC_FILES:
            name, content_type = STATIC_FILES[path]
            self._send(200, (WEB_DIR / name).read_bytes(), content_type)
            return
        if path == "/api/default-prompt":
            try:
                graph = json.loads(UI_WORKFLOW.read_text(encoding="utf-8"))
                prompt_node = next(node for node in graph["nodes"] if node["id"] == 190)
                prompt = prompt_node["widgets_values"][0]
                if not isinstance(prompt, str):
                    raise ValueError("Prompt gốc không phải chuỗi.")
            except (OSError, ValueError, KeyError, IndexError, StopIteration) as exc:
                self._json(500, {"error": f"Không đọc được prompt gốc: {exc}"})
                return
            self._json(200, {"prompt": prompt})
            return
        if path == "/api/workflow":
            try:
                graph = json.loads(API_WORKFLOW.read_text(encoding="utf-8"))
                if not isinstance(graph, dict) or graph.get("121", {}).get("class_type") != "MiniMaxH3ReferenceToVideo":
                    raise ValueError("Workflow API mặc định không hợp lệ.")
            except (OSError, ValueError, KeyError) as exc:
                self._json(500, {"error": f"Không đọc được workflow API: {exc}"})
                return
            self._json(200, graph)
            return
        if path == "/api/settings":
            self._json(200, {
                "api_key": get_saved_key(),
                "durations": DURATIONS,
                "execution_timeout_min": EXECUTION_TIMEOUT_MS // 60000,
            })
            return
        if path == "/api/requests":
            query = fold(urllib.parse.parse_qs(self.path.partition("?")[2]).get("q", [""])[0])
            with connect() as conn:
                rows = conn.execute("SELECT * FROM requests ORDER BY created_at DESC, rowid DESC").fetchall()
                images = conn.execute("SELECT * FROM request_images ORDER BY slot").fetchall()
            rows = [row for row in rows if query in fold(row["title"])][:500]
            by_request: dict[str, list] = {}
            for image in images:
                by_request.setdefault(image["request_id"], []).append(image)
            self._json(200, [request_summary(row, by_request.get(row["id"], []), full=False) for row in rows])
            return
        match = re.fullmatch(r"/api/requests/([^/]+)(?:/(video|chain|images/([1-4])))?", path)
        if match:
            request_id, part, slot = match.groups()
            row = self._request_row(request_id)
            if not row:
                return
            folder = FILES_DIR / request_id
            if part == "chain":
                self._json(200, request_chain(row))
                return
            if part == "video":
                if not row["video_file"]:
                    self._json(404, {"error": "Request này chưa có video lưu trên máy."})
                    return
                self._send_file(folder / row["video_file"], "video/mp4")
                return
            with connect() as conn:
                images = conn.execute("SELECT * FROM request_images WHERE request_id = ? ORDER BY slot", (request_id,)).fetchall()
            if slot:
                image = next((item for item in images if item["slot"] == int(slot)), None)
                if not image:
                    self._json(404, {"error": "Không tìm thấy ảnh."})
                    return
                self._send_file(folder / image["file"], image["mime"])
                return
            summary = request_summary(row, images)
            with connect() as conn:
                summary["next"] = [
                    {"id": child["id"], "title": child["title"], "status": child["status"]}
                    for child in conn.execute(
                        "SELECT id, title, status FROM requests WHERE continues_from = ? ORDER BY created_at", (request_id,))
                ]
            self._json(200, summary)
            return
        if path == "/api/health":
            key = self._key()
            if key and (result := self._runpod("GET", "/health", key)):
                self._send(result[0], result[1], "application/json; charset=utf-8")
            return
        if path.startswith("/api/status/"):
            job_id = path.removeprefix("/api/status/")
            if not JOB_ID.fullmatch(job_id):
                self._json(400, {"error": "Job ID không hợp lệ."})
                return
            key = self._key()
            if not key or not (result := self._runpod("GET", f"/status/{job_id}", key)):
                return
            status, data = result
            if status == 200:
                try:
                    parsed = json.loads(data)
                    record_status(job_id, parsed)
                    if parsed.get("status") == "FAILED":
                        parsed["error"] = job_error(parsed)
                        data = json.dumps(parsed, ensure_ascii=False).encode()
                except (ValueError, AttributeError, TypeError, sqlite3.Error) as exc:
                    print(f"Không lưu được trạng thái {job_id}: {exc}", flush=True)
            self._send(status, data, "application/json; charset=utf-8")
            return
        self._json(404, {"error": "Không tìm thấy đường dẫn."})

    def do_PUT(self) -> None:
        if not self._allowed():
            return
        if self.path != "/api/settings/key":
            self._json(404, {"error": "Không tìm thấy đường dẫn."})
            return
        try:
            body = self._read_json(4096)
        except (ValueError, OverflowError):
            self._json(400, {"error": "Dữ liệu API key không hợp lệ."})
            return
        key = body.get("api_key", "").strip() if isinstance(body, dict) and isinstance(body.get("api_key"), str) else ""
        if not key or any(c in key for c in "\r\n"):
            self._json(400, {"error": "API key không hợp lệ."})
            return
        with connect() as conn:
            conn.execute("INSERT INTO settings VALUES ('runpod_api_key', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key,))
        self._json(200, {"saved": True})

    def do_DELETE(self) -> None:
        if not self._allowed():
            return
        if self.path == "/api/settings/key":
            with connect() as conn:
                conn.execute("DELETE FROM settings WHERE key = 'runpod_api_key'")
            self._json(200, {"saved": False})
            return
        match = re.fullmatch(r"/api/requests/([^/]+)", self.path)
        if not match:
            self._json(404, {"error": "Không tìm thấy đường dẫn."})
            return
        row = self._request_row(match.group(1))
        if not row:
            return
        with connect() as conn:
            conn.execute("DELETE FROM requests WHERE id = ?", (row["id"],))
        shutil.rmtree(FILES_DIR / row["id"], ignore_errors=True)
        self._json(200, {"deleted": row["id"]})

    def do_POST(self) -> None:
        if not self._allowed():
            return
        if self.path != "/api/run":
            self._json(404, {"error": "Không tìm thấy đường dẫn."})
            return
        key = self._key()
        if not key:
            return
        try:
            payload = self._read_json(MAX_REQUEST_BYTES)
        except OverflowError:
            self._json(413, {"error": "Request vượt giới hạn 9,5 MB. Hãy giảm kích thước ảnh."})
            return
        except ValueError:
            self._json(400, {"error": "Request body không phải JSON hợp lệ."})
            return
        # The title is local metadata only; Runpod receives input and the policy below.
        title = payload.pop("title", "") if isinstance(payload, dict) else ""
        title = title.strip() if isinstance(title, str) else ""
        continues_from = payload.pop("continues_from", None) if isinstance(payload, dict) else None
        if continues_from is not None:
            with connect() as conn:
                known = isinstance(continues_from, str) and REQUEST_ID.fullmatch(continues_from) and conn.execute(
                    "SELECT 1 FROM requests WHERE id = ?", (continues_from,)).fetchone()
            if not known:
                self._json(400, {"error": "Không tìm thấy cảnh trước để nối tiếp."})
                return
        job_input = payload.get("input") if isinstance(payload, dict) else None
        if not isinstance(job_input, dict) or not isinstance(job_input.get("workflow"), dict) or not isinstance(job_input.get("images"), list):
            self._json(400, {"error": "Thiếu input.workflow hoặc input.images."})
            return
        duration = job_input["workflow"].get("111", {}).get("inputs", {}).get("value")
        if duration not in DURATIONS:
            self._json(400, {"error": f"Thời lượng phải là một trong {', '.join(map(str, DURATIONS))} giây."})
            return
        # The server owns the Runpod limits so the endpoint timeout is not undercut.
        payload["policy"] = {"executionTimeout": EXECUTION_TIMEOUT_MS, "ttl": JOB_TTL_MS}
        attention = job_input["workflow"].get("120")
        if isinstance(attention, dict) and attention.get("class_type") == "ModelAttentionBackend":
            attention.setdefault("inputs", {})["attention"] = "pytorch attention"
        body = json.dumps(payload, ensure_ascii=False).encode()
        if len(body) > MAX_REQUEST_BYTES:
            self._json(413, {"error": "Request vượt giới hạn 9,5 MB. Hãy giảm kích thước ảnh."})
            return
        try:
            request_id = record_submission(job_input, key, title, continues_from)
        except (ValueError, binascii.Error, OSError, sqlite3.Error) as exc:
            self._json(400, {"error": f"Không lưu được request: {exc}"})
            return
        result = self._runpod("POST", "/run", key, body)
        if not result:
            update_request(request_id, status="SUBMIT_FAILED", error="Không kết nối được Runpod.")
            return
        status, data = result
        try:
            parsed = json.loads(data)
        except ValueError:
            parsed = {}
        job_id = parsed.get("id") if isinstance(parsed, dict) else None
        if status == 200 and isinstance(job_id, str) and JOB_ID.fullmatch(job_id):
            try:
                update_request(request_id, job_id=job_id, status=str(parsed.get("status") or "IN_QUEUE").upper())
                parsed["request_id"] = request_id
            except sqlite3.Error as exc:
                # Runpod already accepted the job; still hand its ID to the browser.
                print(f"Không lưu được job {job_id}: {exc}", flush=True)
            self._json(status, parsed)
            return
        update_request(request_id, status="SUBMIT_FAILED", error=f"HTTP {status}: {data[:500].decode(errors='replace')}")
        self._send(status, data, "application/json; charset=utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="MiniMax H3 local Runpod request UI")
    parser.add_argument("--port", type=int, default=int(os.environ.get("H3_PORT", "8765")))
    parser.add_argument(
        "--allow-host", action="append", default=[],
        help="Extra hostname allowed in Host/Origin, e.g. the Tailscale name used by `tailscale serve`. Repeatable.",
    )
    args = parser.parse_args()
    extra = [*args.allow_host, *os.environ.get("H3_ALLOWED_HOSTS", "").split(",")]
    ALLOWED_HOSTS.update(name.strip().lower().rstrip(".") for name in extra if name.strip())
    init_db()
    stop = threading.Event()
    threading.Thread(target=poll_pending, args=(stop,), daemon=True).start()
    # Always bind to loopback; remote access goes through `tailscale serve`.
    with ThreadingHTTPServer(("127.0.0.1", args.port), RequestHandler) as server:
        print(f"Mở giao diện: http://127.0.0.1:{args.port}", flush=True)
        for name in sorted(ALLOWED_HOSTS - {"127.0.0.1", "localhost"}):
            print(f"Cho phép truy cập qua: https://{name}", flush=True)
        print(f"Dữ liệu lưu tại: {DATA_DIR}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()


if __name__ == "__main__":
    main()
