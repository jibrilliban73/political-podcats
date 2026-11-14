import contextlib
import copy
from datetime import date, datetime
import io
import unittest
from unittest.mock import patch

import verified_sync as sync


CONFIG = {"name": "Test", "playlist_id": "target", "shows": {"managed": "Managed"}, "excluded_shows": ["excluded"]}


def episode(name, played=False, position=0, **extra):
    return {"uri": "spotify:episode:" + name, "resume_point": {"fully_played": played, "resume_position_ms": position}, **extra}


class Client:
    def __init__(self, entries=None, episodes=None):
        self.entries = copy.deepcopy(entries or [])
        self.catalogs = {"managed": copy.deepcopy(episodes or [episode("fresh")]), "excluded": [episode("excluded")]}
        self.writes = []
        self.reads = []
        self.drop_add = False
        self.fail_catalog = False
        self.snapshot = 1

    def pages(self, path, **kwargs):
        self.reads.append(path)
        if path.startswith("/playlists/"):
            return iter(copy.deepcopy(self.entries))
        if self.fail_catalog:
            raise sync.SyncError("failed page")
        return iter(copy.deepcopy(self.catalogs[path.split("/")[2]]))

    def request(self, method, path, *, body=None):
        if method == "GET":
            return {"snapshot_id": str(self.snapshot)}
        self.writes.append((method, body))
        if method == "DELETE":
            remove = {item["uri"] for item in body["items"]}
            self.entries = [entry for entry in self.entries if sync.item_from_entry(entry).get("uri") not in remove]
            self.snapshot += 1
        elif not self.drop_add:
            self.entries.extend({"item": {"uri": uri}} for uri in body["uris"])
            self.snapshot += 1
        return {}


class ReconciliationTests(unittest.TestCase):
    def test_full_sync_repairs_duplicates_preserves_unrelated_then_is_idempotent(self):
        other = {"track": {"uri": "spotify:track:music"}}
        entries = [{"item": episode("keep")}, {"track": episode("keep")}, {"item": episode("done", True)},
                   {"item": episode("excluded")}, other, {"item": None}]
        client = Client(entries, [episode("keep"), episode("new", position=1234), episode("done", True),
                                  episode("unavailable", is_playable=False)])
        result = sync.reconcile(client, CONFIG)
        self.assertTrue(result["verified"])
        self.assertEqual(result["required"], 2)
        self.assertEqual(result["duplicate_episodes_repaired"], 1)
        self.assertEqual(result["shows"][0]["unavailable_unfinished"], 1)
        self.assertIn(other, client.entries)
        self.assertIn({"item": None}, client.entries)
        self.assertEqual(sync.counts(client.entries)["spotify:episode:keep"], 1)
        self.assertEqual(sum(path.startswith("/playlists/") for path in client.reads), 2)
        client.writes.clear()
        again = sync.reconcile(client, CONFIG)
        self.assertTrue(again["verified"])
        self.assertEqual(client.writes, [])
        self.assertEqual((again["add"], again["remove_copies"]), (0, 0))

    def test_partial_unknown_status_fails_before_any_write(self):
        client = Client(episodes=[episode("known"), {"uri": "spotify:episode:unknown"}])
        with self.assertRaisesRegex(sync.SyncError, "unknown playback"):
            sync.reconcile(client, CONFIG)
        self.assertEqual(client.writes, [])

    def test_failed_catalog_and_empty_show_never_mutate(self):
        client = Client()
        client.fail_catalog = True
        with self.assertRaises(sync.SyncError):
            sync.reconcile(client, CONFIG)
        client.fail_catalog = False
        client.catalogs["managed"] = []
        with self.assertRaises(sync.SyncError):
            sync.reconcile(client, CONFIG)
        self.assertEqual(client.writes, [])

    def test_verification_detects_silently_lost_addition(self):
        client = Client()
        client.drop_add = True
        with self.assertRaisesRegex(sync.SyncError, "verification failed"):
            sync.reconcile(client, CONFIG)

    def test_preview_and_verify_only_do_not_mutate(self):
        client = Client()
        result = sync.reconcile(client, CONFIG, dry_run=True)
        self.assertFalse(result["verified"])
        self.assertEqual(result["audit"]["missing"], 1)
        with self.assertRaisesRegex(sync.SyncError, "verification failed"):
            sync.reconcile(client, CONFIG, verify_only=True)
        self.assertEqual(client.writes, [])

    def test_large_catalog_batches_additions_and_removals(self):
        episodes = [episode(str(i)) for i in range(205)] + [episode("done" + str(i), True) for i in range(101)]
        client = Client([{"item": item} for item in episodes[205:]], episodes)
        result = sync.reconcile(client, CONFIG)
        self.assertTrue(result["verified"])
        self.assertEqual([method for method, _ in client.writes], ["DELETE", "DELETE", "POST", "POST", "POST"])
        self.assertEqual([len(body.get("uris", body.get("items"))) for _, body in client.writes], [100, 1, 100, 100, 5])

    def test_reports_never_include_episode_uris_or_credentials(self):
        result = sync.reconcile(Client(), CONFIG)
        with contextlib.redirect_stdout(io.StringIO()) as output, patch.dict("os.environ", {}, clear=True):
            sync.emit_report(result)
        self.assertNotIn("spotify:episode:", output.getvalue())
        self.assertNotIn("refresh_token", output.getvalue())


class PaginationTests(unittest.TestCase):
    def test_every_page_is_read(self):
        client = sync.Spotify("id", "token")
        offsets = []
        def request(method, path, params):
            start = params["offset"]
            offsets.append(start)
            items = list(range(start, min(start + 50, 123)))
            return {"items": items, "total": 123, "next": "yes" if start + 50 < 123 else None}
        client.request = request
        self.assertEqual(list(client.pages("/example")), list(range(123)))
        self.assertEqual(offsets, [0, 50, 100])

    def test_truncated_repeated_or_changing_catalog_is_rejected(self):
        client = sync.Spotify("id", "token")
        for page in [{"items": [1], "total": 2, "next": None}, {"items": [], "next": "yes"},
                     {"items": [1], "next": "yes"}]:
            client.request = lambda *args, **kwargs: page
            with self.assertRaises(sync.SyncError):
                list(client.pages("/example"))

    def test_recent_boundary_reads_entire_old_page_then_stops(self):
        client = sync.Spotify("id", "token")
        offsets = []
        def request(method, path, params):
            offset = params["offset"]
            offsets.append(offset)
            dates = (["2026-11-11"] * 50 if offset == 0 else
                     ["2026-11-03"] * 50 if offset == 50 else ["2026-01-01"] * 50)
            return {"items": [{"release_date": item} for item in dates],
                    "total": 150, "next": "yes" if offset < 100 else None}
        client.request = request
        boundary = sync.RecentPageBoundary(date(2026, 11, 4))
        self.assertEqual(len(list(client.pages("/example", stop_after_page=boundary))), 100)
        self.assertEqual(offsets, [0, 50])
        self.assertTrue(boundary.stopped)

    def test_recent_boundary_falls_back_on_ambiguous_or_unordered_dates(self):
        for first, second in [([{"release_date": "2026-11"}], [{"release_date": "2026-01-01"}]),
                              ([{"release_date": "2026-11-03"}, {"release_date": "2026-11-11"}],
                               [{"release_date": "2026-01-01"}])]:
            boundary = sync.RecentPageBoundary(date(2026, 11, 4))
            self.assertFalse(boundary(first))
            self.assertFalse(boundary(second))
            self.assertTrue(boundary.must_scan_all)


class RecentCatalogTests(unittest.TestCase):
    class DatedPagesClient(Client):
        def __init__(self):
            super().__init__(entries=[{"item": episode("old_done", True)}])
            self.pages_read = 0
            self.groups = [
                [episode("recent", release_date="2026-11-11")],
                [episode("boundary", release_date="2026-11-03")],
                [episode("old_done", True, release_date="2026-01-01")],
            ]
        def pages(self, path, **kwargs):
            if path != "/shows/managed/episodes":
                return super().pages(path, **kwargs)
            boundary = kwargs.get("stop_after_page")
            def iterate():
                for index, group in enumerate(self.groups):
                    self.pages_read += 1
                    yield from copy.deepcopy(group)
                    if index < len(self.groups) - 1 and boundary and boundary(group):
                        return
            return iterate()

    def config(self):
        return {**CONFIG, "recent_show_policy": {"managed": {"days": 7, "full_scan_weekday": 6}}}

    def test_recent_run_preserves_unscanned_older_items_and_reports_scope(self):
        client = self.DatedPagesClient()
        result = sync.reconcile(client, self.config(), as_of=datetime(2026, 11, 11, 7))
        self.assertEqual(client.pages_read, 2)
        self.assertEqual(result["shows"][0]["scan_scope"], "recent_since_2026-11-04")
        self.assertTrue(result["scanned_scope_verified"])
        self.assertFalse(result["full_catalog_scan"])
        self.assertFalse(result["verified"])
        self.assertEqual(sync.counts(client.entries)["spotify:episode:old_done"], 1)
        self.assertEqual(sync.counts(client.entries)["spotify:episode:recent"], 1)
        self.assertTrue(all(method != "DELETE" for method, _ in client.writes))

    def test_sunday_full_scan_reconciles_older_played_items(self):
        client = self.DatedPagesClient()
        result = sync.reconcile(client, self.config(), as_of=datetime(2026, 11, 15, 7), scheduled_morning=True)
        self.assertEqual(client.pages_read, 3)
        self.assertTrue(result["full_catalog_scan"])
        self.assertTrue(result["verified"])
        self.assertEqual(sync.counts(client.entries)["spotify:episode:old_done"], 0)

    def test_manual_full_history_overrides_recent_policy(self):
        client = self.DatedPagesClient()
        result = sync.reconcile(client, self.config(), as_of=datetime(2026, 11, 11, 7), full_history=True)
        self.assertEqual(client.pages_read, 3)
        self.assertTrue(result["verified"])

    def test_missing_date_for_unscanned_episode_cannot_claim_sorted(self):
        client = self.DatedPagesClient()
        with self.assertRaisesRegex(sync.SyncError, "lacks a release date"):
            sync.reconcile(client, self.config(), as_of=datetime(2026, 11, 11, 7), sort_playlist=True)

    def test_sunday_evening_uses_recent_scope(self):
        client = self.DatedPagesClient()
        result = sync.reconcile(client, self.config(), as_of=datetime(2026, 11, 15, 19))
        self.assertEqual(client.pages_read, 2)
        self.assertFalse(result["full_catalog_scan"])
        self.assertTrue(result["scanned_scope_verified"])

    def test_delayed_sunday_morning_run_still_scans_full_history(self):
        client = self.DatedPagesClient()
        result = sync.reconcile(client, self.config(), as_of=datetime(2026, 11, 15, 14), scheduled_morning=True)
        self.assertEqual(client.pages_read, 3)
        self.assertTrue(result["full_catalog_scan"])

    def test_other_shows_keep_full_catalog_reconciliation(self):
        client = self.DatedPagesClient()
        client.catalogs["other"] = [episode("other_old", True, release_date="2025-01-01")]
        client.entries.append({"item": episode("other_old", True, release_date="2025-01-01")})
        config = self.config()
        config["shows"] = {"managed": "Managed", "other": "Other"}
        result = sync.reconcile(client, config, as_of=datetime(2026, 11, 11, 7))
        self.assertEqual(result["shows"][1]["scan_scope"], "full")
        self.assertIn("/shows/other/episodes", client.reads)
        self.assertEqual(sync.counts(client.entries)["spotify:episode:other_old"], 0)


if __name__ == "__main__":
    unittest.main()
