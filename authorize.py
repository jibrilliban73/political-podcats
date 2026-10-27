#!/usr/bin/env python3
"""One-time local Spotify PKCE authorization; do not run in GitHub Actions."""

import base64
import hashlib
import json
import secrets
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer


REDIRECT = "http://127.0.0.1:8888/callback"
SCOPES = "user-read-playback-position playlist-read-private playlist-modify-private playlist-modify-public"


def main():
    print("Run this once on your own computer, not in GitHub Actions.")
    client_id = input("Spotify app Client ID: ").strip()
    if not client_id:
        raise SystemExit("A Spotify Client ID is required.")
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(24)
    query = urllib.parse.urlencode({
        "client_id": client_id, "response_type": "code", "redirect_uri": REDIRECT,
        "scope": SCOPES, "code_challenge_method": "S256",
        "code_challenge": challenge, "state": state,
    })
    url = "https://accounts.spotify.com/authorize?" + query
    received = {}

    class Callback(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            if parsed.path != "/callback" or params.get("state", [""])[0] != state:
                received["error"] = "Invalid callback or state."
            elif params.get("error"):
                received["error"] = "Spotify authorization was denied."
            else:
                received["code"] = params.get("code", [""])[0]
            self.send_response(200 if received.get("code") else 400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"You may close this tab and return to your terminal.")

        def log_message(self, *args):
            pass

    with HTTPServer(("127.0.0.1", 8888), Callback) as server:
        server.timeout = 180
        print("Approve Spotify access in your browser. If it does not open, use this link:")
        print(url)
        webbrowser.open(url)
        server.handle_request()
    if not received.get("code"):
        raise SystemExit(received.get("error", "No Spotify response in three minutes; try again."))
    payload = urllib.parse.urlencode({
        "grant_type": "authorization_code", "code": received["code"],
        "redirect_uri": REDIRECT, "client_id": client_id, "code_verifier": verifier,
    }).encode()
    request = urllib.request.Request("https://accounts.spotify.com/api/token", data=payload, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            token = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"Spotify authorization failed with HTTP {exc.code}. Check app settings.") from None
    if not token.get("refresh_token"):
        raise SystemExit("Spotify did not return a refresh token.")
    print("\nCopy the following refresh token directly into the SPOTIFY_REFRESH_TOKEN")
    print("GitHub Actions secret. Never put it in chat or a repository file:")
    print(token["refresh_token"])


if __name__ == "__main__":
    main()
