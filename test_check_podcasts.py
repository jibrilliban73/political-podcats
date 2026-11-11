import unittest
from unittest.mock import patch
import check_podcasts
import verified_sync


class MiniCheckTests(unittest.TestCase):
    def test_checks_one_item_per_show_without_writes(self):
        class ReadOnly:
            def __init__(self):
                self.calls = []
            def request(self, method, path, *, params):
                self.calls.append((method, path, params))
                if path.startswith('/playlists/'):
                    return {'total': 2700, 'items': [{'item': {}}]}
                return {'items': [{'release_date': '2026-11-02'}]}
        client = ReadOnly()
        result = check_podcasts.light_check(client, {
            'playlist_id': 'a', 'shows': {'one': 'First', 'two': 'Second'}})
        self.assertEqual(result['total'], 2700)
        self.assertEqual(len(result['latest']), 2)
        self.assertEqual(len(client.calls), 3)
        self.assertTrue(all(method == 'GET' for method, _, _ in client.calls))
        self.assertTrue(all(params['limit'] == 1 for _, _, params in client.calls))

    def test_placeholder_at_start_does_not_block_other_shows(self):
        class Placeholder:
            def __init__(self):
                self.limits = []
            def request(self, method, path, *, params):
                if path.startswith('/playlists/'):
                    return {'total': 3}
                self.limits.append(params['limit'])
                if params['limit'] == 1:
                    return {'items': [None]}
                return {'items': [None, {'is_playable': False},
                                  {'release_date': '2026-11-11'}]}
        client = Placeholder()
        result = check_podcasts.light_check(
            client, {'playlist_id': 'x', 'shows': {'y': 'Show'}})
        self.assertEqual(result['latest'], [('Show', '2026-11-11')])
        self.assertEqual(client.limits, [1, 50])

    def test_all_placeholder_records_do_not_claim_success(self):
        class Missing:
            def request(self, method, path, *, params):
                return {'total': 3} if path.startswith('/playlists/') else {'items': [None, {}]}
        with self.assertRaises(verified_sync.SyncError):
            check_podcasts.light_check(Missing(), {'playlist_id': 'x', 'shows': {'y': 'Show'}})

    def test_invalid_or_partial_check_does_not_claim_success(self):
        class Missing:
            def request(self, method, path, *, params):
                return {'total': 3} if path.startswith('/playlists/') else {'items': []}
        with self.assertRaises(verified_sync.SyncError):
            check_podcasts.light_check(Missing(), {'playlist_id': 'x', 'shows': {'y': 'Show'}})

    def test_empty_config_fails_safely(self):
        with self.assertRaises(verified_sync.SyncError):
            check_podcasts.light_check(None, {'playlist_id': '', 'shows': {}})
