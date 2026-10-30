import contextlib
import copy
import io
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request

import spotify_http as http
import verified_sync as sync
from test_playlist_sort import FakeSpotify, episode, entry, display


class RateLimitTests(unittest.TestCase):
    def test_retry_after_is_honored_even_for_quota_response(self):
        error = HTTPError("https://api.spotify.com/v1/example", 429, "limited", {"Retry-After": "75"}, io.BytesIO(b'{"error":{"reason":"QUOTA_EXCEEDED"}}'))
        with patch.object(http, "urlopen", side_effect=[error, io.BytesIO(b'{"ok":true}')]) as send, patch.object(http.time, "sleep") as sleep, patch.object(http.time, "monotonic", return_value=1000000), contextlib.redirect_stdout(io.StringIO()):
            http._last_request = 0
            result = http.send_json(Request("https://api.spotify.com/v1/example"), "GET /example", sync.SyncError)
            self.assertTrue(result["ok"])
            self.assertEqual(send.call_count, 2)
            sleep.assert_any_call(75)

    def test_long_limit_stops_without_retrying_early(self):
        error = HTTPError("https://api.spotify.com/v1/example", 429, "limited", {"Retry-After": "3600"}, io.BytesIO(b"{}"))
        with patch.object(http, "urlopen", side_effect=error) as send, patch.object(http.time, "sleep"), patch.object(http.time, "monotonic", return_value=1000000):
            http._last_request = 0
            with self.assertRaisesRegex(sync.SyncError, "Retry-After=3600"):
                http.send_json(Request("https://api.spotify.com/v1/example"), "GET /example", sync.SyncError)
            self.assertEqual(send.call_count, 1)

    def test_lost_write_is_not_automatically_repeated(self):
        with patch.object(http, "urlopen", side_effect=URLError("lost")) as send, patch.object(http.time, "sleep"), patch.object(http.time, "monotonic", return_value=1000000):
            http._last_request = 0
            with self.assertRaises(sync.SyncError):
                http.send_json(Request("https://api.spotify.com/v1/example", method="POST", data=b"{}"), "POST /example", sync.SyncError)
            self.assertEqual(send.call_count, 1)

    def test_requests_are_paced(self):
        with patch.object(http, "urlopen", return_value=io.BytesIO(b"{}")), patch.object(http.time, "sleep") as sleep, patch.object(http.time, "monotonic", return_value=100):
            http._last_request = 100
            http.send_json(Request("https://api.spotify.com/v1/example"), "GET /example", sync.SyncError)
            sleep.assert_called_once_with(http.MIN_INTERVAL)


class IntegratedTests(unittest.TestCase):
    def test_sync_and_sort_reuse_single_complete_catalog_and_preserve_other_content(self):
        config = {"name": "Test", "playlist_id": "test", "shows": {"z": "Zoo", "a": "Alpha"}}
        catalogs = {"z": [episode("z-old", "2020-02-01"), episode("z-new", "2026-01-01")], "a": [episode("a-old", "2020-01-01"), episode("a-new", "2026-02-01"), episode("done", "2025", True)]}
        class Client(FakeSpotify):
            def __init__(self):
                super().__init__([entry("z-old"), entry("a-old"), entry("done"), {"item": {"uri": "spotify:track:unrelated"}}], catalogs)
                self.scanned = []
            def pages(self, path, **kwargs):
                if path.startswith("/shows/"):
                    self.scanned.append(path)
                return super().pages(path, **kwargs)
            def request(self, method, path, *, body=None, params=None):
                if method == "DELETE":
                    removes = {item["uri"] for item in body["items"]}
                    self.entries = [e for e in self.entries if sync.item_from_entry(e).get("uri") not in removes]
                    self.snapshot += 1
                    return {"snapshot_id": str(self.snapshot)}
                if method == "POST":
                    self.entries.extend({"item": {"uri": u}} for u in body["uris"])
                    self.snapshot += 1
                    return {"snapshot_id": str(self.snapshot)}
                return super().request(method, path, body=body, params=params)
        client = Client()
        with contextlib.redirect_stdout(io.StringIO()):
            result = sync.reconcile(client, config, sort_playlist=True)
        self.assertTrue(result["verified"])
        self.assertTrue(result["ordered"])
        self.assertEqual(client.scanned, ["/shows/z/episodes", "/shows/a/episodes"])
        self.assertEqual([display(e) for e in client.entries], ["a-new", "z-new", "z-old", "a-old", "spotify:track:unrelated"])
        moves = len(client.moves)
        with contextlib.redirect_stdout(io.StringIO()):
            sync.reconcile(client, config, sort_playlist=True)
        self.assertEqual(len(client.moves), moves)

    def test_sort_dry_run_and_verify_only_never_move_items(self):
        config = {"name": "Test", "playlist_id": "test", "shows": {"a": "Alpha"}}
        fake = FakeSpotify([entry("old"), entry("new")], {"a": [episode("old", "2020"), episode("new", "2026")]})
        preview = sync.reconcile(fake, config, dry_run=True, sort_playlist=True)
        self.assertFalse(preview["ordered"])
        with self.assertRaisesRegex(sync.SyncError, "order needs repair"):
            sync.reconcile(fake, config, verify_only=True, sort_playlist=True)
        self.assertEqual(fake.moves, [])
