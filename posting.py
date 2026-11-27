"""Scheduled posting of finished clips: Facebook Page Reels and TikTok inbox drafts.

The first clip of a video is due right away, then one every POST_GAP_HOURS.
The queue lives in queue.json (committed back to the repo by the workflow).
Each platform is used only when its secrets are set:
  Facebook: MAKE_WEBHOOK_URL (Make.com scenario that publishes the reel), or FB_PAGE_ID + FB_PAGE_TOKEN
  TikTok:   TIKTOK_CLIENT_KEY, TIKTOK_CLIENT_SECRET, TIKTOK_REFRESH_TOKEN
"""
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

QUEUE_FILE = Path(__file__).with_name("queue.json")
POST_GAP_HOURS = 0  # TikTok drafts: 0 = all at once (user schedules them in TikTok)
GAP_HOURS = {"tiktok": 0, "facebook": 2}  # per platform: clip 1 at once, then one every N hours
MAX_TRIES = 3          # per platform, one try per run
KEEP_DONE_DAYS = 3
GRAPH = "https://graph.facebook.com/v23.0"
TIKTOK = "https://open.tiktokapis.com/v2"


def _now():
    return datetime.now(timezone.utc)


def facebook_enabled():
    return bool(os.environ.get("MAKE_WEBHOOK_URL")
                or (os.environ.get("FB_PAGE_ID") and os.environ.get("FB_PAGE_TOKEN")))


def tiktok_enabled():
    return all(os.environ.get(k) for k in ("TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET", "TIKTOK_REFRESH_TOKEN"))


def platforms():
    return [p for p, on in (("facebook", facebook_enabled()), ("tiktok", tiktok_enabled())) if on]


# ---------------------------------------------------------------- Queue

def load_queue():
    try:
        return json.loads(QUEUE_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return []


def save_queue(queue):
    QUEUE_FILE.write_text(json.dumps(queue, indent=1, ensure_ascii=False) + "\n")


def enqueue(title, items):
    """items: list of (download_url, clip dict). Returns how many were scheduled."""
    targets = platforms()
    if not targets:
        return 0
    queue = load_queue()
    start = _now()
    for i, (url, clip) in enumerate(items):
        queue.append({
            "url": url,
            "title": title,
            "hook": clip.get("hook", ""),
            "caption": clip.get("caption", ""),
            "n": i + 1,
            "due": {p: (start + timedelta(hours=GAP_HOURS.get(p, POST_GAP_HOURS) * i)).isoformat()
                    for p in targets},
            "status": {p: "pending" for p in targets},
            "tries": {p: 0 for p in targets},
        })
    save_queue(queue)
    return len(items)


def _due_at(entry, platform):
    due = entry["due"]
    return datetime.fromisoformat(due[platform] if isinstance(due, dict) else due)


def _platform_due(entry, platform, now):
    return entry["status"].get(platform) == "pending" and _due_at(entry, platform) <= now


def _due(entry, now):
    return any(_platform_due(entry, p, now) for p in entry["status"])


def has_due():
    now = _now()
    return any(_due(e, now) for e in load_queue())


# ---------------------------------------------------------------- Facebook Reels

def post_facebook_via_make(entry):
    """Hand the clip URL to the Make.com scenario, which publishes it as a Page reel."""
    r = requests.post(os.environ["MAKE_WEBHOOK_URL"], timeout=60, json={
        "video_url": entry["url"], "caption": entry["caption"] or entry["hook"],
        "hook": entry["hook"], "title": entry["title"], "n": entry["n"],
    })
    if r.status_code != 200:
        raise RuntimeError(f"Make webhook returned HTTP {r.status_code}: {r.text[:200]}")


def post_facebook(path, caption):
    page, token = os.environ["FB_PAGE_ID"], os.environ["FB_PAGE_TOKEN"]
    start = requests.post(f"{GRAPH}/{page}/video_reels",
                          data={"upload_phase": "start", "access_token": token}, timeout=60)
    start.raise_for_status()
    video_id = start.json()["video_id"]
    size = path.stat().st_size
    with open(path, "rb") as fh:
        up = requests.post(f"https://rupload.facebook.com/video-upload/v23.0/{video_id}",
                           headers={"Authorization": f"OAuth {token}", "offset": "0", "file_size": str(size)},
                           data=fh, timeout=600)
    up.raise_for_status()
    done = requests.post(f"{GRAPH}/{page}/video_reels",
                         data={"upload_phase": "finish", "video_id": video_id, "video_state": "PUBLISHED",
                               "description": caption, "access_token": token}, timeout=120)
    done.raise_for_status()
    if not done.json().get("success"):
        raise RuntimeError(f"Facebook did not publish: {done.text[:200]}")
    return video_id


# ---------------------------------------------------------------- TikTok inbox draft

def _tiktok_access_token():
    r = requests.post(f"{TIKTOK}/oauth/token/", timeout=60, data={
        "client_key": os.environ["TIKTOK_CLIENT_KEY"],
        "client_secret": os.environ["TIKTOK_CLIENT_SECRET"],
        "grant_type": "refresh_token",
        "refresh_token": os.environ["TIKTOK_REFRESH_TOKEN"],
    })
    data = r.json()
    if "access_token" not in data:
        raise RuntimeError(f"TikTok login expired or invalid: {str(data)[:200]}")
    if data.get("refresh_token") and data["refresh_token"] != os.environ["TIKTOK_REFRESH_TOKEN"]:
        print("TikTok issued a new refresh token; update the TIKTOK_REFRESH_TOKEN secret", file=sys.stderr)
    return data["access_token"]


def _chunks(size):
    """TikTok wants 5-64 MB chunks; one chunk if the file is small, last chunk takes the remainder."""
    mb = 1024 * 1024
    if size <= 64 * mb:
        return size, 1
    chunk = 10 * mb
    return chunk, size // chunk


def post_tiktok_draft(path):
    token = _tiktok_access_token()
    size = path.stat().st_size
    chunk, count = _chunks(size)
    init = requests.post(f"{TIKTOK}/post/publish/inbox/video/init/", timeout=60,
                         headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                         json={"source_info": {"source": "FILE_UPLOAD", "video_size": size,
                                               "chunk_size": chunk, "total_chunk_count": count}})
    body = init.json()
    if body.get("error", {}).get("code") not in (None, "ok"):
        raise RuntimeError(f"TikTok refused the upload: {body['error']}")
    upload_url = body["data"]["upload_url"]
    with open(path, "rb") as fh:
        for i in range(count):
            first = i * chunk
            last = size - 1 if i == count - 1 else first + chunk - 1
            fh.seek(first)
            part = fh.read(last - first + 1)
            r = requests.put(upload_url, data=part, timeout=600, headers={
                "Content-Type": "video/mp4",
                "Content-Range": f"bytes {first}-{last}/{size}",
            })
            if r.status_code not in (200, 201, 206):
                raise RuntimeError(f"TikTok chunk upload failed: HTTP {r.status_code} {r.text[:200]}")
    publish_id = body["data"].get("publish_id")
    status = _tiktok_wait(token, publish_id)
    print(f"TikTok draft {publish_id}: {status}", flush=True)
    return publish_id


def _tiktok_wait(token, publish_id, tries=12):
    """Ask TikTok until the draft reaches the inbox; raise if TikTok reports a failure."""
    status = "unknown"
    for _ in range(tries):
        r = requests.post(f"{TIKTOK}/post/publish/status/fetch/", timeout=60,
                          headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                          json={"publish_id": publish_id})
        data = r.json().get("data", {})
        status = data.get("status", "unknown")
        if status == "FAILED":
            raise RuntimeError(f"TikTok could not process the video: {data.get('fail_reason', 'unknown reason')}")
        if status in ("SEND_TO_USER_INBOX", "PUBLISH_COMPLETE"):
            return status
        time.sleep(10)
    return status


# ---------------------------------------------------------------- Run due posts

POSTERS = {
    "facebook": lambda path, e: (post_facebook_via_make(e) if os.environ.get("MAKE_WEBHOOK_URL")
                                 else post_facebook(path, e["caption"] or e["hook"])),
    "tiktok": lambda path, e: post_tiktok_draft(path),
}


def post_due(say):
    """Post everything that is due. `say` sends a Discord message. Returns True if the queue changed."""
    queue = load_queue()
    now = _now()
    changed = False
    for entry in queue:
        if not _due(entry, now):
            continue
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "clip.mp4"
            try:
                r = requests.get(entry["url"], timeout=300)
                r.raise_for_status()
                path.write_bytes(r.content)
            except Exception as e:
                print(f"Could not fetch {entry['url']}: {e}", file=sys.stderr)
                continue  # try again next run
            for platform, state in list(entry["status"].items()):
                if not _platform_due(entry, platform, now) or platform not in platforms():
                    continue
                changed = True
                entry["tries"][platform] = entry["tries"].get(platform, 0) + 1
                try:
                    POSTERS[platform](path, entry)
                    entry["status"][platform] = "done"
                    if platform == "facebook":
                        say(f"✅ Clip {entry['n']} sent to Facebook - {entry['hook']}")
                    else:
                        say(f"📲 Clip {entry['n']} is in your TikTok inbox - open TikTok and tap Post.\n"
                            f"Caption to paste: {entry['caption'] or entry['hook']}")
                except Exception as e:
                    print(f"{platform} post failed for clip {entry['n']}: {e}", file=sys.stderr, flush=True)
                    if entry["tries"][platform] >= MAX_TRIES:
                        entry["status"][platform] = "failed"
                        say(f"❌ Clip {entry['n']} could not be posted to {platform.title()} after "
                            f"{MAX_TRIES} tries: {str(e)[:200]}")
    cutoff = now - timedelta(days=KEEP_DONE_DAYS)
    kept = [e for e in queue
            if "pending" in e["status"].values() or max(_due_at(e, p) for p in e["status"]) > cutoff]
    if changed or len(kept) != len(queue):
        save_queue(kept)
        return True
    return False
