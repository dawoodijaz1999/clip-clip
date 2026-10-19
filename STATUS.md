# STATUS

## Files
- clip.py: Discord poll -> yt-dlp download -> Gemini picks 5 moments (30-60s) -> ffmpeg 1080x1920 center crop + hook line -> GitHub Release links posted in Discord; releases older than 7 days deleted.
- .github/workflows/clip.yml: runs every 10 min (+ manual), cheap check first, heavy deps only when there is a link.
- requirements.txt, .gitignore, README.md

## Commands run and results
- Actions download test (5 player clients): all blocked by YouTube "Sign in to confirm you're not a bot" -> cookies needed (YT_COOKIES secret).
- Local `clip.py --local` on U65nZIA0QhM (7 min): 5 clips, 1080x1920, 31-59s, hooks + captions OK, 12 min with preset slow (now medium).

## Left
- Cloud workflow is manual-only (YouTube blocks GitHub without cookies).
- PC mode: systemd user service clip-clip (auto-starts at login), secrets in local .env. User must paste DISCORD_BOT_TOKEN into .env.
- First real run on Actions with cookies; Discord posting not yet tested live.
- Scheduled workflows in public repos pause after 60 days without commits.

## 2026-10-19 fix
- 403 on download: yt-dlp needs a JS runtime (PC Node 18 too old). Installed `deno` via pip into .venv, clip.py passes it as js_runtimes; added 10 MB chunked downloads, retries, one retry on 403.
- Re-queued the failed link XV4KSC5nmL0 by clearing the bot's fail mark.
- Service now runs `pip install -U "yt-dlp[default]"` before every start (skipped if offline); yt-dlp 2026.08.19 is current.
