# Politics podcast sync

This repository has its own independent GitHub Actions workflow at `.github/workflows/politics-sync.yml`, scheduled every 15 minutes. It uses this repository's own encrypted `SPOTIFY_REFRESH_TOKEN` and the dedicated Spotify client ID `08d9092293aa43f398d569b4aec930eb`. It does not call another playlist repository.

## What the workflow checks

Every run reads the full catalog of every configured show and every playlist page. It adds playable unplayed and in-progress episodes once, repairs managed duplicates, removes finished managed episodes, and preserves unrelated entries. Unknown playback status, incomplete pagination, expired credentials, failed additions, and failed verification make the run fail visibly.

A live sync rereads the saved playlist; a separate second step rescans all catalogs and audits the saved playlist without editing it. Both must pass before the workflow succeeds. Unit tests are code checks and do not establish that the live Spotify connection works.

## Authorization and running

The required `SPOTIFY_REFRESH_TOKEN` must be authorized for client ID `08d9092293aa43f398d569b4aec930eb`, with `playlist-read-private playlist-read-collaborative playlist-modify-private playlist-modify-public user-read-playback-position`. A public client ID alone is insufficient. Store the refresh token only as this repository's encrypted Actions secret, never in source code or chat.

Open this repository's Actions tab and select **Politics podcast sync**. Use **Run workflow** with **dry_run** checked for a preview; uncheck it for a live sync. Use **verify_only** for a read-only full audit. Scheduled runs perform a live sync and independent audit.

## Email on pass or fail

In GitHub notification settings, enable **Actions → Email** and leave **Only notify for failed workflows** unchecked. Each repository produces its own workflow completion notification. The scheduled workflow's actor receives these notifications. Email delivery must be checked in the mailbox; a GitHub success status alone does not prove email arrival.

GitHub schedules may be delayed. Premium and Spotify authorization must remain valid. Renew expired or revoked authorization when a run reports it.

## Configured playlist and shows

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
    "3tmYU34mCKNjJrpsE2uDcB": "Hasan Minhaj Doesn't Know"
  }
}
```

## Local validation

Run `python -m unittest -v`. Tests cover pagination, incomplete reads, duplicate repair, preservation of unrelated entries, missing additions, and idempotent reruns.
