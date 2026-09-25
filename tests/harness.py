"""
Shared test scaffolding: a local Worker stand-in plus a generated demo bucket.
"""

from __future__ import annotations

import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import make_demo_bucket  # noqa: E402
import mock_worker  # noqa: E402


class MockWorker:
    """Context manager that serves *bucket_dir* over the Worker API."""

    def __init__(self, bucket_dir: Path, token: str = "test-token", prefix: str = "djmax/",
                 delay: float = 0.0):
        self.bucket_dir = Path(bucket_dir)
        self.token = token
        self.prefix = prefix
        self.delay = delay
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.port = 0

    def start(self) -> "MockWorker":
        mock_worker.Handler.bucket = mock_worker.FakeBucket(self.bucket_dir)
        mock_worker.Handler.token = self.token
        mock_worker.Handler.allowed_prefix = self.prefix
        mock_worker.Handler.delay = self.delay
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), mock_worker.Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        time.sleep(0.15)
        return self

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()


def build_demo_bucket(root: Path, **kw) -> Path:
    make_demo_bucket.build(root, **kw)
    return root


def file_tree(root: Path) -> list:
    """Sorted relative paths (posix style) of every file under root."""
    root = Path(root)
    if not root.exists():
        return []
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())
