#!/usr/bin/env python3
"""Reconcile complete podcast catalogs, then verify the saved playlist.

Reports contain aggregate counts only. Credentials and individual listening
states are never printed or uploaded. The existing playlist is never replaced.
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
from urllib.request import Request

from spotify_http import send_json
from playlist_sort import SortError, desired_indices, make_metadata, order_playlist

API_ROOT = "https://api.spotify.com/v1"
TOKEN_URL = "https://accounts.spotify.com/api/token"


class SyncError(Exception):
    pass


class Spotify:
    def __init__(self, client_id, refresh_token):
        if not client_id or not refresh_token:
            raise SyncError("Spotify authorization is missing; no playlist changes made.")
        self.client_id = client_id
        self.refresh_token = refresh_token
        self.token = ""
        self.expires_at = 0

    @staticmethod
    def send(request, label):
        return send_json(request, label, SyncError)

    def access_token(self):
        if self.token and time.time() < self.expires_at:
            return self.token
        body = urlencode({"grant_type": "refresh_token", "refresh_token": self.refresh_token,
                          "client_id": self.client_id}).encode()
        result = self.send(Request(TOKEN_URL, data=body, method="POST",
                                  headers={"Content-Type": "application/x-www-form-urlencoded"}), "token refresh")
        if result.get("refresh_token") and result["refresh_token"] != self.refresh_token:
            raise SyncError("Spotify rotated the refresh token. Update the encrypted credential before the next run.")
        if not result.get("access_token"):
            raise SyncError("Spotify did not return an access token.")
        self.token = result["access_token"]
        self.expires_at = time.time() + max(0, int(result.get("expires_in", 3600)) - 60)
        return self.token

    def request(self, method, path, *, params=None, body=None):
        url = API_ROOT + path + (("?" + urlencode(params)) if params else "")
        return self.send(Request(url, method=method,
                                data=None if body is None else json.dumps(body).encode(),
                                headers={"Authorization": "Bearer " + self.access_token(),
                                         "Content-Type": "application/json"}), f"{method} {path}")

    def pages(self, path, **extra):
        offset = 0
        expected_total = None
        seen_pages = set()
        for _ in range(2000):
            page = self.request("GET", path, params={"limit": 50, "offset": offset, **extra})
            items = page.get("items")
            if not isinstance(items, list):
                raise SyncError(f"Invalid Spotify page for {path}; no complete catalog available.")
            total = page.get("total")
            if isinstance(total, int):
                if expected_total is not None and total != expected_total:
                    raise SyncError("Spotify's catalog changed during pagination; retry for a consistent scan.")
                expected_total = total
            fingerprint = json.dumps(items, sort_keys=True)
            if items and fingerprint in seen_pages:
                raise SyncError("Spotify repeated a page; refusing an incomplete scan.")
            seen_pages.add(fingerprint)
            yield from items
            offset += len(items)
            if not page.get("next"):
                if expected_total is not None and offset != expected_total:
                    raise SyncError(f"Incomplete pagination for {path}: read {offset}, expected {expected_total}.")
                return
            if not items:
                raise SyncError("Spotify returned an empty page with a next link.")
        raise SyncError("Spotify pagination exceeded its safety limit.")


def item_from_entry(entry):
    if not isinstance(entry, dict):
        return {}
    item = entry.get("item") or entry.get("track") or {}
    return item if isinstance(item, dict) else {}


def counts(entries):
    return Counter(item["uri"] for entry in entries
                   if isinstance((item := item_from_entry(entry)).get("uri"), str))


def scan_catalogs(client, config):
    catalogs = {}
    summaries = []
    for show_id, show_name in config["shows"].items():
        episodes = list(client.pages(f"/shows/{show_id}/episodes"))
        if not episodes:
            raise SyncError(f"No episodes returned for {show_name}; refusing to claim a complete scan.")
        unique = {}
        summary = {"show": show_name, "catalog": 0, "unplayed": 0, "in_progress": 0,
                   "finished": 0, "unknown": 0, "unavailable_unfinished": 0,
                   "unavailable_placeholders": 0}
        for episode in episodes:
            # Spotify may return null or unplayable placeholders in a show's pages.
            # These cannot be added, played or safely removed, so count and skip.
            if episode is None:
                summary["unavailable_placeholders"] += 1
                continue
            if not isinstance(episode, dict):
                raise SyncError(f"Unexpected episode record in {show_name}; stopping safely.")
            uri = episode.get("uri")
            if not isinstance(uri, str) or not uri.startswith("spotify:episode:"):
                episode_id = episode.get("id")
                if (episode.get("type") in (None, "episode") and
                        isinstance(episode_id, str) and len(episode_id) == 22 and
                        episode_id.isascii() and episode_id.isalnum()):
                    # Recover standard episode URI from an otherwise valid ID.
                    uri = "spotify:episode:" + episode_id
                    episode = {**episode, "uri": uri}
                elif episode.get("is_playable") is False and not episode_id:
                    summary["unavailable_placeholders"] += 1
                    continue
                else:
                    raise SyncError(f"Unidentifiable episode in {show_name}; stopping safely.")
            point = episode.get("resume_point")
            if (episode.get("is_playable") is False and
                    (not isinstance(point, dict) or
                     not isinstance(point.get("fully_played"), bool))):
                # Do not guess playback for unavailable episodes.
                summary["unavailable_placeholders"] += 1
                continue
            if uri in unique:
                if unique[uri] != episode:
                    raise SyncError(f"Conflicting episode states in {show_name}; retry later.")
                continue
            unique[uri] = episode
            point = episode.get("resume_point")
            state = point.get("fully_played") if isinstance(point, dict) else None
            if not isinstance(state, bool):
                summary["unknown"] += 1
            elif state:
                summary["finished"] += 1
            elif episode.get("is_playable") is False:
                summary["unavailable_unfinished"] += 1
            elif point.get("resume_position_ms", 0) > 0:
                summary["in_progress"] += 1
            else:
                summary["unplayed"] += 1
        if not unique:
            raise SyncError(f"No identifiable episodes for {show_name}; stopping safely.")
        summary["catalog"] = len(unique)
        summaries.append(summary)
        catalogs[show_id] = unique
    if any(s["unknown"] for s in summaries):
        raise SyncError("Some episodes have unknown playback status. Check user-read-playback-position; playlist unchanged.")
    return catalogs, summaries


def build_plan(entries, catalogs, excluded_ids=(), excluded_uris=()):
    current = counts(entries)
    catalog = {uri: episode for episodes in catalogs.values() for uri, episode in episodes.items()}
    desired = {uri for uri, e in catalog.items()
               if e["resume_point"]["fully_played"] is False and e.get("is_playable") is not False}
    finished = {uri for uri, e in catalog.items() if e["resume_point"]["fully_played"] is True}
    excluded = set(excluded_uris)
    for entry in entries:
        item = item_from_entry(entry)
        show = item.get("show") or {}
        if isinstance(show, dict) and show.get("id") in excluded_ids and item.get("uri"):
            excluded.add(item["uri"])
    if desired & excluded:
        raise SyncError("A show is both included and excluded in the configuration.")
    duplicates = {uri for uri in desired if current[uri] > 1}
    remove = (finished | excluded | duplicates) & set(current)
    add = [uri for uri in catalog if uri in desired and (not current[uri] or uri in duplicates)]
    return {"desired": desired, "finished": finished, "excluded": excluded,
            "remove": sorted(remove), "add": add, "duplicates": duplicates,
            "baseline": current}


def audit(plan, entries):
    after = counts(entries)
    return {"missing": sum(after[uri] == 0 for uri in plan["desired"]),
            "duplicate_copies": sum(max(0, after[uri] - 1) for uri in plan["desired"]),
            "finished_remaining": sum(after[uri] for uri in plan["finished"]),
            "excluded_remaining": sum(after[uri] for uri in plan["excluded"])}


def reconcile(client, config, *, dry_run=False, verify_only=False, sort_playlist=False):
    if set(config["shows"]) & set(config.get("excluded_shows", [])):
        raise SyncError("Included and excluded shows overlap.")
    path = f'/playlists/{config["playlist_id"]}/items'
    before = list(client.pages(path, additional_types="track,episode"))
    catalogs, summary = scan_catalogs(client, config)
    excluded_ids = config.get("excluded_shows", [])
    excluded_uris = set()
    for show_id in excluded_ids:
        excluded_uris.update(e["uri"] for e in client.pages(f"/shows/{show_id}/episodes")
                             if isinstance(e, dict) and e.get("uri"))
    plan = build_plan(before, catalogs, excluded_ids, excluded_uris)
    for row, episodes in zip(summary, catalogs.values()):
        row["required"] = row["unplayed"] + row["in_progress"]
        row["already_present"] = sum(uri in plan["desired"] and plan["baseline"][uri] > 0 for uri in episodes)
    result = {"playlist": config["name"], "shows": summary, "full_catalog_scan": True,
              "required": len(plan["desired"]), "add": len(plan["add"]),
              "remove_copies": sum(plan["baseline"][uri] for uri in plan["remove"]),
              "duplicate_episodes_repaired": len(plan["duplicates"]),
              "dry_run": dry_run, "verify_only": verify_only}
    if not dry_run and not verify_only:
        for start in range(0, len(plan["remove"]), 100):
            client.request("DELETE", path, body={"items": [{"uri": uri} for uri in plan["remove"][start:start + 100]]})
        for start in range(0, len(plan["add"]), 100):
            client.request("POST", path, body={"uris": plan["add"][start:start + 100]})
        # Independently reread every saved playlist page, even on a no-op run.
        after = list(client.pages(path, additional_types="track,episode"))
    else:
        after = before
    result["audit"] = audit(plan, after)
    result["playlist_items"] = len(after)
    result["verified"] = not any(result["audit"].values())
    for row, episodes in zip(summary, catalogs.values()):
        actual = counts(after)
        row["present_once"] = sum(uri in plan["desired"] and actual[uri] == 1 for uri in episodes)
    if not dry_run and not result["verified"]:
        raise SyncError("Playlist verification failed: " + json.dumps(result["audit"], sort_keys=True))
    if sort_playlist:
        sort_catalogs = {sid: list(episodes.values()) for sid, episodes in catalogs.items()}
        if dry_run or verify_only:
            metadata = make_metadata(config["shows"], sort_catalogs)
            result["ordered"] = desired_indices(after, metadata) == list(range(len(after)))
            if verify_only and not result["ordered"]:
                raise SyncError("Membership verified but playlist order needs repair.")
        else:
            result["ordered"] = order_playlist(
                client, config["playlist_id"], config["shows"],
                catalogs=sort_catalogs, max_moves=3000,
            )
            if not result["ordered"]:
                raise SyncError("Membership verified; safe sorting made progress and will continue next run.")
    return result


def emit_report(result):
    # Aggregate counts only: do not publish per-episode listening history.
    print(json.dumps(result, indent=2))
    status = "VERIFIED" if result["verified"] else "PREVIEW: changes needed"
    lines = [f'## {result["playlist"]}: {status}', "",
             f'Full catalog scan. Required unfinished episodes: {result["required"]}. Playlist items: {result["playlist_items"]}.', "",
             "| Podcast | Catalog | Unplayed | In progress | Finished | Unavailable unfinished | Unavailable placeholders | Present once |",
             "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for row in result["shows"]:
        lines.append(f'| {row["show"]} | {row["catalog"]} | {row["unplayed"]} | {row["in_progress"]} | {row["finished"]} | {row["unavailable_unfinished"]} | {row["unavailable_placeholders"]} | {row["present_once"]} |')
    lines += ["", f'Added/planned: {result["add"]}; removed/planned copies: {result["remove_copies"]}.',
              "Audit: " + ", ".join(f"{key}={value}" for key, value in result["audit"].items()), ""]
    if "ordered" in result:
        lines += ["Order: " + ("VERIFIED: newest episode first across all podcasts." if result["ordered"] else "Preview: sorting needed."), ""]
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as target:
            target.write("\n".join(lines))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="podcast_config.json")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--verify-only", action="store_true")
    parser.add_argument("--sort", action="store_true", help="Sort all podcasts by newest release; reuse the full catalog scan")
    args = parser.parse_args()
    try:
        config = json.loads(Path(args.config).read_text(encoding="utf-8"))
        client = Spotify(os.environ.get("SPOTIFY_CLIENT_ID", "").strip(),
                         os.environ.get("SPOTIFY_REFRESH_TOKEN", "").strip())
        emit_report(reconcile(client, config, dry_run=args.dry_run, verify_only=args.verify_only, sort_playlist=args.sort))
        return 0
    except (SyncError, SortError, ValueError, KeyError, TypeError, OSError) as exc:
        # Exceptions from Spotify are sanitized above; never print tokens or raw responses.
        message = f"Sync failed: {exc}"
        print(message, file=sys.stderr)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as target:
                target.write("## Sync failed\n\n" + message + "\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
