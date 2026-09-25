#!/usr/bin/env python3
"""
Local stand-in for the Cloudflare Worker.

Serves a local folder as if it were the R2 bucket through the same HTTP API, so
you can run the GUI end to end without deploying anything (and so the test
suite has something to talk to).

    python tools/mock_worker.py --bucket tools/demo_bucket --token dev-token

Then start the GUI and set the API URL to http://127.0.0.1:8787 with token
"dev-token" (the GUI's Settings button, or --api-url / --token on the CLI).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Tuple

APP_VERSION = "1.0.0-mock"

mimetypes.add_type("audio/ogg", ".ogg")
mimetypes.add_type("video/mp4", ".mp4")


class FakeBucket:
    """A directory dressed up as an object store."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self._etag_cache: Dict[tuple, str] = {}

    def _abs(self, key: str) -> Path:
        return (self.root / key).resolve()

    def exists(self, key: str) -> bool:
        path = self._abs(key)
        if not str(path).startswith(str(self.root)):
            return False
        return path.is_file()

    def objects(self, prefix: str = "") -> List[Tuple[str, int]]:
        base = self._abs(prefix) if prefix else self.root
        if not str(base).startswith(str(self.root)):
            return []
        out: List[Tuple[str, int]] = []
        if not base.exists():
            return out
        for dirpath, _dirnames, filenames in os.walk(base):
            for name in filenames:
                full = Path(dirpath) / name
                key = full.relative_to(self.root).as_posix()
                try:
                    size = full.stat().st_size
                except OSError:
                    continue
                out.append((key, size))
        out.sort(key=lambda pair: pair[0])
        return out

    def etag(self, path: Path) -> str:
        """Real content MD5, exactly like R2/S3 produce for simple uploads.

        That is what lets the client verify a download instead of trusting it.
        """
        stat = path.stat()
        token = (str(path), stat.st_size, int(stat.st_mtime))
        cached = self._etag_cache.get(token)
        if cached:
            return cached
        digest = hashlib.md5()
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(256 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        value = digest.hexdigest()
        self._etag_cache[token] = value
        return value

    def list(self, prefix: str = "", delimiter: str | None = None,
             cursor: str | None = None, limit: int = 1000) -> dict:
        keys = self.objects(prefix)
        prefixes: List[str] = []
        if delimiter:
            grouped: Dict[str, List[Tuple[str, int]]] = {}
            direct: List[Tuple[str, int]] = []
            for key, size in keys:
                rest = key[len(prefix):]
                if delimiter in rest:
                    head = prefix + rest.split(delimiter, 1)[0] + delimiter
                    grouped.setdefault(head, []).append((key, size))
                else:
                    direct.append((key, size))
            prefixes = sorted(grouped)
            keys = direct

        offset = int(cursor) if cursor and cursor.isdigit() else 0
        page = keys[offset:offset + limit]
        truncated = offset + limit < len(keys)
        objects = []
        for key, size in page:
            path = self._abs(key)
            objects.append({
                "key": key,
                "size": size,
                "etag": self.etag(path),
                "uploaded": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(path.stat().st_mtime)),
            })
        return {
            "objects": objects,
            "delimitedPrefixes": prefixes,
            "truncated": truncated,
            "cursor": str(offset + limit) if truncated else None,
        }


class Handler(BaseHTTPRequestHandler):
    server_version = f"DJMAXStashMock/{APP_VERSION}"
    protocol_version = "HTTP/1.1"

    bucket: FakeBucket
    token: str
    allowed_prefix: str
    delay: float = 0.0

    # -- plumbing -----------------------------------------------------------
    def log_message(self, fmt, *args):  # quieter output
        if os.environ.get("MOCK_VERBOSE"):
            super().log_message(fmt, *args)

    def _json(self, payload: dict, status: int = 200):
        body = json.dumps(payload, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _authorised(self) -> bool:
        auth = self.headers.get("Authorization", "")
        provided = auth[7:].strip() if auth.lower().startswith("bearer ") else \
            self.headers.get("X-Stash-Token", "")
        if provided != self.token:
            self._json({"error": "Bad or missing token", "status": 401}, 401)
            return False
        return True

    def _parse(self):
        parts = urllib.parse.urlsplit(self.path)
        return parts.path.rstrip("/") or "/", dict(urllib.parse.parse_qsl(parts.query))

    # -- routes -------------------------------------------------------------
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path, query = self._parse()
        if path in ("/", "/api", "/api/ping"):
            if not self._authorised():
                return
            sample = self.bucket.objects(self.allowed_prefix)[:1]
            return self._json({
                "ok": True, "app": "djmax-stash-worker(mock)", "version": APP_VERSION,
                "bucket_ok": True, "scope": self.allowed_prefix,
                "allow_delete": False, "sample_keys": [k for k, _ in sample],
            })
        if not self._authorised():
            return

        if path == "/api/list":
            return self._list(query)
        if path == "/api/stats":
            return self._stats(query)
        if path == "/api/search":
            return self._search(query)
        if path == "/api/file":
            return self._file(query)
        self._json({"error": f"Unknown route {path}"}, 404)

    def _scope_ok(self, key: str) -> bool:
        """Mirror the Worker: the allowed prefix and any ancestor of it are listable."""
        if ".." in key:
            return False
        return (self.allowed_prefix.startswith(key) or key.startswith(self.allowed_prefix))

    def _list(self, query):
        prefix = query.get("prefix", self.allowed_prefix)
        if not self._scope_ok(prefix):
            return self._json({"error": "Prefix outside token scope", "status": 403}, 403)
        data = self.bucket.list(prefix, query.get("delimiter"),
                                query.get("cursor"), int(query.get("limit", 1000) or 1000))
        self._json({
            "prefix": prefix,
            "delimiter": query.get("delimiter"),
            "keys": data["objects"],
            "prefixes": data["delimitedPrefixes"],
            "cursor": data["cursor"],
            "truncated": data["truncated"],
        })

    def _stats(self, query):
        prefix = query.get("prefix", self.allowed_prefix)
        if not self._scope_ok(prefix):
            return self._json({"error": "Prefix outside token scope", "status": 403}, 403)
        objects = self.bucket.objects(prefix)
        self._json({"prefix": prefix, "files": len(objects),
                    "bytes": sum(size for _k, size in objects),
                    "scanned": len(objects), "capped": False})

    def _search(self, query):
        needle = (query.get("q") or "").strip().lower()
        if len(needle) < 2:
            return self._json({"error": "Query must be at least 2 characters", "status": 400}, 400)
        results = []
        for key, size in self.bucket.objects(self.allowed_prefix):
            if needle in key.lower():
                results.append({"path": key, "name": key.rsplit("/", 1)[-1], "level": 0, "size": size})
        self._json({"query": needle, "scope": self.allowed_prefix, "results": results[:500]})

    def _file(self, query):
        key = query.get("key", "")
        if not self._scope_ok(key):
            return self._json({"error": "Object key outside token scope", "status": 403}, 403)
        path = self.bucket._abs(key)
        if not path.is_file():
            return self._json({"error": f"No such object: {key}", "status": 404}, 404)

        size = path.stat().st_size
        start, end = 0, max(0, size - 1)
        status = 200
        range_header = self.headers.get("Range")
        if range_header:
            match = re.match(r"bytes=(\d*)-(\d*)$", range_header.strip())
            if match and (match.group(1) or match.group(2)):
                if match.group(1):
                    start = int(match.group(1))
                    end = int(match.group(2)) if match.group(2) else size - 1
                else:  # suffix range
                    start = max(0, size - int(match.group(2)))
                if start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                end = min(end, size - 1)
                status = 206

        length = end - start + 1 if size else 0
        if self.delay:
            time.sleep(self.delay)

        self.send_response(status)
        ctype = mimetypes.guess_type(key)[0] or "application/octet-stream"
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", '"%s"' % self.bucket.etag(path))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD" or not length:
            return
        with open(path, "rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                remaining -= len(chunk)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Mock DJMAX Stash Worker")
    parser.add_argument("--bucket", default="tools/demo_bucket",
                        help="folder to serve as the bucket (default: tools/demo_bucket)")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--token", default="dev-token")
    parser.add_argument("--prefix", default="djmax/")
    parser.add_argument("--delay", type=float, default=0.0,
                        help="artificial per-request delay, to make progress bars visible")
    args = parser.parse_args(argv)

    Handler.bucket = FakeBucket(Path(args.bucket))
    Handler.token = args.token
    Handler.allowed_prefix = args.prefix
    Handler.delay = args.delay

    if not Handler.bucket.root.exists():
        print(f"! bucket folder {Handler.bucket.root} does not exist - creating it")
        Handler.bucket.root.mkdir(parents=True, exist_ok=True)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    print(f"Mock Worker on http://{args.host}:{args.port}")
    print(f"  bucket  : {Handler.bucket.root}")
    print(f"  token   : {args.token}")
    print(f"  scope   : {args.prefix}")
    print("Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
