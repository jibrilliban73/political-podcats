"""Offline unit tests: run with python -m unittest -v test_playlist_sort.py."""
import copy
import unittest
from collections import Counter

import playlist_sort as sorter


class FakeSpotify:
    def __init__(self, entries, catalogs, playlist_id="test"):
        self.entries = copy.deepcopy(entries)
        self.catalogs = copy.deepcopy(catalogs)
        self.playlist_id = playlist_id
        self.moves = []
        self.snapshot = 1

    def request(self, method, path, *, params=None, body=None):
        if method == "GET" and path == f"/playlists/{self.playlist_id}":
            return {"snapshot_id": str(self.snapshot)}
        if method == "PUT" and path == f"/playlists/{self.playlist_id}/items":
            assert set(body) == {"snapshot_id", "range_start", "insert_before", "range_length"}
            assert body["snapshot_id"] == str(self.snapshot)
            src, dst, n = body["range_start"], body["insert_before"], body["range_length"]
            assert src > dst and n <= 100
            moved = self.entries[src:src + n]
            del self.entries[src:src + n]
            self.entries[dst:dst] = moved
            self.moves.append((src, dst, n))
            self.snapshot += 1
            return {"snapshot_id": str(self.snapshot)}
        raise AssertionError((method, path))

    def pages(self, path, **kwargs):
        if path == f"/playlists/{self.playlist_id}/items":
            return iter(copy.deepcopy(self.entries))
        if path.startswith("/shows/") and path.endswith("/episodes"):
            return iter(copy.deepcopy(self.catalogs[path.split("/")[2]]))
        raise AssertionError(path)


def episode(uri, date, played=False):
    return {"uri": "spotify:episode:" + uri, "release_date": date, "resume_point": {"fully_played": played}}


def entry(uri):
    return {"item": {"uri": "spotify:episode:" + uri}}


def display(entry):
    uri = sorter.extract_uri(entry)
    return uri.removeprefix("spotify:episode:") if uri else None


class OrderingTests(unittest.TestCase):
    def test_global_newest_first_ignores_creator_and_keeps_unrelated_items(self):
        shows = {"z": "Zoo Talk", "a": "Art Cast"}
        all_eps = {"z": [episode("z-old", "2020-01-01"), episode("z-new", "2026-01-01")],
                   "a": [episode("a-old", "2019-01-01"), episode("a-new", "2026-02-01")]}
        original = [entry(x) for x in ["z-old", "unrelated", "a-old", "z-new", "a-new", "other"]]
        fake = FakeSpotify(original, all_eps)
        self.assertTrue(sorter.order_playlist(fake, "test", shows))
        expected = ["a-new", "z-new", "z-old", "a-old", "unrelated", "other"]
        self.assertEqual([display(e) for e in fake.entries], expected)
        self.assertEqual(Counter(sorter.extract_uri(e) for e in fake.entries),
                         Counter(sorter.extract_uri(e) for e in original))
        self.assertTrue(sorter.order_playlist(fake, "test", shows))
        # Second pass must be a no-op.
        self.assertLess(len(fake.moves), 10)

    def test_additional_podcast_in_playlist_is_sorted_by_its_own_date(self):
        extra = {"item": episode("other-show", "2026-10-30")}
        original = [entry("managed"), extra, {"track": {"uri": "spotify:track:keep"}}]
        fake = FakeSpotify(original, {"a": [episode("managed", "2026-01-01")]})
        self.assertTrue(sorter.order_playlist(fake, "test", {"a": "A"}))
        self.assertEqual([display(e) for e in fake.entries], ["other-show", "managed", "spotify:track:keep"])
        self.assertEqual(Counter(sorter.extract_uri(e) for e in fake.entries), Counter(sorter.extract_uri(e) for e in original))

    def test_changed_snapshot_is_reread_before_any_reorder(self):
        class Changing(FakeSpotify):
            changed = False
            def pages(self, path, **kwargs):
                if path.startswith("/playlists/") and not self.changed:
                    self.snapshot += 1
                    self.changed = True
                return super().pages(path, **kwargs)
        client = Changing([entry("old"), entry("new")], {"a": [episode("old", "2020"), episode("new", "2026")]})
        self.assertTrue(sorter.order_playlist(client, "test", {"a": "A"}))
        self.assertEqual([display(e) for e in client.entries], ["new", "old"])

    def test_only_one_show_sorts_newest_first(self):
        shows = {"w": "What's The Juice?"}
        episodes = {"w": [episode("older", "2023"), episode("newer", "2026-10-12")]}
        fake = FakeSpotify([entry("older"), entry("newer")], episodes)
        self.assertTrue(sorter.order_playlist(fake, "test", shows))
        self.assertEqual([display(e) for e in fake.entries], ["newer", "older"])

    def test_moves_cap_and_next_run_continues(self):
        shows = {"a": "Podcast"}
        catalog = {"a": [episode(f"e{i:03}", f"2026-01-{1+i%28:02}") for i in range(90)]}
        fake = FakeSpotify([entry(f"e{i:03}") for i in range(90)], catalog)
        done = sorter.order_playlist(fake, "test", shows, max_moves=1)
        self.assertFalse(done)
        self.assertEqual(len(fake.moves), 1)
        for _ in range(100):
            done = sorter.order_playlist(fake, "test", shows, max_moves=4)
            if done:
                break
        self.assertTrue(done)
        self.assertEqual(len(fake.entries), 90)

    def test_long_matching_runs_are_moved_in_blocks(self):
        shows = {"a": "Alpha", "b": "Beta"}
        catalog = {"a": [episode(f"a{i}", "2026-01-01") for i in range(210)],
                   "b": [episode(f"b{i}", "2025-01-01") for i in range(20)]}
        fake = FakeSpotify([entry(f"b{i}") for i in range(20)] + [entry(f"a{i}") for i in range(210)], catalog)
        for _ in range(8):
            if sorter.order_playlist(fake, "test", shows, max_moves=40):
                break
        else:
            self.fail("Sorting did not converge")
        self.assertEqual([display(e) for e in fake.entries[:3]], ["a0", "a1", "a2"])
        self.assertTrue(all(n <= 100 for _, _, n in fake.moves))

    def test_duplicate_uris_and_null_playlist_items_preserved(self):
        shows = {"a": "A"}
        catalog = {"a": [episode("x", "2025"), episode("y", "2026")]}
        fake = FakeSpotify([entry("x"), {"item": None}, entry("y"), entry("x")], catalog)
        self.assertTrue(sorter.order_playlist(fake, "test", shows))
        self.assertEqual([display(e) for e in fake.entries], ["y", "x", "x", None])

    def test_missing_show_metadata_refuses_to_sort(self):
        with self.assertRaises(sorter.SortError):
            sorter.make_metadata({"one": "One", "two": "Two"}, {"one": []})

    def test_release_date_precision(self):
        self.assertLess(sorter.date_rank({"release_date": "2020"}),
                        sorter.date_rank({"release_date": "2020-05-01"}))
        self.assertEqual(sorter.date_rank({"release_date": "junk"}), 0)


if __name__ == "__main__":
    unittest.main()
