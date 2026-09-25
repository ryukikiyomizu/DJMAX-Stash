"""
DJMAX Stash -- headless core.

Everything that touches the network, the bucket layout or the filesystem lives
here so it can be tested (and used from a CLI) without a GUI.

The intended deployment is:

    [ Tkinter GUI ]  --HTTPS-->  [ Cloudflare Worker ]  --R2 binding-->  [ R2 bucket ]
                                        ^ holds the secrets

The client never sees R2 keys.  It only knows a Worker URL + an app token that
can do exactly two things: list objects under ALLOWED_PREFIX and download them.

Standard library only -- no pip installs required.
"""

from __future__ import annotations

import base64
import errno
import hashlib
import http.client
import json
import os
import queue
import random
import re
import socket
import ssl
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

APP_NAME = "DJMAX Stash"
APP_VERSION = "1.0.0"
USER_AGENT = f"DJMAX-Stash/{APP_VERSION} (+https://github.com/ryukikiyomizu/DJMAX-Stash)"

CHUNK_SIZE = 256 * 1024
PROGRESS_INTERVAL = 0.25  # seconds between "progress" events

# Folders that may hold a song's chart + keysounds + MV.  Matching is done on a
# normalised form ("Chart and OGG" == "Chart & OGG" == "Chart_and_OGG").
DEFAULT_CHART_DIRS: Tuple[str, ...] = ("Chart and OGG", "Chart & OGG", "Chart+OGG", "Chart_OGG")
DEFAULT_SONG_DIR = "Songs"
DEFAULT_DLC_DIR = "By_DLC"
DEFAULT_ROOT = "djmax/"

# Characters Windows forbids in file names.  macOS/Linux only forbid "/" but we
# sanitise uniformly so a stash downloaded on Windows and copied to Linux (or
# vice-versa) keeps the same shape.
_ILLEGAL_PATH_CHARS = '<>:"/\\|?*'
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------


def norm_key(text: str) -> str:
    """Normalise a folder name for fuzzy comparison.

    'Chart and OGG' -> 'chartogg', and so are 'Chart & OGG', 'Chart_OGG' and
    'Chart+OGG'.  The filler word "and" is dropped on purpose so both phrasings
    of a chart folder hash to the same thing, without breaking titles that
    merely contain the letters (e.g. 'Candy' stays 'candy').
    """
    tokens = [t for t in re.split(r"[^a-z0-9]+", (text or "").lower()) if t]
    return "".join(t for t in tokens if t != "and")


def natural_key(text: str):
    """Sort key that puts 'Song 2' before 'Song 10'."""
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", text or "")]


def human_bytes(num: float) -> str:
    if num is None:
        return "?"
    num = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(num)} {unit}"
            return f"{num:.1f} {unit}"
        num /= 1024.0
    return f"{num:.1f} TB"


def human_duration(seconds: Optional[float]) -> str:
    if seconds is None or seconds != seconds or seconds < 0:
        return "--:--"
    seconds = int(seconds)
    if seconds >= 3600:
        return f"{seconds // 3600}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 60}:{seconds % 60:02d}"


def safe_name(name: str, fallback: str = "_") -> str:
    """Make a single path segment safe on Windows/macOS/Linux."""
    name = (name or "").strip()
    out = []
    for ch in name:
        if ch in _ILLEGAL_PATH_CHARS or ord(ch) < 32:
            out.append("_")
        else:
            out.append(ch)
    cleaned = "".join(out).strip().rstrip(". ")
    if not cleaned:
        cleaned = fallback
    stem = cleaned.split(".")[0].upper()
    if stem in _RESERVED_NAMES:
        cleaned = "_" + cleaned
    # Windows also dislikes very long segments
    if len(cleaned) > 120:
        stem, dot, ext = cleaned.rpartition(".")
        if dot and len(ext) <= 8:
            cleaned = stem[: 120 - len(ext) - 1] + "." + ext
        else:
            cleaned = cleaned[:120]
    return cleaned


def safe_rel_path(key: str) -> str:
    """Convert a bucket key into a relative local path, sanitising each segment."""
    parts = [p for p in (key or "").split("/") if p not in ("", ".", "..")]
    return os.path.join(*[safe_name(p) for p in parts]) if parts else ""


def long_path(path: Path) -> str:
    """On Windows prefix long paths with \\\\?\\ so >260 char paths still work."""
    text = os.path.abspath(str(path))
    if os.name == "nt" and len(text) > 240 and not text.startswith("\\\\?\\"):
        if text.startswith("\\\\"):
            return "\\\\?\\UNC\\" + text[2:]
        return "\\\\?\\" + text
    return text


def _xor(data: bytes, key: bytes) -> bytes:
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))


_OBF_SALT = b"DJMAX-Stash/v1"


def obfuscate(secret: str) -> str:
    """Very light obfuscation so a shipped config.json isn't plain-text readable.

    This is NOT encryption -- anyone determined can reverse it.  It exists so a
    casual user can't just open config.json and grab the token; the real
    protection is that the token only ever grants read access to one prefix.
    """
    if not secret:
        return ""
    raw = _xor(secret.encode("utf-8"), _OBF_SALT)
    return "obf:" + base64.urlsafe_b64encode(raw).decode("ascii")


def deobfuscate(secret: str) -> str:
    if not secret:
        return ""
    if not secret.startswith("obf:"):
        return secret
    try:
        raw = base64.urlsafe_b64decode(secret[4:].encode("ascii"))
        return _xor(raw, _OBF_SALT).decode("utf-8")
    except Exception:
        return secret


def is_obfuscated(secret: str) -> bool:
    return bool(secret) and secret.startswith("obf:")


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------


class StashError(Exception):
    """Base class for anything this module raises."""


class AuthError(StashError):
    """Token missing / wrong / revoked (HTTP 401 or 403)."""


class NotFoundError(StashError):
    """Object or prefix does not exist (HTTP 404)."""


class NetworkError(StashError):
    """Could not reach the server, or the connection dropped repeatedly."""


class CancelledError(StashError):
    """The user cancelled the operation."""


# --------------------------------------------------------------------------
# HTTP session (thread-safe, keep-alive aware)
# --------------------------------------------------------------------------


class HttpSession:
    """Tiny HTTP(S) client with one reused connection per thread.

    urllib opens a fresh TLS connection per request which is painfully slow when
    pulling hundreds of small .pt/.ogg files, so we keep a connection alive per
    worker thread and transparently reconnect when the server closes it.
    """

    def __init__(self, base_url: str, token: str = "", timeout: float = 45.0,
                 retries: int = 3, user_agent: str = USER_AGENT):
        self.base_url = (base_url or "").rstrip("/")
        if not self.base_url:
            raise StashError("No API URL configured")
        parts = urllib.parse.urlsplit(self.base_url)
        if parts.scheme not in ("http", "https"):
            raise StashError(f"API URL must start with http:// or https:// (got {base_url!r})")
        self.scheme = parts.scheme
        self.host = parts.hostname or ""
        self.port = parts.port or (443 if parts.scheme == "https" else 80)
        self.base_path = parts.path.rstrip("/")
        self.token = token or ""
        self.timeout = timeout
        self.retries = max(0, int(retries))
        self.user_agent = user_agent
        self._local = threading.local()
        self._ssl_ctx = ssl.create_default_context()

    # -- connection handling ------------------------------------------------
    def _new_conn(self):
        if self.scheme == "https":
            return http.client.HTTPSConnection(self.host, self.port, timeout=self.timeout,
                                               context=self._ssl_ctx)
        return http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)

    def _conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._new_conn()
            self._local.conn = conn
        return conn

    def close(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self._local.conn = None
        for parked in getattr(self._local, "extra_conns", []) or []:
            try:
                parked.close()
            except Exception:
                pass
        self._local.extra_conns = []

    def _drop_conn(self):
        try:
            self.close()
        except Exception:
            pass

    def build_url(self, path: str, params: Optional[Dict[str, object]] = None) -> str:
        if not path.startswith("/"):
            path = "/" + path
        url = f"{self.scheme}://{self.host}"
        if (self.scheme == "https" and self.port != 443) or (self.scheme == "http" and self.port != 80):
            url += f":{self.port}"
        url += self.base_path + path
        if params:
            clean = {k: v for k, v in params.items() if v not in (None, "")}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)
        return url

    def _request_path(self, path: str, params: Optional[Dict[str, object]] = None) -> str:
        if not path.startswith("/"):
            path = "/" + path
        url_path = self.base_path + path
        if params:
            clean = {k: v for k, v in params.items() if v not in (None, "")}
            if clean:
                url_path += "?" + urllib.parse.urlencode(clean)
        return url_path or "/"

    # -- request ------------------------------------------------------------
    def request(self, method: str, path: str, params: Optional[Dict[str, object]] = None,
                headers: Optional[Dict[str, str]] = None, _redirects: int = 0):
        """Return (status, headers, response). Caller must close the response."""
        url_path = self._request_path(path, params)
        req_headers = {
            "User-Agent": self.user_agent,
            "Accept": "*/*",
            "Host": self.host if self.port in (80, 443) else f"{self.host}:{self.port}",
        }
        if self.token:
            req_headers["Authorization"] = f"Bearer {self.token}"
            req_headers["X-Stash-Token"] = self.token
        if headers:
            req_headers.update(headers)

        last_exc: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            conn = self._conn()
            try:
                conn.request(method, url_path, headers=req_headers)
                resp = conn.getresponse()
            except (http.client.HTTPException, OSError, socket.error, ssl.SSLError) as exc:
                self._drop_conn()
                last_exc = exc
                if attempt >= self.retries:
                    break
                time.sleep(min(8.0, 0.5 * (2 ** attempt)) + random.random() * 0.25)
                continue

            status = resp.status
            if status in (301, 302, 303, 307, 308) and _redirects < 4:
                location = resp.getheader("Location")
                resp.read()
                if not location:
                    return status, resp.headers, resp
                parts = urllib.parse.urlsplit(urllib.parse.urljoin(self.base_url + "/", location))
                if parts.hostname and parts.hostname != self.host:
                    # Follow cross-host redirects with a throwaway connection.
                    return _follow_foreign_redirect(self, method, parts, _redirects, req_headers)
                new_path = parts.path
                if self.base_path and new_path.startswith(self.base_path):
                    new_path = new_path[len(self.base_path):]
                return self.request(method, new_path,
                                    params=dict(urllib.parse.parse_qsl(parts.query)),
                                    headers=None, _redirects=_redirects + 1)

            if status in (429, 500, 502, 503, 504) and attempt < self.retries:
                retry_after = resp.getheader("Retry-After")
                resp.read()
                self._drop_conn()
                delay = min(15.0, 0.75 * (2 ** attempt)) + random.random() * 0.4
                if retry_after:
                    try:
                        delay = max(delay, min(30.0, float(retry_after)))
                    except ValueError:
                        pass
                time.sleep(delay)
                continue

            return status, resp.headers, resp

        raise NetworkError(
            f"Could not reach {self.host}: {last_exc}"
            if last_exc else f"Request failed after {self.retries + 1} attempts"
        )

    def get_json(self, path: str, params: Optional[Dict[str, object]] = None,
                 timeout_note: str = "") -> dict:
        status, headers, resp = self.request("GET", path, params=params)
        try:
            body = resp.read()
        finally:
            resp.close()
        if status in (401, 403):
            raise AuthError(_auth_message(status, body))
        if status == 404:
            raise NotFoundError(f"Not found: {path} {params or ''}")
        if status != 200:
            raise StashError(f"HTTP {status} from {path}: {_brief(body)}")
        try:
            return json.loads(body.decode("utf-8"))
        except Exception as exc:
            raise StashError(f"Bad JSON from {path}: {_brief(body)}") from exc

    def open(self, path: str, params: Optional[Dict[str, object]] = None,
             headers: Optional[Dict[str, str]] = None):
        """Open a streaming GET. Returns (status, headers, response)."""
        return self.request("GET", path, params=params, headers=headers)

    def head(self, path: str, params: Optional[Dict[str, object]] = None):
        return self.request("HEAD", path, params=params)


def _follow_foreign_redirect(session: HttpSession, method: str, parts,
                             redirects: int, req_headers: Dict[str, str]):
    """Follow a redirect that leaves our Worker (e.g. to a custom CDN domain).

    The token is deliberately NOT forwarded to the foreign host.  The throwaway
    connection is parked on the session so it stays alive while the caller
    streams the body, and gets closed with the session.
    """
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    if parts.scheme == "https":
        conn = http.client.HTTPSConnection(host, port, timeout=session.timeout,
                                           context=session._ssl_ctx)
    else:
        conn = http.client.HTTPConnection(host, port, timeout=session.timeout)
    path = parts.path + (("?" + parts.query) if parts.query else "")
    headers = {
        "User-Agent": session.user_agent,
        "Accept": "*/*",
        "Host": host if port in (80, 443) else f"{host}:{port}",
    }
    if "Range" in req_headers:
        headers["Range"] = req_headers["Range"]
    try:
        conn.request(method, path or "/", headers=headers)
        resp = conn.getresponse()
    except Exception as exc:
        try:
            conn.close()
        except Exception:
            pass
        raise NetworkError(f"Redirect to {host} failed: {exc}") from exc
    parked = getattr(session._local, "extra_conns", None)
    if parked is None:
        parked = []
        session._local.extra_conns = parked
    parked.append(conn)
    return resp.status, resp.headers, resp


def _auth_message(status: int, body: bytes) -> str:
    if status == 403:
        return ("Access denied (403). The app token is wrong, revoked, or this build is "
                "not allowed to reach that prefix.")
    return ("Not authorised (401). Check the access token in Settings "
            "(a copy-paste mistake is the usual cause).")


def _brief(body: bytes, limit: int = 200) -> str:
    try:
        text = body.decode("utf-8", "replace")
    except Exception:
        text = repr(body)
    text = " ".join(text.split())
    return text[:limit] + ("..." if len(text) > limit else "")


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------


@dataclass
class Config:
    api_url: str = ""
    token: str = ""

    root_prefix: str = DEFAULT_ROOT
    dlc_dir: str = DEFAULT_DLC_DIR
    song_dir: str = DEFAULT_SONG_DIR
    chart_dirs: List[str] = field(default_factory=lambda: list(DEFAULT_CHART_DIRS))

    output_dir: str = ""
    workers: int = 4
    timeout: float = 45.0
    retries: int = 3
    verify_md5: bool = True
    resume: bool = True
    make_zip: bool = False
    open_after: bool = False

    # bookkeeping
    source_path: str = ""          # where the values came from (for the GUI)
    save_path: str = ""            # where the GUI writes them back

    def normalised(self) -> "Config":
        cfg = Config(**{k: v for k, v in self.__dict__.items()})
        cfg.api_url = (cfg.api_url or "").strip().rstrip("/")
        cfg.root_prefix = normalise_prefix(cfg.root_prefix)
        cfg.dlc_dir = (cfg.dlc_dir or "").strip().strip("/")
        cfg.song_dir = (cfg.song_dir or DEFAULT_SONG_DIR).strip().strip("/")
        cfg.chart_dirs = [c.strip().strip("/") for c in (cfg.chart_dirs or []) if c and c.strip()]
        if not cfg.chart_dirs:
            cfg.chart_dirs = list(DEFAULT_CHART_DIRS)
        cfg.workers = max(1, min(16, int(cfg.workers or 4)))
        cfg.timeout = max(5.0, float(cfg.timeout or 45.0))
        cfg.retries = max(0, min(10, int(cfg.retries or 3)))
        return cfg

    def to_dict(self, obfuscate_token: bool = False) -> dict:
        data = {
            "api_url": self.api_url,
            "token": obfuscate(self.token) if obfuscate_token else self.token,
            "root_prefix": self.root_prefix,
            "dlc_dir": self.dlc_dir,
            "song_dir": self.song_dir,
            "chart_dirs": list(self.chart_dirs),
            "output_dir": self.output_dir,
            "workers": self.workers,
            "timeout": self.timeout,
            "retries": self.retries,
            "verify_md5": self.verify_md5,
            "resume": self.resume,
            "make_zip": self.make_zip,
        }
        return data


def normalise_prefix(prefix: str) -> str:
    prefix = (prefix or "").strip().lstrip("/")
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    return prefix


def config_paths() -> List[str]:
    """Config files that are merged, lowest priority first."""
    paths: List[str] = []
    here = app_dir()
    paths.append(str(here / "config.json"))
    paths.append(str(here / "stash_config.json"))
    home = Path.home() / ".djmax_stash" / "config.json"
    paths.append(str(home))
    return paths


def app_dir() -> Path:
    """Folder holding the script (or the .exe when frozen by PyInstaller)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def user_config_path() -> Path:
    return Path.home() / ".djmax_stash" / "config.json"


_ENV_MAP = {
    "api_url": "DJMAX_STASH_API_URL",
    "token": "DJMAX_STASH_TOKEN",
    "root_prefix": "DJMAX_STASH_ROOT_PREFIX",
    "dlc_dir": "DJMAX_STASH_DLC_DIR",
    "song_dir": "DJMAX_STASH_SONG_DIR",
    "output_dir": "DJMAX_STASH_OUTPUT_DIR",
    "workers": "DJMAX_STASH_WORKERS",
}


def load_config(explicit_path: Optional[str] = None,
                overrides: Optional[dict] = None,
                include_user: bool = True) -> Config:
    """Merge shipped config -> user config -> env vars -> explicit overrides."""
    merged: dict = {}
    sources: List[str] = []

    candidates = [explicit_path] if explicit_path else config_paths()
    if not include_user:
        candidates = [c for c in candidates if str(user_config_path()) != str(c)]
    if explicit_path and os.path.exists(explicit_path):
        candidates = [explicit_path]

    for path in candidates:
        if not path or not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                merged.update({k: v for k, v in data.items() if v is not None})
                sources.append(str(path))
        except Exception:
            # A broken config should never stop the app from starting.
            merged.setdefault("_warnings", [])
            merged["_warnings"].append(f"Could not read {path}")

    # If the caller pointed at a specific file, don't silently fall back.
    if explicit_path:
        merged = {}
        if os.path.exists(explicit_path):
            with open(explicit_path, "r", encoding="utf-8") as fh:
                merged = json.load(fh)

    env = os.environ
    for key, var in _ENV_MAP.items():
        if env.get(var):
            value = env[var]
            if key == "workers":
                try:
                    value = int(value)
                except ValueError:
                    continue
            merged[key] = value
            if var not in sources:
                sources.append(f"env:{var}")

    if overrides:
        merged.update({k: v for k, v in overrides.items() if v is not None})

    known = {f for f in Config.__dataclass_fields__}
    clean = {k: v for k, v in merged.items() if k in known}
    cfg = Config(**clean).normalised()
    cfg.token = deobfuscate(str(cfg.token or ""))
    cfg.source_path = sources[-1] if sources else ""
    cfg.save_path = str(Path(explicit_path) if explicit_path else user_config_path())
    if not cfg.output_dir:
        cfg.output_dir = str(Path.home() / "DJMAX Stash")
    return cfg


def save_config(cfg: Config, path: Optional[str] = None, obfuscate_token: bool = True) -> str:
    target = Path(path or cfg.save_path or user_config_path())
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = cfg.to_dict(obfuscate_token=obfuscate_token)
    tmp = target.with_suffix(target.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, target)
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    cfg.save_path = str(target)
    return str(target)


# --------------------------------------------------------------------------
# bucket model
# --------------------------------------------------------------------------


@dataclass
class RemoteObject:
    key: str
    size: int = 0
    etag: str = ""
    uploaded: str = ""


@dataclass
class Dlc:
    name: str
    prefix: str

    @property
    def id(self) -> str:
        return self.name


@dataclass
class Song:
    dlc: str
    title: str
    prefix: str
    chart_prefix: Optional[str] = None
    chart_dir: Optional[str] = None
    subfolders: List[str] = field(default_factory=list)
    _size: Optional[int] = field(default=None, repr=False)

    @property
    def has_chart_folder(self) -> bool:
        return self.chart_prefix is not None

    @property
    def id(self) -> str:
        return f"{self.dlc}/{self.title}"


@dataclass
class FolderInfo:
    name: str
    prefix: str
    parent: str = ""
    is_song_container: bool = False


@dataclass
class DownloadItem:
    key: str
    rel_path: str
    size: int = 0
    etag: str = ""

    @property
    def name(self) -> str:
        return self.rel_path.replace("\\", "/").rsplit("/", 1)[-1]


@dataclass
class DownloadResult:
    files_total: int = 0
    files_done: int = 0
    files_skipped: int = 0
    files_failed: int = 0
    bytes_total: int = 0
    bytes_done: int = 0
    cancelled: bool = False
    errors: List[Tuple[str, str]] = field(default_factory=list)
    elapsed: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.errors and not self.cancelled


@dataclass
class DownloadStats:
    files_total: int = 0
    files_done: int = 0
    files_skipped: int = 0
    files_failed: int = 0
    bytes_total: int = 0
    bytes_done: int = 0
    speed_bps: float = 0.0
    eta_seconds: Optional[float] = None
    current: Tuple[str, ...] = ()
    elapsed: float = 0.0

    @property
    def percent(self) -> float:
        if self.bytes_total <= 0:
            return 0.0
        return min(100.0, 100.0 * self.bytes_done / self.bytes_total)

    @property
    def file_percent(self) -> float:
        if self.files_total <= 0:
            return 0.0
        return min(100.0, 100.0 * (self.files_done + self.files_skipped) / self.files_total)


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------


class StashAPI:
    """Thin wrapper over the Worker API."""

    def __init__(self, config: Config):
        self.config = config
        self.session = HttpSession(config.api_url, config.token, timeout=config.timeout,
                                   retries=config.retries)

    # -- endpoints ----------------------------------------------------------
    def ping(self) -> dict:
        return self.session.get_json("/api/ping")

    def list_prefix(self, prefix: str = "", delimiter: Optional[str] = "/",
                    cursor: Optional[str] = None, limit: int = 1000) -> dict:
        data = self.session.get_json("/api/list", {
            "prefix": prefix,
            "delimiter": delimiter,
            "cursor": cursor,
            "limit": limit,
        })
        return data

    def list_objects(self, prefix: str, on_page: Optional[Callable[[int], None]] = None) -> List[RemoteObject]:
        """Every object under *prefix* (recursive)."""
        out: List[RemoteObject] = []
        cursor = None
        pages = 0
        while True:
            data = self.list_prefix(prefix, delimiter=None, cursor=cursor)
            for obj in data.get("keys", []) or []:
                out.append(RemoteObject(str(obj.get("key", "")), int(obj.get("size") or 0),
                                        str(obj.get("etag") or ""), str(obj.get("uploaded") or "")))
            cursor = data.get("cursor")
            pages += 1
            if on_page:
                on_page(len(out))
            if not data.get("truncated") or not cursor:
                break
            if pages > 250:  # ~250k objects; stop rather than loop forever
                break
        return out

    def list_folders(self, prefix: str, on_page: Optional[Callable[[int], None]] = None) -> List[str]:
        """Immediate sub-folders of *prefix*."""
        out: List[str] = []
        cursor = None
        pages = 0
        while True:
            data = self.list_prefix(prefix, delimiter="/", cursor=cursor)
            for name in data.get("prefixes", []) or []:
                out.append(str(name))
            cursor = data.get("cursor")
            pages += 1
            if on_page:
                on_page(len(out))
            if not data.get("truncated") or not cursor:
                break
            if pages > 60:
                break
        return out

    def list_children(self, prefix: str) -> Tuple[List[str], List[RemoteObject]]:
        """(sub-folders, files) directly under prefix - one paged loop."""
        folders: List[str] = []
        files: List[RemoteObject] = []
        cursor = None
        pages = 0
        while True:
            data = self.list_prefix(prefix, delimiter="/", cursor=cursor)
            for name in data.get("prefixes", []) or []:
                folders.append(str(name))
            for obj in data.get("keys", []) or []:
                files.append(RemoteObject(str(obj.get("key", "")), int(obj.get("size") or 0),
                                          str(obj.get("etag") or ""), str(obj.get("uploaded") or "")))
            cursor = data.get("cursor")
            pages += 1
            if not data.get("truncated") or not cursor:
                break
            if pages > 60:
                break
        return folders, files

    def stat_prefix(self, prefix: str) -> dict:
        return self.session.get_json("/api/stats", {"prefix": prefix})

    def open_object(self, key: str, offset: int = 0, extra_headers: Optional[dict] = None):
        """Stream an object. Returns (status, headers, response)."""
        headers = dict(extra_headers or {})
        if offset:
            headers["Range"] = f"bytes={offset}-"
        return self.session.open("/api/file", params={"key": key}, headers=headers)


# --------------------------------------------------------------------------
# layout / planning
# --------------------------------------------------------------------------


def dlc_root_prefix(cfg: Config) -> str:
    base = cfg.root_prefix
    if cfg.dlc_dir:
        return f"{base}{cfg.dlc_dir.strip('/')}/"
    return base


class StashBrowser:
    """High-level view of the bucket: DLCs -> folders / songs -> files."""

    def __init__(self, api: StashAPI):
        self.api = api
        self.cfg = api.config
        self._dlc_cache: Optional[List[Dlc]] = None
        self._song_cache: Dict[str, List[Song]] = {}
        self._size_cache: Dict[str, int] = {}
        self._lock = threading.Lock()

    # -- DLCs ---------------------------------------------------------------
    def dlcs(self, force: bool = False) -> List[Dlc]:
        with self._lock:
            if self._dlc_cache is not None and not force:
                return list(self._dlc_cache)
        prefixes = self.api.list_folders(dlc_root_prefix(self.cfg))
        dlcs = []
        for prefix in prefixes:
            name = prefix.rstrip("/").rsplit("/", 1)[-1]
            dlcs.append(Dlc(name=name, prefix=prefix))
        dlcs.sort(key=lambda d: natural_key(d.name))
        with self._lock:
            self._dlc_cache = dlcs
        return list(dlcs)

    # -- folders inside a DLC ----------------------------------------------
    def dlc_folders(self, dlc: Dlc) -> List[FolderInfo]:
        prefixes = self.api.list_folders(dlc.prefix)
        song_norm = norm_key(self.cfg.song_dir)
        out = []
        for prefix in prefixes:
            name = prefix.rstrip("/").rsplit("/", 1)[-1]
            out.append(FolderInfo(name=name, prefix=prefix, parent=dlc.name,
                                  is_song_container=(norm_key(name) == song_norm)))
        out.sort(key=lambda f: (not f.is_song_container, natural_key(f.name)))
        return out

    # -- songs --------------------------------------------------------------
    def songs(self, dlc: Dlc, force: bool = False) -> List[Song]:
        with self._lock:
            if dlc.name in self._song_cache and not force:
                return list(self._song_cache[dlc.name])
        songs = self._scan_songs(dlc)
        with self._lock:
            self._song_cache[dlc.name] = songs
        return list(songs)

    def _song_container(self, dlc: Dlc) -> Optional[str]:
        folders = self.api.list_folders(dlc.prefix)
        target = norm_key(self.cfg.song_dir)
        for prefix in folders:
            name = prefix.rstrip("/").rsplit("/", 1)[-1]
            if norm_key(name) == target:
                return prefix
        return None

    def _scan_songs(self, dlc: Dlc) -> List[Song]:
        container = self._song_container(dlc)
        if not container:
            return []
        chart_norms = {norm_key(c): c for c in self.cfg.chart_dirs}
        songs: List[Song] = []
        for prefix in self.api.list_folders(container):
            title = prefix.rstrip("/").rsplit("/", 1)[-1]
            sub_folders, _files = self.api.list_children(prefix)
            sub_names = [p.rstrip("/").rsplit("/", 1)[-1] for p in sub_folders]
            chart_prefix = None
            chart_dir = None
            for name, full in zip(sub_names, sub_folders):
                match = chart_norms.get(norm_key(name))
                if match:
                    chart_prefix = full
                    chart_dir = name
                    break
            songs.append(Song(dlc=dlc.name, title=title, prefix=prefix,
                              chart_prefix=chart_prefix, chart_dir=chart_dir,
                              subfolders=sub_names))
        songs.sort(key=lambda s: natural_key(s.title))
        return songs

    # -- sizes --------------------------------------------------------------
    def prefix_size(self, prefix: str, use_server: bool = True) -> int:
        with self._lock:
            if prefix in self._size_cache:
                return self._size_cache[prefix]
        total = 0
        if use_server:
            try:
                data = self.api.stat_prefix(prefix)
                total = int(data.get("bytes") or 0)
                with self._lock:
                    self._size_cache[prefix] = total
                return total
            except NotFoundError:
                pass
            except StashError:
                pass
        objs = self.api.list_objects(prefix)
        total = sum(o.size for o in objs)
        with self._lock:
            self._size_cache[prefix] = total
        return total

    def clear_cache(self):
        with self._lock:
            self._dlc_cache = None
            self._song_cache.clear()
            self._size_cache.clear()

    # -- planning -----------------------------------------------------------
    def plan_songs(self, songs: Sequence[Song], whole_song_folder: bool = False,
                   on_progress: Optional[Callable[[int, str], None]] = None) -> List[DownloadItem]:
        """One item per file. 'Download Songs' grabs <song>/<Chart and OGG>/**."""
        items: List[DownloadItem] = []
        seen = set()
        for song in songs:
            if whole_song_folder or not song.chart_prefix:
                prefix = song.prefix
            else:
                prefix = song.chart_prefix
            objs = self.api.list_objects(prefix)
            for obj in objs:
                if not obj.key.endswith("/") and obj.key not in seen:
                    seen.add(obj.key)
                    items.append(DownloadItem(key=obj.key, rel_path=self.local_rel(obj.key),
                                              size=obj.size, etag=obj.etag))
            if on_progress:
                on_progress(len(items), f"{song.title} ({len(objs)} files)")
        return items

    def plan_dlc(self, dlcs: Sequence[Dlc], include_songs: bool = True,
                 on_progress: Optional[Callable[[int, str], None]] = None) -> List[DownloadItem]:
        """Everything under each DLC, optionally skipping the Songs/ container."""
        items: List[DownloadItem] = []
        seen = set()
        song_norm = norm_key(self.cfg.song_dir)
        for dlc in dlcs:
            objs = self.api.list_objects(dlc.prefix)
            kept = 0
            for obj in objs:
                if not include_songs and self._is_song_key(obj.key, dlc.prefix, song_norm):
                    continue
                if obj.key.endswith("/") or obj.key in seen:
                    continue
                seen.add(obj.key)
                kept += 1
                items.append(DownloadItem(key=obj.key, rel_path=self.local_rel(obj.key),
                                          size=obj.size, etag=obj.etag))
            if on_progress:
                on_progress(len(items), f"{dlc.name} ({kept} files)")
        return items

    def plan_folder(self, prefix: str,
                    on_progress: Optional[Callable[[int, str], None]] = None) -> List[DownloadItem]:
        items: List[DownloadItem] = []
        seen = set()
        for obj in self.api.list_objects(prefix):
            if obj.key.endswith("/") or obj.key in seen:
                continue
            seen.add(obj.key)
            items.append(DownloadItem(key=obj.key, rel_path=self.local_rel(obj.key),
                                      size=obj.size, etag=obj.etag))
        if on_progress:
            on_progress(len(items), prefix)
        return items

    @staticmethod
    def _is_song_key(key: str, dlc_prefix: str, song_norm: str) -> bool:
        rest = key[len(dlc_prefix):] if key.startswith(dlc_prefix) else key
        first = rest.split("/", 1)[0]
        return norm_key(first) == song_norm

    def local_rel(self, key: str) -> str:
        """Bucket key -> path relative to the output folder."""
        rel = key
        if self.cfg.root_prefix and rel.startswith(self.cfg.root_prefix):
            rel = rel[len(self.cfg.root_prefix):]
        return safe_rel_path(rel)

    def group_label(self, key: str) -> str:
        """Top-level folder a key belongs to (used to group zips/statistics)."""
        rel = key
        if self.cfg.root_prefix and rel.startswith(self.cfg.root_prefix):
            rel = rel[len(self.cfg.root_prefix):]
        if self.cfg.dlc_dir and rel.startswith(self.cfg.dlc_dir + "/"):
            rel = rel[len(self.cfg.dlc_dir) + 1:]
        parts = rel.split("/")
        return parts[0] if len(parts) > 1 else ""


def auto_detect_layout(cfg: Config, api: Optional[StashAPI] = None) -> dict:
    """Try to work out root_prefix / dlc_dir / song_dir from the bucket.

    Returns {'config': {...}, 'notes': [...], 'ok': bool}
    """
    api = api or StashAPI(cfg)
    notes: List[str] = []
    probe = Config(**cfg.__dict__)

    roots_to_try = [cfg.root_prefix] if cfg.root_prefix else [""]
    for extra in (DEFAULT_ROOT, ""):
        if extra not in roots_to_try:
            roots_to_try.append(extra)

    for root in roots_to_try:
        probe.root_prefix = normalise_prefix(root)
        try:
            folders = api.list_folders(probe.root_prefix)
        except StashError as exc:
            notes.append(f"Could not list {probe.root_prefix or '<root>'}: {exc}")
            continue

        try:
            names = [f.rstrip("/").rsplit("/", 1)[-1] for f in folders]

            # Case 1: root/<dlc_dir>/<DLC>/...
            for name in names:
                if norm_key(name) not in {norm_key(DEFAULT_DLC_DIR), "dlc", "dlcs"}:
                    continue
                probe.dlc_dir = name
                dlcs = api.list_folders(f"{probe.root_prefix}{name}/")
                if not dlcs:
                    continue
                probe.song_dir = _guess_song_dir(api, dlcs[0], cfg.song_dir)
                notes.append(f"Layout: {probe.root_prefix}{name}/<DLC>/...")
                return {"config": probe, "notes": notes, "ok": True}

            # Case 2: root itself holds DLC folders
            for folder in folders:
                if _looks_like_dlc(api, folder):
                    probe.dlc_dir = ""
                    probe.song_dir = _guess_song_dir(api, folder, cfg.song_dir)
                    notes.append(f"Layout: {probe.root_prefix}<DLC>/... (no DLC container folder)")
                    return {"config": probe, "notes": notes, "ok": True}
        except AuthError:
            notes.append(f"{probe.root_prefix or '<root>'} is outside this token's scope")
            continue
        except StashError as exc:
            notes.append(f"Probing {probe.root_prefix or '<root>'} failed: {exc}")
            continue

        notes.append(f"Nothing that looks like a DLC folder under {probe.root_prefix or '<root>'}")

    return {"config": probe, "notes": notes, "ok": False}


def _looks_like_dlc(api: StashAPI, prefix: str) -> bool:
    try:
        children = api.list_folders(prefix)
    except StashError:
        return False
    if not children:
        return False
    names = {norm_key(c.rstrip("/").rsplit("/", 1)[-1]) for c in children}
    interesting = {norm_key(DEFAULT_SONG_DIR), "gears", "assets", "otherassets", "gear", "fonts"}
    if names & interesting:
        return True
    # A DLC folder that only has songs directly inside it
    for child in children:
        try:
            grand = api.list_folders(child)
        except StashError:
            continue
        if len(grand) >= 1 and any(norm_key(g.rstrip("/").rsplit("/", 1)[-1]) in
                                   {norm_key(c) for c in DEFAULT_CHART_DIRS} for g in grand):
            return True
    return False


def _guess_song_dir(api: StashAPI, dlc_prefix: str, fallback: str) -> str:
    try:
        children = api.list_folders(dlc_prefix)
    except StashError:
        return fallback
    names = [c.rstrip("/").rsplit("/", 1)[-1] for c in children]
    for name in names:
        if norm_key(name) in (norm_key(DEFAULT_SONG_DIR), "song", "songs"):
            return name
    return fallback


# --------------------------------------------------------------------------
# downloader
# --------------------------------------------------------------------------


class Downloader:
    """Threaded, resumable, cancellable downloader.

    Events are dicts pushed to ``on_event`` from worker threads -- a GUI should
    put them on a queue and drain them on its own thread.
    """

    def __init__(self, api: StashAPI, dest_root, workers: int = 4,
                 verify_md5: bool = True, resume: bool = True, overwrite: bool = False,
                 on_event: Optional[Callable[[dict], None]] = None):
        self.api = api
        self.cfg = api.config
        self.dest_root = Path(dest_root)
        self.workers = max(1, min(16, int(workers or 4)))
        self.verify_md5 = bool(verify_md5)
        self.resume = bool(resume)
        self.overwrite = bool(overwrite)
        self.on_event = on_event

        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._bytes_done = 0
        self._total_bytes = 0
        self._total_files = 0
        self._files_done = 0
        self._files_skipped = 0
        self._files_failed = 0
        self._active: Dict[int, str] = {}
        self._speed_samples: List[Tuple[float, int]] = []
        self._errors: List[Tuple[str, str]] = []
        self._started = 0.0
        self._last_emit = 0.0
        self._auth_failed = False

    # -- public -------------------------------------------------------------
    def cancel(self):
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def run(self, items: Sequence[DownloadItem]) -> DownloadResult:
        items = [i for i in items if i.rel_path]
        self._started = time.time()
        self._total_files = len(items)
        self._total_bytes = sum(max(0, i.size) for i in items)
        result = DownloadResult(files_total=self._total_files, bytes_total=self._total_bytes)
        if not items:
            result.elapsed = 0.0
            self._emit({"type": "done", "result": result})
            return result

        self.dest_root.mkdir(parents=True, exist_ok=True)
        work: "queue.Queue[DownloadItem]" = queue.Queue()
        for item in items:
            work.put(item)

        threads = [threading.Thread(target=self._worker, args=(work,), daemon=True,
                                    name=f"dl-{i}") for i in range(min(self.workers, len(items)))]
        for t in threads:
            t.start()

        self._emit_progress(force=True)
        for t in threads:
            while t.is_alive():
                t.join(0.2)
                self._emit_progress()

        self._emit_progress(force=True)
        result.files_done = self._files_done
        result.files_skipped = self._files_skipped
        result.files_failed = self._files_failed
        result.bytes_done = self._bytes_done
        result.errors = list(self._errors)
        result.cancelled = self._cancel.is_set()
        result.elapsed = time.time() - self._started
        self._emit({"type": "done", "result": result})
        return result

    # -- internals ----------------------------------------------------------
    def _worker(self, work: "queue.Queue[DownloadItem]"):
        while not self._cancel.is_set():
            try:
                item = work.get_nowait()
            except queue.Empty:
                return
            try:
                self._download_one(item)
            except CancelledError:
                return
            except AuthError as exc:
                self._auth_failed = True
                self._record_error(item, str(exc))
                self._cancel.set()
                return
            except Exception as exc:  # never let a worker die silently
                self._record_error(item, str(exc))
            finally:
                work.task_done()

    def _record_error(self, item: DownloadItem, message: str):
        with self._lock:
            self._files_failed += 1
            self._errors.append((item.key, message))
        self._emit({"type": "file_error", "item": item, "error": message})
        self._emit({"type": "log", "level": "error", "msg": f"FAILED {item.rel_path}: {message}"})

    def _download_one(self, item: DownloadItem):
        if self._cancel.is_set():
            raise CancelledError()
        dest = self.dest_root / item.rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")

        self._emit({"type": "file_start", "item": item})

        # Already complete?
        if not self.overwrite and dest.exists():
            existing = dest.stat().st_size
            if item.size and existing == item.size:
                self._finish(item, item.size, skipped=True)
                return
            if not item.size:
                self._finish(item, existing, skipped=True)
                return

        offset = 0
        if self.resume and part.exists() and not self.overwrite:
            offset = part.stat().st_size
            if item.size and offset >= item.size:
                offset = 0
                try:
                    part.unlink()
                except OSError:
                    pass

        status, headers, resp = self.api.open_object(item.key, offset=offset)
        try:
            if status in (401, 403):
                raise AuthError(_auth_message(status, b""))
            if status == 404:
                raise NotFoundError(f"{item.key} vanished from the bucket")
            if status == 416 and offset:
                # Server says our range is bogus -- start over.
                offset = 0
                resp.close()
                status, headers, resp = self.api.open_object(item.key, offset=0)
            if offset and status != 206:
                # Server ignored the range: restart cleanly.
                offset = 0
                try:
                    part.unlink()
                except OSError:
                    pass
            if status not in (200, 206):
                body = resp.read()
                raise StashError(f"HTTP {status}: {_brief(body)}")

            remote_size = _content_length(headers, item.size)
            mode = "ab" if offset else "wb"
            written = offset
            hasher = hashlib.md5() if (self.verify_md5 and offset == 0) else None
            last_emit = 0.0
            with open(long_path(part), mode) as fh:
                while True:
                    if self._cancel.is_set():
                        raise CancelledError()
                    chunk = resp.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    fh.write(chunk)
                    written += len(chunk)
                    if hasher is not None:
                        hasher.update(chunk)
                    with self._lock:
                        self._bytes_done += len(chunk)
                    now = time.time()
                    if now - last_emit > 0.15:
                        last_emit = now
                        self._emit_progress()
            fh_size = part.stat().st_size
        finally:
            try:
                resp.close()
            except Exception:
                pass

        expected = remote_size if remote_size else None
        if expected is not None and fh_size != expected:
            try:
                part.unlink()
            except OSError:
                pass
            raise NetworkError(f"incomplete download ({fh_size} of {expected} bytes)")

        if hasher is not None and item.etag and "-" not in item.etag:
            local_md5 = hasher.hexdigest()
            if local_md5.lower() != item.etag.strip('"').lower():
                try:
                    part.unlink()
                except OSError:
                    pass
                raise NetworkError("checksum mismatch (file was truncated in transit)")

        os.replace(long_path(part), long_path(dest))
        self._finish(item, fh_size, skipped=False)

    def _finish(self, item: DownloadItem, size: int, skipped: bool):
        with self._lock:
            if skipped:
                self._files_skipped += 1
                self._bytes_done += item.size or size
            else:
                self._files_done += 1
        self._emit({"type": "file_done", "item": item, "bytes": size, "skipped": skipped})
        self._emit_progress(force=True)

    # -- progress -----------------------------------------------------------
    def _emit_progress(self, force: bool = False):
        now = time.time()
        if not force and (now - self._last_emit) < PROGRESS_INTERVAL:
            return
        self._last_emit = now
        with self._lock:
            done = self._bytes_done
            self._speed_samples.append((now, done))
            horizon = now - 6.0
            while len(self._speed_samples) > 2 and self._speed_samples[0][0] < horizon:
                self._speed_samples.pop(0)
            speed = 0.0
            if len(self._speed_samples) >= 2:
                t0, b0 = self._speed_samples[0]
                dt = now - t0
                if dt > 0.25:
                    speed = (done - b0) / dt
            total = self._total_bytes
            stats = DownloadStats(
                files_total=self._total_files,
                files_done=self._files_done,
                files_skipped=self._files_skipped,
                files_failed=self._files_failed,
                bytes_total=total,
                bytes_done=done,
                speed_bps=speed,
                eta_seconds=((total - done) / speed) if (speed > 1024 and total > done) else None,
                current=tuple(sorted(self._active.values())),
                elapsed=now - self._started,
            )
        self._emit({"type": "progress", "stats": stats})

    def _emit(self, event: dict):
        if not self.on_event:
            return
        if event.get("type") == "file_start":
            with self._lock:
                self._active[threading.get_ident()] = event["item"].name
        elif event.get("type") in ("file_done", "file_error"):
            with self._lock:
                self._active.pop(threading.get_ident(), None)
        try:
            self.on_event(event)
        except Exception:
            pass

def _content_length(headers, fallback: int) -> int:
    """Total object size from the response (handles 206 partial content)."""
    try:
        content_range = headers.get("Content-Range") or ""
        if "/" in content_range:
            tail = content_range.rsplit("/", 1)[-1].strip()
            if tail.isdigit():
                return int(tail)
        length = headers.get("Content-Length")
        if length is not None and str(length).isdigit():
            return int(length)
    except Exception:
        pass
    return int(fallback or 0)


# --------------------------------------------------------------------------
# convenience: run a download with callbacks (used by the CLI)
# --------------------------------------------------------------------------


def make_api(cfg: Config) -> StashAPI:
    if not cfg.api_url:
        raise StashError(
            "No API URL configured. Deploy worker/worker.js, then put its URL in "
            "config.json (or Settings in the GUI)."
        )
    return StashAPI(cfg)


def download_items(cfg: Config, items: Sequence[DownloadItem], output_dir: Optional[str] = None,
                   on_event: Optional[Callable[[dict], None]] = None,
                   cancel_event: Optional[threading.Event] = None) -> DownloadResult:
    api = make_api(cfg)
    dl = Downloader(api, output_dir or cfg.output_dir, workers=cfg.workers,
                    verify_md5=cfg.verify_md5, resume=cfg.resume, on_event=on_event)
    if cancel_event is not None:
        def watch():
            cancel_event.wait()
            dl.cancel()
        threading.Thread(target=watch, daemon=True).start()
    return dl.run(list(items))


def name_from_key(key: str) -> str:
    return key.rstrip("/").rsplit("/", 1)[-1]


def bytes_to_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
