import contextlib
import copy
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

    def pages(self, path, **kwargs):
        self.reads.append(path)
        if path.startswith("/playlists/"):
            return iter(copy.deepcopy(self.entries))
        if self.fail_catalog:
            raise sync.SyncError("failed page")
        return iter(copy.deepcopy(self.catalogs[path.split("/")[2]]))

    def request(self, method, path, *, body):
        self.writes.append((method, body))
        if method == "DELETE":
            remove = {item["uri"] for item in body["items"]}
            self.entries = [entry for entry in self.entries if sync.item_from_entry(entry).get("uri") not in remove]
        elif not self.drop_add:
            self.entries.extend({"item": {"uri": uri}} for uri in body["uris"])
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


if __name__ == "__main__":
    unittest.main()
