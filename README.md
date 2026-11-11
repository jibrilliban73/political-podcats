# Politics podcast sync

This repository independently synchronizes the [Politics Spotify playlist](https://open.spotify.com/playlist/47qxeTou9y8CP9mlQOttIZ) using the GitHub Actions workflow at [`.github/workflows/politics-sync.yml`](.github/workflows/politics-sync.yml). The active show list is stored in [`podcast_config.json`](podcast_config.json).

## Schedule (Calgary time)

- **6:00 AM and 6:00 PM:** Lightweight, read-only check of the playlist and each configured show's latest episode.
- **7:00 AM and 7:00 PM:** Live sync and newest-first sorting attempt, provided the earlier check succeeded and Spotify is not in cooldown.
- **Sunday morning live sync:** Also scan the full IHIP News history to reconcile older episodes and playback states; the evening run uses the recent window.
- **Manual run:** GitHub **Actions → Politics podcast sync → Run workflow**. A push to `main` affecting tracked workflow/code/config files runs offline preflight tests **only**; it does not access Spotify.

GitHub-hosted schedules can be delayed. The workflow uses `America/Edmonton` for the local schedule.

## What the automation does

The live sync scans the playlist and each configured podcast. Routine runs read IHIP News episodes covering at least the past seven days, plus an older boundary page; Sunday morning it attempts its full history. All other configured shows are scanned in full at their existing twice-daily times. This reduces routine requests while preserving a weekly attempt to reconcile older IHIP listening progress. It then:

- Adds playable **unplayed** and **in-progress** episodes that are missing.
- Removes **fully played** managed episodes and repairs managed duplicates within the scanned scope. Older IHIP episodes are reconciled during full-history runs.
- Leaves unrelated playlist entries alone.
- Reads the playlist again to verify membership after changes.
- Attempts to put episodes **newest first across all podcasts**, not grouped by creator.

For safety and Spotify rate limits, sorting is limited to **20 reorder moves per live sync**. A recent-only run reports `scanned_scope_verified` separately and does not claim full membership verification. Treat newest-first sorting as complete only when a completed live run explicitly reports `"ordered": true`, `"full_catalog_scan": true`, and `"verified": true`.

Unknown playback state, incomplete catalog scans, invalid credentials, and failed verification can stop the run. Spotify-requested cooldowns can safely defer checks and syncs without touching the playlist.

## Configured playlist and shows

The following are the **7** currently configured shows. To change the list, edit `podcast_config.json` (not the README). 
```json
{
  "name": "Politics podcast",
  "playlist_id": "47qxeTou9y8CP9mlQOttIZ",
  "shows": {
    "30CG2yb1mBN9ahkaZUKT69": "Left In The Attic",
    "4gnbg5moE7d882Ag5Yhiz2": "IHIP News",
    "5HYmraLuzUrzy7yX5mzMLB": "I've Had It",
    "2po3b2uU76tfZ59K0Tqm3V": "Seamossgirlies",
    "3aM90v8H0DOqkAQaHsSAY2": "Critical Babe Theory",
    "3tmYU34mCKNjJrpsE2uDcB": "Hasan Minhaj Doesn't Know",
    "3kgamV3lbZIGIHW2H0XUTR": "The Tara Palmeri Show"
  }
}
```

## Running a safe test

1. Open [Politics podcast sync in GitHub Actions](https://github.com/jibrilliban73/political-podcats/actions/workflows/politics-sync.yml).
2. Choose **Run workflow** on `main`.
3. Keep **`dry_run = true`**, **`verify_only = false`**, and **`check_only = false`** to preview counts, additions/removals, and sorting **without making playlist changes**. Select **`full_history = true`** to include all older IHIP episodes.
4. Review the job summary/logs to verify that all 7 shows were scanned, including The Tara Palmeri Show. A green run alone does not confirm a Spotify scan if access was deferred due to cooldown.

For a read-only full membership-and-ordering audit, select `verify_only = true`; it always scans the full history and may fail if sorting is still in progress. Select `check_only = true` for the lightweight read-only check. Uncheck `dry_run` only when intentionally requesting a manual live sync.

## Spotify authorization

The workflow uses repository-secret `SPOTIFY_REFRESH_TOKEN` with the existing dedicated Spotify client ID `08d9092293aa43f398d569b4aec930eb`. It expects scopes `playlist-read-private playlist-read-collaborative playlist-modify-private playlist-modify-public user-read-playback-position`.

**Adding a show to `podcast_config.json` does not require a new Spotify app or a new authorization** if the existing token remains valid. Never commit or share the refresh token. Spotify Premium and API authorization must remain valid; reauthorize only if a run reports invalid/revoked credentials.

## GitHub notifications

In GitHub notification settings, enable **Actions → Email** and leave **Only notify for failed workflows** unchecked to receive applicable workflow completion notifications. Delivery should be checked in the mailbox; a successful workflow status is not proof an email was delivered.

## Local tests

Run `python -m unittest -v` to test the code offline. These tests do not establish that the live Spotify connection works.
