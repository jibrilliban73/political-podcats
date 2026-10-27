import unittest

import politics_sync as sync


SHOW = next(iter(sync.SHOWS))
OTHER_SHOW = "some-other-show"


def episode(uri, played=None, show=None, playable=True):
    item = {"uri": uri, "is_playable": playable}
    if played is not None:
        item["resume_point"] = {"fully_played": played}
    if show:
        item["show"] = {"id": show}
    return item


class PlanTests(unittest.TestCase):
    def test_managed_statuses_and_unrelated_items(self):
        entries = [
            {"item": episode("spotify:episode:already", False, SHOW)},
            {"item": episode("spotify:episode:finished", True, SHOW)},
            {"item": episode("spotify:episode:unknown", None, SHOW)},
            {"item": episode("spotify:episode:other", True, OTHER_SHOW)},
            {"item": {"uri": "spotify:track:music"}},
            {"item": None},
        ]
        shows = {SHOW: [
            episode("spotify:episode:new", False),
            episode("spotify:episode:new", False),
            episode("spotify:episode:already", False),
            episode("spotify:episode:finished", True),
            episode("spotify:episode:unknown", None),
            episode("spotify:episode:unplayable", False, playable=False),
        ]}
        add, remove, unknown, examined = sync.plan_changes(entries, shows)
        self.assertEqual(add, ["spotify:episode:new"])
        self.assertEqual(remove, ["spotify:episode:finished"])
        self.assertEqual(unknown, 1)
        self.assertEqual(examined, 5)

    def test_newer_missing_status_overrides_old_status(self):
        entries = [{"item": episode("spotify:episode:one", True, SHOW)}]
        add, remove, unknown, _ = sync.plan_changes(entries, {SHOW: [episode("spotify:episode:one")]})
        self.assertEqual((add, remove, unknown), ([], [], 1))


class FakeClient:
    def __init__(self, fail_show=False):
        self.fail_show = fail_show
        self.writes = []
        self.reads = []

    def pages(self, path, params=None, first_page_only=False):
        self.reads.append((path, first_page_only))
        if path.startswith("/playlists/"):
            yield {"item": episode("spotify:episode:done", True, SHOW)}
        elif path.startswith("/shows/"):
            if self.fail_show:
                raise sync.SyncError("simulated failure")
            if f"/shows/{SHOW}/" in path:
                yield episode("spotify:episode:done", True)
                yield episode("spotify:episode:fresh", False)

    def request(self, method, path, params=None, body=None):
        self.writes.append((method, path, body))
        return {}


class SyncTests(unittest.TestCase):
    def test_preview_never_writes_and_full_scans_all_shows(self):
        client = FakeClient()
        sync.sync(client, dry_run=True, full=True)
        self.assertEqual(client.writes, [])
        self.assertEqual(len(client.reads), 1 + len(sync.SHOWS))
        self.assertTrue(all(not recent for path, recent in client.reads))

    def test_recent_run_only_changes_named_episodes(self):
        client = FakeClient()
        sync.sync(client, full=False)
        self.assertTrue(all(recent for path, recent in client.reads if path.startswith("/shows/")))
        self.assertEqual([write[0] for write in client.writes], ["DELETE", "POST"])
        self.assertEqual(client.writes[0][2], {"items": [{"uri": "spotify:episode:done"}]})
        self.assertEqual(client.writes[1][2], {"uris": ["spotify:episode:fresh"]})

    def test_failed_read_never_mutates_playlist(self):
        client = FakeClient(fail_show=True)
        with self.assertRaises(sync.SyncError):
            sync.sync(client)
        self.assertEqual(client.writes, [])

    def test_missing_status_does_not_change_playlist(self):
        client = FakeClient()
        client.pages = lambda path, params=None, first_page_only=False: iter(
            [episode("spotify:episode:unknown")] if path.startswith("/shows/") else []
        )
        with self.assertRaisesRegex(sync.SyncError, "No playback states"):
            sync.sync(client)
        self.assertEqual(client.writes, [])


class PaginationTests(unittest.TestCase):
    def test_reads_all_pages_with_offsets(self):
        client = sync.Spotify("client", "token")
        calls = []

        def request(method, path, params=None, body=None):
            calls.append(params["offset"])
            return {"items": [1, 2], "next": "next"} if params["offset"] == 0 else {"items": [3], "next": None}

        client.request = request
        self.assertEqual(list(client.pages("/shows/example/episodes")), [1, 2, 3])
        self.assertEqual(calls, [0, 2])

    def test_inconsistent_empty_page_fails_safely(self):
        client = sync.Spotify("client", "token")
        client.request = lambda *args, **kwargs: {"items": [], "next": "next"}
        with self.assertRaises(sync.SyncError):
            list(client.pages("/shows/example/episodes"))


if __name__ == "__main__":
    unittest.main()
