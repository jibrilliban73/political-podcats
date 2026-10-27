#!/usr/bin/env python3
"""Keep a Spotify playlist in sync with six podcast shows.

Only explicit Spotify playback states are acted on. Episodes from other shows
and items with an unknown playback state are left alone.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


PLAYLIST_ID = "47qxeTou9y8CP9mlQOttIZ"
SHOWS = {
    "30CG2yb1mBN9ahkaZUKT69": "Left In The Attic",
    "4gnbg5moE7d882Ag5Yhiz2": "IHIP News",
    "5HYmraLuzUrzy7yX5mzMLB": "I've Had It",
    "2po3b2uU76tfZ59K0Tqm3V": "Seamossgirlies",
    "3aM90v8H0DOqkAQaHsSAY2": "Critical Babe Theory",
    "3tmYU34mCKNjJrpsE2uDcB": "Hasan Minhaj Doesn't Know",
}
API_ROOT = "https://api.spotify.com/v1"
TOKEN_URL = "https://accounts.spotify.com/api/token"


class SyncError(Exception):
    pass


def playback_state(item):
    """Return True, False, or None: unknown is never assumed to be unplayed."""
    point = item.get("resume_point")
    if not isinstance(point, dict):
        return None
    value = point.get("fully_played")
    return value if isinstance(value, bool) else None


def plan_changes(playlist_entries, show_episodes):
    """Return changes for managed episodes; preserve all unrelated items."""
    existing = set()
    states = {}
    for entry in playlist_entries:
        item = entry.get("item") or entry.get("track") or {}
        if not isinstance(item, dict):
            continue
        uri = item.get("uri")
        if not isinstance(uri, str):
            continue
        existing.add(uri)
        show = item.get("show") or {}
        if uri.startswith("spotify:episode:") and isinstance(show, dict) and show.get("id") in SHOWS:
            states[uri] = (playback_state(item), item.get("is_playable") is not False)

    # The show response is read after the playlist and takes precedence if an
    # episode's state changes between the two requests.
    for show_id, episodes in show_episodes.items():
        if show_id not in SHOWS:
            continue
        for episode in episodes:
            if not isinstance(episode, dict):
                continue
            uri = episode.get("uri")
            if isinstance(uri, str) and uri.startswith("spotify:episode:"):
                states[uri] = (playback_state(episode), episode.get("is_playable") is not False)

    add = [uri for uri, (played, playable) in states.items()
           if played is False and playable and uri not in existing]
    remove = [uri for uri, (played, _) in states.items()
              if played is True and uri in existing]
    unknown = sum(played is None for played, _ in states.values())
    return add, remove, unknown, len(states)


class Spotify:
    def __init__(self, client_id, refresh_token):
        self.client_id = client_id
        self.refresh_token = refresh_token
        self.token = None
        self.expires_at = 0

    @staticmethod
    def _send(request, label):
        for attempt in range(4):
            try:
                with urllib.request.urlopen(request, timeout=45) as response:
                    raw = response.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt < 3:
                    try:
                        body = json.loads(exc.read())
                        reason = body.get("error", {}).get("reason")
                    except (ValueError, AttributeError):
                        reason = None
                    if reason == "QUOTA_EXCEEDED":
                        raise SyncError("Spotify's development-mode API quota was exceeded; retry later.") from None
                    wait = exc.headers.get("Retry-After", "3")
                    wait = int(wait) if wait.isdigit() else 3
                    if wait > 60:
                        raise SyncError("Spotify requested a long rate-limit delay; retry later.") from None
                    time.sleep(max(wait, 1))
                    continue
                hint = " Check Premium, app authorization, playlist ownership, and Spotify permissions." if exc.code == 403 else ""
                raise SyncError(f"Spotify returned HTTP {exc.code} for {label}.{hint}") from None
            except (urllib.error.URLError, TimeoutError, OSError):
                # A mutation may have succeeded despite a lost response. Stop;
                # a later run will reread the playlist before making changes.
                raise SyncError(f"Network error during {label}; retry the workflow later.") from None
            except ValueError:
                raise SyncError(f"Spotify returned invalid JSON for {label}.") from None
        raise SyncError("Spotify rate-limit retries exhausted.")

    def access_token(self):
        if self.token and time.time() < self.expires_at:
            return self.token
        payload = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "client_id": self.client_id,
        }).encode()
        request = urllib.request.Request(TOKEN_URL, data=payload, method="POST")
        data = self._send(request, "token refresh")
        new_refresh = data.get("refresh_token")
        if new_refresh and new_refresh != self.refresh_token:
            raise SyncError("Spotify rotated the refresh token; reauthorize and update the GitHub secret.")
        if not data.get("access_token"):
            raise SyncError("Spotify did not return an access token.")
        self.token = data["access_token"]
        self.expires_at = time.time() + int(data.get("expires_in", 3600)) - 60
        return self.token

    def request(self, method, path, params=None, body=None):
        url = API_ROOT + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        encoded = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=encoded, method=method, headers={
            "Authorization": "Bearer " + self.access_token(),
            "Content-Type": "application/json",
        })
        return self._send(request, f"{method} {path}")

    def pages(self, path, params=None, first_page_only=False):
        offset = 0
        while True:
            query = {"limit": 50, "offset": offset, **(params or {})}
            page = self.request("GET", path, params=query)
            items = page.get("items")
            if not isinstance(items, list):
                raise SyncError(f"Spotify returned an invalid page for {path}; playlist unchanged.")
            yield from items
            if first_page_only or not page.get("next"):
                return
            if not items:
                raise SyncError(f"Spotify returned an empty page with a next page for {path}.")
            offset += len(items)


def sync(client, dry_run=False, full=False):
    entries = list(client.pages(f"/playlists/{PLAYLIST_ID}/items", {"additional_types": "track,episode"}))
    episodes = {}
    for show_id, show_name in SHOWS.items():
        episodes[show_id] = list(client.pages(
            f"/shows/{show_id}/episodes", first_page_only=not full
        ))
        print(f"Checked {len(episodes[show_id])} episodes of {show_name}.")
    add, remove, unknown, examined = plan_changes(entries, episodes)
    print(f"Managed episodes examined: {examined}; unknown playback state: {unknown}; "
          f"to add: {len(add)}; finished to remove: {len(remove)}.")
    if examined and unknown == examined:
        raise SyncError("No playback states were available. Check the user-read-playback-position scope.")
    if dry_run:
        print("Preview only: playlist was not changed.")
        return
    # Never replace the full playlist. Only remove explicitly finished managed
    # episodes, and append missing unfinished managed episodes.
    for start in range(0, len(remove), 100):
        chunk = remove[start:start + 100]
        client.request("DELETE", f"/playlists/{PLAYLIST_ID}/items",
                       body={"items": [{"uri": uri} for uri in chunk]})
        print(f"Removed {min(start + 100, len(remove))} of {len(remove)} finished episodes.")
    for start in range(0, len(add), 100):
        chunk = add[start:start + 100]
        client.request("POST", f"/playlists/{PLAYLIST_ID}/items", body={"uris": chunk})
        print(f"Added {min(start + 100, len(add))} of {len(add)} unfinished episodes.")
    print("Sync complete.")


def main():
    parser = argparse.ArgumentParser(description="Sync politics podcasts to Spotify")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without editing")
    parser.add_argument("--full", action="store_true", help="Scan all episodes, not only the newest 50 per show")
    args = parser.parse_args()
    client_id = os.environ.get("SPOTIFY_CLIENT_ID", "").strip()
    refresh_token = os.environ.get("SPOTIFY_REFRESH_TOKEN", "").strip()
    if not client_id or not refresh_token:
        raise SyncError("Set SPOTIFY_CLIENT_ID and SPOTIFY_REFRESH_TOKEN as repository secrets first.")
    sync(Spotify(client_id, refresh_token), dry_run=args.dry_run, full=args.full)


if __name__ == "__main__":
    try:
        main()
    except (SyncError, ValueError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
