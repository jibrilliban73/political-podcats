#!/usr/bin/env python3
"""Small read-only Spotify check for the scheduled one-hour-later sync.

Reads one playlist page and one most-recent episode per subscribed show.
Never retrieves detailed playback history, reorders, adds or removes episodes.
Records only a success timestamp for the next scheduled sync.
"""
import json
import os
from pathlib import Path
import sys
import time
from verified_sync import Spotify, SyncError


def light_check(client, config):
    playlist = config.get('playlist_id')
    shows = config.get('shows', {})
    if not isinstance(playlist, str) or not playlist or not isinstance(shows, dict) or not shows:
        raise SyncError('Invalid playlist configuration: no Spotify changes made.')
    page = client.request('GET', f'/playlists/{playlist}/items', params={'limit': 1, 'offset': 0})
    total = page.get('total')
    if not isinstance(total, int) or total < 0:
        raise SyncError('Playlist check returned an invalid total. No Spotify changes made.')
    latest = []
    for show_id, label in shows.items():
        result = client.request('GET', f'/shows/{show_id}/episodes', params={'limit': 1})
        episodes = result.get('items')
        if not isinstance(episodes, list) or not episodes or not isinstance(episodes[0], dict):
            raise SyncError(f'Unable to check latest episode for {label}; no Spotify changes made.')
        latest.append((label, episodes[0].get('release_date', 'unknown')))
    return {'total': total, 'latest': latest}


def main():
    try:
        config = json.loads(Path('podcast_config.json').read_text(encoding='utf-8'))
        spotify = Spotify(os.environ.get('SPOTIFY_CLIENT_ID', '').strip(),
                          os.environ.get('SPOTIFY_REFRESH_TOKEN', '').strip())
        result = light_check(spotify, config)
        lines = ['## Read-only Spotify episode check', '',
                 f"Playlist: {config['name']}",
                 f"Current playlist items: {result['total']}",
                 f"Subscribed podcasts checked: {len(result['latest'])}", '',
                 '| Podcast | Most recent episode release date |', '| --- | --- |']
        for name, date in result['latest']:
            print(f'{name}: latest release {date}')
            lines.append(f'| {name.replace(chr(124), "-")} | {date} |')
        lines.extend(['', 'No playlist changes made. Next scheduled sync is one hour after the check slot.'])
        if os.environ.get('GITHUB_STEP_SUMMARY'):
            with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as out:
                out.write('\n'.join(lines) + '\n')
        state_file = os.environ.get('SPOTIFY_STATE_FILE')
        if state_file:
            path = Path(state_file)
            state = json.loads(path.read_text()) if path.exists() else {}
            if not isinstance(state, dict):
                state = {}
            state['last_check_success'] = int(time.time())
            path.write_text(json.dumps(state))
        print('CHECK COMPLETE: no playlist modifications. Sync is scheduled one hour later.')
        return 0
    except (SyncError, OSError, ValueError, KeyError, TypeError) as e:
        print(f'Lightweight check failed: {e}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
