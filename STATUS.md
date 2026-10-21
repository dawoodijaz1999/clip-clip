# STATUS

In daily personal use. Discord posting is tested live (PC mode, multiple videos).

## Files
- clip.py: Discord poll -> input (YouTube via yt-dlp, Discord attachment, Drive/Dropbox link) -> Gemini picks 5 moments (30-60s) -> ffmpeg 1080x1920 center crop + hook line -> GitHub Release links posted in Discord; releases older than 7 days deleted.
- .github/workflows/clip.yml: runs every 15 min (GitHub may delay by 1-4 h) + manual, cheap check first, heavy deps only when there is work. `test_url` input checks the YouTube cookies.
- .github/workflows/keepalive.yml: monthly timestamp commit + re-enables clip.yml.
- requirements.txt, .gitignore, README.md

## Commands run and results
- Actions download test (5 player clients): all blocked by YouTube "Sign in to confirm you're not a bot" -> cookies needed (YT_COOKIES secret).
- Local `clip.py --local` on U65nZIA0QhM (7 min): 5 clips, 1080x1920, 31-59s, hooks + captions OK, 12 min with preset slow (now medium).

## Left
- PC mode: systemd user service clip-clip (auto-starts at login, updates yt-dlp first), secrets in local .env.
- A full clip run on GitHub Actions (with cookies) has not been tested yet; only the cookie download test passed.
- Drive and Dropbox were tested with URL conversion and a local server, not with real share links.
- YT_COOKIES expire every few weeks; refreshing needs a desktop browser.

## 2026-10-19 fix
- 403 on download: yt-dlp needs a JS runtime (PC Node 18 too old). Installed `deno` via pip into .venv, clip.py passes it as js_runtimes; added 10 MB chunked downloads, retries, one retry on 403.
- Re-queued the failed link XV4KSC5nmL0 by clearing the bot's fail mark.
- Service now runs `pip install -U "yt-dlp[default]"` before every start (skipped if offline); yt-dlp 2026.08.19 is current.
- 2026-10-19: YT_COOKIES (throwaway account), DISCORD_BOT_TOKEN, GEMINI_API_KEY added as repo secrets. Cloud cookie test downloaded 20s of a Rajab vlog OK. Manual workflow has a test_url input to check cookies.

## 2026-10-20
- File inputs: Discord video attachments (mp4/mov/mkv/webm) and Google Drive / Dropbox share links, converted to direct downloads and run through the same pipeline (`get_source` -> `find_moments` -> `render`). Clear Discord errors for unsupported type, over 500 MB, private/unreachable link, unplayable file, video under 30 s. `--check` detects all input types.
- `--local` accepts a local file path or any supported URL.
- Lock: a bot ⏳ reaction means "in progress", so the PC service and the cloud never take the same message; a ⏳ whose "Working on it" reply is older than 2 h is retried.
- Gemini fallback chain fixed: gemini-2.5-flash and gemini-2.0-flash were shut down (caused evenly spaced clips on sKLTvXQI7nw). Now gemini-flash-latest (twice, 15 s apart), gemini-3.5-flash, gemini-flash-lite-latest; each failure is logged.
- clip.yml schedule restored (every 15 min); keepalive.yml added.
- .gitignore: cookies files, media outputs (mp4/mov/mkv/webm/mp3/m4a), test.*, *.pyc.
- Security scan of current files and all git history (Discord token / Google key / cookie / GitHub token patterns, plus exact values of the current token and key): nothing found.
- README rewritten for clients/recruiters.
- Tests: `--local` on a 2-min sample file -> 3 clips 1080x1920, 30-32 s, Gemini hooks/captions OK (7 min). `download_file` against a local server: good file OK, attachment name kept, HTML page / 404 / 5 s video / fake file / over-limit all give the right user error.
- PC service restarted on the new code.
