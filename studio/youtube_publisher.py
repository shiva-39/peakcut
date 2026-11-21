"""YouTube publishing: quota-aware resumable uploads, drip scheduling, and thumbnail handling."""
import json
import threading
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta

import httpx

from . import accounts, config, ctx, db, jobs, load, media, notices, render, safe, brain
from .db import publish_posts


_YT_UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
_YT_THUMB_URL = "https://www.googleapis.com/upload/youtube/v3/thumbnails/set"
_YT_SCOPES = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly"

_lock = threading.RLock()
_scheduler_thread = None
_running = False


def _dir() -> Path:
    d = config.user_dir(ctx.uid()) / "accounts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read_posts() -> list:
    f = _dir() / "publish_posts.json"
    if not f.exists():
        return []
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except ValueError:
        return []


def _write_posts(posts: list):
    with _lock:
        f = _dir() / "publish_posts.json"
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(posts, indent=1), encoding="utf-8")
        tmp.replace(f)


def auto_prep_and_queue(job, clip):
    """Auto-generate YouTube metadata and thumbnail for a finished clip.
    If yt_skip_review_auto_queue is enabled, immediately queue the clip for upload."""
    try:
        settings = accounts.settings()
        if not settings.get("yt_auto_prep_clips", True):
            return

        # Generate YouTube metadata
        meta = brain.youtube_metadata(clip, job.get("title", ""), job.get("uploader", ""))
        clip["yt_title"] = meta.get("yt_title", "")
        clip["yt_description"] = meta.get("yt_description", "")
        clip["yt_tags"] = meta.get("yt_tags", [])

        # Extract thumbnail
        thumb_path = render.extract_thumbnail(Path(job["dir"]), clip["id"])
        if thumb_path:
            clip["yt_thumbnail"] = thumb_path

        # If auto-queue is enabled, queue the clip immediately
        if settings.get("yt_skip_review_auto_queue", False):
            yt_accounts = [a for a in accounts.list_accounts() if a["platform"] == "youtube"]
            if yt_accounts:
                acc = yt_accounts[0]
                privacy = settings.get("yt_default_privacy", "private")
                post = queue_post(
                    clip["id"],
                    job["id"],
                    clip["yt_title"],
                    clip["yt_description"],
                    clip["yt_tags"],
                    privacy
                )
                post["account_id"] = acc["id"]
                _write_posts([p if p["id"] != post["id"] else post for p in _read_posts()])
                notices.add(ctx.uid(), "post", f"Auto-queued for YouTube: {clip['yt_title']}",
                            clip["yt_title"], f"/app/publishing")
    except Exception as e:
        # Log but don't fail the clip rendering
        import traceback
        traceback.print_exc()


def _pt_day_start(t: float) -> float:
    """Midnight Pacific Time (America/Los_Angeles) for the given UTC timestamp, returned as UTC timestamp."""
    try:
        from zoneinfo import ZoneInfo
        pt = ZoneInfo("America/Los_Angeles")
    except Exception:
        pt = timezone(timedelta(hours=-7))
    dt = datetime.fromtimestamp(t, tz=pt).replace(hour=0, minute=0, second=0, microsecond=0)
    return dt.timestamp()


def _pt_day_end(t: float) -> float:
    return _pt_day_start(t) + 86400


def _next_pt_midnight(t: float) -> float:
    return _pt_day_end(t)


def _settings() -> dict:
    return accounts.settings()


def _daily_limit() -> int:
    return max(1, min(100, int(_settings().get("yt_daily_upload_limit", 6) or 6)))


def _min_interval_minutes() -> int:
    return max(1, int(_settings().get("yt_min_interval_minutes", 120) or 120))


def _default_privacy() -> str:
    return _settings().get("yt_default_privacy", "private") or "private"


def _count_posted_today() -> int:
    """How many posts have been posted today (PT day) for the current user."""
    posts = _read_posts()
    today_start = _pt_day_start(time.time())
    today_end = _pt_day_end(time.time())
    return sum(1 for p in posts if p.get("status") == "posted" and today_start <= (p.get("posted_at") or 0) < today_end)


def _last_posted_at() -> float:
    posts = _read_posts()
    posted = [p.get("posted_at") for p in posts if p.get("status") == "posted" and p.get("posted_at")]
    return max(posted) if posted else 0


def _quota_status() -> dict:
    """Current quota usage and next reset info."""
    used = _count_posted_today()
    limit = _daily_limit()
    next_reset = _next_pt_midnight(time.time())
    return {"used": used, "limit": limit, "remaining": max(0, limit - used), "next_reset": next_reset}


def _extract_thumbnail(job_dir: Path, clip_id: str) -> str | None:
    """Extract a clean JPEG cover frame from the rendered clip via ffmpeg."""
    clip_file = job_dir / "clips" / f"{clip_id}.mp4"
    cover_file = job_dir / "clips" / f"{clip_id}.jpg"
    if not clip_file.exists():
        return None
    if cover_file.exists():
        return str(cover_file)
    work = Path(__import__("tempfile").mkdtemp(prefix="pk_thumb_"))
    try:
        out = work / f"{clip_id}.jpg"
        from .media import run
        cover_t = 1.2
        run(["ffmpeg", "-y", "-v", "error", "-ss", f"{cover_t:.2f}", "-i", str(clip_file), "-frames:v", "1", "-q:v", "3", str(out)], cwd=work)
        if out.exists():
            import shutil
            shutil.move(str(out), str(cover_file))
            return str(cover_file)
    except Exception:
        pass
    finally:
        import shutil
        shutil.rmtree(work, ignore_errors=True)
    return None


def _yt_token(acc: dict) -> str:
    """Get a valid access token for the YouTube account, refreshing if needed."""
    if acc.get("expires", 0) > time.time():
        return acc["access_token"]
    cid, secret = accounts.app_keys("google")
    r = httpx.post("https://oauth2.googleapis.com/token", data={
        "client_id": cid, "client_secret": secret,
        "refresh_token": acc["refresh_token"], "grant_type": "refresh_token"}, timeout=30)
    r.raise_for_status()
    tok = r.json()
    acc.update(access_token=tok["access_token"], expires=time.time() + tok.get("expires_in", 3600) - 60)
    accounts._put(acc)
    return acc["access_token"]


def _resumable_upload(acc: dict, video_path: Path, meta: dict) -> tuple[str, str]:
    """Upload video via YouTube resumable upload. Returns (video_id, url)."""
    token = _yt_token(acc)
    size = video_path.stat().st_size
    start = httpx.post(_YT_UPLOAD_URL, params={"uploadType": "resumable", "part": "snippet,status"}, json=meta,
                       headers={"Authorization": f"Bearer {token}", "X-Upload-Content-Type": "video/mp4",
                                "X-Upload-Content-Length": str(size)}, timeout=60)
    start.raise_for_status()
    location = start.headers["Location"]
    with video_path.open("rb") as f:
        done = httpx.put(location, content=f.read(),
                         headers={"Authorization": f"Bearer {token}", "Content-Type": "video/mp4"}, timeout=1800)
    done.raise_for_status()
    data = done.json()
    return data["id"], f"https://www.youtube.com/shorts/{data['id']}"


def _set_thumbnail(acc: dict, video_id: str, thumb_path: Path):
    """Set custom thumbnail for the uploaded video."""
    token = _yt_token(acc)
    with thumb_path.open("rb") as f:
        r = httpx.post(_YT_THUMB_URL, params={"videoId": video_id}, content=f.read(),
                       headers={"Authorization": f"Bearer {token}", "Content-Type": "image/jpeg"}, timeout=60)
    r.raise_for_status()


def _can_post_now() -> tuple[bool, str]:
    """Check if we can post now respecting daily limit and min interval."""
    used = _count_posted_today()
    limit = _daily_limit()
    if used >= limit:
        return False, f"Daily upload limit reached ({used}/{limit}). Resets at next midnight PT."
    last = _last_posted_at()
    if last:
        min_interval = _min_interval_minutes() * 60
        if time.time() - last < min_interval:
            wait = int(min_interval - (time.time() - last))
            return False, f"Minimum interval not elapsed. Wait {wait // 60}m {wait % 60}s."
    return True, "OK"


def queue_post(clip_id: str, job_id: str, title: str, description: str, tags: list, privacy: str = None,
               scheduled_for: float = None) -> dict:
    """Queue a clip for YouTube upload. Returns the created post."""
    posts = _read_posts()
    post = {
        "id": len(posts) + 1,
        "user_id": ctx.uid(),
        "clip_id": f"{job_id}.{clip_id}",
        "platform": "youtube",
        "youtube_video_id": "",
        "youtube_url": "",
        "title": title[:200],
        "description": description[:5000],
        "tags": json.dumps(tags[:15]),
        "privacy_status": privacy or _default_privacy(),
        "thumbnail_path": "",
        "status": "queued",
        "error_message": "",
        "retry_count": 0,
        "scheduled_for": scheduled_for,
        "posted_at": None,
        "created_at": time.time(),
    }
    posts.append(post)
    _write_posts(posts)
    ctx.bump()
    return post


def get_posts(status_filter: str = "all", sort: str = "posted_desc") -> list:
    """Get posts with optional filtering and sorting."""
    posts = _read_posts()
    if status_filter != "all":
        posts = [p for p in posts if p["status"] == status_filter]
    if sort == "posted_desc":
        posts.sort(key=lambda p: (-(p.get("posted_at") or 0), p.get("scheduled_for") or 0, -p["created_at"]))
    elif sort == "scheduled_asc":
        posts.sort(key=lambda p: (p.get("scheduled_for") or 0, -p["created_at"]))
    return posts


def update_post(post_id: int, **fields) -> dict | None:
    """Update a post's fields (title, description, tags, privacy_status, scheduled_for)."""
    posts = _read_posts()
    for p in posts:
        if p["id"] == post_id and p["user_id"] == ctx.uid():
            allowed = {"title", "description", "tags", "privacy_status", "scheduled_for"}
            for k, v in fields.items():
                if k in allowed:
                    if k == "tags" and isinstance(v, list):
                        v = json.dumps(v[:15])
                    if k == "title":
                        v = str(v)[:200]
                    if k == "description":
                        v = str(v)[:5000]
                    if k == "privacy_status" and v not in ("public", "unlisted", "private"):
                        v = "private"
                    p[k] = v
            _write_posts(posts)
            ctx.bump()
            return p
    return None


def retry_post(post_id: int) -> dict | None:
    """Reset a failed post back to queued."""
    posts = _read_posts()
    for p in posts:
        if p["id"] == post_id and p["user_id"] == ctx.uid() and p["status"] == "failed":
            p.update(status="queued", error_message="", retry_count=p.get("retry_count", 0) + 1)
            _write_posts(posts)
            ctx.bump()
            return p
    return None


def delete_post(post_id: int) -> bool:
    """Remove a queued/failed post."""
    posts = _read_posts()
    new = [p for p in posts if not (p["id"] == post_id and p["user_id"] == ctx.uid() and p["status"] in ("queued", "failed", "quota_paused"))]
    if len(new) != len(posts):
        _write_posts(new)
        ctx.bump()
        return True
    return False


def _process_post(post: dict):
    """Upload a single post to YouTube."""
    job_id, clip_id = post["clip_id"].split(".", 1)
    job = jobs.get(job_id)
    if not job:
        post["status"] = "failed"
        post["error_message"] = "Source job not found"
        return

    clip = next((c for c in job["clips"] if c["id"] == clip_id), None)
    if not clip or clip.get("status") != "done":
        post["status"] = "failed"
        post["error_message"] = "Clip not ready"
        return

    video_path = Path(job["dir"]) / "clips" / clip["file"]
    if not video_path.exists():
        post["status"] = "failed"
        post["error_message"] = "Video file missing"
        return

    acc = accounts.get(post.get("account_id") or "")
    if not acc or acc["platform"] != "youtube":
        post["status"] = "failed"
        post["error_message"] = "YouTube account not connected"
        return

    post["status"] = "uploading"
    _write_posts([p if p["id"] != post["id"] else post for p in _read_posts()])

    try:
        thumb_path = None
        if clip.get("cover"):
            thumb_path = Path(job["dir"]) / "clips" / clip["cover"]
            if not thumb_path.exists():
                thumb_path = Path(_extract_thumbnail(Path(job["dir"]), clip_id))

        tags = json.loads(post.get("tags", "[]")) if post.get("tags") else []
        meta = {
            "snippet": {
                "title": post["title"] or clip.get("title") or clip.get("hook") or "Clip",
                "description": post["description"] + "\n\n#Shorts",
                "tags": tags,
                "categoryId": "20",
            },
            "status": {"privacyStatus": post["privacy_status"], "selfDeclaredMadeForKids": False}
        }

        video_id, url = _resumable_upload(acc, video_path, meta)
        post["youtube_video_id"] = video_id
        post["youtube_url"] = url

        if thumb_path and thumb_path.exists():
            try:
                _set_thumbnail(acc, video_id, thumb_path)
            except Exception:
                pass

        post["status"] = "posted"
        post["posted_at"] = time.time()
        post["error_message"] = ""
        notices.add(ctx.uid(), "post", f"Posted to YouTube: {post['title'] or clip.get('title') or clip.get('hook')}",
                    post["title"] or clip.get("title") or clip.get("hook") or "Clip", url)

    except httpx.HTTPStatusError as e:
        if e.response.status_code == 403:
            err = e.response.json().get("error", {})
            for er in err.get("errors", []):
                if er.get("reason") in ("quotaExceeded", "uploadLimitExceeded"):
                    post["status"] = "quota_paused"
                    post["error_message"] = "YouTube quota exceeded"
                    _pause_all_queued()
                    return
        post["status"] = "failed"
        post["error_message"] = safe.friendly(e, 300)
    except Exception as e:
        post["status"] = "failed"
        post["error_message"] = safe.friendly(e, 300)

    _write_posts([p if p["id"] != post["id"] else post for p in _read_posts()])


def _pause_all_queued():
    """Mark all queued posts as quota_paused."""
    posts = _read_posts()
    changed = False
    for p in posts:
        if p["status"] == "queued":
            p["status"] = "quota_paused"
            changed = True
    if changed:
        _write_posts(posts)


def _resume_quota_paused():
    """Resume quota_paused posts back to queued."""
    posts = _read_posts()
    changed = False
    for p in posts:
        if p["status"] == "quota_paused":
            p["status"] = "queued"
            changed = True
    if changed:
        _write_posts(posts)


def _scheduler_loop():
    global _running
    _running = True
    while _running:
        time.sleep(15)
        try:
            with ctx.as_user(ctx.uid()):
                _tick()
        except Exception:
            pass


def _tick():
    """Process due posts respecting quota and interval."""
    posts = _read_posts()
    quota_ok, reason = _can_post_now()
    if not quota_ok:
        for p in posts:
            if p["status"] == "queued":
                p["status"] = "quota_paused"
        _write_posts(posts)
        return

    due = [p for p in posts if p["status"] == "queued" and (p.get("scheduled_for") or 0) <= time.time()]
    for p in due:
        quota_ok, _ = _can_post_now()
        if not quota_ok:
            p["status"] = "quota_paused"
            _write_posts(posts)
            return
        with _lock:
            posts = _read_posts()
            p = next((x for x in posts if x["id"] == p["id"]), None)
            if not p or p["status"] != "queued":
                continue
            p["status"] = "uploading"
            _write_posts(posts)
        _process_post(p)


def start():
    """Start the publishing scheduler and register clip-ready callback."""
    global _scheduler_thread
    if _scheduler_thread and _scheduler_thread.is_alive():
        return
    # Register the auto-prep callback for when clips finish rendering
    from . import jobs
    jobs.on_clip_ready.append(auto_prep_and_queue)
    _scheduler_thread = threading.Thread(target=_scheduler_loop, daemon=True)
    _scheduler_thread.start()


def stop():
    """Stop the publishing scheduler."""
    global _running
    _running = False


def get_quota_info() -> dict:
    """Get quota info for the current user."""
    info = _quota_status()
    info["next_reset_iso"] = datetime.fromtimestamp(info["next_reset"], tz=timezone.utc).isoformat()
    info["min_interval_minutes"] = _min_interval_minutes()
    info["daily_limit"] = _daily_limit()
    info["default_privacy"] = _default_privacy()
    return info


def save_publishing_settings(daily_limit: int = None, min_interval: int = None, default_privacy: str = None):
    """Save YouTube publishing settings."""
    patch = {}
    if daily_limit is not None:
        patch["yt_daily_upload_limit"] = max(1, min(100, int(daily_limit)))
    if min_interval is not None:
        patch["yt_min_interval_minutes"] = max(1, int(min_interval))
    if default_privacy is not None and default_privacy in ("public", "unlisted", "private"):
        patch["yt_default_privacy"] = default_privacy
    if patch:
        accounts.save_settings(patch)
    return accounts.settings()