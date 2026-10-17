# STATUS

## Files
- clip.py: Discord poll -> yt-dlp download -> Gemini picks 5 moments (30-60s) -> ffmpeg 1080x1920 center crop + hook line -> GitHub Release links posted in Discord; releases older than 7 days deleted.
- .github/workflows/clip.yml: runs every 10 min (+ manual), cheap check first, heavy deps only when there is a link.
- requirements.txt, .gitignore, README.md

## Commands run and results
- Actions download test (5 player clients): all blocked by YouTube "Sign in to confirm you're not a bot" -> cookies needed (YT_COOKIES secret).
- Local `clip.py --local` on U65nZIA0QhM (7 min): 5 clips, 1080x1920, 31-59s, hooks + captions OK, 12 min with preset slow (now medium).

## Left
- Push this commit to GitHub (needs user approval).
- User adds secrets: DISCORD_BOT_TOKEN, GEMINI_API_KEY, YT_COOKIES.
- First real run on Actions with cookies; Discord posting not yet tested live.
- Scheduled workflows in public repos pause after 60 days without commits.
