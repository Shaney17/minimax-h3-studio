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
import subprocess
import threading
import time
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
PROJECTS_DIR = DATA_DIR / "projects"
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
FPS = 24
PROJECT_TICK_SECONDS = 5
MAX_SCENES = 200
# A failed scene is submitted this many times in total before the film pauses.
SCENE_ATTEMPTS = 2
PROJECT_STATES = {"DRAFT", "RUNNING", "PAUSED", "FAILED", "COMPLETED"}
# Frame anchor wording from MiniMax's official Ref2VA prompt guide (ref-en.txt);
# web/app.js adds the same lines for manually continued shots.
ANCHOR_LABEL = "<Picture 4>"
ANCHOR_DEFINITION = (
    "<Picture 4> is the first frame of [Shot 1]: the final frame of the previous scene. The target video starts "
    "exactly from this frame, preserving its composition, character positions and poses, lighting, and camera "
    "framing, then continues the action naturally from that state."
)
ANCHOR_RETENTION = (
    "<Picture 4> ([Shot 1] first frame): fully_preserved - the composition, character positions, poses, lighting, "
    "and framing of the previous scene's final frame are kept at 0.00 seconds."
)

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
    continues_from TEXT,
    project_id TEXT,
    scene_index INTEGER
);
CREATE TABLE IF NOT EXISTS request_images (
    request_id TEXT NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
    slot INTEGER NOT NULL,
    file TEXT NOT NULL,
    original_name TEXT NOT NULL,
    mime TEXT NOT NULL,
    PRIMARY KEY (request_id, slot)
);
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    subjects TEXT NOT NULL,
    style TEXT NOT NULL DEFAULT '',
    soundscape TEXT NOT NULL DEFAULT '',
    music TEXT NOT NULL DEFAULT '',
    crf INTEGER NOT NULL,
    resolution REAL NOT NULL,
    current_index INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    film_file TEXT
);
CREATE TABLE IF NOT EXISTS project_images (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    slot INTEGER NOT NULL,
    file TEXT NOT NULL,
    original_name TEXT NOT NULL,
    mime TEXT NOT NULL,
    PRIMARY KEY (project_id, slot)
);
CREATE TABLE IF NOT EXISTS project_scenes (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    duration INTEGER NOT NULL,
    description TEXT NOT NULL,
    soundscape TEXT NOT NULL DEFAULT '',
    music TEXT NOT NULL DEFAULT '',
    request_id TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (project_id, idx)
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
    PROJECTS_DIR.mkdir(mode=0o700, exist_ok=True)
    with connect() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(requests)")}
        if "title" not in columns:
            conn.execute("ALTER TABLE requests ADD COLUMN title TEXT NOT NULL DEFAULT ''")
        for column, kind in (
            ("delay_ms", "INTEGER"), ("execution_ms", "INTEGER"), ("continues_from", "TEXT"),
            ("project_id", "TEXT"), ("scene_index", "INTEGER"),
        ):
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


def record_submission(
    job_input: dict, key: str, title: str, continues_from: str | None = None,
    project_id: str | None = None, scene_index: int | None = None,
) -> str:
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
                "INSERT INTO requests (id, created_at, updated_at, status, title, prompt, duration, crf, resolution, key_hint,"
                " workflow, continues_from, project_id, scene_index) VALUES (?, ?, ?, 'SUBMITTING', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    request_id, now(), now(), title[:200] or default_title(prompt), prompt,
                    graph.get("111", {}).get("inputs", {}).get("value"),
                    graph.get("154", {}).get("inputs", {}).get("crf"),
                    0.2 if megapixels == 0.2 else 1,
                    f"…{key[-4:]}",
                    json.dumps(graph, ensure_ascii=False),
                    continues_from,
                    project_id,
                    scene_index,
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


class SubmitError(Exception):
    def __init__(self, status: int, message: str, request_id: str | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.request_id = request_id


def submit_job(
    payload: dict, key: str, title: str, continues_from: str | None = None,
    project_id: str | None = None, scene_index: int | None = None,
) -> dict:
    """Validate, store, and send one job to Runpod; return Runpod's reply plus request_id.

    Raises SubmitError. Its request_id is set when the request was stored before failing.
    """
    job_input = payload.get("input")
    if not isinstance(job_input, dict) or not isinstance(job_input.get("workflow"), dict) or not isinstance(job_input.get("images"), list):
        raise SubmitError(400, "Thiếu input.workflow hoặc input.images.")
    duration = job_input["workflow"].get("111", {}).get("inputs", {}).get("value")
    if duration not in DURATIONS:
        raise SubmitError(400, f"Thời lượng phải là một trong {', '.join(map(str, DURATIONS))} giây.")
    # The server owns the Runpod limits so the endpoint timeout is not undercut.
    payload["policy"] = {"executionTimeout": EXECUTION_TIMEOUT_MS, "ttl": JOB_TTL_MS}
    attention = job_input["workflow"].get("120")
    if isinstance(attention, dict) and attention.get("class_type") == "ModelAttentionBackend":
        attention.setdefault("inputs", {})["attention"] = "pytorch attention"
    body = json.dumps(payload, ensure_ascii=False).encode()
    if len(body) > MAX_REQUEST_BYTES:
        raise SubmitError(413, "Request vượt giới hạn 9,5 MB. Hãy giảm kích thước ảnh.")
    try:
        request_id = record_submission(job_input, key, title, continues_from, project_id, scene_index)
    except (ValueError, binascii.Error, OSError, sqlite3.Error) as exc:
        raise SubmitError(400, f"Không lưu được request: {exc}") from exc
    try:
        status, data = runpod_call("POST", "/run", key, body)
    except (urllib.error.URLError, TimeoutError) as exc:
        update_request(request_id, status="SUBMIT_FAILED", error="Không kết nối được Runpod.")
        raise SubmitError(502, f"Không kết nối được Runpod: {getattr(exc, 'reason', exc)}", request_id) from exc
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
            # Runpod already accepted the job; still hand its ID back.
            print(f"Không lưu được job {job_id}: {exc}", flush=True)
        return parsed
    detail = parsed.get("error") or parsed.get("message") if isinstance(parsed, dict) else None
    message = f"HTTP {status}: {detail or data[:500].decode(errors='replace')}"
    update_request(request_id, status="SUBMIT_FAILED", error=message)
    raise SubmitError(status if status >= 400 else 502, message, request_id)


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
        "project_id": row["project_id"],
        "scene_index": row["scene_index"],
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


# ---------------------------------------------------------------- media (ffmpeg)


def ffmpeg_tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f"Máy chủ chưa cài {name}. Chạy lại scripts/install.sh để cài ffmpeg.")
    return path


def last_frame_png(request_id: str) -> Path:
    """Final frame of a request's stored video as PNG, cached next to the video."""
    with connect() as conn:
        row = conn.execute("SELECT video_file FROM requests WHERE id = ?", (request_id,)).fetchone()
    if not row or not row["video_file"]:
        raise ValueError("Request này chưa có video lưu trên máy.")
    video = FILES_DIR / request_id / row["video_file"]
    out = FILES_DIR / request_id / "last_frame.png"
    if out.is_file() and out.stat().st_mtime >= video.stat().st_mtime:
        return out
    # Decode only the last second; `-update 1` keeps overwriting one image, so the
    # file ends up holding the final decoded frame.
    subprocess.run(
        [ffmpeg_tool("ffmpeg"), "-v", "error", "-y", "-sseof", "-1", "-i", str(video), "-update", "1", str(out)],
        check=True, capture_output=True, timeout=120,
    )
    if not out.is_file():
        raise RuntimeError("ffmpeg không tạo được ảnh khung cuối.")
    return out


def probe_video(path: Path) -> dict:
    result = subprocess.run(
        [ffmpeg_tool("ffprobe"), "-v", "error", "-show_entries", "stream=codec_type,width,height,r_frame_rate",
         "-of", "json", str(path)],
        check=True, capture_output=True, timeout=60,
    )
    streams = json.loads(result.stdout).get("streams", [])
    video = next((item for item in streams if item.get("codec_type") == "video"), None)
    if not video:
        raise RuntimeError(f"{path.name} không có luồng video.")
    return {"width": video["width"], "height": video["height"], "audio": any(i.get("codec_type") == "audio" for i in streams)}


def concat_scenes(videos: list[Path], out: Path) -> None:
    """Join scene videos into one MP4. Every scene after the first starts on its
    anchor, which repeats the previous scene's last frame, so drop that frame."""
    info = [probe_video(path) for path in videos]
    width, height = info[0]["width"], info[0]["height"]
    with_audio = all(item["audio"] for item in info)
    parts, labels = [], []
    for index in range(len(videos)):
        trim_video = "trim=start_frame=1," if index else ""
        parts.append(f"[{index}:v]{trim_video}setpts=PTS-STARTPTS,scale={width}:{height},setsar=1,fps={FPS}[v{index}]")
        labels.append(f"[v{index}]")
        if with_audio:
            trim_audio = f"atrim=start={1 / FPS:.6f}," if index else ""
            parts.append(f"[{index}:a]{trim_audio}asetpts=PTS-STARTPTS[a{index}]")
            labels.append(f"[a{index}]")
    parts.append(f"{''.join(labels)}concat=n={len(videos)}:v=1:a={1 if with_audio else 0}[v]" + ("[a]" if with_audio else ""))
    command = [ffmpeg_tool("ffmpeg"), "-v", "error", "-y"]
    for path in videos:
        command += ["-i", str(path)]
    command += ["-filter_complex", ";".join(parts), "-map", "[v]"]
    if with_audio:
        command += ["-map", "[a]", "-c:a", "aac", "-b:a", "192k"]
    command += ["-c:v", "libx264", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)]
    tmp = out.with_suffix(".part.mp4")
    command[-1] = str(tmp)
    subprocess.run(command, check=True, capture_output=True, timeout=3600)
    tmp.replace(out)


# ---------------------------------------------------------------- workflow and prompts


def add_frame_anchor(graph: dict, name: str) -> None:
    """Anchor frame 0 to image `name`; mirrors addFrameAnchor in web/app.js.

    LoadImage 300 is also ref_image_3 (<Picture 4>). Stage 1 (guider 145) guides the
    base latent from node 121; stage 2 (guider 146) needs its own guide on the
    upscaled latent from node 157, because MiniMaxH3AddGuide encodes the image at
    the size of the latent it receives.
    """
    graph["300"] = {"class_type": "LoadImage", "inputs": {"image": name}, "_meta": {"title": "Frame anchor"}}
    graph["121"]["inputs"]["ref_images.ref_image_3"] = ["300", 0]
    for node, latent, stage in (("301", ["121", 1], 1), ("302", ["157", 0], 2)):
        graph[node] = {
            "class_type": "MiniMaxH3AddGuide",
            "inputs": {"positive": ["121", 0], "vae": ["4", 0], "latent": latent, "image": ["300", 0], "frame_idx": 0},
            "_meta": {"title": f"Anchor frame 0 · stage {stage}"},
        }
    graph["145"]["inputs"]["conditioning"] = ["301", 0]
    graph["146"]["inputs"]["conditioning"] = ["302", 0]


def build_workflow(prompt: str, duration: int, crf: int, resolution: float, names: list[str], anchor: str | None) -> dict:
    """The bundled API graph with the same edits web/app.js makes for one request."""
    graph = json.loads(API_WORKFLOW.read_text(encoding="utf-8"))
    graph["190"]["inputs"]["value"] = prompt
    graph["111"]["inputs"]["value"] = duration
    graph["154"]["inputs"]["crf"] = crf
    if resolution == 0.2:
        graph["112"]["inputs"]["megapixels"] = 0.2
        graph["102"]["inputs"]["value"] = 0.2
    for node, name in zip(("28", "29", "30"), names):
        graph[node]["inputs"]["image"] = name
    if anchor:
        add_frame_anchor(graph, anchor)
    return graph


PROMPT_FIELDS = ("subject_definitions", "summary", "retention_analysis", "detailed_description",
                 "overall_soundscape", "non_diegetic_music")


def prompt_sections(prompt: str) -> dict[str, str]:
    """Split a six-field H3 prompt into its sections (header lines may carry extra text)."""
    pattern = re.compile(r"^(%s)\b[^\n]*\n" % "|".join(PROMPT_FIELDS), re.M)
    marks = list(pattern.finditer(prompt))
    return {
        mark.group(1): prompt[mark.end(): marks[i + 1].start() if i + 1 < len(marks) else len(prompt)].strip()
        for i, mark in enumerate(marks)
    }


def project_template() -> dict:
    """Starting values for a new film, taken from the bundled workflow's prompt."""
    graph = json.loads(API_WORKFLOW.read_text(encoding="utf-8"))
    sections = prompt_sections(graph["190"]["inputs"]["value"])
    description = sections.get("detailed_description", "")
    style = description.split("[Shot 1]", 1)[0].strip()
    return {
        "subjects": sections.get("subject_definitions", ""),
        "style": style,
        "soundscape": sections.get("overall_soundscape", ""),
        "music": sections.get("non_diegetic_music", ""),
    }


def first_sentence(text: str) -> str:
    plain = re.sub(r"\[Shot \d+\](?: At [0-9:.]+,?)?", " ", text)
    plain = re.sub(r"<d>.*?</d>", " ", plain, flags=re.S)
    plain = " ".join(plain.split())
    match = re.match(r"(.+?[.!?])(\s|$)", plain)
    return (match.group(1) if match else plain)[:400]


def compose_scene_prompt(project: sqlite3.Row, scene: sqlite3.Row, index: int, total: int) -> str:
    """Six-field Ref2VA prompt for one scene; scenes after the first start on <Picture 4>."""
    anchored = index > 0
    subjects = project["subjects"].strip()
    labels = list(dict.fromkeys(re.findall(r"<Subject \d+>", subjects)))
    definitions = subjects + (f"\n{ANCHOR_DEFINITION}" if anchored else "")
    task = "[reference generation + keyframe completion]" if anchored else "[reference generation]"
    summary = (
        f"{task} This is scene {index + 1} of {total} of one continuous film"
        + (", starting from <Picture 4>, the final frame of the previous scene. " if anchored else ". ")
        + first_sentence(scene["description"])
    )
    retention = [
        f"{label} (appears where described): fully_preserved - the identity, appearance, costume, and design "
        f"defined for {label} are retained."
        for label in labels
    ]
    if anchored:
        retention.append(ANCHOR_RETENTION)
    body = scene["description"].strip()
    if not body.startswith("[Shot 1]"):
        body = f"[Shot 1] {body}"
    if anchored:
        body = body.replace(
            "[Shot 1]",
            "[Shot 1] The shot begins from <Picture 4>, continuing seamlessly from the previous scene with the same "
            "framing, character positions, poses, and lighting.",
            1,
        )
    style = project["style"].strip()
    soundscape = scene["soundscape"].strip() or project["soundscape"].strip() or "Quiet room tone."
    music = scene["music"].strip() or project["music"].strip() or "N/A"
    if anchored:
        soundscape = f"The ambience continues seamlessly from the previous scene. {soundscape}"
        if music != "N/A":
            music = f"The score continues from the previous scene without restarting. {music}"
    return "\n\n".join([
        f"subject_definitions:\n{definitions}",
        f"summary:\n{summary}",
        "retention_analysis:\n" + "\n".join(retention),
        "detailed_description:\n" + (f"{style}\n" if style else "") + body,
        f"overall_soundscape:\n{soundscape}",
        f"non_diegetic_music:\n{music}",
    ])


# ---------------------------------------------------------------- film projects


PROJECT_LOCK = threading.Lock()


def update_project(project_id: str, **fields) -> None:
    fields["updated_at"] = now()
    columns = ", ".join(f"{name} = ?" for name in fields)
    with connect() as conn:
        conn.execute(f"UPDATE projects SET {columns} WHERE id = ?", (*fields.values(), project_id))


def project_scenes(conn: sqlite3.Connection, project_id: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM project_scenes WHERE project_id = ? ORDER BY idx", (project_id,)).fetchall()


def data_url(path: Path, mime: str) -> str:
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def submit_scene(project: sqlite3.Row, scenes: list[sqlite3.Row], index: int, key: str) -> None:
    """Build and send one scene; from the second scene on, anchor it to the previous video's last frame."""
    scene = scenes[index]
    folder = PROJECTS_DIR / project["id"]
    stamp = int(time.time() * 1000)
    with connect() as conn:
        refs = conn.execute("SELECT * FROM project_images WHERE project_id = ? ORDER BY slot", (project["id"],)).fetchall()
        conn.execute("UPDATE project_scenes SET attempts = attempts + 1 WHERE project_id = ? AND idx = ?", (project["id"], index))
    images, names = [], []
    for ref in refs:
        name = f"h3_ref_{ref['slot']}_{stamp}_{re.sub(r'[^A-Za-z0-9._-]', '_', ref['original_name'])}"
        names.append(name)
        images.append({"name": name, "image": data_url(folder / ref["file"], ref["mime"])})
    anchor = previous = None
    if index:
        previous = scenes[index - 1]["request_id"]
        anchor = f"h3_ref_4_{stamp}_last_frame_scene_{index}.png"
        images.append({"name": anchor, "image": data_url(last_frame_png(previous), "image/png")})
    prompt = compose_scene_prompt(project, scene, index, len(scenes))
    graph = build_workflow(prompt, scene["duration"], project["crf"], project["resolution"], names, anchor)
    title = f"{project['title']} · cảnh {index + 1}/{len(scenes)}"
    request_id = None
    try:
        request_id = submit_job({"input": {"workflow": graph, "images": images}}, key, title, previous,
                                project["id"], index)["request_id"]
    except SubmitError as exc:
        request_id = exc.request_id
        if not request_id:
            raise
    finally:
        if request_id:
            with connect() as conn:
                conn.execute("UPDATE project_scenes SET request_id = ? WHERE project_id = ? AND idx = ?",
                             (request_id, project["id"], index))


def advance_project(project_id: str) -> None:
    """One step of the film state machine; called repeatedly by the runner thread."""
    with PROJECT_LOCK:
        with connect() as conn:
            project = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
            if not project or project["status"] != "RUNNING":
                return
            scenes = project_scenes(conn, project_id)
            index = project["current_index"]
            request = None
            if index < len(scenes) and scenes[index]["request_id"]:
                request = conn.execute("SELECT * FROM requests WHERE id = ?", (scenes[index]["request_id"],)).fetchone()
        try:
            if index >= len(scenes):
                videos = []
                for scene in scenes:
                    with connect() as conn:
                        row = conn.execute("SELECT video_file FROM requests WHERE id = ?", (scene["request_id"],)).fetchone()
                    videos.append(FILES_DIR / scene["request_id"] / row["video_file"])
                (PROJECTS_DIR / project_id).mkdir(mode=0o700, exist_ok=True)
                concat_scenes(videos, PROJECTS_DIR / project_id / "film.mp4")
                update_project(project_id, status="COMPLETED", film_file="film.mp4", error=None)
                return
            scene = scenes[index]
            if request is None or (request["status"] in TERMINAL_STATES and request["status"] != "COMPLETED"):
                if scene["attempts"] >= SCENE_ATTEMPTS:
                    reason = request["error"] if request is not None and request["error"] else "Job không hoàn tất."
                    update_project(project_id, status="FAILED", error=f"Cảnh {index + 1} lỗi sau {SCENE_ATTEMPTS} lần thử: {reason}")
                    return
                key = get_saved_key()
                if not key:
                    update_project(project_id, status="FAILED", error="Chưa lưu Runpod API key trong Cài đặt.")
                    return
                submit_scene(project, scenes, index, key)
                return
            if request["status"] == "COMPLETED":
                if not request["video_file"]:
                    update_project(project_id, status="FAILED",
                                   error=f"Cảnh {index + 1} chỉ có video trên S3; cần video lưu trên máy để nối cảnh.")
                    return
                update_project(project_id, current_index=index + 1, error=None)
        except (SubmitError, ValueError, RuntimeError, OSError, subprocess.SubprocessError, sqlite3.Error) as exc:
            detail = exc.message if isinstance(exc, SubmitError) else str(exc)
            if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
                detail = exc.stderr.decode(errors="replace")[-800:]
            where = "Ghép phim" if index >= len(scenes) else f"Cảnh {index + 1}"
            update_project(project_id, status="FAILED", error=f"{where}: {detail}")


def run_projects(stop: threading.Event) -> None:
    """Advance running films in the background, so no browser needs to stay open."""
    while not stop.wait(PROJECT_TICK_SECONDS):
        try:
            with connect() as conn:
                running = [row["id"] for row in conn.execute("SELECT id FROM projects WHERE status = 'RUNNING'")]
            for project_id in running:
                advance_project(project_id)
        except sqlite3.Error as exc:
            print(f"Runner: {exc}", flush=True)


def clean_project(body: object) -> dict:
    """Validate a film create/update body; images are optional here and checked by the caller."""
    if not isinstance(body, dict):
        raise ValueError("Dữ liệu phim không hợp lệ.")
    text = lambda name, limit: (body.get(name) or "").strip()[:limit] if isinstance(body.get(name, ""), str) else ""
    cleaned = {
        "title": text("title", 200) or "Phim chưa đặt tên",
        "subjects": text("subjects", 20000),
        "style": text("style", 5000),
        "soundscape": text("soundscape", 5000),
        "music": text("music", 5000),
        "crf": body.get("crf"),
        "resolution": body.get("resolution"),
    }
    if not cleaned["subjects"]:
        raise ValueError("Hãy nhập mô tả nhân vật và bối cảnh (subject_definitions).")
    if ANCHOR_LABEL in cleaned["subjects"]:
        raise ValueError(f"{ANCHOR_LABEL} được dành cho khung neo; đừng dùng nó trong mô tả nhân vật.")
    if cleaned["crf"] not in (15, 28) or cleaned["resolution"] not in (0.2, 1):
        raise ValueError("Thiết lập video không hợp lệ.")
    scenes = body.get("scenes")
    if not isinstance(scenes, list) or not 1 <= len(scenes) <= MAX_SCENES:
        raise ValueError(f"Phim cần từ 1 đến {MAX_SCENES} phân cảnh.")
    cleaned["scenes"] = []
    for number, scene in enumerate(scenes, 1):
        if not isinstance(scene, dict):
            raise ValueError(f"Phân cảnh {number} không hợp lệ.")
        get = lambda name: (scene.get(name) or "").strip() if isinstance(scene.get(name, ""), str) else ""
        if scene.get("duration") not in DURATIONS:
            raise ValueError(f"Phân cảnh {number}: thời lượng phải là một trong {', '.join(map(str, DURATIONS))} giây.")
        if not get("description"):
            raise ValueError(f"Phân cảnh {number} chưa có mô tả.")
        cleaned["scenes"].append({
            "duration": scene["duration"], "description": get("description")[:20000],
            "soundscape": get("soundscape")[:5000], "music": get("music")[:5000],
        })
    images = body.get("images") or []
    if not isinstance(images, list):
        raise ValueError("Ảnh tham chiếu không hợp lệ.")
    cleaned["images"] = []
    for item in images:
        slot = item.get("slot") if isinstance(item, dict) else None
        match = DATA_URL.match(item.get("image", "")) if slot in (1, 2, 3) else None
        if not match:
            raise ValueError("Ảnh tham chiếu phải là PNG, JPG hoặc WebP.")
        name = item.get("name") if isinstance(item.get("name"), str) else f"ref_{slot}"
        cleaned["images"].append({"slot": slot, "name": name[:200], "mime": match.group(1),
                                  "data": base64.b64decode(match.group(2), validate=True)})
    return cleaned


def locked_scene_count(conn: sqlite3.Connection, project: sqlite3.Row) -> int:
    """Scenes that are done or still rendering and therefore cannot be edited."""
    scenes = project_scenes(conn, project["id"])
    locked = project["current_index"]
    if locked < len(scenes) and scenes[locked]["request_id"]:
        row = conn.execute("SELECT status FROM requests WHERE id = ?", (scenes[locked]["request_id"],)).fetchone()
        if row and (row["status"] == "COMPLETED" or row["status"] not in TERMINAL_STATES):
            locked += 1
    return locked


def save_project(project_id: str | None, cleaned: dict) -> str:
    """Create a film, or update one that is not running. After it has started,
    shared settings and finished scenes stay as they were."""
    with connect() as conn:
        existing = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone() if project_id else None
        if project_id and not existing:
            raise LookupError("Không tìm thấy phim.")
        if existing and existing["status"] == "RUNNING":
            raise PermissionError("Phim đang được tạo. Hãy dừng trước khi sửa.")
        started = bool(existing) and (existing["current_index"] > 0 or any(
            scene["request_id"] for scene in project_scenes(conn, existing["id"])))
        locked = locked_scene_count(conn, existing) if existing else 0
        old_scenes = project_scenes(conn, existing["id"]) if existing else []
    if not existing and len({image["slot"] for image in cleaned["images"]}) != 3:
        raise ValueError("Hãy chọn đủ 3 ảnh tham chiếu.")
    if started and cleaned["images"]:
        raise PermissionError("Không đổi được ảnh tham chiếu sau khi phim đã bắt đầu.")
    project_id = project_id or uuid.uuid4().hex
    folder = PROJECTS_DIR / project_id
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    for image in cleaned["images"]:
        file = f"ref_{image['slot']}{IMAGE_EXT[image['mime']]}"
        (folder / file).write_bytes(image["data"])
        image["file"] = file
    shared = ("title",) if started else ("title", "subjects", "style", "soundscape", "music", "crf", "resolution")
    scenes = [dict(scene) for scene in old_scenes[:locked]] + cleaned["scenes"][locked:]
    if len(scenes) < max(locked, 1):
        raise ValueError("Không xoá được các phân cảnh đã tạo xong hoặc đang tạo.")
    with connect() as conn:
        if existing:
            values = {name: cleaned[name] for name in shared}
            columns = ", ".join(f"{name} = ?" for name in values)
            conn.execute(f"UPDATE projects SET {columns}, updated_at = ? WHERE id = ?", (*values.values(), now(), project_id))
        else:
            conn.execute(
                "INSERT INTO projects (id, created_at, updated_at, title, status, subjects, style, soundscape, music, crf, resolution)"
                " VALUES (?, ?, ?, ?, 'DRAFT', ?, ?, ?, ?, ?, ?)",
                (project_id, now(), now(), cleaned["title"], cleaned["subjects"], cleaned["style"], cleaned["soundscape"],
                 cleaned["music"], cleaned["crf"], cleaned["resolution"]),
            )
        for image in cleaned["images"]:
            conn.execute("INSERT OR REPLACE INTO project_images VALUES (?, ?, ?, ?, ?)",
                         (project_id, image["slot"], image["file"], image["name"], image["mime"]))
        conn.execute("DELETE FROM project_scenes WHERE project_id = ? AND idx >= ?", (project_id, locked))
        for index, scene in enumerate(scenes[locked:], locked):
            conn.execute(
                "INSERT INTO project_scenes (project_id, idx, duration, description, soundscape, music) VALUES (?, ?, ?, ?, ?, ?)",
                (project_id, index, scene["duration"], scene["description"], scene["soundscape"], scene["music"]),
            )
        # A finished film whose scene list changed needs to render again.
        conn.execute("UPDATE projects SET film_file = NULL, status = CASE WHEN status = 'COMPLETED' THEN 'PAUSED' ELSE status END"
                     " WHERE id = ? AND current_index < (SELECT COUNT(*) FROM project_scenes WHERE project_id = ?)",
                     (project_id, project_id))
    return project_id


def project_summary(conn: sqlite3.Connection, project: sqlite3.Row, full: bool = False) -> dict:
    scenes = project_scenes(conn, project["id"])
    base = f"/api/projects/{project['id']}"
    summary = {
        "id": project["id"], "title": project["title"], "status": project["status"],
        "created_at": project["created_at"], "updated_at": project["updated_at"],
        "current_index": project["current_index"], "scene_count": len(scenes),
        "total_duration": sum(scene["duration"] for scene in scenes), "error": project["error"],
        "film": f"{base}/film" if project["film_file"] else None,
    }
    if not full:
        return summary
    images = conn.execute("SELECT * FROM project_images WHERE project_id = ? ORDER BY slot", (project["id"],)).fetchall()
    summary.update({
        "subjects": project["subjects"], "style": project["style"], "soundscape": project["soundscape"],
        "music": project["music"], "crf": project["crf"], "resolution": project["resolution"],
        "locked_scenes": locked_scene_count(conn, project),
        "images": [{"slot": image["slot"], "name": image["original_name"], "mime": image["mime"],
                    "url": f"{base}/images/{image['slot']}"} for image in images],
        "scenes": [],
    })
    for index, scene in enumerate(scenes):
        request = conn.execute("SELECT * FROM requests WHERE id = ?", (scene["request_id"],)).fetchone() if scene["request_id"] else None
        summary["scenes"].append({
            "index": index, "duration": scene["duration"], "description": scene["description"],
            "soundscape": scene["soundscape"], "music": scene["music"], "attempts": scene["attempts"],
            "prompt": compose_scene_prompt(project, scene, index, len(scenes)),
            "request": request_summary(request, [], full=False) if request else None,
        })
    return summary


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
        if path == "/api/project-template":
            self._json(200, project_template())
            return
        if path == "/api/projects":
            with connect() as conn:
                rows = conn.execute("SELECT * FROM projects ORDER BY created_at DESC").fetchall()
                self._json(200, [project_summary(conn, row) for row in rows])
            return
        match = re.fullmatch(r"/api/projects/([a-f0-9]{32})(?:/(film|images/([123])))?", path)
        if match:
            project_id, part, slot = match.groups()
            with connect() as conn:
                project = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
                if not project:
                    self._json(404, {"error": "Không tìm thấy phim."})
                    return
                if part == "film":
                    if not project["film_file"]:
                        self._json(404, {"error": "Phim chưa ghép xong."})
                    else:
                        self._send_file(PROJECTS_DIR / project_id / project["film_file"], "video/mp4")
                    return
                if slot:
                    image = conn.execute("SELECT * FROM project_images WHERE project_id = ? AND slot = ?",
                                         (project_id, int(slot))).fetchone()
                    if not image:
                        self._json(404, {"error": "Không tìm thấy ảnh."})
                    else:
                        self._send_file(PROJECTS_DIR / project_id / image["file"], image["mime"])
                    return
                self._json(200, project_summary(conn, project, full=True))
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
        match = re.fullmatch(r"/api/requests/([^/]+)(?:/(video|chain|last-frame|images/([1-4])))?", path)
        if match:
            request_id, part, slot = match.groups()
            row = self._request_row(request_id)
            if not row:
                return
            folder = FILES_DIR / request_id
            if part == "chain":
                self._json(200, request_chain(row))
                return
            if part == "last-frame":
                try:
                    frame = last_frame_png(request_id)
                except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
                    self._json(409, {"error": f"Không lấy được khung cuối: {exc}"})
                    return
                self._send_file(frame, "image/png")
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

    def _save_project(self, project_id: str | None) -> None:
        try:
            cleaned = clean_project(self._read_json(MAX_REQUEST_BYTES))
            saved = save_project(project_id, cleaned)
        except OverflowError:
            self._json(413, {"error": "Dữ liệu vượt giới hạn 9,5 MB. Hãy giảm kích thước ảnh."})
            return
        except LookupError as exc:
            self._json(404, {"error": str(exc)})
            return
        except PermissionError as exc:
            self._json(409, {"error": str(exc)})
            return
        except (ValueError, binascii.Error) as exc:
            self._json(400, {"error": str(exc) or "Dữ liệu phim không hợp lệ."})
            return
        except (OSError, sqlite3.Error) as exc:
            self._json(500, {"error": f"Không lưu được phim: {exc}"})
            return
        with connect() as conn:
            self._json(200, project_summary(conn, conn.execute("SELECT * FROM projects WHERE id = ?", (saved,)).fetchone(), full=True))

    def do_PUT(self) -> None:
        if not self._allowed():
            return
        match = re.fullmatch(r"/api/projects/([a-f0-9]{32})", self.path)
        if match:
            self._save_project(match.group(1))
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
        match = re.fullmatch(r"/api/projects/([a-f0-9]{32})", self.path)
        if match:
            with connect() as conn:
                deleted = conn.execute("DELETE FROM projects WHERE id = ?", (match.group(1),)).rowcount
            if not deleted:
                self._json(404, {"error": "Không tìm thấy phim."})
                return
            # Scene requests stay in the request history.
            shutil.rmtree(PROJECTS_DIR / match.group(1), ignore_errors=True)
            self._json(200, {"deleted": match.group(1)})
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
        if self.path == "/api/projects":
            self._save_project(None)
            return
        match = re.fullmatch(r"/api/projects/([a-f0-9]{32})/(start|stop)", self.path)
        if match:
            project_id, action = match.groups()
            # No PROJECT_LOCK here: the runner may be busy joining a long film, and a
            # status change is picked up on its next step.
            with connect() as conn:
                project = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
                if not project:
                    self._json(404, {"error": "Không tìm thấy phim."})
                    return
                if action == "start":
                    if project["status"] in ("RUNNING", "COMPLETED"):
                        self._json(409, {"error": "Phim đang chạy hoặc đã hoàn tất."})
                        return
                    if not get_saved_key():
                        self._json(409, {"error": "Chưa lưu Runpod API key trong Cài đặt."})
                        return
                    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
                        self._json(409, {"error": "Máy chủ chưa cài ffmpeg. Chạy lại scripts/install.sh."})
                        return
                    # A retry after a failure gets fresh attempts for the current scene.
                    conn.execute("UPDATE project_scenes SET attempts = 0 WHERE project_id = ? AND idx = ?",
                                 (project_id, project["current_index"]))
                    conn.execute("UPDATE projects SET status = 'RUNNING', error = NULL, updated_at = ? WHERE id = ?", (now(), project_id))
                elif project["status"] == "RUNNING":
                    conn.execute("UPDATE projects SET status = 'PAUSED', updated_at = ? WHERE id = ?", (now(), project_id))
                self._json(200, project_summary(conn, conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone(), full=True))
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
        try:
            self._json(200, submit_job(payload, key, title, continues_from))
        except SubmitError as exc:
            self._json(exc.status, {"error": exc.message})


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
    threading.Thread(target=run_projects, args=(stop,), daemon=True).start()
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
