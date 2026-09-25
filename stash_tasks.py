"""
DJMAX Stash -- background tasks.

The GUI is a thin shell over this module: every menu action turns into a
:class:`DownloadPlan`, and everything that talks to the network runs on a worker
thread and reports back as events on a queue.  Keeping it out of the Tkinter file
means all of it can be tested without a display.

Event protocol (dicts, always with a "type" key):

  connection      {"type": "connecting" | "connected" | "connection_failed",
                   "url": str, "error": str, "info": dict}
  scan            {"type": "scan_start", "dlcs": int | None}
                  {"type": "scan_dlc", "dlc": str, "folders": int, "songs": int, "index": int, "total": int}
                  {"type": "scan_songs", "dlc": str, "songs": [Song, ...]}
                  {"type": "scan_done", "dlcs": [Dlc, ...], "warnings": [str, ...]}
                  {"type": "scan_failed", "error": str}
  plan            {"type": "plan_start", "label": str}
                  {"type": "plan_progress", "files": int, "detail": str}
                  {"type": "plan_done", "items": [DownloadItem, ...], "label": str, "cancelled": bool}
                  {"type": "plan_empty", "label": str}
  download        (all Downloader events pass through unchanged)
  busy            {"type": "busy", "value": bool}
  log             {"type": "log", "level": str, "msg": str}
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence

import stash_core as core


# ---------------------------------------------------------------------------
# what a menu action means
# ---------------------------------------------------------------------------


@dataclass
class DownloadPlan:
    """A resolved 'download this' request, before it hits the network."""

    kind: str                      # songs | assets | dlc | folder | song_folder
    label: str                     # human readable, used in the UI + logs
    dlcs: List[core.Dlc] = field(default_factory=list)
    songs: List[core.Song] = field(default_factory=list)
    prefixes: List[str] = field(default_factory=list)
    include_songs: bool = True

    def describe(self) -> str:
        if self.kind == "songs":
            return f"Songs ({self.label})"
        if self.kind == "assets":
            return f"Assets ({self.label})"
        if self.kind == "dlc":
            return f"DLC - assets + songs ({self.label})"
        if self.kind == "song_folder":
            return f"Full song folder ({self.label})"
        return f"Folder ({self.label})"

    # -- menu actions -------------------------------------------------------
    @classmethod
    def songs_of(cls, songs: Sequence[core.Song], whole_folder: bool = False) -> "DownloadPlan":
        names = ", ".join(s.title for s in songs[:2])
        if len(songs) > 2:
            names += f", +{len(songs) - 2}"
        return cls(kind="song_folder" if whole_folder else "songs",
                   label=names or "nothing selected", songs=list(songs))

    @classmethod
    def assets_of(cls, dlcs: Sequence[core.Dlc]) -> "DownloadPlan":
        return cls(kind="assets", label=_names(dlcs), dlcs=list(dlcs), include_songs=False)

    @classmethod
    def dlc_of(cls, dlcs: Sequence[core.Dlc], include_songs: bool = True) -> "DownloadPlan":
        return cls(kind="dlc", label=_names(dlcs), dlcs=list(dlcs), include_songs=include_songs)

    @classmethod
    def folder_of(cls, prefix: str) -> "DownloadPlan":
        return cls(kind="folder", label=prefix.rstrip("/").rsplit("/", 1)[-1] or prefix,
                   prefixes=[prefix if prefix.endswith("/") else prefix + "/"])


def _names(dlcs: Sequence[core.Dlc]) -> str:
    names = ", ".join(d.name for d in dlcs[:2])
    if len(dlcs) > 2:
        names += f", +{len(dlcs) - 2}"
    return names or "nothing selected"


# ---------------------------------------------------------------------------
# task manager
# ---------------------------------------------------------------------------


class TaskManager:
    """Owns the worker threads and the event queue the GUI drains.

    ``emit`` is called from worker threads -- any GUI must make it thread-safe
    (the bundled GUI just puts events on a queue and polls with after()).
    """

    def __init__(self, config: core.Config, emit: Optional[Callable[[dict], None]] = None):
        self.cfg = config
        self._emit = emit or (lambda event: None)
        self.browser: Optional[core.StashBrowser] = None
        self.api: Optional[core.StashAPI] = None
        self._lock = threading.Lock()
        self._busy = False
        self._cancel = threading.Event()
        self._downloader: Optional[core.Downloader] = None
        self._threads: List[threading.Thread] = []

    # -- state --------------------------------------------------------------
    @property
    def busy(self) -> bool:
        with self._lock:
            return self._busy

    @property
    def connected(self) -> bool:
        return self.api is not None and self.browser is not None

    def cancel(self):
        self._cancel.set()
        with self._lock:
            downloader = self._downloader
        if downloader is not None:
            downloader.cancel()
        self.emit({"type": "log", "level": "warn", "msg": "Cancel requested..."})

    def emit(self, event: dict):
        try:
            self._emit(event)
        except Exception:
            pass

    def _set_busy(self, value: bool):
        with self._lock:
            self._busy = value
        self.emit({"type": "busy", "value": value})

    def _spawn(self, target, name: str):
        thread = threading.Thread(target=target, name=name, daemon=True)
        thread.start()
        with self._lock:
            self._threads = [t for t in self._threads if t.is_alive()]
            self._threads.append(thread)
        return thread

    # -- connect + scan -----------------------------------------------------
    def connect(self, config: Optional[core.Config] = None, deep: bool = True):
        """Ping the Worker, list DLCs, and (deep) their folders + songs."""
        if config is not None:
            self.cfg = config
        if self.busy:
            self.emit({"type": "log", "level": "warn", "msg": "Already busy - ignored"})
            return
        self._cancel.clear()

        def job():
            self._set_busy(True)
            try:
                self.emit({"type": "connecting", "url": self.cfg.api_url})
                api = core.make_api(self.cfg)
                info = api.ping()
                browser = core.StashBrowser(api)
                self.api, self.browser = api, browser
                self.emit({"type": "connected", "url": self.cfg.api_url, "info": info})

                self.emit({"type": "scan_start", "dlcs": None})
                dlcs = browser.dlcs(force=True)
                warnings: List[str] = []
                if not dlcs:
                    warnings.append(
                        f"No DLC folders under {core.dlc_root_prefix(self.cfg)} - "
                        f"check the layout in Settings.")
                self.emit({"type": "scan_start", "dlcs": len(dlcs)})

                for index, dlc in enumerate(dlcs, 1):
                    if self._cancel.is_set():
                        warnings.append("Scan cancelled - list may be incomplete.")
                        break
                    folders = browser.dlc_folders(dlc)
                    songs: List[core.Song] = []
                    if deep:
                        songs = browser.songs(dlc)
                        if not any(f.is_song_container for f in folders) and songs:
                            warnings.append(f"{dlc.name}: songs found but no '{self.cfg.song_dir}' folder")
                        if folders and not songs:
                            warnings.append(f"{dlc.name}: no songs found under '{self.cfg.song_dir}'")
                    self.emit({"type": "scan_dlc", "dlc": dlc.name, "dlc_obj": dlc,
                               "folders": folders, "songs": songs, "songs_loaded": deep,
                               "index": index, "total": len(dlcs)})
                self.emit({"type": "scan_done", "dlcs": dlcs, "warnings": warnings})
            except core.AuthError as exc:
                self.api = self.browser = None
                self.emit({"type": "connection_failed", "url": self.cfg.api_url,
                           "error": str(exc), "hint": "Check the token in Settings."})
            except core.NetworkError as exc:
                self.api = self.browser = None
                self.emit({"type": "connection_failed", "url": self.cfg.api_url,
                           "error": str(exc),
                           "hint": "Check the URL and that the Worker is deployed."})
            except core.StashError as exc:
                self.api = self.browser = None
                self.emit({"type": "connection_failed", "url": self.cfg.api_url, "error": str(exc)})
            except Exception as exc:  # pragma: no cover - last resort
                self.api = self.browser = None
                self.emit({"type": "connection_failed", "url": self.cfg.api_url,
                           "error": f"{type(exc).__name__}: {exc}"})
            finally:
                self._set_busy(False)

        self._spawn(job, "connect")

    def load_songs(self, dlc: core.Dlc):
        """Fetch songs for a single DLC (used when a tree node is expanded)."""
        if not self.browser:
            return

        def job():
            try:
                songs = self.browser.songs(dlc)
                self.emit({"type": "scan_songs", "dlc": dlc.name, "dlc_obj": dlc, "songs": songs})
            except core.StashError as exc:
                self.emit({"type": "log", "level": "error",
                           "msg": f"Could not list songs for {dlc.name}: {exc}"})

        self._spawn(job, f"songs-{dlc.name}")

    def load_size(self, prefix: str, token: str):
        """Fetch the byte size of a prefix and report it back with its token."""
        if not self.browser:
            return

        def job():
            try:
                size = self.browser.prefix_size(prefix)
                self.emit({"type": "size", "prefix": prefix, "token": token, "bytes": size})
            except core.StashError as exc:
                self.emit({"type": "size", "prefix": prefix, "token": token, "bytes": None,
                           "error": str(exc)})

        self._spawn(job, "size")

    def search(self, query: str, dlcs: Sequence[core.Dlc] = ()) -> None:
        """Client-side search across already-scanned DLCs and songs."""
        if not self.browser:
            return
        needle = (query or "").strip().lower()
        if len(needle) < 2:
            self.emit({"type": "search_results", "query": query, "results": []})
            return

        def job():
            results = []
            try:
                for dlc in (dlcs or self.browser.dlcs()):
                    if needle in dlc.name.lower():
                        results.append({"kind": "dlc", "name": dlc.name, "dlc": dlc.name})
                    for folder in self.browser.dlc_folders(dlc):
                        if needle in folder.name.lower():
                            results.append({"kind": "folder", "name": folder.name,
                                            "dlc": dlc.name, "prefix": folder.prefix})
                    for song in self.browser.songs(dlc):
                        if needle in song.title.lower():
                            results.append({"kind": "song", "name": song.title,
                                            "dlc": dlc.name})
                    if self._cancel.is_set():
                        break
            except core.StashError as exc:
                self.emit({"type": "log", "level": "error", "msg": f"Search failed: {exc}"})
            self.emit({"type": "search_results", "query": query, "results": results})

        self._spawn(job, "search")

    # -- planning -----------------------------------------------------------
    def plan_items(self, plan: DownloadPlan) -> List[core.DownloadItem]:
        """Turn a plan into concrete file items (blocking; runs on a worker)."""
        browser = self.browser
        if browser is None:
            raise core.StashError("Not connected")

        def progress(count, detail):
            self.emit({"type": "plan_progress", "files": count, "detail": detail})

        if plan.kind in ("songs", "song_folder"):
            return browser.plan_songs(plan.songs, whole_song_folder=(plan.kind == "song_folder"),
                                      on_progress=progress)
        if plan.kind == "assets":
            items: List[core.DownloadItem] = []
            for dlc in plan.dlcs:
                items.extend(browser.plan_dlc([dlc], include_songs=False, on_progress=progress))
            return items
        if plan.kind == "dlc":
            items = []
            for dlc in plan.dlcs:
                items.extend(browser.plan_dlc([dlc], include_songs=plan.include_songs,
                                              on_progress=progress))
            return items
        if plan.kind == "folder":
            items = []
            for prefix in plan.prefixes:
                items.extend(browser.plan_folder(prefix, on_progress=progress))
            return items
        raise core.StashError(f"Unknown plan kind {plan.kind!r}")

    # -- download -----------------------------------------------------------
    def download(self, plan: DownloadPlan, output_dir: Optional[str] = None,
                 on_finished: Optional[Callable[[core.DownloadResult], None]] = None):
        if self.busy:
            self.emit({"type": "log", "level": "warn", "msg": "Already busy - ignored"})
            return
        if not self.connected:
            self.emit({"type": "log", "level": "error", "msg": "Not connected - press Refresh first"})
            return
        self._cancel.clear()
        dest = output_dir or self.cfg.output_dir

        def job():
            self._set_busy(True)
            try:
                self.emit({"type": "plan_start", "label": plan.describe()})
                items = self.plan_items(plan)
                if self._cancel.is_set():
                    self.emit({"type": "plan_done", "items": [], "label": plan.describe(),
                               "cancelled": True})
                    return
                if not items:
                    self.emit({"type": "plan_empty", "label": plan.describe()})
                    return
                self.emit({"type": "plan_done", "items": items, "label": plan.describe(),
                           "cancelled": False})

                total = sum(i.size for i in items)
                self.emit({"type": "log", "level": "info",
                           "msg": f"{plan.describe()}: {len(items)} files, {core.human_bytes(total)}"})

                downloader = core.Downloader(
                    core.make_api(self.cfg), dest, workers=self.cfg.workers,
                    verify_md5=self.cfg.verify_md5, resume=self.cfg.resume,
                    on_event=self.emit)
                with self._lock:
                    self._downloader = downloader
                result = downloader.run(items)
                with self._lock:
                    self._downloader = None
                if on_finished:
                    on_finished(result)
            except core.AuthError as exc:
                self.emit({"type": "download_failed", "error": str(exc)})
            except core.StashError as exc:
                self.emit({"type": "download_failed", "error": str(exc)})
            except Exception as exc:  # pragma: no cover
                self.emit({"type": "download_failed", "error": f"{type(exc).__name__}: {exc}"})
            finally:
                self._set_busy(False)

        self._spawn(job, "download")

    # -- convenience for tests / scripting ---------------------------------
    def run_plan_sync(self, plan: DownloadPlan, output_dir: Optional[str] = None,
                      timeout: float = 300.0) -> core.DownloadResult:
        """Blocking variant used by tests: returns the finished result."""
        done = threading.Event()
        box: Dict[str, core.DownloadResult] = {}

        def finished(result: core.DownloadResult):
            box["result"] = result
            done.set()

        self.download(plan, output_dir, on_finished=finished)
        if not done.wait(timeout):
            self.cancel()
            raise TimeoutError("download did not finish in time")
        return box["result"]


def drain(events: "queue.Queue[dict]", limit: int = 10000) -> List[dict]:
    """Pull every queued event (helper for tests and the GUI's after() loop)."""
    out: List[dict] = []
    for _ in range(limit):
        try:
            out.append(events.get_nowait())
        except queue.Empty:
            break
    return out


def wait_for(events: "queue.Queue[dict]", predicate: Callable[[dict], bool],
             timeout: float = 60.0) -> Optional[dict]:
    """Watch an event queue until predicate matches (helper for tests)."""
    deadline = time.time() + timeout
    seen: List[dict] = []
    while time.time() < deadline:
        try:
            event = events.get(timeout=min(0.3, max(0.01, deadline - time.time())))
        except queue.Empty:
            continue
        if predicate(event):
            return event
        seen.append(event)
    return None
