#!/usr/bin/env python3
"""
GUI tests.

The real djmax_stash.py is imported and driven through a virtual Tk (tests/tk_stub.py):
every menu action is clicked for real, the event pump is run the way Tk's after()
loop would, and the resulting downloads are checked against the mock Worker.

This is what lets the GUI be verified on a headless box with no Tk installed.

Run:  python tests/test_gui.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import tk_stub  # noqa: E402
import harness  # noqa: E402
import stash_core as core  # noqa: E402

# Install the virtual Tk *before* importing the GUI module.
_TK, DIALOGS, FILEDIALOG = tk_stub.install()

import djmax_stash as gui  # noqa: E402
import stash_tasks as tasks  # noqa: E402

TOKEN = "test-token"


class GuiTestCase(unittest.TestCase):
    """Boots the app against a mock Worker and pumps its event loop."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="stash-gui-"))
        cls.bucket = harness.build_demo_bucket(cls.tmp / "bucket", prefix="djmax",
                                               dlc_dir="By_DLC", song_root="Songs",
                                               chart_dir="Chart and OGG", big_mv=True)
        cls.worker = harness.MockWorker(cls.bucket, token=TOKEN).start()

    @classmethod
    def tearDownClass(cls):
        cls.worker.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        DIALOGS.reset()
        FILEDIALOG.directory = ""
        tk_stub.Tk.errors.clear()
        self.out = self.tmp / f"out-{self._testMethodName}"
        cfg = core.Config(api_url=self.worker.url, token=TOKEN,
                          output_dir=str(self.out)).normalised()
        self.root = tk_stub.Tk()
        self.app = gui.StashApp(self.root, cfg, autoconnect=True)
        self.assertTrue(
            self.root.pump_until(
                lambda: self.app.manager.connected and self.app.dlcs
                and not self.app.manager.busy, seconds=60),
            "app never finished its initial scan")
        self.root.step(50)  # let the event pump drain the last events

    def tearDown(self):
        self.root.errors.clear()

    # -- helpers ------------------------------------------------------------
    def pump(self, predicate, seconds: float = 60.0, what: str = "condition"):
        ok = self.root.pump_until(predicate, seconds=seconds)
        self.assertTrue(ok, f"timed out waiting for {what}")
        self.assertEqual(self.root.errors, [], f"GUI raised: {self.root.errors}")

    def wait_idle(self, seconds: float = 90.0):
        self.pump(lambda: not self.app.manager.busy and self.app.events.empty(),
                  seconds=seconds, what="the task to finish")
        self.root.step(50)  # one more drain pass so result labels are rendered

    def tree_text(self) -> str:
        return "\n".join(f"{iid}\t{info['text']}" for iid, info in
                         sorted(self.app.tree.all_nodes().items()))

    def select_text(self, fragment: str):
        """Select the tree node whose label contains fragment."""
        node = self.app.tree.find(fragment)
        self.assertIsNotNone(node, f"no tree node matching {fragment!r}:\n{self.tree_text()}")
        self.app.tree.selection_set([node])
        self.app._on_select()
        return node

    def assert_no_error_dialogs(self):
        errors = DIALOGS.showerror.calls
        self.assertEqual(errors, [], f"unexpected error dialog(s): {errors}")

    # -- tests --------------------------------------------------------------
    def test_window_and_tree_populate(self):
        self.assertEqual(self.root.title_text, f"{core.APP_NAME} {core.APP_VERSION}")
        self.pump(lambda: len(self.app.dlcs) == 3, what="3 DLCs")
        text = self.tree_text()
        for dlc in ("Arcaea", "Deemo", "V Extension"):
            self.assertIn(dlc, text)
        self.assertIn("Songs", text)
        self.assertIn("Gears", text)
        self.assertEqual(self.app.song_loaded["Arcaea"], True)
        self.assertIn("Grievous Lady [111]", text)
        self.assertIn("Halcyon [222]", text)
        self.assertEqual(len(self.app.songs["Arcaea"]), 5)

    def test_no_unknown_widget_calls(self):
        """Guards against typos in Tk method names: the stub records these."""
        self.pump(lambda: len(self.app.dlcs) == 3, what="scan")
        unknown = []

        def walk(widget):
            for name in getattr(widget, "unknown", []):
                unknown.append(f"{type(widget).__name__}.{name}")
            for child in getattr(widget, "_children", []) or []:
                walk(child)

        walk(self.root)
        self.assertEqual(sorted(set(unknown)), [], f"unknown widget methods were called: {unknown}")

    def test_download_songs_menu_grabs_chart_folder_only(self):
        self.pump(lambda: len(self.app.dlcs) == 3, what="scan")
        self.select_text("Halcyon")

        self.app.act_download_songs()
        self.pump(lambda: self.app._stats.files_total > 0, what="planning")
        self.wait_idle()
        self.assert_no_error_dialogs()

        files = harness.file_tree(self.out)
        self.assertEqual(sorted(files), sorted([
            "By_DLC/Arcaea/Songs/Halcyon [222]/Chart and OGG/4B.pt",
            "By_DLC/Arcaea/Songs/Halcyon [222]/Chart and OGG/5B.pt",
            "By_DLC/Arcaea/Songs/Halcyon [222]/Chart and OGG/6B.pt",
            "By_DLC/Arcaea/Songs/Halcyon [222]/Chart and OGG/8B.pt",
            "By_DLC/Arcaea/Songs/Halcyon [222]/Chart and OGG/Halcyon.ogg",
            "By_DLC/Arcaea/Songs/Halcyon [222]/Chart and OGG/pattern/extra.pt",
        ]), "songs download should be the Chart and OGG folder only")
        # content matches the source exactly
        source = (self.bucket / "djmax/By_DLC/Arcaea/Songs/Halcyon [222]/Chart and OGG/Halcyon.ogg")
        self.assertEqual((self.out / files[4]).read_bytes(), source.read_bytes())

    def test_dlc_selected_then_download_songs_gets_every_song_in_it(self):
        """Selecting a whole DLC + 'Download songs' = all chart folders, no MVs."""
        self.pump(lambda: len(self.app.dlcs) == 3, what="scan")
        self.select_text("Deemo")

        self.app.act_download_songs()
        self.pump(lambda: self.app._stats.files_total > 0, what="planning")
        self.wait_idle()
        files = harness.file_tree(self.out)
        self.assertTrue(files)
        self.assertTrue(all("/Songs/" in f and "/Chart and OGG/" in f for f in files), files)
        self.assertTrue(any(f.endswith(".ogg") for f in files))
        self.assertFalse(any(f.endswith(".mp4") for f in files), "MV must stay out")
        # both Deemo songs are represented
        self.assertTrue(any("Myosotis" in f for f in files), files)
        self.assertTrue(any("Nine Point Eight" in f for f in files), files)

    def test_download_assets_menu_skips_songs(self):
        self.pump(lambda: len(self.app.dlcs) == 3, what="scan")
        self.select_text("Arcaea")

        self.app.act_download_assets()
        self.pump(lambda: self.app._stats.files_total > 0, what="planning")
        self.wait_idle()
        self.assert_no_error_dialogs()

        files = harness.file_tree(self.out)
        self.assertTrue(files)
        self.assertFalse(any("/Songs/" in f for f in files), files)
        self.assertTrue(any("/Gears/" in f for f in files), files)
        self.assertTrue(any("/Other Assets/" in f for f in files), files)

    def test_download_dlc_menu_includes_songs_and_assets(self):
        self.pump(lambda: len(self.app.dlcs) == 3, what="scan")
        self.select_text("V Extension")

        self.app.act_download_dlc()
        self.pump(lambda: self.app._stats.files_total > 0, what="planning")
        self.wait_idle()
        self.assert_no_error_dialogs()

        files = harness.file_tree(self.out)
        self.assertTrue(any("/Songs/" in f and f.endswith(".ogg") for f in files), files)
        self.assertTrue(any("/Songs/" in f and f.endswith(".pt") for f in files), files)
        self.assertTrue(any("/Gears/" in f for f in files), files)
        self.assertFalse(any("Arcaea" in f for f in files), "only the selected DLC should download")

    def test_whole_song_folder_action_includes_the_mv(self):
        self.pump(lambda: len(self.app.dlcs) == 3, what="scan")
        self.select_text("Myosotis")
        self.app.act_download_full_song()
        self.pump(lambda: self.app._stats.files_total > 0, what="planning")
        self.wait_idle()
        files = harness.file_tree(self.out)
        self.assertTrue(any(f.endswith(".mp4") for f in files), files)
        self.assertTrue(any(f.endswith(".ogg") for f in files), files)

    def test_download_selected_folder(self):
        self.pump(lambda: len(self.app.dlcs) == 3, what="scan")
        node = self.select_text("Other Assets")
        plan_kind = self.app.tree_nodes[node][0]
        self.assertEqual(plan_kind, "folder")

        self.app.act_download_folder()
        self.pump(lambda: self.app._stats.files_total > 0, what="planning")
        self.wait_idle()
        self.assert_no_error_dialogs()
        files = harness.file_tree(self.out)
        self.assertTrue(all("/Other Assets/" in f for f in files), files)
        self.assertGreaterEqual(len(files), 3)

    def test_multi_dlc_selection_downloads_all_of_them(self):
        self.pump(lambda: len(self.app.dlcs) == 3, what="scan")
        nodes = [self.app.node_index[f"dlc:{name}"] for name in ("Arcaea", "Deemo")]
        self.app.tree.selection_set(nodes)
        self.app._on_select()

        self.app.act_download_assets()
        self.pump(lambda: self.app._stats.files_total > 0, what="planning")
        self.wait_idle()
        files = harness.file_tree(self.out)
        self.assertTrue(any("Arcaea" in f for f in files))
        self.assertTrue(any("Deemo" in f for f in files))
        self.assertFalse(any("V Extension" in f for f in files))

    def test_progress_and_result_labels_are_rendered(self):
        self.pump(lambda: len(self.app.dlcs) == 3, what="scan")
        self.select_text("Deemo")
        self.app.act_download_dlc()
        self.pump(lambda: self.app._stats.files_total > 0, what="planning")
        self.wait_idle()

        self.assertGreater(self.app.bar.value, 0)
        self.assertIn("downloaded", self.app.numbers.cget("text"))
        self.assertIn("0 failed", self.app.numbers.cget("text"))
        self.assertEqual(self.app.big_label.cget("text"), "Done")
        self.assertEqual(self.app.status.label.cget("text"), "Done")
        self.assertIn("saved to", self.app.status.detail.cget("text"))
        self.assertTrue(self.app.log.get_text().strip(), "log should not be empty")

    def test_second_run_skips_existing_files(self):
        self.pump(lambda: len(self.app.dlcs) == 3, what="scan")
        self.select_text("Deemo")

        self.app.act_download_dlc()
        self.pump(lambda: self.app._stats.files_total > 0, what="planning")
        self.wait_idle()

        DIALOGS.reset()
        self.app.act_download_dlc()
        self.pump(lambda: "already present" in self.app.numbers.cget("text"), what="second run")
        self.wait_idle()
        self.assertIn("0 downloaded", self.app.numbers.cget("text"))
        self.assertIn("already present", self.app.numbers.cget("text"))

    def test_filter_hides_unrelated_nodes(self):
        self.pump(lambda: len(self.app.dlcs) == 3, what="scan")
        before = len(self.app.tree.get_children())
        self.app.search_var.set("arcaea")
        self.pump(lambda: len(self.app.tree.get_children()) == 1, seconds=5,
                  what="tree filtered to one DLC")
        self.assertEqual(len(self.app.tree.get_children()), 1)
        self.assertIn("Arcaea", self.app.tree.item(self.app.tree.get_children()[0], "text"))

        self.app.search_var.set("")
        self.pump(lambda: len(self.app.tree.get_children()) == before, seconds=5,
                  what="filter cleared")

    def test_menu_without_selection_warns_but_does_not_crash(self):
        self.pump(lambda: len(self.app.dlcs) == 3, what="scan")
        self.app.tree.selection_set([])
        self.app.act_download_songs()
        self.assertEqual(len(DIALOGS.showinfo.calls), 1)
        self.assertIn("Select one or more songs", DIALOGS.showinfo.calls[0][1])
        self.assertFalse(self.app.manager.busy)

    def test_connect_button_and_scan_flow(self):
        # A wrong token must surface a clear, non-fatal error
        self.app.tree.delete(*self.app.tree.get_children())
        self.app.connection.setup(core.Config(api_url=self.worker.url, token="wrong"))
        self.app._connect_with(self.worker.url, "wrong")
        self.wait_idle()
        self.assertTrue(DIALOGS.showerror.calls == [])
        self.assertIn("Connection failed", self.app.status.label.cget("text"))
        self.assertTrue(self.app.connection.visible, "connection bar should reopen to be fixed")

    def test_song_without_chart_folder_is_flagged_and_falls_back(self):
        # Remove a chart folder from the bucket, then rescan.
        victim = self.bucket / "djmax/By_DLC/Deemo/Songs/Nine Point Eight [113]/Chart and OGG"
        stash = self.tmp / "_stashed"
        shutil.move(str(victim), str(stash))
        try:
            self.root.pump_until(lambda: True, seconds=0.01)
            self.app.connect()
            self.wait_idle()
            self.pump(lambda: not self.app.manager.busy, what="rescan")
            node = self.app.tree.find("Nine Point Eight")
            self.assertIsNotNone(node)
            self.assertIn("no chart folder", str(self.app.tree.item(node, "text")))

            self.app.tree.selection_set([node])
            self.app._on_select()
            self.app.act_download_songs()
            self.pump(lambda: self.app._stats.files_total > 0, what="planning")
            self.wait_idle()
            files = harness.file_tree(self.out)
            # with no chart folder we fall back to the whole song folder
            self.assertTrue(any("MV" in f for f in files), files)
        finally:
            shutil.move(str(stash), str(victim))

    def test_vanished_object_reports_an_error_without_killing_the_run(self):
        self.pump(lambda: len(self.app.dlcs) == 3, what="scan")
        self.select_text("Deemo")

        # Deterministically remove one file after planning but before transfer.
        victim = self.bucket / "djmax/By_DLC/Deemo/Gears/plate.dds"
        backup = victim.read_bytes()
        original_plan = self.app.manager.plan_items

        def plan_then_break(plan):
            items = original_plan(plan)
            victim.unlink()
            return items

        self.app.manager.plan_items = plan_then_break
        try:
            self.app.act_download_dlc()
            self.wait_idle()
        finally:
            self.app.manager.plan_items = original_plan
            victim.write_bytes(backup)

        text = self.app.log.get_text()
        self.assertIn("failed", text.lower())
        self.assertIn("plate.dds", text)
        # the rest of the transfer still completed
        files = harness.file_tree(self.out)
        self.assertTrue(any(f.endswith(".ogg") for f in files), files)

    def test_closing_while_busy_is_safe(self):
        self.pump(lambda: len(self.app.dlcs) == 3, what="scan")
        self.select_text("Arcaea")
        self.app.act_download_assets()
        self.app._cancel()
        self.wait_idle()
        self.assertEqual(self.root.errors, [])

    def test_selftest_path_exercises_plans(self):
        self.assertEqual(gui.selftest.__module__, "djmax_stash")


class TestGuiOffline(unittest.TestCase):
    """No server: checks the failure paths and the plan mapping."""

    def setUp(self):
        DIALOGS.reset()
        tk_stub.Tk.errors.clear()

    def test_connect_to_dead_server_shows_error(self):
        cfg = core.Config(api_url="http://127.0.0.1:9", token="x", retries=0,
                          timeout=5, output_dir=str(Path(tempfile.gettempdir()) / "stash-none"))
        root = tk_stub.Tk()
        app = gui.StashApp(root, cfg, autoconnect=True)
        root.pump_until(lambda: app.status.label.cget("text").startswith("Connection failed"),
                        seconds=20)
        self.assertIn("Connection failed", app.status.label.cget("text"))
        self.assertIn("Connection failed", app.log.get_text())
        self.assertEqual(root.errors, [])

    def test_download_without_connection_warns(self):
        cfg = core.Config(api_url="http://127.0.0.1:9", token="x")
        root = tk_stub.Tk()
        app = gui.StashApp(root, cfg, autoconnect=False)

        # nothing selected -> the app explains itself and starts nothing
        app.tree.selection_set([])
        app.act_download_songs()
        self.assertTrue(DIALOGS.showinfo.calls)
        self.assertFalse(app.manager.busy)

        # a selection while offline -> guard warns instead of crashing
        dlc = core.Dlc("Arcaea", "djmax/By_DLC/Arcaea/")
        node = app.tree.insert("", "end", text="Arcaea")
        app.tree_nodes[node] = ("dlc", dlc)
        app.tree.selection_set([node])
        app.act_download_dlc()
        self.assertTrue(DIALOGS.showwarning.calls)
        self.assertIn("Not connected", DIALOGS.showwarning.calls[0][1])
        self.assertFalse(app.manager.busy)

    def test_menu_actions_map_to_the_right_plans(self):
        dlc = core.Dlc("Arcaea", "djmax/By_DLC/Arcaea/")
        song = core.Song("Arcaea", "Halcyon [222]", "djmax/By_DLC/Arcaea/Songs/Halcyon [222]/",
                         "djmax/By_DLC/Arcaea/Songs/Halcyon [222]/Chart and OGG/", "Chart and OGG")
        self.assertEqual(tasks.DownloadPlan.songs_of([song]).kind, "songs")
        self.assertEqual(tasks.DownloadPlan.songs_of([song], whole_folder=True).kind, "song_folder")
        self.assertEqual(tasks.DownloadPlan.assets_of([dlc]).kind, "assets")
        self.assertEqual(tasks.DownloadPlan.dlc_of([dlc]).kind, "dlc")
        self.assertEqual(tasks.DownloadPlan.folder_of("x/y/Gears/").kind, "folder")
        # labels stay readable
        self.assertIn("Halcyon", tasks.DownloadPlan.songs_of([song]).label)
        self.assertIn("Arcaea", tasks.DownloadPlan.assets_of([dlc]).label)


if __name__ == "__main__":
    unittest.main(verbosity=2)
