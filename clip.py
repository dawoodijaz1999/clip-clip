"""clip-clip: video in Discord -> 5 vertical TikTok clips with a hook line.

Inputs: a YouTube link, a video attachment (mp4/mov/mkv/webm), or a public
Google Drive / Dropbox share link to a video.

Run modes:
  python clip.py --check                 # GitHub Actions: is there new work?
  python clip.py                         # GitHub Actions: process new work, reply in Discord
  python clip.py --watch                 # PC mode: keep checking Discord every 30s (auto-start service)
  python clip.py --local <url-or-path>   # local test: make clips into ./out, no Discord/GitHub
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

API = "https://discord.com/api/v10"
YT_RE = re.compile(
    r"https?://(?:www\.|m\.)?(?:youtube\.com/(?:watch\?(?:\S*&)?v=|shorts/|live/)|youtu\.be/)([\w-]{11})"
)
DRIVE_RE = re.compile(
    r"https?://(?:drive|docs)\.google\.com/(?:file/d/|open\?(?:\S*&)?id=|uc\?(?:\S*&)?id=)([\w-]{20,})"
)
DROPBOX_RE = re.compile(r"https?://(?:www\.)?dropbox\.com/(?:s|scl/fi|sh)/[^\s<>]+")
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm"}
MAX_FILE_MB = 500
DONE, FAIL, WORK = "✅", "❌", "⏳"
WORKING_TEXT = "Working on it"
LOCK_HOURS = 2  # a ⏳ older than this (crashed run) is retried
CLIPS_PER_VIDEO = 5
MIN_LEN, MAX_LEN = 30, 60
MAX_LINKS_PER_RUN = 2
KEEP_DAYS = 7
GEMINI_MODELS = ["gemini-flash-latest", "gemini-flash-latest", "gemini-3.5-flash", "gemini-flash-lite-latest"]


class UserError(Exception):
    """A problem with the input that should be shown to the user as-is."""
WORK_DIR = Path("work")
DEFAULT_REPO = "dawoodijaz1999/clip-clip"
POLL_SECONDS = 30


def _load_env(path=Path(__file__).with_name(".env")):
    """Read KEY=VALUE lines from .env (PC mode); real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


# ---------------------------------------------------------------- Discord

def _discord(method, path, **kw):
    headers = {"Authorization": f"Bot {os.environ['DISCORD_BOT_TOKEN']}"}
    for _ in range(5):
        r = requests.request(method, API + path, headers=headers, timeout=30, **kw)
        if r.status_code == 429:
            time.sleep(float(r.json().get("retry_after", 1)) + 0.5)
            continue
        r.raise_for_status()
        return r.json() if r.content else None
    raise RuntimeError(f"Discord rate limit on {path}")


def _channel():
    return os.environ["DISCORD_CHANNEL_ID"]


def _react(msg_id, emoji, remove=False):
    e = urllib.parse.quote(emoji)
    _discord("DELETE" if remove else "PUT", f"/channels/{_channel()}/messages/{msg_id}/reactions/{e}/@me")


def _say(text, reply_to=None):
    body = {"content": text[:2000], "allowed_mentions": {"parse": []}}
    if reply_to:
        body["message_reference"] = {"message_id": reply_to, "fail_if_not_exists": False}
    _discord("POST", f"/channels/{_channel()}/messages", json=body)


def _job_from_message(m):
    """Turn a Discord message into a job dict, or None if it has nothing to clip.

    Order: video attachment, then Drive/Dropbox link, then YouTube link.
    Unsupported or oversized attachments become an "error" job so the user gets a reply.
    """
    content = m.get("content", "")
    attachments = m.get("attachments", [])
    for a in attachments:
        if Path(a["filename"]).suffix.lower() in VIDEO_EXTS:
            if a.get("size", 0) > MAX_FILE_MB * 1024 * 1024:
                return {"kind": "error", "error": f"That file is over {MAX_FILE_MB} MB. Send a shorter or smaller video."}
            return {"kind": "file", "url": a["url"], "name": a["filename"], "key": m["id"]}
    if match := DRIVE_RE.search(content):
        return {"kind": "file", "url": drive_direct_url(match.group(1)), "name": "", "key": m["id"]}
    if match := DROPBOX_RE.search(content):
        return {"kind": "file", "url": dropbox_direct_url(match.group(0)), "name": "", "key": m["id"]}
    if match := YT_RE.search(content):
        return {"kind": "youtube", "video_id": match.group(1), "key": match.group(1)}
    if attachments:
        exts = ", ".join(sorted(VIDEO_EXTS))
        return {"kind": "error", "error": f"Unsupported file type. Send a video ({exts}) or a YouTube/Drive/Dropbox link."}
    return None


def _ts(m):
    return datetime.fromisoformat(m["timestamp"])


def pending_jobs():
    """Messages with work the bot has not marked done/failed, oldest first.

    A ⏳ from the bot means another instance (PC or cloud) is on it; skip it unless the
    bot's "Working on it" reply is older than LOCK_HOURS (that run crashed).
    """
    msgs = _discord("GET", f"/channels/{_channel()}/messages?limit=50") or []
    started = {}
    for m in msgs:
        ref = (m.get("message_reference") or {}).get("message_id")
        if m["author"].get("bot") and ref and m.get("content", "").startswith(WORKING_TEXT):
            started[ref] = max(started.get(ref, _ts(m)), _ts(m))
    stale = datetime.now(timezone.utc) - timedelta(hours=LOCK_HOURS)
    jobs = []
    for m in reversed(msgs):
        if m["author"].get("bot"):
            continue
        mine = {r["emoji"]["name"] for r in m.get("reactions", []) if r.get("me")}
        if DONE in mine or FAIL in mine:
            continue
        if WORK in mine and started.get(m["id"], _ts(m)) > stale:
            continue
        job = _job_from_message(m)
        if job:
            job["msg_id"] = m["id"]
            jobs.append(job)
    return jobs


# ---------------------------------------------------------------- File links

def drive_direct_url(file_id):
    return f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t"


def dropbox_direct_url(url):
    parts = urllib.parse.urlsplit(url)
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query) if k not in ("dl", "raw")]
    query.append(("dl", "1"))
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


def _filename_from(resp, fallback):
    cd = resp.headers.get("Content-Disposition", "")
    match = re.search(r"filename\*=UTF-8''([^;]+)", cd) or re.search(r'filename="?([^";]+)"?', cd)
    return urllib.parse.unquote(match.group(1)) if match else fallback


def download_file(url, dest, name=""):
    """Download a direct video URL (Discord attachment, Drive, Dropbox) to dest/src.<ext>."""
    dest.mkdir(parents=True, exist_ok=True)
    limit = MAX_FILE_MB * 1024 * 1024
    with requests.get(url, stream=True, timeout=60, allow_redirects=True) as r:
        if r.status_code in (401, 403, 404) or "text/html" in r.headers.get("Content-Type", ""):
            raise UserError("Couldn't download that file. Make sure the link is shared as "
                            "'Anyone with the link' and points to a single video file.")
        r.raise_for_status()
        if int(r.headers.get("Content-Length") or 0) > limit:
            raise UserError(f"That file is over {MAX_FILE_MB} MB. Send a shorter or smaller video.")
        url_name = urllib.parse.unquote(Path(urllib.parse.urlsplit(r.url).path).name)
        name = name or _filename_from(r, url_name if Path(url_name).suffix else "video.mp4")
        ext = Path(name).suffix.lower()
        if ext not in VIDEO_EXTS:
            raise UserError(f"Unsupported file type ({ext or 'unknown'}). Send an mp4, mov, mkv or webm video.")
        src = dest / f"src{ext}"
        size = 0
        with open(src, "wb") as fh:
            for chunk in r.iter_content(1024 * 1024):
                size += len(chunk)
                if size > limit:
                    raise UserError(f"That file is over {MAX_FILE_MB} MB. Send a shorter or smaller video.")
                fh.write(chunk)
    return src, Path(name).stem, _checked_duration(src)


def _checked_duration(src):
    try:
        duration = _duration(src)
    except (subprocess.CalledProcessError, ValueError):
        raise UserError("That file isn't a playable video.")
    if duration < MIN_LEN:
        raise UserError(f"That video is only {int(duration)}s long; it needs to be at least {MIN_LEN}s.")
    return duration


# ---------------------------------------------------------------- Download

def download(video_id, dest):
    import yt_dlp

    dest.mkdir(parents=True, exist_ok=True)
    opts = {
        "format": "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/bv*[height<=1080]+ba/b[height<=1080]/b",
        "merge_output_format": "mp4",
        "outtmpl": str(dest / "src.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "noprogress": True,
        "http_chunk_size": 10 * 1024 * 1024,  # small pieces avoid YouTube's mid-download 403s
        "retries": 10,
        "fragment_retries": 10,
    }
    deno = shutil.which("deno") or str(Path(sys.executable).with_name("deno"))
    if Path(deno).exists():
        opts["js_runtimes"] = {"deno": {"path": deno}}  # YouTube needs a JS runtime for its download checks
    cookies = os.environ.get("YT_COOKIES", "").strip()
    if cookies:
        cpath = dest / "cookies.txt"
        cpath.write_text(cookies + "\n")
        opts["cookiefile"] = str(cpath)
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as e:
        if "403" not in str(e):
            raise
        # 403s are often temporary; retry once with fresh URLs
        for leftover in dest.glob("src.*"):
            leftover.unlink()
        time.sleep(10)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    src = next(dest.glob("src.*"))
    return src, info.get("title", video_id), float(info.get("duration") or _duration(src))


def _duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


# ---------------------------------------------------------------- Moments (Gemini)

PROMPT = """This is the audio of a Pakistani YouTube vlog (Urdu/Punjabi), titled "{title}", {mins} minutes long.

Pick the {n} best moments to post as TikTok clips for a Pakistani audience.
Rules:
- Each clip is {min_len}-{max_len} seconds long and makes sense on its own.
- Prefer funny, dramatic, emotional, surprising or argument moments.
- Start at the beginning of a sentence, end after the punchline or reaction.
- Clips must not overlap. Spread them across the whole video.
- Skip moments with heavy swearing, sexual jokes, religious insults or court/legal case talk.

For each clip return:
- "start" and "end": seconds from the start of the audio (numbers).
- "hook": one short line of text for the top of the video, Roman Urdu, max 28 characters, NO emoji, makes people want to watch (e.g. "Rajab ne yeh kya kar diya").
- "caption": a TikTok caption in Roman Urdu with 4-6 hashtags (emoji allowed).

Return only JSON: {{"clips": [{{"start": 0, "end": 0, "hook": "", "caption": ""}}]}}"""


def find_moments(src, title, duration, work):
    from google import genai
    from google.genai import types

    audio = work / "audio.mp3"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-b:a", "32k", str(audio)],
        check=True,
    )
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    uploaded = client.files.upload(file=str(audio))
    for _ in range(60):
        if uploaded.state.name != "PROCESSING":
            break
        time.sleep(5)
        uploaded = client.files.get(name=uploaded.name)

    prompt = PROMPT.format(title=title, mins=round(duration / 60), n=CLIPS_PER_VIDEO,
                           min_len=MIN_LEN, max_len=MAX_LEN)
    last_err = None
    try:
        for model in GEMINI_MODELS:
            try:
                resp = client.models.generate_content(
                    model=model,
                    contents=[uploaded, prompt],
                    config=types.GenerateContentConfig(response_mime_type="application/json"),
                )
                clips = _clean(json.loads(resp.text).get("clips", []), duration)
                if clips:
                    return clips
            except Exception as e:  # retry / try the next model
                last_err = e
                print(f"Gemini {model} failed: {str(e)[:200]}", file=sys.stderr, flush=True)
                time.sleep(15)
    finally:
        try:
            client.files.delete(name=uploaded.name)
        except Exception:
            pass
    print(f"Gemini failed, using evenly spaced clips: {last_err}")
    return _evenly_spaced(title, duration)


def _clean(raw, duration):
    clips = []
    for c in sorted(raw, key=lambda c: float(c.get("start", 0))):
        try:
            start, end = float(c["start"]), float(c["end"])
        except (KeyError, TypeError, ValueError):
            continue
        start = max(0.0, min(start, duration - MIN_LEN))
        end = min(max(end, start + MIN_LEN), start + MAX_LEN, duration)
        if end - start < MIN_LEN - 1 or (clips and start < clips[-1]["end"]):
            continue
        hook = re.sub(r"[^\w\s?!.,'-]", "", str(c.get("hook", ""))).strip()[:32]
        clips.append({"start": start, "end": end, "hook": hook, "caption": str(c.get("caption", "")).strip()})
    return clips[:CLIPS_PER_VIDEO]


def _evenly_spaced(title, duration):
    length = 45
    step = duration / (CLIPS_PER_VIDEO + 1)
    hook = re.sub(r"[^\w\s]", "", title).strip()[:28]
    return [
        {"start": step * (i + 1), "end": min(step * (i + 1) + length, duration), "hook": hook, "caption": ""}
        for i in range(CLIPS_PER_VIDEO)
        if step * (i + 1) + MIN_LEN <= duration
    ]


# ---------------------------------------------------------------- Render

def _font():
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"):
        if Path(p).exists():
            return p
    out = subprocess.run(["fc-match", "-f", "%{file}", "sans:bold"], capture_output=True, text=True)
    return out.stdout.strip()


def render(src, clip, out_path, work):
    hook_file = work / f"{out_path.stem}.txt"
    hook_file.write_text(clip["hook"] or " ")
    size = min(76, int(1000 / (0.62 * max(len(clip["hook"]), 1))))
    vf = (
        "scale='if(gt(a,9/16),-2,1080)':'if(gt(a,9/16),1920,-2)':flags=lanczos,"
        "crop=1080:1920,"
        f"drawtext=fontfile='{_font()}':textfile='{hook_file}':fontsize={size}:fontcolor=white:"
        "box=1:boxcolor=black@0.65:boxborderw=26:x=(w-text_w)/2:y=260"
    )
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-ss", f"{clip['start']:.2f}", "-to", f"{clip['end']:.2f}", "-i", str(src),
         "-vf", vf, "-r", "30",
         "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-profile:v", "high", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
         "-movflags", "+faststart", str(out_path)],
        check=True,
    )


def get_source(job, work):
    """Fetch the job's video into work/ and return (src, title, duration)."""
    if job["kind"] == "youtube":
        return download(job["video_id"], work)
    if job["kind"] == "path":
        work.mkdir(parents=True, exist_ok=True)
        src = Path(job["path"])
        if src.suffix.lower() not in VIDEO_EXTS:
            raise UserError(f"Unsupported file type ({src.suffix or 'unknown'}).")
        return src, src.stem, _checked_duration(src)
    return download_file(job["url"], work, job.get("name", ""))


def make_clips(job, work):
    src, title, duration = get_source(job, work)
    moments = find_moments(src, title, duration, work)
    files = []
    for i, clip in enumerate(moments, 1):
        out = work / f"{job['key']}_clip{i}.mp4"
        render(src, clip, out, work)
        files.append((out, clip))
    return title, files


# ---------------------------------------------------------------- GitHub release hosting

def publish(tag, title, files):
    repo = os.environ.get("GITHUB_REPOSITORY", DEFAULT_REPO)
    subprocess.run(
        ["gh", "release", "create", tag, "-R", repo, *[str(f) for f, _ in files],
         "--title", title[:100], "--notes", "Auto clips. Deleted after 7 days."],
        check=True,
    )
    return [f"https://github.com/{repo}/releases/download/{tag}/{f.name}" for f, _ in files]


def cleanup_old_releases():
    repo = os.environ.get("GITHUB_REPOSITORY", DEFAULT_REPO)
    out = subprocess.run(["gh", "release", "list", "-R", repo, "--limit", "100", "--json", "tagName,createdAt"],
                         capture_output=True, text=True)
    if out.returncode != 0:
        return
    cutoff = datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)
    for rel in json.loads(out.stdout or "[]"):
        created = datetime.fromisoformat(rel["createdAt"].replace("Z", "+00:00"))
        if rel["tagName"].startswith("clips-") and created < cutoff:
            subprocess.run(["gh", "release", "delete", rel["tagName"], "-R", repo, "--cleanup-tag", "-y"])


# ---------------------------------------------------------------- Main

def _fmt(sec):
    return f"{int(sec // 60)}:{int(sec % 60):02d}"


def _error_text(e):
    if isinstance(e, UserError):
        return str(e)
    text = str(e)
    if "Sign in to confirm" in text or "cookies" in text.lower():
        return "YouTube blocked the download. The YT_COOKIES secret has probably expired - export fresh cookies and update it."
    if "Private video" in text or "unavailable" in text.lower():
        return "That video is private or unavailable."
    return f"Something went wrong: {text[:300]}"


def process(job):
    msg_id = job["msg_id"]
    if job["kind"] == "error":
        _say(f"{FAIL} {job['error']}", reply_to=msg_id)
        _react(msg_id, FAIL)
        return
    work = WORK_DIR / job["key"]
    _react(msg_id, WORK)
    _say(f"{WORKING_TEXT}... clips in about 10-20 minutes.", reply_to=msg_id)
    try:
        title, files = make_clips(job, work)
        if not files:
            raise RuntimeError("no clips could be made from this video")
        links = publish(f"clips-{msg_id}", title, files)
        _say(f"**{title}**\n{len(files)} clips ready (1080x1920). Tap a link to download:", reply_to=msg_id)
        for i, ((path, clip), url) in enumerate(zip(files, links), 1):
            _say(
                f"**Clip {i}** ({_fmt(clip['start'])}-{_fmt(clip['end'])}) - {clip['hook']}\n"
                f"{url}\n"
                f"Caption: {clip['caption'] or '-'}"
            )
        _react(msg_id, DONE)
    except Exception as e:
        print(f"Failed {job['key']}: {e}", file=sys.stderr)
        _say(f"{FAIL} {_error_text(e)}", reply_to=msg_id)
        _react(msg_id, FAIL)
    finally:
        _react(msg_id, WORK, remove=True)
        shutil.rmtree(work, ignore_errors=True)


def watch():
    """PC mode: keep checking Discord and process links as they arrive."""
    print("clip-clip is watching Discord...", flush=True)
    last_cleanup = 0.0
    while True:
        try:
            for job in pending_jobs():
                process(job)
            if time.time() - last_cleanup > 3600:
                cleanup_old_releases()
                last_cleanup = time.time()
        except Exception as e:  # network hiccup etc. - keep running
            print(f"watch error: {e}", file=sys.stderr, flush=True)
        time.sleep(POLL_SECONDS)


def _local_job(arg):
    """--local accepts a YouTube/Drive/Dropbox/direct URL or a local file path."""
    path = Path(arg).expanduser()
    if path.exists():
        return {"kind": "path", "path": str(path.resolve()), "key": re.sub(r"\W", "_", path.stem)[:40]}
    job = _job_from_message({"id": "local", "content": arg})
    if job:
        return job
    return {"kind": "file", "url": arg, "name": "", "key": "local"}


def main():
    _load_env()
    os.chdir(Path(__file__).parent)
    args = sys.argv[1:]
    if args[:1] == ["--watch"]:
        watch()
        return
    if args[:1] == ["--local"]:
        job = _local_job(args[1])
        title, files = make_clips(job, Path("out") / job["key"])
        print(title)
        for f, c in files:
            print(f"{f}  {_fmt(c['start'])}-{_fmt(c['end'])}  {c['hook']}  |  {c['caption']}")
        return
    if args[:1] == ["--check"]:
        has_work = bool(pending_jobs())
        print(f"has_work={'true' if has_work else 'false'}")
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a") as fh:
                fh.write(f"has_work={'true' if has_work else 'false'}\n")
        return
    for job in pending_jobs()[:MAX_LINKS_PER_RUN]:
        process(job)
    cleanup_old_releases()


if __name__ == "__main__":
    main()
