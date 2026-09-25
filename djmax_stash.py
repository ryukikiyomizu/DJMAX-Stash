#!/usr/bin/env python3
"""
DJMAX Stash - desktop downloader
================================

A Tkinter GUI for pulling songs, assets and whole DLCs out of a Cloudflare R2
bucket that is fronted by the Worker in worker/worker.js.

    python djmax_stash.py                     # normal use
    python djmax_stash.py --api-url URL --token XXX
    python djmax_stash.py --output "D:/DJMAX Stash"
    python djmax_stash.py --demo              # try it against the mock server

Menu
----
  Download -> Songs ................. <Song>/Chart and OGG/**  (the .ogg keysound, all
                                      .pt charts, MV excluded)
  Download -> Assets ................ Gears/, Other Assets/, anything that isn't Songs
  Download -> DLC Assets + Songs .... the entire <DLC>/ folder

Standard library only, Python 3.8+.
"""

from __future__ import annotations

import argparse
import os
import platform
import queue
import subprocess
import sys
import threading
import time
import traceback
import types
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import stash_core as core  # noqa: E402
import stash_tasks as tasks  # noqa: E402

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    TK_AVAILABLE = True
except ImportError:  # pragma: no cover - explained with a friendly message in main()
    TK_AVAILABLE = False

    class _MissingTk:
        """Placeholder base so the class definitions below still import."""

        def __init__(self, *args, **kwargs):
            raise RuntimeError("Tkinter is not installed")

    tk = ttk = types.SimpleNamespace(Frame=_MissingTk, Tk=_MissingTk, Toplevel=_MissingTk)
    filedialog = types.SimpleNamespace(askdirectory=lambda **kw: "")
    messagebox = types.SimpleNamespace(showinfo=lambda *a, **kw: None,
                                       showwarning=lambda *a, **kw: None,
                                       showerror=lambda *a, **kw: None)


# ---------------------------------------------------------------------------
# theme
# ---------------------------------------------------------------------------

DARK = {
    "bg": "#12141c", "bg_alt": "#1a1e2a", "bg_sel": "#2a3550",
    "fg": "#e8ecf5", "fg_dim": "#8b95ad", "accent": "#4da3ff",
    "accent_hot": "#7bbdff", "ok": "#4dd08a", "warn": "#ffc857",
    "err": "#ff6b6b", "border": "#2a3040",
}

FONT_UI = ("Segoe UI", 10) if os.name == "nt" else ("Helvetica", 11)
FONT_SMALL = (FONT_UI[0], FONT_UI[1] - 1)
FONT_BOLD = (FONT_UI[0], FONT_UI[1], "bold")
FONT_MONO = ("Consolas", 9) if os.name == "nt" else ("Menlo", 10)


def app_icon() -> Optional[object]:
    """A tiny generated icon so the window doesn't look anonymous."""
    if not TK_AVAILABLE:
        return None
    try:
        image = tk.PhotoImage(width=32, height=32)
        rows = []
        for y in range(32):
            row = []
            for x in range(32):
                row.append(DARK["accent"] if 0 <= x - y < 6 else DARK["bg"])
            rows.append("{" + " ".join(row) + "}")
        image.put(" ".join(rows), to=(0, 0, 32, 32))  # one call, not 1024
        return image
    except Exception:
        return None


# ---------------------------------------------------------------------------
# small widgets
# ---------------------------------------------------------------------------


class StatusBar(ttk.Frame):
    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self.dot = tk.Canvas(self, width=12, height=12, highlightthickness=0,
                             bg=DARK["bg_alt"])
        self.dot.pack(side="left", padx=(8, 4))
        self._dot_id = self.dot.create_oval(3, 3, 10, 10, fill=DARK["fg_dim"], outline="")
        self.label = ttk.Label(self, text="Not connected", style="Dim.TLabel")
        self.label.pack(side="left")
        self.detail = ttk.Label(self, text="", style="Dim.TLabel")
        self.detail.pack(side="right", padx=8)

    def set_state(self, state: str, text: str, detail: str = ""):
        colours = {"ok": DARK["ok"], "busy": DARK["warn"], "err": DARK["err"],
                   "idle": DARK["fg_dim"], "accent": DARK["accent"]}
        self.dot.itemconfig(self._dot_id, fill=colours.get(state, DARK["fg_dim"]))
        self.label.configure(text=text)
        self.detail.configure(text=detail)


class ConnectionBar(ttk.Frame):
    """URL + token entry, with a Test button. Collapsed by default."""

    def __init__(self, master, on_connect, on_save, **kwargs):
        super().__init__(master, **kwargs)
        self.on_connect = on_connect
        self.on_save = on_save
        self.visible = False

        self.url_var = tk.StringVar()
        self.token_var = tk.StringVar()

        self.body = ttk.Frame(self, style="Panel.TFrame")
        self.body.columnconfigure(1, weight=1)

        ttk.Label(self.body, text="API URL", style="PanelDim.TLabel").grid(
            row=0, column=0, sticky="w", padx=(12, 8), pady=(10, 2))
        ttk.Entry(self.body, textvariable=self.url_var).grid(
            row=0, column=1, sticky="ew", padx=(0, 12), pady=(10, 2))
        ttk.Label(self.body, text="Token", style="PanelDim.TLabel").grid(
            row=1, column=0, sticky="w", padx=(12, 8), pady=2)
        ttk.Entry(self.body, textvariable=self.token_var, show="*").grid(
            row=1, column=1, sticky="ew", padx=(0, 12), pady=2)

        buttons = ttk.Frame(self.body, style="Panel.TFrame")
        buttons.grid(row=2, column=0, columnspan=2, sticky="ew", padx=12, pady=10)
        buttons.columnconfigure(0, weight=1)
        ttk.Button(buttons, text="How do I get these?", command=self._help).grid(
            row=0, column=0, sticky="w")
        ttk.Button(buttons, text="Test && connect", style="Accent.TButton",
                   command=self._connect).grid(row=0, column=1, padx=(0, 8))
        ttk.Button(buttons, text="Save to config", command=self._save).grid(row=0, column=2)
        ttk.Label(self.body, text="The token is stored obfuscated in config.json "
                                  "(read-only, prefix-scoped on the server).",
                  style="PanelDim.TLabel", wraplength=560, justify="left").grid(
            row=3, column=0, columnspan=2, sticky="w", padx=12, pady=(0, 10))

        self.body.pack(fill="x")   # built visible...
        self.visible = True
        self.set_visible(False)    # ...but starts collapsed

    def setup(self, cfg: core.Config):
        self.url_var.set(cfg.api_url)
        self.token_var.set(cfg.token)

    def set_visible(self, show: bool):
        self.visible = bool(show)
        if self.visible:
            self.body.pack(fill="x")
        else:
            self.body.pack_forget()

    def toggle(self, show: Optional[bool] = None):
        self.set_visible((not self.visible) if show is None else show)

    def values(self) -> Tuple[str, str]:
        return self.url_var.get().strip(), self.token_var.get().strip()

    def _help(self):
        messagebox.showinfo(f"{core.APP_NAME} - connecting",
                            "To connect this app you need:\n\n"
                            "  1. worker/worker.js deployed to Cloudflare, with an R2 binding\n"
                            "     named BUCKET (see worker/README.md - about two minutes)\n"
                            "  2. The Worker URL, e.g.\n"
                            "     https://djmax-stash-api.you.workers.dev\n"
                            "  3. An app token: the APP_TOKEN secret you set with\n"
                            "     'wrangler secret put APP_TOKEN'\n\n"
                            "Paste both above and press 'Test & connect', then 'Save to config'.\n\n"
                            "Command line checks:\n"
                            "  python djmax_stash_cli.py doctor --api-url ... --token ...\n"
                            "  python djmax_stash.py --selftest --api-url ... --token ...")

    def _connect(self):
        self.on_connect(*self.values())

    def _save(self):
        self.on_save(*self.values())


class FolderPicker:
    """Entry + Browse, wired to a StringVar."""

    def __init__(self, master, label: str, on_change=None, panel: bool = False):
        self.frame = ttk.Frame(master, style="Panel.TFrame" if panel else "TFrame")
        self.frame.columnconfigure(1, weight=1)
        self.var = tk.StringVar()
        ttk.Label(self.frame, text=label, style="Dim.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 8))
        entry = ttk.Entry(self.frame, textvariable=self.var)
        entry.grid(row=0, column=1, sticky="ew")
        ttk.Button(self.frame, text="...", width=3, command=self._browse).grid(
            row=0, column=2, padx=(6, 0))
        if on_change:
            self.var.trace_add("write", lambda *_: on_change(self.var.get()))

    def _browse(self):
        chosen = filedialog.askdirectory(title="Choose download folder",
                                         initialdir=self.var.get() or str(Path.home()))
        if chosen:
            self.var.set(os.path.normpath(chosen))

    def get(self) -> str:
        return self.var.get().strip()

    def set(self, value: str):
        self.var.set(value)


# ---------------------------------------------------------------------------
# main window
# ---------------------------------------------------------------------------


class StashApp:
    POLL_MS = 80
    MAX_LOG_LINES = 800

    def __init__(self, root: "tk.Tk", cfg: core.Config, autoconnect: bool = True):
        self.root = root
        self.cfg = cfg
        self.events: "queue.Queue[dict]" = queue.Queue()
        self.manager = tasks.TaskManager(self.cfg, emit=self.events.put)

        self.dlcs: List[core.Dlc] = []
        self.songs: Dict[str, List[core.Song]] = {}
        self.song_loaded: Dict[str, bool] = {}
        self._folder_cache: Dict[str, List[core.FolderInfo]] = {}
        self.tree_nodes: Dict[str, Tuple[str, object]] = {}   # item id -> (kind, payload)
        self.node_index: Dict[str, str] = {}                  # "dlc:Arcaea" -> item id
        self.node_parent: Dict[str, str] = {}                 # item id -> parent item id
        self.node_children: Dict[str, List[str]] = {}         # item id -> child item ids
        self._detached: set = set()                           # hidden by the filter box
        self._stats = core.DownloadStats()
        self._last_progress = 0.0
        self._tick = time.time()
        self._done_info: Optional[core.DownloadResult] = None

        self._build_window()
        self._build_widgets()
        self.root.after(self.POLL_MS, self._drain_events)
        if autoconnect and self.cfg.api_url:
            self.root.after(150, lambda: self.connect(silent=True))
        else:
            self._status("idle", "Not connected",
                         "Use Connection to enter your API URL and token")

    # -- construction -------------------------------------------------------
    def _build_window(self):
        self.root.title(f"{core.APP_NAME} {core.APP_VERSION}")
        self.root.geometry("1060x780")
        self.root.minsize(940, 660)
        self.root.configure(bg=DARK["bg"])
        icon = app_icon()
        if icon:
            self.root.iconphoto(True, icon)
            self._icon = icon  # keep a reference alive
        self._style()

    def _style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", background=DARK["bg"], foreground=DARK["fg"],
                        fieldbackground=DARK["bg_alt"], bordercolor=DARK["border"],
                        font=FONT_UI)
        style.configure("TFrame", background=DARK["bg"])
        style.configure("Panel.TFrame", background=DARK["bg_alt"])
        style.configure("Bar.TFrame", background=DARK["bg_alt"])
        style.configure("TLabel", background=DARK["bg"], foreground=DARK["fg"])
        style.configure("Dim.TLabel", background=DARK["bg_alt"], foreground=DARK["fg_dim"],
                        font=FONT_SMALL)
        style.configure("PanelDim.TLabel", background=DARK["bg_alt"], foreground=DARK["fg_dim"],
                        font=FONT_SMALL)
        style.configure("Head.TLabel", background=DARK["bg"], foreground=DARK["fg"],
                        font=FONT_BOLD)
        style.configure("Big.TLabel", background=DARK["bg_alt"], foreground=DARK["fg"],
                        font=FONT_BOLD)
        style.configure("TButton", background=DARK["bg_alt"], foreground=DARK["fg"],
                        borderwidth=1, focusthickness=0, padding=(10, 5))
        style.map("TButton",
                  background=[("active", DARK["bg_sel"]), ("disabled", DARK["bg"])],
                  foreground=[("disabled", DARK["fg_dim"])])
        style.configure("Accent.TButton", background=DARK["accent"], foreground="#10141c",
                        font=FONT_BOLD)
        style.map("Accent.TButton",
                  background=[("active", DARK["accent_hot"]), ("disabled", DARK["border"])])
        style.configure("TEntry", fieldbackground=DARK["bg_alt"], foreground=DARK["fg"],
                        insertcolor=DARK["fg"], bordercolor=DARK["border"])
        # TCheckbutton: the clam theme's default indicator is near-invisible on a
        # dark background, so drive indicatorcolor explicitly -- a filled accent
        # square when ticked, an outlined dark square when not.
        style.configure("TCheckbutton", background=DARK["bg"], foreground=DARK["fg"],
                        indicatorcolor=DARK["bg_alt"], focuscolor=DARK["accent"],
                        padding=(2, 3))
        style.map("TCheckbutton",
                  background=[("active", DARK["bg"])],
                  foreground=[("disabled", DARK["fg_dim"])],
                  indicatorcolor=[("disabled", DARK["border"]),
                                  ("selected", DARK["accent"]),
                                  ("!selected", DARK["bg_alt"])],
                  bordercolor=[("selected", DARK["accent"])])
        style.configure("Panel.TCheckbutton", background=DARK["bg_alt"], foreground=DARK["fg"],
                        indicatorcolor=DARK["bg"], focuscolor=DARK["accent"],
                        padding=(2, 3))
        style.map("Panel.TCheckbutton",
                  background=[("active", DARK["bg_alt"])],
                  indicatorcolor=[("disabled", DARK["border"]),
                                  ("selected", DARK["accent"]),
                                  ("!selected", DARK["bg"])],
                  bordercolor=[("selected", DARK["accent"])])
        # TCombobox replaces the Spinbox: clam's spin arrows render as black
        # blocks on dark themes, whereas the combobox dropdown is reliable.
        style.configure("TCombobox", fieldbackground=DARK["bg_alt"], background=DARK["bg_alt"],
                        foreground=DARK["fg"], arrowcolor=DARK["fg"],
                        bordercolor=DARK["border"], lightcolor=DARK["bg_alt"],
                        darkcolor=DARK["bg_alt"], selectbackground=DARK["bg_alt"],
                        selectforeground=DARK["fg"], padding=(4, 2))
        style.map("TCombobox",
                  fieldbackground=[("readonly", DARK["bg_alt"])],
                  foreground=[("readonly", DARK["fg"])],
                  background=[("active", DARK["bg_sel"])])
        self.root.option_add("*TCombobox*Listbox.background", DARK["bg_alt"])
        self.root.option_add("*TCombobox*Listbox.foreground", DARK["fg"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", DARK["bg_sel"])
        self.root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        style.configure("Treeview", background=DARK["bg_alt"], fieldbackground=DARK["bg_alt"],
                        foreground=DARK["fg"], rowheight=24, borderwidth=0,
                        font=FONT_UI)
        style.map("Treeview",
                  background=[("selected", DARK["bg_sel"])],
                  foreground=[("selected", "#ffffff")])
        style.configure("Treeview.Heading", background=DARK["bg"], foreground=DARK["fg_dim"],
                        relief="flat", font=FONT_SMALL)
        style.map("Treeview.Heading", background=[("active", DARK["bg_alt"])])
        style.configure("TProgressbar", background=DARK["accent"], troughcolor=DARK["bg_alt"],
                        borderwidth=0, thickness=14)
        for orient in ("Vertical", "Horizontal"):
            style.configure(f"{orient}.TScrollbar", background=DARK["bg_alt"],
                            troughcolor=DARK["bg"], bordercolor=DARK["bg"],
                            arrowcolor=DARK["fg_dim"], darkcolor=DARK["bg_alt"],
                            lightcolor=DARK["bg_alt"], gripcount=0)
            style.map(f"{orient}.TScrollbar",
                      background=[("active", DARK["bg_sel"]), ("pressed", DARK["accent"])],
                      arrowcolor=[("active", DARK["fg"])])
        style.configure("TSpinbox", fieldbackground=DARK["bg_alt"], foreground=DARK["fg"],
                        arrowcolor=DARK["fg"], bordercolor=DARK["border"])
        style.configure("TNotebook", background=DARK["bg"], borderwidth=0)
        style.configure("TNotebook.Tab", background=DARK["bg_alt"], foreground=DARK["fg_dim"],
                        padding=(14, 7))
        style.map("TNotebook.Tab",
                  background=[("selected", DARK["bg"])],
                  foreground=[("selected", DARK["fg"])])
        style.configure("TSeparator", background=DARK["border"])

    def _build_widgets(self):
        # The window is laid out with grid: only the middle row (the tree +
        # detail panel) has weight, so when the window is small Tk shrinks that
        # row instead of clipping the notebook or the status bar off the bottom.
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(2, weight=1)   # row 0 toolbar, 1 connection, 2 body,
                                              # 3 notebook, 4 status bar

        # ---- top toolbar
        toolbar = ttk.Frame(self.root, style="Bar.TFrame", padding=(10, 8))
        toolbar.grid(row=0, column=0, sticky="ew")

        ttk.Label(toolbar, text="DJMAX STASH", style="Head.TLabel").pack(side="left",
                                                                        padx=(4, 16))
        self.conn_btn = ttk.Button(toolbar, text="Connection", command=self._toggle_connection)
        self.conn_btn.pack(side="left")
        self.refresh_btn = ttk.Button(toolbar, text="Refresh", command=lambda: self.connect())
        self.refresh_btn.pack(side="left", padx=6)
        self.cancel_btn = ttk.Button(toolbar, text="Cancel", command=self._cancel, state="disabled")
        self.cancel_btn.pack(side="left")

        search_box = ttk.Frame(toolbar, style="Bar.TFrame")
        search_box.pack(side="right", padx=(0, 10))
        ttk.Label(search_box, text="Filter", style="Dim.TLabel").pack(side="left", padx=(0, 6))
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._apply_filter())
        ttk.Entry(search_box, textvariable=self.search_var, width=24).pack(side="left")

        # ---- connection bar
        self.connection = ConnectionBar(self.root, self._connect_with, self._save_connection)
        self.connection.grid(row=1, column=0, sticky="ew")
        self.connection.setup(self.cfg)
        self.connection.set_visible(not self.cfg.api_url)

        # ---- body: tree + detail
        body = ttk.Frame(self.root, padding=(10, 8))
        body.grid(row=2, column=0, sticky="nsew")
        body.columnconfigure(0, weight=3, minsize=380)
        body.columnconfigure(1, weight=2, minsize=280)
        body.rowconfigure(0, weight=1)

        left = ttk.Frame(body)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)

        self.tree = ttk.Treeview(left, selectmode="extended", show="tree headings",
                                 columns=("info",))
        self.tree.heading("#0", text="DLC  /  Songs", anchor="w")
        self.tree.heading("info", text="Chart folder  /  Size", anchor="w")
        self.tree.column("#0", width=330, stretch=True, minwidth=200)
        self.tree.column("info", width=200, stretch=False, minwidth=120)
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.bind("<<TreeviewOpen>>", self._on_expand)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.tag_configure("dlc", font=FONT_BOLD)
        self.tree.tag_configure("dim", foreground=DARK["fg_dim"])
        self.tree.tag_configure("song", foreground=DARK["fg"])
        self.tree.tag_configure("missing", foreground=DARK["warn"])
        self.tree.tag_configure("folder", foreground=DARK["fg_dim"])

        scroll = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)

        # ---- right panel
        right = ttk.Frame(body, style="Panel.TFrame", padding=12)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)

        ttk.Label(right, text="Selection", style="Big.TLabel").grid(row=0, column=0, sticky="w")
        self.selection_label = ttk.Label(right, text="Nothing selected", style="PanelDim.TLabel",
                                         wraplength=320, justify="left")
        self.selection_label.grid(row=1, column=0, sticky="w", pady=(4, 10))

        quick = ttk.Frame(right, style="Panel.TFrame")
        quick.grid(row=2, column=0, sticky="ew")
        quick.columnconfigure(0, weight=1)
        for index, (text, command, style) in enumerate((
            ("Download songs", self.act_download_songs, "Accent.TButton"),
            ("Download assets", self.act_download_assets, "TButton"),
            ("Download DLC (assets + songs)", self.act_download_dlc, "TButton"),
            ("Download whole song folders (with MV)", self.act_download_full_song, "TButton"),
            ("Download selected folder", self.act_download_folder, "TButton"),
        )):
            ttk.Button(quick, text=text, command=command, style=style).grid(
                row=index, column=0, sticky="ew", pady=2)

        self.folder = FolderPicker(right, "Save to", on_change=lambda _v: self._sync_options(),
                                   panel=True)
        self.folder.frame.grid(row=3, column=0, sticky="ew", pady=(12, 0))

        self.var_overwrite = tk.BooleanVar(value=False)
        self.var_verify = tk.BooleanVar(value=self.cfg.verify_md5)
        self.var_resume = tk.BooleanVar(value=self.cfg.resume)
        self.var_open = tk.BooleanVar(value=False)

        workers = ttk.Frame(right, style="Panel.TFrame")
        workers.grid(row=4, column=0, sticky="ew", pady=(12, 0))
        workers.columnconfigure(0, weight=1)
        ttk.Label(workers, text="Parallel downloads", style="PanelDim.TLabel").grid(
            row=0, column=0, sticky="w")
        self.workers_var = tk.StringVar(value=str(self.cfg.workers))
        combo = ttk.Combobox(workers, textvariable=self.workers_var, width=3,
                             state="readonly", values=[str(i) for i in range(1, 17)])
        combo.grid(row=0, column=1, sticky="e")
        combo.bind("<<ComboboxSelected>>", lambda _e: self._sync_options())

        # keep the panel content pinned to the top when the window is tall
        right.rowconfigure(6, weight=1)

        # ---- notebook: transfers / activity
        book = ttk.Notebook(self.root)
        book.grid(row=3, column=0, sticky="ew", padx=10, pady=(0, 6))

        self.transfer_tab = ttk.Frame(book, padding=12)
        self.activity_tab = ttk.Frame(book, padding=(0, 0))
        self.options_tab = ttk.Frame(book, padding=12)
        book.add(self.transfer_tab, text="Transfers")
        book.add(self.activity_tab, text="Activity")
        book.add(self.options_tab, text="Options")

        self.options_tab.columnconfigure(1, weight=1)
        for index, (text, var, command, help_text) in enumerate((
            ("Re-download existing files", self.var_overwrite, self._sync_options,
             "ignore files already on disk and fetch them again"),
            ("Verify checksums", self.var_verify, self._sync_options,
             "compare every finished file against the bucket's MD5 (recommended)"),
            ("Resume partial downloads", self.var_resume, self._sync_options,
             "continue a stopped transfer instead of starting the file over"),
            ("Open folder when finished", self.var_open, None,
             "launch the save folder after a download completes"),
        )):
            ttk.Checkbutton(self.options_tab, text=text, variable=var,
                            command=command).grid(row=index, column=0, sticky="w",
                                                  pady=2, padx=(0, 14))
            ttk.Label(self.options_tab, text=help_text,
                      style="Dim.TLabel").grid(row=index, column=1, sticky="w", pady=2)
        self.transfer_tab.columnconfigure(0, weight=1)

        self.big_label = ttk.Label(self.transfer_tab, text="Idle",
                                   style="Big.TLabel", background=DARK["bg"])
        self.big_label.grid(row=0, column=0, sticky="w")
        self.bar = ttk.Progressbar(self.transfer_tab, maximum=100.0)
        self.bar.grid(row=1, column=0, sticky="ew", pady=8)
        self.numbers = ttk.Label(self.transfer_tab, text="", style="Dim.TLabel",
                                 background=DARK["bg"])
        self.numbers.grid(row=2, column=0, sticky="w")
        self.eta_label = ttk.Label(self.transfer_tab, text="", style="Dim.TLabel",
                                   background=DARK["bg"])
        self.eta_label.grid(row=3, column=0, sticky="w", pady=(2, 8))
        self.files_label = ttk.Label(self.transfer_tab, text="", style="Dim.TLabel",
                                     background=DARK["bg"], justify="left", wraplength=900)
        self.files_label.grid(row=4, column=0, sticky="w")
        self.result_label = ttk.Label(self.transfer_tab, text="", style="Dim.TLabel",
                                      background=DARK["bg"], justify="left", wraplength=900)
        self.result_label.grid(row=5, column=0, sticky="w", pady=(6, 0))

        self.log = tk.Text(self.activity_tab, height=8, wrap="none", bg=DARK["bg_alt"],
                           fg=DARK["fg"], insertbackground=DARK["fg"], relief="flat",
                           font=FONT_MONO, padx=8, pady=6, state="disabled")
        self.log.pack(fill="both", expand=True)
        for tag, colour in (("info", DARK["fg"]), ("warn", DARK["warn"]),
                            ("error", DARK["err"]), ("ok", DARK["ok"]),
                            ("dim", DARK["fg_dim"])):
            self.log.tag_configure(tag, foreground=colour)

        # ---- status bar
        self.status = StatusBar(self.root, style="Bar.TFrame")
        self.status.grid(row=4, column=0, sticky="ew")

        self.root.bind("<Control-a>", lambda _e: self._select_all())
        self.root.bind("<Control-A>", lambda _e: self._select_all())
        self.root.bind("<Escape>", lambda _e: self._cancel())
        self.root.bind("<F5>", lambda _e: self.connect())
        self._sync_options()
        self._log("info", f"{core.APP_NAME} {core.APP_VERSION} ready")
        if self.cfg.source_path:
            self._log("dim", f"config loaded from {self.cfg.source_path}")

    # -- helpers ------------------------------------------------------------
    def _status(self, state: str, text: str, detail: str = ""):
        self.status.set_state(state, text, detail)

    def _log(self, level: str, message: str):
        self.log.configure(state="normal")
        stamp = time.strftime("%H:%M:%S")
        self.log.insert("end", f"{stamp}  {message}\n", level)
        lines = int(self.log.index("end-1c").split(".")[0])
        if lines > self.MAX_LOG_LINES:
            self.log.delete("1.0", f"{lines - self.MAX_LOG_LINES}.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _sync_options(self):
        try:
            self.cfg.workers = max(1, min(16, int(self.workers_var.get())))
        except (tk.TclError, ValueError):
            self.workers_var.set(str(self.cfg.workers))
        self.cfg.verify_md5 = bool(self.var_verify.get())
        self.cfg.resume = bool(self.var_resume.get())
        self.cfg.output_dir = self.folder.get() or self.cfg.output_dir

    # -- connection ---------------------------------------------------------
    def _toggle_connection(self):
        self.connection.setup(self.cfg)
        self.connection.toggle()

    def _connect_with(self, url: str, token: str):
        self.cfg.api_url = url
        self.cfg.token = token
        self.connect()

    def _save_connection(self, url: str, token: str):
        self.cfg.api_url = url
        self.cfg.token = token
        try:
            path = core.save_config(self.cfg)
            self._log("ok", f"Saved settings to {path}")
            messagebox.showinfo(core.APP_NAME, f"Settings saved to:\n{path}")
        except OSError as exc:
            messagebox.showerror(core.APP_NAME, f"Could not save settings:\n{exc}")

    def connect(self, silent: bool = False):
        self._sync_options()
        if not self.cfg.api_url:
            self.connection.toggle(True)
            self.connection.setup(self.cfg)
            return
        self.dlcs.clear()
        self.songs.clear()
        self.song_loaded.clear()
        self._folder_cache.clear()
        self.tree.delete(*self.tree.get_children())
        self.tree_nodes.clear()
        self.node_index.clear()
        self.node_parent.clear()
        self.node_children.clear()
        self._detached.clear()
        self._status("busy", "Connecting...", self.cfg.api_url)
        if not silent:
            self._log("info", f"Connecting to {self.cfg.api_url}")
        self.manager.connect(self.cfg, deep=True)

    # -- event pump ---------------------------------------------------------
    def _drain_events(self):
        try:
            for _ in range(400):
                try:
                    event = self.events.get_nowait()
                except queue.Empty:
                    break
                self._handle_event(event)
        except Exception:  # never let the UI die on one bad event
            self._log("error", "UI error:\n" + traceback.format_exc())
        finally:
            self.root.after(self.POLL_MS, self._drain_events)

    def _handle_event(self, event: dict):
        kind = event.get("type")

        if kind == "busy":
            busy = bool(event["value"])
            self.cancel_btn.configure(state="normal" if busy else "disabled")
            self.refresh_btn.configure(state="disabled" if busy else "normal")
            return

        if kind == "connecting":
            self._status("busy", "Connecting...", event.get("url", ""))
            return

        if kind == "connected":
            info = event.get("info") or {}
            self._log("ok", f"Connected. Worker v{info.get('version', '?')}, "
                            f"scope {info.get('scope')!r}")
            self._status("busy", "Scanning bucket...", event.get("url", ""))
            return

        if kind == "connection_failed":
            self._status("err", "Connection failed", event.get("error", ""))
            self._log("error", f"Connection failed: {event.get('error')}")
            if event.get("hint"):
                self._log("dim", event["hint"])
            self.connection.toggle(True)
            return

        if kind == "scan_start":
            total = event.get("dlcs")
            self._status("busy", "Scanning bucket...",
                         f"{total} DLC(s)" if total else "")
            return

        if kind == "scan_dlc":
            self._add_dlc_node(event)
            if event.get("index") and event.get("total"):
                self._status("busy", "Scanning bucket...",
                             f"{event['index']}/{event['total']} - {event['dlc']}")
            return

        if kind == "scan_songs":
            dlc_name = event["dlc"]
            self.song_loaded[dlc_name] = True
            self.songs[dlc_name] = list(event["songs"])
            node = self.node_index.get(f"dlc:{dlc_name}")
            dlc = event.get("dlc_obj")
            if node is not None and isinstance(dlc, core.Dlc):
                folders = self._folders_for(dlc)
                self._fill_dlc(node, dlc, folders, self.songs[dlc_name])
            else:
                self._log("dim", f"{dlc_name}: {len(self.songs[dlc_name])} song(s) loaded")
            return

        if kind == "folder_children":
            self._on_folder_children(event)
            return

        if kind == "folder_failed":
            self._log("warn", f"Could not list that folder: {event.get('error')}")
            for child in self.tree.get_children(event.get("node", "")):
                if str(self.tree.item(child, "text")) == "Loading...":
                    self.tree.delete(child)
            return

        if kind == "scan_done":
            self.dlcs = list(event.get("dlcs") or [])
            warnings = event.get("warnings") or []
            for item in self.tree.get_children():
                self.tree.item(item, open=False)
            self._status("ok", f"Connected - {len(self.dlcs)} DLC(s)",
                         self.cfg.api_url)
            for warning in warnings:
                self._log("warn", warning)
            self._log("ok", f"Scan complete: {len(self.dlcs)} DLC(s)")
            self._select_first_dlc()
            return

        if kind == "size":
            size_text = (core.human_bytes(event["bytes"])
                         if event.get("bytes") is not None else "?")
            for item_id, (node_kind, payload) in self.tree_nodes.items():
                if node_kind in ("dlc", "folder") and \
                        getattr(payload, "prefix", None) == event["prefix"]:
                    self.tree.item(item_id, values=(size_text,))
            return

        if kind == "plan_start":
            self._status("busy", f"Scanning {event['label']}...", "")
            self.result_label.configure(text="")
            return

        if kind == "plan_progress":
            self._status("busy", "Scanning...", f"{event['files']} files - {event.get('detail', '')}")
            return

        if kind == "plan_done":
            self._log("dim", f"Planned {len(event['items'])} file(s): {event['label']}")
            return

        if kind == "plan_empty":
            self._status("ok", "Nothing to download", event["label"])
            self._log("warn", f"No files found for: {event['label']}. "
                              f"Check the layout in Settings (root prefix / DLC folder / Song folder).")
            messagebox.showinfo(core.APP_NAME,
                                f"Nothing to download for:\n{event['label']}\n\n"
                                "If that looks wrong, check the bucket layout in Settings "
                                "(root prefix, DLC folder, song folder).")
            return

        if kind == "file_start":
            return
        if kind == "file_done":
            item = event["item"]
            if not event.get("skipped"):
                self._log("dim", f"  saved {item.rel_path} ({core.human_bytes(event['bytes'])})")
            return
        if kind == "file_error":
            self._log("error", f"  failed {event['item'].rel_path}: {event['error']}")
            return

        if kind == "progress":
            self._render_progress(event["stats"])
            return

        if kind == "done":
            self._render_result(event["result"])
            return

        if kind == "download_failed":
            self._status("err", "Download failed", event.get("error", ""))
            self._log("error", f"Download failed: {event.get('error')}")
            messagebox.showerror(core.APP_NAME, f"Download failed:\n{event.get('error')}")
            return

        if kind == "log":
            self._log(event.get("level", "info"), event.get("msg", ""))
            return

        if kind == "search_results":
            return  # the filter box does live filtering; this is for API users

    def _select_first_dlc(self):
        """Open and select the first DLC so a fresh window isn't a blank list."""
        if self.tree.selection():
            return
        children = self.tree.get_children()
        if not children:
            return
        first = children[0]
        self.tree.item(first, open=True)
        self.tree.selection_set([first])
        self.tree.see(first)
        self._on_select()

    # -- tree ---------------------------------------------------------------
    def _folders_for(self, dlc: core.Dlc) -> List[core.FolderInfo]:
        cached = self._folder_cache.get(dlc.name)
        if cached:
            return cached
        if not self.manager.browser:
            return []
        try:
            folders = self.manager.browser.dlc_folders(dlc)
        except core.StashError as exc:
            self._log("warn", f"Could not list folders for {dlc.name}: {exc}")
            return []
        self._folder_cache[dlc.name] = folders
        return folders

    def _add_dlc_node(self, event: dict):
        dlc = event.get("dlc_obj")
        if not isinstance(dlc, core.Dlc) or dlc.name in self.node_index:
            return
        node = self._insert("", dlc.name, "dlc", dlc, tags=("dlc",))
        self.node_index[f"dlc:{dlc.name}"] = node
        folders = event.get("folders") or []
        if folders:
            self._folder_cache[dlc.name] = folders
        songs = list(event.get("songs") or [])
        self.songs[dlc.name] = songs
        self.song_loaded[dlc.name] = bool(event.get("songs_loaded"))
        self._fill_dlc(node, dlc, folders, songs)

    # -- node bookkeeping ---------------------------------------------------
    def _insert(self, parent: str, text: str, kind: str, payload, tags=(), values=()) -> str:
        """Insert a node and keep the Python-side model in step with the tree."""
        node = self.tree.insert(parent, "end", text=text, tags=tags, values=values)
        self.tree_nodes[node] = (kind, payload)
        self.node_parent[node] = parent
        self.node_children.setdefault(parent, []).append(node)
        self.node_children.setdefault(node, [])
        return node

    def _delete(self, node: str):
        for child in list(self.node_children.get(node, [])):
            self._delete(child)
        parent = self.node_parent.get(node, "")
        kids = self.node_children.get(parent)
        if kids and node in kids:
            kids.remove(node)
        self.tree_nodes.pop(node, None)
        self.node_parent.pop(node, None)
        self.node_children.pop(node, None)
        self._detached.discard(node)
        if self.tree.exists(node):
            self.tree.delete(node)

    def _clear_children(self, node: str):
        for child in list(self.node_children.get(node, [])):
            self._delete(child)

    def _fill_dlc(self, node: str, dlc: core.Dlc, folders: List[core.FolderInfo],
                  songs: List[core.Song]):
        """(Re)draw a DLC's children: Songs node + song list + asset folders."""
        self._clear_children(node)

        loaded = self.song_loaded.get(dlc.name, False)
        self.tree.item(node, values=(f"{len(songs)} songs" if loaded else "",))
        songs_node = self._insert(
            node,
            f"{self.cfg.song_dir}" if loaded else f"{self.cfg.song_dir}  (click to load)",
            "song_container" if loaded else "lazy_songs", dlc, tags=("folder",),
            values=(str(len(songs)) if loaded else "",))
        self.node_index[f"songs:{dlc.name}"] = songs_node

        if loaded:
            for song in songs:
                self._insert(songs_node, song.title, "song", song,
                             tags=() if song.has_chart_folder else ("missing",),
                             values=(song.chart_dir if song.has_chart_folder
                                     else "no chart folder",))
            if not songs:
                self._insert(songs_node, "(no songs found)", "dim", None, tags=("dim",))
        else:
            self._insert(songs_node, "Loading...", "dim", None, tags=("dim",))

        for folder in folders:
            if folder.is_song_container:
                continue
            child = self._insert(node, folder.name, "folder", folder, tags=("folder",),
                                 values=("...",))
            self._insert(child, "Loading...", "dim", None, tags=("dim",))
            self.manager.load_size(folder.prefix, f"folder:{dlc.name}:{folder.name}")

    def _on_expand(self, _event=None):
        item = self.tree.focus()
        if item not in self.tree_nodes:
            selection = self.tree.selection()
            item = selection[0] if selection else ""
        entry = self.tree_nodes.get(item)
        if not entry:
            return
        kind, payload = entry
        if kind == "lazy_songs" and isinstance(payload, core.Dlc):
            self.song_loaded[payload.name] = True
            self.tree.item(item, text=f"{self.cfg.song_dir}  (loading...)")
            self.manager.load_songs(payload)
            return
        if kind == "folder" and isinstance(payload, core.FolderInfo) and payload.prefix:
            children = self.node_children.get(item, [])
            if children and all(self.tree_nodes.get(c, ("dim", None))[0] == "dim"
                                for c in children):
                self._clear_children(item)
                self._load_subfolders(item, payload.prefix)

    def _on_folder_children(self, event: dict):
        node = event.get("node")
        if not node or not self.tree.exists(node):
            return
        self._clear_children(node)
        prefix = event["prefix"]
        for name in event.get("folders") or []:
            self._insert(node, name, "folder",
                         core.FolderInfo(name=name, prefix=f"{prefix}{name}/"),
                         tags=("folder",))
        for key, size in (event.get("files") or [])[:400]:
            name = key.rsplit("/", 1)[-1]
            self._insert(node, name, "prefix", key, tags=("dim",),
                         values=(core.human_bytes(size),))
        if not self.node_children.get(node):
            self._insert(node, "(empty)", "dim", None, tags=("dim",))

    def _load_subfolders(self, node: str, prefix: str):
        """Fetch a folder's children so the tree can be browsed deeper."""
        if not self.manager.browser:
            return
        self._insert(node, "Loading...", "dim", None, tags=("dim",))

        def job():
            try:
                folders, files = self.manager.browser.api.list_children(prefix)
                self.events.put({"type": "folder_children", "node": node, "prefix": prefix,
                                 "folders": [f.rstrip("/").rsplit("/", 1)[-1] for f in folders],
                                 "files": [(f.key, f.size) for f in files]})
            except core.StashError as exc:
                self.events.put({"type": "folder_failed", "node": node, "error": str(exc)})

        threading.Thread(target=job, daemon=True, name="children").start()


    def _on_select(self, _event=None):
        summary = self._summarise_selection()
        self.selection_label.configure(text=summary)

    def _apply_filter(self):
        needle = self.search_var.get().strip().lower()
        for node in list(self.node_children.get("", [])):
            self._filter_node(node, needle)

    def _filter_node(self, item: str, needle: str, parent_hit: bool = False) -> bool:
        """Hide or show a node, walking the Python model (Tk forgets detached nodes)."""
        if self.tree_nodes.get(item) is None:
            return False
        label = str(self.tree.item(item, "text")).lower() if self.tree.exists(item) else ""
        hit = (not needle) or parent_hit or (needle in label)
        visible_children = 0
        for child in self.node_children.get(item, []):
            if self._filter_node(child, needle, hit):
                visible_children += 1
        show = hit or visible_children > 0
        if show:
            if item in self._detached:
                parent = self.node_parent.get(item, "")
                if not self.tree.exists(parent):
                    parent = ""
                self.tree.move(item, parent, "end")
                self._detached.discard(item)
        elif item not in self._detached:
            self.tree.detach(item)
            self._detached.add(item)
        return show

    def _select_all(self):
        self.tree.selection_set(self.tree.get_children())
        self._on_select()

    # -- selection helpers --------------------------------------------------
    def _summarise_selection(self) -> str:
        selection = self._selection()
        parts = []
        for kind, payload in selection:
            if kind == "dlc":
                parts.append(f"DLC {payload.name}")
            elif kind == "song_container":
                parts.append(f"all songs in {payload.name}")
            elif kind == "folder":
                parts.append(f"folder {payload.name}")
            elif kind == "song":
                parts.append(f"song {payload.title}")
            elif kind == "prefix":
                parts.append(f"folder {payload}")
        if not parts:
            return "Nothing selected"
        text = ", ".join(parts[:6])
        if len(parts) > 6:
            text += f"  (+{len(parts) - 6} more)"
        return text

    def _selection(self) -> List[Tuple[str, object]]:
        out: List[Tuple[str, object]] = []
        for item in self.tree.selection():
            entry = self.tree_nodes.get(item)
            if entry:
                out.append(entry)
        return out

    def _selected_dlcs(self) -> List[core.Dlc]:
        dlcs: List[core.Dlc] = []
        for kind, payload in self._selection():
            if kind == "dlc" and isinstance(payload, core.Dlc):
                dlcs.append(payload)
            elif kind == "song_container" and isinstance(payload, core.Dlc):
                dlcs.append(payload)
            elif kind == "folder" and isinstance(payload, core.FolderInfo) and payload.parent:
                dlc = next((d for d in self.dlcs if d.name == payload.parent), None)
                if dlc:
                    dlcs.append(dlc)
        seen, unique = set(), []
        for dlc in dlcs:
            if dlc.name not in seen:
                seen.add(dlc.name)
                unique.append(dlc)
        return unique

    def _selected_songs(self) -> List[core.Song]:
        songs: List[core.Song] = []
        dlcs_from_songs: List[core.Dlc] = []
        for kind, payload in self._selection():
            if kind == "song":
                songs.append(payload)
                continue
            if kind == "song_container" and isinstance(payload, core.Dlc):
                # selecting the Songs node means every song in it
                dlcs_from_songs.append(payload)
            if kind == "dlc" and isinstance(payload, core.Dlc):
                dlcs_from_songs.append(payload)
        for dlc in dlcs_from_songs:
            songs.extend(self.songs.get(dlc.name, []))
        seen, unique = set(), []
        for song in songs:
            if song.id not in seen:
                seen.add(song.id)
                unique.append(song)
        return unique

    def _selected_prefixes(self) -> List[str]:
        prefixes = []
        for kind, payload in self._selection():
            if kind == "folder" and isinstance(payload, core.FolderInfo) and payload.prefix:
                prefixes.append(payload.prefix)
            elif kind == "prefix":
                prefixes.append(str(payload))
        return prefixes

    # -- menu actions -------------------------------------------------------
    def _guard(self) -> bool:
        if not self.manager.connected:
            messagebox.showwarning(core.APP_NAME, "Not connected yet.\nPress Refresh.")
            return False
        if self.manager.busy:
            messagebox.showinfo(core.APP_NAME, "Still busy with the previous task.")
            return False
        return True

    def _start(self, plan: tasks.DownloadPlan):
        if not self._guard():
            return
        self._sync_options()
        self._stats = core.DownloadStats()
        self._tick = time.time()
        try:
            self.cfg.output_dir = self.folder.get() or self.cfg.output_dir
        except Exception:
            pass
        self._log("info", f"Starting: {plan.describe()}")
        self.manager.download(plan, output_dir=self.cfg.output_dir)

    def act_download_songs(self):
        songs = self._selected_songs()
        if not songs:
            messagebox.showinfo(core.APP_NAME,
                                "Select one or more songs (or a DLC) in the list first.\n\n"
                                "Expand a DLC, then pick songs under its Songs folder.")
            return
        whole = any(not s.has_chart_folder for s in songs)
        self._start(tasks.DownloadPlan.songs_of(songs, whole_folder=whole))

    def act_download_full_song(self):
        songs = self._selected_songs()
        if not songs:
            messagebox.showinfo(core.APP_NAME, "Select songs first.")
            return
        self._start(tasks.DownloadPlan.songs_of(songs, whole_folder=True))

    def act_download_assets(self):
        dlcs = self._selected_dlcs()
        if not dlcs:
            messagebox.showinfo(core.APP_NAME, "Select one or more DLCs first.")
            return
        self._start(tasks.DownloadPlan.assets_of(dlcs))

    def act_download_dlc(self):
        dlcs = self._selected_dlcs()
        if not dlcs:
            messagebox.showinfo(core.APP_NAME, "Select one or more DLCs first.")
            return
        self._start(tasks.DownloadPlan.dlc_of(dlcs, include_songs=True))

    def act_download_folder(self):
        prefixes = self._selected_prefixes()
        if not prefixes:
            messagebox.showinfo(core.APP_NAME,
                                "Select a folder node (e.g. Gears or Other Assets) first.")
            return
        plan = tasks.DownloadPlan(kind="folder",
                                  label=", ".join(p.rstrip('/').rsplit('/', 1)[-1] for p in prefixes[:3]),
                                  prefixes=prefixes)
        self._start(plan)

    def _cancel(self):
        if self.manager.busy:
            self._log("warn", "Cancelling...")
            self.manager.cancel()

    # -- rendering ----------------------------------------------------------
    def _render_progress(self, stats: core.DownloadStats):
        now = time.time()
        if now - self._last_progress < 0.15:
            return
        self._last_progress = now
        self._stats = stats
        self.bar.configure(maximum=max(1.0, stats.bytes_total), value=stats.bytes_done)
        self.big_label.configure(text=f"Downloading {stats.files_done + stats.files_skipped}"
                                      f"/{stats.files_total} files")
        self.numbers.configure(
            text=f"{core.human_bytes(stats.bytes_done)} of {core.human_bytes(stats.bytes_total)}"
                 f"  ({stats.percent:.1f}%)   {core.human_bytes(stats.speed_bps)}/s")
        eta = core.human_duration(stats.eta_seconds) if stats.eta_seconds else "--:--"
        self.eta_label.configure(text=f"Elapsed {core.human_duration(stats.elapsed)}   "
                                      f"ETA {eta}   "
                                      f"failed {stats.files_failed}")
        if stats.current:
            shown = ", ".join(stats.current[:4])
            if len(stats.current) > 4:
                shown += f" (+{len(stats.current) - 4} more)"
            self.files_label.configure(text=f"Now: {shown}")
        self._status("busy", f"Downloading - {stats.percent:.0f}%",
                     f"{core.human_bytes(stats.speed_bps)}/s")

    def _render_result(self, result: core.DownloadResult):
        self.bar.configure(value=result.bytes_done, maximum=max(1, result.bytes_total or result.bytes_done))
        headline = "Cancelled" if result.cancelled else ("Finished with errors" if result.errors
                                                         else "Done")
        self.big_label.configure(text=headline)
        self.numbers.configure(
            text=f"{result.files_done} downloaded, {result.files_skipped} already present, "
                 f"{result.files_failed} failed - {core.human_bytes(result.bytes_done)} "
                 f"in {core.human_duration(result.elapsed)}")
        self.files_label.configure(text="")
        self.eta_label.configure(text="")
        detail = ""
        if result.errors:
            detail = f"{len(result.errors)} error(s) - first: {result.errors[0][1]}"
            for key, message in result.errors[:8]:
                self._log("error", f"  {key}: {message}")
        self.result_label.configure(text=detail)
        state = "err" if result.errors else ("busy" if result.cancelled else "ok")
        self._status(state, headline, f"saved to {self.cfg.output_dir}")
        self._log("ok" if not result.errors else "warn",
                  f"{headline}: {result.files_done} downloaded, "
                  f"{result.files_skipped} present, {result.files_failed} failed")
        self._notice_extras(result)
        if self.var_open.get() and result.bytes_done:
            self.open_folder()

    def _notice_extras(self, result: core.DownloadResult):
        """Flag song folders that look incomplete (no keysound / no charts).

        Runs on a worker thread: the output folder can hold thousands of files and
        we do not want to freeze the window walking it.
        """
        if not result.bytes_done:
            return

        def job():
            try:
                self._notice_extras_worker()
            except Exception:
                pass

        threading.Thread(target=job, daemon=True, name="notice").start()

    def _notice_extras_worker(self):
        try:
            root = Path(self.cfg.output_dir)
            chart_dirs = {core.norm_key(c) for c in self.cfg.chart_dirs}
            missing = []
            for folder in root.rglob("*"):
                if not folder.is_dir():
                    continue
                if core.norm_key(folder.name) in chart_dirs:
                    names = [f.name.lower() for f in folder.iterdir() if f.is_file()]
                    if not any(n.endswith((".ogg", ".mp3", ".wav")) for n in names):
                        missing.append((folder, "no keysound (.ogg) here"))
                    if not any(n.endswith(".pt") for n in names):
                        missing.append((folder, "no .pt charts here"))
            for folder, problem in missing[:6]:
                self.events.put({"type": "log", "level": "warn",
                                 "msg": f"  check: {folder.relative_to(root)} - {problem}"})
            if missing:
                self.events.put({"type": "log", "level": "dim",
                                 "msg": "  (normal if that song has no charts in the bucket)"})
        except Exception:
            pass

    def open_folder(self):
        target = self.cfg.output_dir
        try:
            Path(target).mkdir(parents=True, exist_ok=True)
            if os.name == "nt":
                os.startfile(target)  # type: ignore[attr-defined]
            elif platform.system() == "Darwin":
                subprocess.Popen(["open", target])
            else:
                subprocess.Popen(["xdg-open", target])
        except Exception as exc:
            messagebox.showwarning(core.APP_NAME, f"Could not open {target}:\n{exc}")


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def load_gui_config(args) -> core.Config:
    overrides = {"api_url": args.api_url, "token": args.token, "output_dir": args.output_dir,
                 "root_prefix": args.root_prefix, "dlc_dir": args.dlc_dir,
                 "song_dir": args.song_dir, "workers": args.jobs}
    cfg = core.load_config(args.config, overrides=overrides)
    if args.demo:
        # explicit intent: point at the local mock, unless the user typed one in
        if not args.api_url:
            cfg.api_url = "http://127.0.0.1:8787"
        if not args.token:
            cfg.token = "dev-token"
    return cfg


def setup_help() -> str:
    return (
        "To connect this app you need:\n\n"
        "  1. worker/worker.js deployed to Cloudflare, with an R2 binding named BUCKET\n"
        "     (see worker/README.md - it takes about two minutes)\n"
        "  2. The Worker URL, e.g. https://djmax-stash-api.you.workers.dev\n"
        "  3. An app token: the APP_TOKEN secret you set with 'wrangler secret put APP_TOKEN'\n\n"
        "Put both in the Connection bar and press 'Test & connect'."
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="djmax_stash",
                                     description="DJMAX Stash downloader (GUI)")
    parser.add_argument("--api-url", dest="api_url", help="Worker URL")
    parser.add_argument("--token", help="app token")
    parser.add_argument("--output", dest="output_dir", help="download folder")
    parser.add_argument("--root-prefix", dest="root_prefix", help='e.g. "djmax/"')
    parser.add_argument("--dlc-dir", dest="dlc_dir", help='e.g. "By_DLC"')
    parser.add_argument("--song-dir", dest="song_dir", help='e.g. "Songs"')
    parser.add_argument("-j", "--jobs", type=int, help="parallel downloads (1-16)")
    parser.add_argument("--config", help="explicit config.json path")
    parser.add_argument("--demo", action="store_true",
                        help="point at the local mock server (http://127.0.0.1:8787)")
    parser.add_argument("--no-autoconnect", action="store_true", help="start without connecting")
    parser.add_argument("--setup-help", action="store_true", help="print deployment help and exit")
    parser.add_argument("--selftest", action="store_true",
                        help="run the headless checks and exit (no window)")
    parser.add_argument("--version", action="version", version=core.APP_VERSION)
    args = parser.parse_args(argv)

    if args.setup_help:
        print(setup_help())
        return 0

    cfg = load_gui_config(args)
    if args.selftest:
        return selftest(cfg)

    if not TK_AVAILABLE:
        message = ("Tkinter is not available in this Python.\n\n"
                   "Windows/macOS: reinstall Python from python.org with the default "
                   "options (Tkinter is included).\n"
                   "Debian/Ubuntu : sudo apt install python3-tk\n"
                   "Fedora        : sudo dnf install python3-tkinter\n\n"
                   "The CLI works without it: python djmax_stash_cli.py doctor")
        print(message, file=sys.stderr)
        return 2

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"Could not open a window: {exc}", file=sys.stderr)
        print("If you are on a headless machine, use djmax_stash_cli.py instead.", file=sys.stderr)
        return 3

    try:
        # Leave Tk's DPI scaling alone (it follows the OS, so 125%/150% displays
        # get the right text size); only nudge it up if the system reports none.
        if float(root.call("tk", "scaling")) <= 1.0:
            root.call("tk", "scaling", 1.2)
    except (tk.TclError, ValueError, TypeError):
        pass
    StashApp(root, cfg, autoconnect=not args.no_autoconnect)
    root.mainloop()
    return 0


def selftest(cfg: Optional[core.Config] = None) -> int:
    """Headless checks of the pieces the GUI depends on (no window).

    Runs the same config the GUI would use, including --api-url/--token, so it
    doubles as a connection test on machines where the window won't open.
    """
    print(f"{core.APP_NAME} {core.APP_VERSION} self-test")
    failures = 0

    cfg = cfg or core.load_config(None, overrides=None)
    print(f"  config      : {cfg.source_path or '(defaults)'}")
    print(f"  output dir  : {cfg.output_dir}")
    print(f"  api url     : {cfg.api_url or '(not set)'}")

    # plan wiring: every menu action must resolve to a plan
    fake_dlc = core.Dlc(name="Arcaea", prefix="djmax/By_DLC/Arcaea/")
    fake_song = core.Song(dlc="Arcaea", title="Halcyon [222]",
                          prefix="djmax/By_DLC/Arcaea/Songs/Halcyon [222]/",
                          chart_prefix="djmax/By_DLC/Arcaea/Songs/Halcyon [222]/Chart and OGG/",
                          chart_dir="Chart and OGG")
    cases = [
        (tasks.DownloadPlan.songs_of([fake_song]), "songs"),
        (tasks.DownloadPlan.songs_of([fake_song], whole_folder=True), "song_folder"),
        (tasks.DownloadPlan.assets_of([fake_dlc]), "assets"),
        (tasks.DownloadPlan.dlc_of([fake_dlc]), "dlc"),
        (tasks.DownloadPlan.folder_of("djmax/By_DLC/Arcaea/Gears/"), "folder"),
    ]
    for plan, expected in cases:
        ok = plan.kind == expected
        failures += 0 if ok else 1
        print(f"  plan {expected:<12s}: {'ok' if ok else 'FAILED'} - {plan.describe()}")

    if cfg.api_url:
        events: "queue.Queue[dict]" = queue.Queue()
        manager = tasks.TaskManager(cfg, emit=events.put)
        manager.connect(cfg, deep=True)
        event = tasks.wait_for(events, lambda e: e["type"] in ("scan_done", "connection_failed"),
                               timeout=90)
        if event and event["type"] == "scan_done":
            print(f"  connection  : ok - {len(event['dlcs'])} DLC(s)")
        else:
            failures += 1
            print(f"  connection  : FAILED - {event.get('error') if event else 'timeout'}")
    else:
        print("  connection  : skipped (no api_url configured)")

    print("PASS" if not failures else f"{failures} FAILURE(S)")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
