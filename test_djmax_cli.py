import pathlib
import tempfile
import unittest
from unittest import mock

import djmax_cli


class _FakeResponse:
    def __init__(self, payload: bytes, status: int = 200):
        self._payload = payload
        self.status = status
        self.headers = {}

    def read(self, size=-1):
        if size == -1:
            size = len(self._payload)
        chunk, self._payload = self._payload[:size], self._payload[size:]
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class DjmaxCliTests(unittest.TestCase):
    def test_build_url_from_base(self):
        self.assertEqual(
            djmax_cli._build_url("songs/a.zip", "https://cdn.example.com/files"),
            "https://cdn.example.com/files/songs/a.zip",
        )

    def test_build_url_requires_base_for_relative_target(self):
        with self.assertRaises(ValueError):
            djmax_cli._build_url("songs/a.zip", None)

    def test_download_file_writes_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = pathlib.Path(tmp) / "file.bin"
            with mock.patch("urllib.request.urlopen", return_value=_FakeResponse(b"abc")):
                djmax_cli.download_file("https://example.com/file.bin", target, force=False)
            self.assertEqual(target.read_bytes(), b"abc")


if __name__ == "__main__":
    unittest.main()
