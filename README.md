# Politics podcast playlist sync

This project is configured for [Politics podcast](https://open.spotify.com/playlist/47qxeTou9y8CP9mlQOttIZ) and six shows: Left In The Attic, IHIP News, I've Had It, Seamossgirlies, Critical Babe Theory, and Hasan Minhaj Doesn't Know.

Once Spotify is authorized, the workflow checks recent episodes and the playlist approximately every 30 minutes. It also checks all available episodes every six hours, so older episodes newly marked unplayed can be picked up. GitHub schedules can run late.

The workflow adds missing episodes Spotify identifies as unfinished (unplayed or in progress) and removes episodes of these six shows that Spotify identifies as finished. Other shows, music, and episodes with unknown playback status are left alone. It reads the existing playlist before making changes and avoids adding known duplicates.

## Current setup status

This repository needs Spotify authorization before it can sync. **A Spotify developer app in Development Mode requires its owner to have Spotify Premium.** A Spotify Free account by itself cannot run this developer-app approach. The Spotify app must have access to the account that owns the playlist, and the authenticated user must own or collaborate on it.

The workflow exits without changing the playlist when credentials are missing. A successful connection and a preview are needed to verify the setup; merely creating this repository does not mean the sync is working.

## One-time setup

1. In the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard), create or select your app. Record its **Client ID** and add this exact redirect URI in its settings: `http://127.0.0.1:8888/callback`. If you use a different Spotify account from the app owner, add the listening account under **Users and Access**.
2. On your own computer, download this repository and install Python 3.10 or later. Open Terminal or PowerShell in the downloaded folder and run `python3 authorize.py` (Windows: `py authorize.py`). Enter the Client ID and approve access in Spotify. The computer is needed only for this one-time authorization.
3. In this repository's **Settings → Secrets and variables → Actions → New repository secret**, add `SPOTIFY_CLIENT_ID` with the Client ID and `SPOTIFY_REFRESH_TOKEN` with the refresh token displayed in your terminal. Do not put tokens into this repository or paste them into chat.
4. In **Actions → Politics podcast sync → Run workflow**, leave **Preview without editing the playlist** and **Check the entire back catalog** checked. Run it and inspect the counts and any errors in the log.
5. Scheduled runs will then run automatically in GitHub. If you want the first full sync immediately, run the workflow again with **Preview** unchecked and **Check the entire back catalog** checked.

If the preview reports unknown playback states, those episodes will be skipped rather than guessed. If Spotify returns a 403, verify Premium, app authorization and allowlisting, and playlist ownership. If Spotify changes or expires the refresh token, repeat the authorization and update the secret. Spotify may also impose API quotas; a quota error stops the run without guessing at missing data. GitHub can disable scheduled workflows in inactive public repositories.
