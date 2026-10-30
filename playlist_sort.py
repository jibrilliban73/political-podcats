#!/usr/bin/env python3
"""Safely sort Spotify playlist items by show A-Z, then release date newest.

Intended to run AFTER an existing playlist synchronization. Never replaces a
playlist, changes listening state, removes an episode, or creates duplicates.
At most MAX_MOVES reorder requests are made per run. Subsequent scheduled runs
continue incrementally when more work is needed.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API = "https://api.spotify.com/v1"
TOKEN_URL = "https://accounts.spotify.com/api/token"
MAX_MOVES = 40
PROFILES = {
    "general": ("863d034412554af6be0f88f51becfe89", "0Hu69WnnxAgPP59X46XMQs", None),
    "politics": ("08d9092293aa43f398d569b4aec930eb", "47qxeTou9y8CP9mlQOttIZ", None),
    "wtj": ("658f7327c1c7439c9cc8cec9aa94de4f", "1A8zHSvbvWAnK0ayho09nj", "2WDgGOFL1vkOEEyGc5W78b"),
}


class SortError(Exception):
    pass


def episode_uri(episode):
    if not isinstance(episode, dict):
        return None
    uri = episode.get("uri")
    if isinstance(uri, str) and uri.startswith("spotify:episode:"):
        return uri
    identifier = episode.get("id")
    if (isinstance(identifier, str) and len(identifier) == 22 and
            identifier.isascii() and identifier.isalnum() and episode.get("type") in (None, "episode")):
        return "spotify:episode:" + identifier
    return None


def extract_uri(entry):
    if not isinstance(entry, dict):
        return None
    item = entry.get("item") or entry.get("track") or {}
    return item.get("uri") if isinstance(item, dict) and isinstance(item.get("uri"), str) else None


def date_rank(episode):
    """ISO release dates (YYYY, YYYY-MM, YYYY-MM-DD) rank newest first."""
    date = episode.get("release_date", "")
    if not isinstance(date, str):
        return 0
    digits = date.replace("-", "")
    if not digits.isdecimal() or len(digits) not in (4, 6, 8):
        return 0
    return int(digits.ljust(8, "0"))


def make_metadata(shows, catalogs):
    """Map episode URI to (show name, release date)."""
    if set(catalogs) != set(shows):
        raise SortError("Not all configured podcast catalogs were fetched; order unchanged")
    metadata = {}
    for show_id, name in shows.items():
        for episode in catalogs[show_id]:
            uri = episode_uri(episode)
            if uri:
                value = (name.casefold(), date_rank(episode))
                if uri in metadata and metadata[uri] != value:
                    raise SortError("Conflicting episode metadata; order unchanged")
                metadata[uri] = value
    return metadata


def desired_indices(entries, metadata):
    """Managed episodes first; unrelated entries retain their relative order.

    Unique occurrence indexes allow existing duplicates or unavailable entries
    to be moved without removing, recreating, or changing any playlist content.
    """
    uris = [extract_uri(e) for e in entries]

    def key(i):
        uri = uris[i]
        if uri in metadata:
            name, date = metadata[uri]
            return (0, name, -date, uri, i)
        return (1, "", 0, "", i)

    return sorted(range(len(entries)), key=key)


def plan_moves(entries, metadata, limit=MAX_MOVES):
    """Return (position moves, projected occurrence-order, is_complete).

    A move is (range_start, insert_before, range_length); each range is
    contiguous, up to 100 items, and is moved earlier into its target position.
    """
    current = list(range(len(entries)))
    wanted = desired_indices(entries, metadata)
    moves = []
    for dest in range(len(wanted)):
        if current[dest] == wanted[dest]:
            continue
        if len(moves) >= limit:
            break
        source = current.index(wanted[dest], dest + 1)
        length = 1
        while (length < 100 and source + length < len(current) and
               dest + length < len(wanted) and
               current[source + length] == wanted[dest + length]):
            length += 1
        block = current[source:source + length]
        del current[source:source + length]
        current[dest:dest] = block
        moves.append((source, dest, length))
    return moves, current, current == wanted


class Spotify:
    def __init__(self, client_id, refresh_token):
        if not refresh_token:
            raise SortError("SPOTIFY_REFRESH_TOKEN missing; do not continue")
        self.client_id = client_id
        self.refresh_token = refresh_token
        self.token = None
        self.expiry = 0

    @staticmethod
    def send(request, action):
        for attempt in range(3):
            try:
                with urlopen(request, timeout=45) as response:
                    body = response.read()
                    return json.loads(body) if body else {}
            except HTTPError as exc:
                if exc.code == 429 and attempt < 2:
                    wait_text = exc.headers.get("Retry-After", "5")
                    wait = int(wait_text) if wait_text.isdecimal() else 5
                    if wait > 30:
                        raise SortError("Spotify rate-limited sorting; a future scheduled run can retry") from None
                    time.sleep(max(wait, 1))
                    continue
                raise SortError(f"Spotify HTTP {exc.code} while {action}; no further moves attempted") from None
            except (OSError, URLError, TimeoutError):
                raise SortError(f"Spotify connection failed while {action}; retry later") from None
        raise SortError("Spotify rate limit exceeded")

    def access_token(self):
        if self.token and time.time() < self.expiry:
            return self.token
        payload = urlencode({"grant_type": "refresh_token", "client_id": self.client_id,
                             "refresh_token": self.refresh_token}).encode()
        data = self.send(Request(TOKEN_URL, data=payload, method="POST"), "refreshing token")
        if data.get("refresh_token") and data["refresh_token"] != self.refresh_token:
            raise SortError("Spotify rotated its refresh token; update GitHub secret before next run")
        if not data.get("access_token"):
            raise SortError("Spotify returned no access token")
        self.token = data["access_token"]
        self.expiry = time.time() + max(0, int(data.get("expires_in", 3600)) - 60)
        return self.token

    def request(self, method, path, *, params=None, body=None):
        url = API + path
        if params:
            url += "?" + urlencode(params)
        payload = None if body is None else json.dumps(body).encode()
        req = Request(url, method=method, data=payload,
                      headers={"Authorization": "Bearer " + self.access_token(),
                               "Content-Type": "application/json"})
        return self.send(req, f"{method} {path}")

    def pages(self, path, **other):
        offset = 0
        total = None
        for _ in range(2000):
            page = self.request("GET", path, params={"limit": 50, "offset": offset, **other})
            items = page.get("items")
            if not isinstance(items, list):
                raise SortError("Spotify returned an invalid catalog/playlist page")
            page_total = page.get("total")
            if isinstance(page_total, int):
                if total is not None and total != page_total:
                    raise SortError("Spotify changed the list during pagination; retry next run")
                total = page_total
            yield from items
            offset += len(items)
            if not page.get("next"):
                if total is not None and total != offset:
                    raise SortError("Spotify supplied incomplete pagination; order unchanged")
                return
            if not items:
                raise SortError("Spotify returned empty page with a next link")
        raise SortError("Spotify pagination exceeded safety limit")


def order_playlist(client, playlist_id, shows, *, max_moves=MAX_MOVES, catalogs=None):
    path = f"/playlists/{playlist_id}/items"
    # Check a snapshot before and after reading; never move positions from a
    # stale playlist state. New sync runs are serialized by GitHub concurrency.
    start_snapshot = client.request("GET", f"/playlists/{playlist_id}").get("snapshot_id")
    if not isinstance(start_snapshot, str) or not start_snapshot:
        raise SortError("No Spotify playlist snapshot; no moves attempted")
    entries = list(client.pages(path, additional_types="track,episode"))
    if client.request("GET", f"/playlists/{playlist_id}").get("snapshot_id") != start_snapshot:
        raise SortError("Playlist changed during read; retry next scheduled run")
    if catalogs is None:
        catalogs = {show_id: list(client.pages(f"/shows/{show_id}/episodes")) for show_id in shows}
    if any(not catalog for catalog in catalogs.values()):
        raise SortError("A podcast catalog is empty; no moves attempted")
    metadata = make_metadata(shows, catalogs)
    moves, projected, done = plan_moves(entries, metadata, max_moves)
    if not moves:
        print(f"SORT VERIFIED: {len(entries)} items already grouped alphabetically and newest-first")
        return True

    snapshot = start_snapshot
    for source, dest, count in moves:
        response = client.request("PUT", path, body={"range_start": source, "insert_before": dest,
                                                    "range_length": count, "snapshot_id": snapshot})
        snapshot = response.get("snapshot_id")
        if not snapshot:
            raise SortError("Spotify did not confirm playlist reorder snapshot")

    # Independently reread to confirm neither content nor ordering was lost.
    after = list(client.pages(path, additional_types="track,episode"))
    before_uris = [extract_uri(e) for e in entries]
    after_uris = [extract_uri(e) for e in after]
    expected = [before_uris[i] for i in projected]
    if len(after) != len(entries) or Counter(after_uris) != Counter(before_uris):
        raise SortError("Playlist contents changed while sorting; inspect the saved playlist")
    if after_uris != expected:
        raise SortError("Spotify returned a different order; inspect the playlist before retrying")
    if done:
        print(f"SORT VERIFIED: {len(after)} items grouped by podcast A-Z, newest release first ({len(moves)} moves)")
    else:
        print(f"SORT PROGRESS: {len(moves)} safe reorder moves completed; future scheduled runs continue")
    return done


def main():
    parser = argparse.ArgumentParser(description="Group Spotify episodes by podcast and release date")
    parser.add_argument("--profile", required=True, choices=sorted(PROFILES))
    parser.add_argument("--max-moves", type=int, default=MAX_MOVES)
    args = parser.parse_args()
    if not 1 <= args.max_moves <= 80:
        parser.error("max-moves must be from 1 to 80")
    client_id, playlist_id, show_id = PROFILES[args.profile]
    if show_id:
        shows = {show_id: "What's The Juice?"}
    else:
        config = json.loads(Path(__file__).with_name("podcast_config.json").read_text(encoding="utf-8"))
        if config.get("playlist_id") != playlist_id:
            raise SortError("Unexpected playlist ID in configuration; refusing reorder")
        shows = config["shows"]
    if not shows or not isinstance(shows, dict):
        raise SortError("No podcasts configured; refusing reorder")
    order_playlist(Spotify(client_id, os.environ.get("SPOTIFY_REFRESH_TOKEN", "").strip()),
                   playlist_id, shows, max_moves=args.max_moves)


if __name__ == "__main__":
    try:
        main()
    except (SortError, ValueError, KeyError, TypeError, OSError) as exc:
        print(f"Sorting failed: {exc}", file=sys.stderr)
        sys.exit(1)
