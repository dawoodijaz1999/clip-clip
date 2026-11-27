"""One-time TikTok login for clip-clip.

    .venv/bin/python tiktok_login.py

Asks for the app's Client key and Client secret (typed here, never printed), opens
TikTok in the browser, waits for you to tap Allow, then saves TIKTOK_CLIENT_KEY,
TIKTOK_CLIENT_SECRET and TIKTOK_REFRESH_TOKEN as GitHub Actions secrets with `gh`.
"""
import getpass
import http.server
import secrets
import subprocess
import sys
import threading
import urllib.parse
import webbrowser

import requests

REPO = "dawoodijaz1999/clip-clip"
REDIRECT_URI = "https://dawoodijaz1999.github.io/clip-clip/tiktok-callback.html"
SCOPES = "user.info.basic,video.upload"
PORT = 8765

result = {}


class Callback(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        result.update({k: v[0] for k, v in query.items()})
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        text = "TikTok connected. You can close this tab." if "code" in result else "TikTok login failed. Check the terminal."
        self.wfile.write(f"<h2 style='font-family:sans-serif'>{text}</h2>".encode())

    def log_message(self, *args):
        pass


def set_secret(name, value):
    subprocess.run(["gh", "secret", "set", name, "-R", REPO], input=value, text=True, check=True,
                   capture_output=True)


def main():
    client_key = input("TikTok Client key: ").strip()
    client_secret = getpass.getpass("TikTok Client secret (hidden): ").strip()
    if not client_key or not client_secret:
        sys.exit("Both the Client key and Client secret are needed.")

    state = secrets.token_urlsafe(16)
    server = http.server.HTTPServer(("127.0.0.1", PORT), Callback)
    threading.Thread(target=server.handle_request, daemon=True).start()

    url = "https://www.tiktok.com/v2/auth/authorize/?" + urllib.parse.urlencode({
        "client_key": client_key, "scope": SCOPES, "response_type": "code",
        "redirect_uri": REDIRECT_URI, "state": state,
    })
    print("\nOpening TikTok in your browser. Log in as Viral Went and tap Allow.")
    print(f"If it doesn't open, copy this link into the browser:\n{url}\n")
    webbrowser.open(url)

    print("Waiting for TikTok (up to 5 minutes)...")
    for _ in range(300):
        if result:
            break
        threading.Event().wait(1)
    server.server_close()

    if "code" not in result:
        sys.exit(f"No login code received: {result.get('error_description') or result.get('error') or 'timed out'}")
    if result.get("state") != state:
        sys.exit("Login response did not match this session; please run the command again.")

    token = requests.post("https://open.tiktokapis.com/v2/oauth/token/", timeout=60, data={
        "client_key": client_key, "client_secret": client_secret, "code": result["code"],
        "grant_type": "authorization_code", "redirect_uri": REDIRECT_URI,
    }).json()
    if "refresh_token" not in token:
        sys.exit(f"TikTok did not return a login token: {token.get('error_description') or token.get('error') or token}")

    missing = set(SCOPES.split(",")) - set(token.get("scope", "").split(","))
    if missing:
        print(f"Warning: TikTok did not grant {', '.join(sorted(missing))}. Check the app's Scopes page.")

    set_secret("TIKTOK_CLIENT_KEY", client_key)
    set_secret("TIKTOK_CLIENT_SECRET", client_secret)
    set_secret("TIKTOK_REFRESH_TOKEN", token["refresh_token"])
    days = int(token.get("refresh_expires_in", 0)) // 86400
    print(f"\nDone. TikTok is connected and saved to GitHub (login valid for about {days} days).")


if __name__ == "__main__":
    main()
