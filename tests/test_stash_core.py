#!/usr/bin/env python3
"""
Tests for DJMAX Stash.

Two layers:
  * pure unit tests (no network) for naming, config and path safety
  * an end-to-end test that boots tools/mock_worker.py against a generated demo
    bucket and downloads through the real client code

Run:  python tests/test_stash_core.py         (or python -m pytest tests/)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import stash_core as core  # noqa: E402
import mock_worker  # noqa: E402
import make_demo_bucket  # noqa: E402


class TestNaming(unittest.TestCase):
    def test_safe_name_strips_illegal_characters(self):
        self.assertEqual(core.safe_name('AI:UE"OON?'), "AI_UE_OON_")
        self.assertEqual(core.safe_name("Song/With/Slashes"), "Song_With_Slashes")
        self.assertEqual(core.safe_name("  trailing dot .  "), "trailing dot")
        self.assertEqual(core.safe_name(""), "_")
        self.assertEqual(core.safe_name("CON"), "_CON")
        self.assertEqual(core.safe_name("NUL.txt"), "_NUL.txt")

    def test_safe_name_keeps_unicode(self):
        # Japanese/Korean titles must survive untouched
        self.assertEqual(core.safe_name("에일리 - 꺼져"), "에일리 - 꺼져")
        self.assertEqual(core.safe_name("オンライン"), "オンライン")

    def test_safe_name_truncates_long_segments_but_keeps_extension(self):
        name = core.safe_name("x" * 300 + ".pt")
        self.assertLessEqual(len(name), 120)
        self.assertTrue(name.endswith(".pt"))

    def test_safe_rel_path_drops_traversal(self):
        rel = core.safe_rel_path("djmax/../../etc/passwd")
        self.assertNotIn("..", rel)
        self.assertTrue(rel.startswith("djmax"))

    def test_the_projects_reference_layout_survives_untouched(self):
        """djmax/By_DLC/Arcaea/Songs/AI[UE]OON [737]/Chart and OGG/ must round-trip."""
        key = "djmax/By_DLC/Arcaea/Songs/AI[UE]OON [737]/Chart and OGG/AI[UE]OON.ogg"
        rel = core.safe_rel_path(key).replace(os.sep, "/")
        self.assertEqual(rel, key)
        cfg = core.Config(root_prefix="djmax/", dlc_dir="By_DLC", song_dir="Songs").normalised()
        self.assertEqual(core.dlc_root_prefix(cfg), "djmax/By_DLC/")

    def test_safe_rel_path_handles_windows_hostile_names(self):
        rel = core.safe_rel_path('djmax/By_DLC/Arcaea/Songs/AI[UE]OON [737]/Chart and OGG/4B.pt')
        self.assertIn("Chart and OGG", rel)
        self.assertTrue(rel.endswith("4B.pt"))


class TestNormalisation(unittest.TestCase):
    def test_norm_key_matches_chart_folder_variants(self):
        wanted = core.norm_key("Chart and OGG")
        for variant in ("Chart & OGG", "chart and ogg", "Chart_and_OGG", "Chart+OGG"):
            self.assertEqual(core.norm_key(variant), wanted, variant)

    def test_natural_sort_orders_numbers(self):
        names = ["Song 10", "Song 2", "Song 1"]
        self.assertEqual(sorted(names, key=core.natural_key), ["Song 1", "Song 2", "Song 10"])

    def test_human_bytes_and_duration(self):
        self.assertEqual(core.human_bytes(512), "512 B")
        self.assertEqual(core.human_bytes(1536), "1.5 KB")
        self.assertEqual(core.human_bytes(5 * 1024**3), "5.0 GB")
        self.assertEqual(core.human_duration(65), "1:05")
        self.assertEqual(core.human_duration(None), "--:--")

    def test_normalise_prefix(self):
        self.assertEqual(core.normalise_prefix("/djmax"), "djmax/")
        self.assertEqual(core.normalise_prefix("djmax/"), "djmax/")
        self.assertEqual(core.normalise_prefix(""), "")


class TestSecretObfuscation(unittest.TestCase):
    def test_roundtrip(self):
        secret = "s3cret-token-with-!@#$%^&*()_+unicode-한글"
        blob = core.obfuscate(secret)
        self.assertTrue(blob.startswith("obf:"))
        self.assertNotIn(secret, blob)
        self.assertEqual(core.deobfuscate(blob), secret)

    def test_plain_text_passthrough(self):
        self.assertEqual(core.deobfuscate("not-obfuscated"), "not-obfuscated")

    def test_empty(self):
        self.assertEqual(core.obfuscate(""), "")
        self.assertEqual(core.deobfuscate(""), "")


class TestConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="stash-cfg-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        for var in ("DJMAX_STASH_API_URL", "DJMAX_STASH_TOKEN"):
            os.environ.pop(var, None)

    def test_save_and_load_roundtrip(self):
        path = self.tmp / "config.json"
        cfg = core.Config(api_url="https://example.workers.dev", token="tok-123",
                          output_dir=str(self.tmp / "out"))
        core.save_config(cfg, str(path))

        on_disk = path.read_text()
        self.assertNotIn("tok-123", on_disk, "token should not be plain text on disk")

        loaded = core.load_config(str(path))
        self.assertEqual(loaded.token, "tok-123")
        self.assertEqual(loaded.api_url, "https://example.workers.dev")

    def test_env_var_overrides_file(self):
        path = self.tmp / "config.json"
        core.save_config(core.Config(api_url="https://from-file", token="a"), str(path))
        os.environ["DJMAX_STASH_TOKEN"] = "from-env"
        loaded = core.load_config(str(path))
        self.assertEqual(loaded.token, "from-env")

    def test_clamps_silly_values(self):
        cfg = core.Config(workers=999, timeout=0, retries=99).normalised()
        self.assertEqual(cfg.workers, 16)
        self.assertGreaterEqual(cfg.timeout, 5.0)
        self.assertEqual(cfg.retries, 10)

    def test_defaults_fill_empty_chart_dirs(self):
        cfg = core.Config(chart_dirs=[]).normalised()
        self.assertTrue(cfg.chart_dirs)

    def test_missing_file_falls_back_to_defaults(self):
        cfg = core.load_config(str(self.tmp / "nope.json"))
        self.assertEqual(cfg.api_url, "")
        self.assertTrue(cfg.output_dir)
        self.assertEqual(cfg.workers, 4)


class TestHttpSessionUrls(unittest.TestCase):
    def test_build_url_includes_port_and_prefix(self):
        session = core.HttpSession("http://127.0.0.1:8787/base", "tok")
        self.assertEqual(session.build_url("/api/list", {"prefix": "djmax/"}),
                         "http://127.0.0.1:8787/base/api/list?prefix=djmax%2F")

    def test_build_url_omits_default_port(self):
        session = core.HttpSession("https://stash.example.workers.dev", "tok")
        self.assertEqual(session.build_url("/api/ping"), "https://stash.example.workers.dev/api/ping")

    def test_rejects_bad_scheme(self):
        with self.assertRaises(core.StashError):
            core.HttpSession("ftp://nope")
        with self.assertRaises(core.StashError):
            core.HttpSession("")


# ---------------------------------------------------------------------------
# end to end
# ---------------------------------------------------------------------------


class TestEndToEnd(unittest.TestCase):
    """Boot the mock worker, then behave like the GUI would."""

    TOKEN = "test-token"
    PREFIX = "djmax/"

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="stash-e2e-"))
        cls.bucket = cls.tmp / "bucket"
        make_demo_bucket.build(cls.bucket, prefix="djmax", dlc_dir="By_DLC",
                               song_root="Songs", chart_dir="Chart and OGG", big_mv=True)

        from http.server import ThreadingHTTPServer

        mock_worker.Handler.bucket = mock_worker.FakeBucket(cls.bucket)
        mock_worker.Handler.token = cls.TOKEN
        mock_worker.Handler.allowed_prefix = cls.PREFIX
        mock_worker.Handler.delay = 0.0

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), mock_worker.Handler)
        cls.server.daemon_threads = True
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        time.sleep(0.2)

        cls.cfg = core.Config(api_url=f"http://127.0.0.1:{cls.port}", token=cls.TOKEN,
                              dlc_dir="By_DLC", output_dir=str(cls.tmp / "out")).normalised()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def api(self):
        return core.StashAPI(self.cfg)

    # -- listing ------------------------------------------------------------
    def test_ping(self):
        data = self.api().ping()
        self.assertTrue(data["ok"])
        self.assertEqual(data["scope"], self.PREFIX)

    def test_bad_token_is_reported_as_auth_error(self):
        bad = core.Config(**{**self.cfg.__dict__, "token": "wrong"})
        with self.assertRaises(core.AuthError):
            core.StashAPI(bad).ping()

    def test_lists_dlcs(self):
        browser = core.StashBrowser(self.api())
        names = [d.name for d in browser.dlcs()]
        self.assertIn("Arcaea", names)
        self.assertIn("Deemo", names)
        self.assertEqual(names, sorted(names, key=core.natural_key))

    def test_lists_dlc_folders_with_songs_first(self):
        browser = core.StashBrowser(self.api())
        arcaea = [d for d in browser.dlcs() if d.name == "Arcaea"][0]
        folders = browser.dlc_folders(arcaea)
        labels = [f.name for f in folders]
        self.assertEqual(labels[0], "Songs", "the song container should sort first")
        self.assertIn("Gears", labels)
        self.assertIn("Other Assets", labels)
        self.assertTrue(folders[0].is_song_container)
        self.assertFalse(folders[1].is_song_container)

    def test_lists_songs_and_detects_chart_folder(self):
        browser = core.StashBrowser(self.api())
        arcaea = [d for d in browser.dlcs() if d.name == "Arcaea"][0]
        songs = browser.songs(arcaea)
        titles = [s.title for s in songs]
        self.assertIn("Grievous Lady [111]", titles)
        for song in songs:
            self.assertTrue(song.has_chart_folder, song.title)
            self.assertEqual(song.chart_dir, "Chart and OGG")
            self.assertIn("MV", song.subfolders)

    # -- planning -----------------------------------------------------------
    def test_plan_songs_only_takes_the_chart_folder(self):
        browser = core.StashBrowser(self.api())
        arcaea = [d for d in browser.dlcs() if d.name == "Arcaea"][0]
        song = [s for s in browser.songs(arcaea) if s.title.startswith("Grievous Lady")][0]

        items = browser.plan_songs([song])
        keys = [i.key for i in items]
        self.assertTrue(keys, "should have planned files")
        self.assertTrue(all("/Chart and OGG/" in k for k in keys), keys)
        self.assertTrue(any(k.endswith(".ogg") for k in keys))
        self.assertTrue(any(k.endswith(".pt") for k in keys))
        self.assertFalse(any(k.endswith(".mp4") for k in keys),
                         "MV must not be part of a Chart-and-OGG download")
        # the extra nested pattern folder comes along for the ride
        self.assertTrue(any(k.endswith("pattern/extra.pt") for k in keys))

        rel = items[0].rel_path.replace("\\", "/")
        self.assertTrue(rel.startswith("By_DLC/Arcaea/Songs/"), rel)
        self.assertNotIn("djmax/", rel, "root prefix should be stripped from local paths")

    def test_plan_songs_can_take_the_whole_song_folder(self):
        browser = core.StashBrowser(self.api())
        arcaea = [d for d in browser.dlcs() if d.name == "Arcaea"][0]
        song = [s for s in browser.songs(arcaea) if s.title.startswith("Grievous Lady")][0]
        items = browser.plan_songs([song], whole_song_folder=True)
        self.assertTrue(any(i.key.endswith(".mp4") for i in items), "MV should be included")

    def test_plan_assets_excludes_songs(self):
        browser = core.StashBrowser(self.api())
        arcaea = [d for d in browser.dlcs() if d.name == "Arcaea"][0]
        items = browser.plan_dlc([arcaea], include_songs=False)
        keys = [i.key for i in items]
        self.assertTrue(keys)
        self.assertTrue(any("/Gears/" in k for k in keys))
        self.assertTrue(any("/Other Assets/" in k for k in keys))
        self.assertFalse(any("/Songs/" in k for k in keys), "assets must skip Songs")

    def test_plan_dlc_includes_songs(self):
        browser = core.StashBrowser(self.api())
        arcaea = [d for d in browser.dlcs() if d.name == "Arcaea"][0]
        items = browser.plan_dlc([arcaea], include_songs=True)
        keys = [i.key for i in items]
        self.assertTrue(any("/Songs/" in k and k.endswith(".ogg") for k in keys))
        self.assertTrue(any("/Songs/" in k and k.endswith(".mp4") for k in keys))
        self.assertTrue(any("/Gears/" in k for k in keys))

    def test_plan_folder_of_arbitrary_path(self):
        browser = core.StashBrowser(self.api())
        items = browser.plan_folder("djmax/By_DLC/Deemo/Gears/")
        self.assertEqual(len(items), len(make_demo_bucket.GEAR_FILES))
        self.assertTrue(all(i.rel_path.replace("\\", "/").startswith("By_DLC/Deemo/Gears/")
                            for i in items))

    def test_prefix_size_uses_server_stats(self):
        browser = core.StashBrowser(self.api())
        arcaea = [d for d in browser.dlcs() if d.name == "Arcaea"][0]
        size = browser.prefix_size(arcaea.prefix)
        expected = sum(f.stat().st_size for f in (self.bucket / "djmax/By_DLC/Arcaea").rglob("*")
                       if f.is_file())
        self.assertEqual(size, expected)
        self.assertGreater(size, 0)

    # -- auto detect --------------------------------------------------------
    def test_auto_detect_layout(self):
        probe = core.Config(api_url=self.cfg.api_url, token=self.TOKEN, root_prefix="")
        result = core.auto_detect_layout(probe, core.StashAPI(probe))
        self.assertTrue(result["ok"], result["notes"])
        detected = result["config"]
        self.assertEqual(detected.root_prefix, "djmax/")
        self.assertEqual(detected.dlc_dir, "By_DLC")
        self.assertEqual(detected.song_dir, "Songs")

    # -- downloading --------------------------------------------------------
    def test_download_songs_end_to_end(self):
        out = self.tmp / "dl-songs"
        browser = core.StashBrowser(self.api())
        arcaea = [d for d in browser.dlcs() if d.name == "Arcaea"][0]
        song = [s for s in browser.songs(arcaea) if s.title.startswith("Halcyon")][0]
        items = browser.plan_songs([song])

        events = []
        dl = core.Downloader(self.api(), out, workers=3,
                             on_event=lambda e: events.append(e))
        result = dl.run(items)

        self.assertEqual(result.files_failed, 0, result.errors)
        self.assertEqual(result.files_done, len(items))
        self.assertEqual(result.bytes_done, sum(i.size for i in items))

        ogg = out / "By_DLC/Arcaea/Songs/Halcyon [222]/Chart and OGG/Halcyon.ogg"
        self.assertTrue(ogg.exists(), f"missing {ogg}")
        source_key = "djmax/By_DLC/Arcaea/Songs/Halcyon [222]/Chart and OGG/Halcyon.ogg"
        self.assertEqual(ogg.read_bytes(), (self.bucket / source_key).read_bytes())
        self.assertFalse(list(out.rglob("*.part")), "no partial files should remain")

        self.assertTrue(any(e["type"] == "progress" for e in events))
        self.assertEqual(events[-1]["type"], "done")

    def test_download_is_resumable_and_skips_existing(self):
        out = self.tmp / "dl-resume"
        browser = core.StashBrowser(self.api())
        arcaea = [d for d in browser.dlcs() if d.name == "Arcaea"][0]
        items = browser.plan_songs([s for s in browser.songs(arcaea)
                                    if s.title.startswith("Sheriruth")])

        first = core.Downloader(self.api(), out, workers=2).run(items)
        self.assertEqual(first.files_done, len(items))

        second = core.Downloader(self.api(), out, workers=2).run(items)
        self.assertEqual(second.files_skipped, len(items))
        self.assertEqual(second.files_done, 0)

    def test_resume_from_a_half_written_part_file(self):
        out = self.tmp / "dl-part"
        browser = core.StashBrowser(self.api())
        arcaea = [d for d in browser.dlcs() if d.name == "Arcaea"][0]
        song = [s for s in browser.songs(arcaea) if s.title.startswith("Axium Crisis")][0]
        items = browser.plan_songs([song])
        target = [i for i in items if i.key.endswith(".ogg")][0]

        dest = out / target.rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        real = (self.bucket / target.key).read_bytes()
        part.write_bytes(real[: len(real) // 2])

        result = core.Downloader(self.api(), out, workers=1).run([target])
        self.assertEqual(result.files_failed, 0, result.errors)
        self.assertEqual(dest.read_bytes(), real, "resumed file must be byte-identical")
        self.assertFalse(part.exists())

    def test_corrupt_existing_file_is_redownloaded(self):
        out = self.tmp / "dl-corrupt"
        browser = core.StashBrowser(self.api())
        arcaea = [d for d in browser.dlcs() if d.name == "Arcaea"][0]
        items = browser.plan_songs([s for s in browser.songs(arcaea)
                                    if s.title.startswith("Fracture Ray")])
        target = items[0]
        dest = out / target.rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"this is not the right size at all")

        result = core.Downloader(self.api(), out, workers=1).run([target])
        self.assertEqual(result.files_failed, 0, result.errors)
        self.assertEqual(dest.read_bytes(), (self.bucket / target.key).read_bytes())

    def test_md5_mismatch_is_detected(self):
        out = self.tmp / "dl-md5"
        target = core.DownloadItem(key="djmax/By_DLC/Deemo/Gears/plate.dds",
                                   rel_path="By_DLC/Deemo/Gears/plate.dds",
                                   size=(self.bucket / "djmax/By_DLC/Deemo/Gears/plate.dds")
                                   .stat().st_size,
                                   etag="0" * 32)
        result = core.Downloader(self.api(), out, workers=1).run([target])
        self.assertEqual(result.files_failed, 1)
        self.assertIn("checksum", result.errors[0][1].lower())
        self.assertFalse((out / target.rel_path).exists(),
                         "a file that failed its checksum must not be left behind")

    def test_cancel_stops_the_run(self):
        out = self.tmp / "dl-cancel"
        browser = core.StashBrowser(self.api())
        items = browser.plan_dlc(browser.dlcs())  # everything
        self.assertGreater(len(items), 4)

        dl = core.Downloader(self.api(), out, workers=1)
        timer = threading.Timer(0.05, dl.cancel)
        timer.start()
        result = dl.run(items)
        timer.cancel()

        self.assertTrue(result.cancelled)
        self.assertLess(result.files_done + result.files_skipped, len(items))

    def test_download_reports_unreachable_server(self):
        cfg = core.Config(api_url="http://127.0.0.1:9", token=self.TOKEN, retries=0,
                          timeout=5, output_dir=str(self.tmp / "dl-dead"))
        item = core.DownloadItem(key="djmax/x.ogg", rel_path="x.ogg", size=10)
        result = core.Downloader(core.StashAPI(cfg), cfg.output_dir, workers=1).run([item])
        self.assertEqual(result.files_failed, 1)
        self.assertIn("reach", result.errors[0][1].lower())

    def test_progress_stats_are_sane(self):
        out = self.tmp / "dl-stats"
        browser = core.StashBrowser(self.api())
        dlcs = [d for d in browser.dlcs() if d.name == "Deemo"]
        items = browser.plan_dlc(dlcs)
        seen = []

        def collect(event):
            if event["type"] == "progress":
                seen.append(event["stats"])

        core.Downloader(self.api(), out, workers=3, on_event=collect).run(items)
        self.assertTrue(seen)
        last = seen[-1]
        self.assertEqual(last.files_total, len(items))
        self.assertGreater(last.bytes_done, 0)
        self.assertLessEqual(last.percent, 100.0)
        self.assertEqual(last.percent, 100.0)
        self.assertGreaterEqual(last.files_done + last.files_skipped, len(items) - last.files_failed)

    # -- api edge cases -----------------------------------------------------
    def test_list_outside_scope_is_refused(self):
        with self.assertRaises(core.AuthError):
            self.api().list_prefix("secret/", delimiter="/")

    def test_traversal_is_refused(self):
        status, _headers, resp = self.api().open_object("djmax/../../etc/passwd")
        try:
            self.assertEqual(status, 403)
        finally:
            resp.close()

    def test_missing_folder_returns_empty_not_error(self):
        browser = core.StashBrowser(self.api())
        items = browser.plan_folder("djmax/By_DLC/Does Not Exist/")
        self.assertEqual(items, [])

    def test_fallback_size_when_stats_unavailable(self):
        class NoStats(core.StashAPI):
            def stat_prefix(self, prefix):
                raise core.StashError("nope")

        browser = core.StashBrowser(NoStats(self.cfg))
        expected = sum(f.stat().st_size
                       for f in (self.bucket / "djmax/By_DLC/Deemo").rglob("*") if f.is_file())
        self.assertEqual(browser.prefix_size("djmax/By_DLC/Deemo/"), expected)


class TestCoreAgainstWorkerSource(unittest.TestCase):
    """The Worker is JS, but we can still sanity-check it as text."""

    def test_worker_exists_and_covers_the_routes_the_client_uses(self):
        worker = (ROOT / "worker" / "worker.js").read_text()
        for route in ("/api/list", "/api/stats", "/api/search", "/api/file", "/api/ping"):
            self.assertIn(route, worker, f"worker is missing {route}")

    def test_client_only_calls_routes_the_worker_implements(self):
        source = (ROOT / "stash_core.py").read_text()
        worker = (ROOT / "worker" / "worker.js").read_text()
        called = set()
        for line in source.splitlines():
            for route in ("/api/list", "/api/stats", "/api/search", "/api/file", "/api/ping"):
                if f'"{route}"' in line:
                    called.add(route)
        self.assertTrue(called)
        for route in called:
            self.assertIn(route, worker)


class TestWorkerBehaviour(unittest.TestCase):
    """Run tests/test_worker.mjs (the Worker against a fake R2 binding)."""

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_worker_passes_its_own_suite(self):
        script = ROOT / "tests" / "test_worker.mjs"
        result = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("0 failed", result.stdout)


class TestCliSmoke(unittest.TestCase):
    def test_new_token_generates_something_usable(self):
        result = subprocess.run([sys.executable, str(ROOT / "djmax_stash_cli.py"),
                                 "new-token", "--length", "32"],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        # first non-empty line that isn't a label is the token itself
        token = ""
        for line in result.stdout.splitlines():
            line = line.strip()
            if line and not line.endswith(":") and " " not in line:
                token = line
                break
        self.assertGreaterEqual(len(token), 32)
        self.assertIn("APP_TOKEN", result.stdout)
        self.assertIn("never be read back", result.stdout)

    def test_connecting_guide_exists_and_names_the_two_values(self):
        guide = (ROOT / "CONNECTING.md").read_text()
        for needed in ("ALLOWED_PREFIX", "APP_TOKEN", "BUCKET", "workers.dev",
                       "Bindings", "Variables and Secrets",
                       "Start with Hello World!", "Bad or missing token"):
            self.assertIn(needed, guide, f"CONNECTING.md should mention {needed}")

    def test_cli_help_runs(self):
        result = subprocess.run([sys.executable, str(ROOT / "djmax_stash_cli.py"), "--help"],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--dlc", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
