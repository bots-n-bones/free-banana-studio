"""Hosted studio dashboard, job queue and image gallery."""

import json
import base64
import binascii
import hashlib
import hmac
import os
import re
import secrets
import threading
import time
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from PIL import Image


ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("STUDIO_DATA_DIR", "/data"))
GALLERY = DATA / "gallery"
REFS = DATA / "refs"
JOBS = DATA / "jobs.json"
WORKER = DATA / "worker.json"
KEY = os.environ["STUDIO_WORKER_KEY"]
PASSWORD = os.environ["STUDIO_PASSWORD"]
SESSION_KEY = os.environ["STUDIO_SESSION_KEY"].encode()
if min(len(KEY), len(PASSWORD), len(SESSION_KEY)) < 24:
    raise RuntimeError("Studio credentials must be at least 24 characters")
PASSWORD_SALT = secrets.token_bytes(16)
PASSWORD_HASH = hashlib.pbkdf2_hmac("sha256", PASSWORD.encode(), PASSWORD_SALT, 250_000)
del PASSWORD
LOCK = threading.RLock()
ASPECTS = {"16:9", "4:3", "1:1", "3:4", "9:16"}
ERRORS = {"timeout", "generation_failed", "content_blocked", "quota", "worker_auth", "bad_request", "refs_unsupported", "upscale_unavailable"}
for folder in (DATA, GALLERY, REFS):
    folder.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Model 1 Studio", docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def site_password(request: Request, call_next):
    if request.url.path in ("/backend.php", "/healthz", "/login"):
        return await call_next(request)
    cookie = request.cookies.get("studio_session", "")
    if not valid_session(cookie) and not valid_basic(request.headers.get("authorization", "")):
        if request.url.path.startswith("/api/") or request.url.path.startswith("/gallery/"):
            return JSONResponse({"error": "authentication required"}, status_code=401)
        return RedirectResponse("/login", status_code=303)
    return await call_next(request)


def valid_basic(header: str) -> bool:
    try:
        scheme, encoded = header.split(" ", 1)
        if scheme.lower() != "basic":
            return False
        username, password = base64.b64decode(encoded, validate=True).decode("utf-8").split(":", 1)
        return valid_password(username, password)
    except (ValueError, UnicodeError, binascii.Error, KeyError):
        return False


def valid_password(username: str, password: str) -> bool:
    actual = hashlib.pbkdf2_hmac("sha256", password.encode(), PASSWORD_SALT, 250_000)
    return username == "studio" and secrets.compare_digest(actual, PASSWORD_HASH)


def valid_session(cookie: str) -> bool:
    try:
        issued, signature = cookie.split(".", 1)
        if not issued.isdecimal() or abs(time.time() - int(issued)) > 7 * 86400:
            return False
        expected = hmac.new(SESSION_KEY, issued.encode(), hashlib.sha256).hexdigest()
        return secrets.compare_digest(signature, expected)
    except (ValueError, KeyError):
        return False


@app.get("/login")
def login_page():
    return FileResponse(ROOT / "login.html")


@app.post("/login")
def login(username: str = Form(...), password: str = Form(...)):
    if not valid_password(username, password):
        return RedirectResponse("/login?error=1", status_code=303)
    issued = str(int(time.time()))
    signature = hmac.new(SESSION_KEY, issued.encode(), hashlib.sha256).hexdigest()
    response = RedirectResponse("/", status_code=303)
    response.set_cookie("studio_session", f"{issued}.{signature}", max_age=7 * 86400,
                        httponly=True, secure=True, samesite="lax")
    return response


@app.get("/healthz")
def healthz():
    return {"ok": True}


def read_jobs() -> dict:
    try:
        return json.loads(JOBS.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def write_jobs(jobs: dict) -> None:
    tmp = JOBS.with_suffix(".tmp")
    tmp.write_text(json.dumps(jobs, ensure_ascii=False), encoding="utf-8")
    tmp.replace(JOBS)


def touch_worker() -> None:
    WORKER.write_text(json.dumps({"last_seen": time.time()}), encoding="utf-8")


def worker_online() -> bool:
    try:
        last_seen = float(json.loads(WORKER.read_text(encoding="utf-8"))["last_seen"])
    except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False
    return time.time() - last_seen <= 90


def housekeeping(jobs: dict) -> None:
    now = time.time()
    for job_id, job in list(jobs.items()):
        if job["status"] in ("queued", "processing") and now - job["created_at"] > 2700:
            job.update(status="failed", error_code="timeout", finished_at=now)
        elif job["status"] == "processing" and now - job.get("claimed_at", now) > 1200:
            if job.get("attempts", 0) < 2:
                job["status"] = "queued"
            else:
                job.update(status="failed", error_code="timeout", finished_at=now)
        elif job["status"] in ("done", "failed") and now - job.get("finished_at", now) > 7 * 86400:
            del jobs[job_id]


def require_worker(key: str | None) -> None:
    if not key or not secrets.compare_digest(key, KEY):
        raise HTTPException(403, "forbidden")


class CreateJob(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)
    aspect_ratio: str = "1:1"
    count: int = Field(default=1, ge=1, le=4)
    resolution: str = "1k"
    image_urls: list[str] = Field(default_factory=list, max_length=10)


@app.get("/")
def home():
    return FileResponse(ROOT / "index.html")


@app.post("/api/jobs")
def create_job(body: CreateJob):
    if body.aspect_ratio not in ASPECTS or body.resolution not in ("1k", "2k") or not body.prompt.strip():
        raise HTTPException(400, "invalid settings")
    for url in body.image_urls:
        if not re.fullmatch(r"/gallery/RefAssets/[A-Za-z0-9_-]+\.(?:jpg|png|webp)", url) or not (REFS / Path(url).name).is_file():
            raise HTTPException(400, "invalid reference")
    job_id = "m1_" + uuid4().hex
    job = dict(id=job_id, type="generate", prompt=body.prompt.strip(), aspect_ratio=body.aspect_ratio,
               count=body.count, resolution=body.resolution, image_urls=body.image_urls,
               status="queued", attempts=0, created_at=time.time(), urls=[], error_code=None)
    with LOCK:
        jobs = read_jobs()
        housekeeping(jobs)
        jobs[job_id] = job
        write_jobs(jobs)
    return {"id": job_id, "status": "queued"}


@app.get("/api/jobs")
def list_jobs():
    with LOCK:
        jobs = read_jobs()
        housekeeping(jobs)
        write_jobs(jobs)
        return {
            "jobs": sorted(jobs.values(), key=lambda j: j["created_at"], reverse=True)[:30],
            "worker_online": worker_online(),
        }


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    with LOCK:
        job = read_jobs().get(job_id)
    if not job:
        raise HTTPException(404, "unknown job")
    return job


@app.post("/api/refs")
async def upload_ref(file: UploadFile = File(...)):
    data = file.file.read(20 * 1024 * 1024 + 1)
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(413, "file too large")
    import io
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            image = image.convert("RGB")
            image.thumbnail((2048, 2048))
            name = uuid4().hex + ".jpg"
            image.save(REFS / name, format="JPEG", quality=92)
    except Exception:
        raise HTTPException(415, "invalid image")
    return {"url": f"/gallery/RefAssets/{name}"}


@app.get("/gallery/{kind}/{name}")
def gallery(kind: str, name: str):
    if kind not in ("PicAssets", "RefAssets") or not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise HTTPException(404)
    file = (GALLERY if kind == "PicAssets" else REFS) / name
    if not file.is_file():
        raise HTTPException(404)
    return FileResponse(file)


@app.post("/backend.php")
async def worker_action(request: Request, action: str, x_worker_key: str | None = Header(default=None)):
    require_worker(x_worker_key)
    body = await request.json() if action == "m1_fail" else None
    form = await request.form() if action == "m1_complete" else None
    with LOCK:
        jobs = read_jobs()
        housekeeping(jobs)
        if action == "m1_next":
            touch_worker()
            candidate = next((job for job in sorted(jobs.values(), key=lambda j: j["created_at"]) if job["status"] == "queued"), None)
            if candidate:
                candidate["status"] = "processing"
                candidate["claimed_at"] = time.time()
                candidate["attempts"] += 1
            write_jobs(jobs)
            return {"job": {k: candidate[k] for k in ("id", "type", "prompt", "aspect_ratio", "count", "image_urls", "resolution")} if candidate else None}
        if action == "m1_fail":
            job = jobs.get(str(body.get("job_id", "")))
            if not job:
                raise HTTPException(404, "unknown job")
            if job["status"] not in ("done", "failed"):
                job.update(status="failed", error_code=body.get("error_code") if body.get("error_code") in ERRORS else "generation_failed", finished_at=time.time())
            write_jobs(jobs)
            return {"ok": True}
        if action == "m1_complete":
            job = jobs.get(str(form.get("job_id", "")))
            if not job:
                raise HTTPException(404, "unknown job")
            if job["status"] not in ("processing", "queued"):
                raise HTTPException(409, "already finished")
            files = form.getlist("files[]")
            if not files:
                raise HTTPException(400, "no files")
            urls = []
            import io
            for index, file in enumerate(files[:job["count"]]):
                data = file.file.read(20 * 1024 * 1024 + 1)
                if len(data) > 20 * 1024 * 1024:
                    raise HTTPException(413, "file too large")
                try:
                    with Image.open(io.BytesIO(data)) as image:
                        image.verify()
                except Exception:
                    raise HTTPException(415, "invalid image")
                ext = ".png" if data.startswith(b"\x89PNG") else ".jpg" if data.startswith(b"\xff\xd8") else ".webp" if data.startswith(b"RIFF") else None
                if not ext:
                    raise HTTPException(415, "invalid image type")
                name = f"{job['id']}_{index + 1}{ext}"
                (GALLERY / name).write_bytes(data)
                urls.append(f"/gallery/PicAssets/{name}")
            job.update(status="done", urls=urls, resolutions=str(form.get("resolutions", "")).split(","), finished_at=time.time())
            write_jobs(jobs)
            return {"ok": True, "urls": urls}
    return JSONResponse({"error": "unknown action"}, status_code=400)
