"""Open a local path or URL as a streaming, decompressed, byte-counted binary file.

Nothing here downloads a whole file: callers read as far as they need and close.
"""
from __future__ import annotations

import contextlib
import gzip
import io
import time
from pathlib import Path
from urllib.parse import urlparse

import requests


class ByteCapReached(Exception):
    pass


class CountingReader(io.RawIOBase):
    """Wraps a binary stream, counts bytes read, and raises once a cap is passed."""

    def __init__(self, inner, max_bytes: int | None = None):
        self._inner = inner
        self.bytes_read = 0
        self.max_bytes = max_bytes

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        if self.max_bytes is not None and self.bytes_read >= self.max_bytes:
            raise ByteCapReached(self.bytes_read)
        data = self._inner.read(len(b))
        n = len(data)
        b[:n] = data
        self.bytes_read += n
        return n


def is_remote(src: str) -> bool:
    return urlparse(src).scheme in ("http", "https")


def _open_raw(src: str, timeout: int, retries: int, user_agent: str):
    """Returns (binary file-like, closer)."""
    parsed = urlparse(src)
    if parsed.scheme == "file":
        fh = open(parsed.path, "rb")
        return fh, fh.close
    if not is_remote(src):
        fh = open(Path(src), "rb")
        return fh, fh.close
    last_exc = None
    for attempt in range(retries):
        try:
            resp = requests.get(
                src, stream=True, timeout=timeout, headers={"User-Agent": user_agent, "Accept-Encoding": "gzip"}
            )
            resp.raise_for_status()
            resp.raw.decode_content = True  # undo transport-level Content-Encoding: gzip
            return resp.raw, resp.close
        except requests.HTTPError:
            raise
        except requests.RequestException as exc:  # connection problems: back off and retry
            last_exc = exc
            time.sleep(2 ** attempt)
    raise last_exc


@contextlib.contextmanager
def open_stream(src: str, max_bytes: int | None = None, timeout: int = 60, retries: int = 3,
                user_agent: str = "hilabs-c2-pipeline/1.0"):
    """Yields (reader, counter). `counter.bytes_read` is the number of decompressed bytes consumed."""
    raw, closer = _open_raw(src, timeout, retries, user_agent)
    try:
        buffered = io.BufferedReader(raw if isinstance(raw, io.RawIOBase) else _RawAdapter(raw), 1 << 16)
        if buffered.peek(2)[:2] == b"\x1f\x8b":  # gzip file body (e.g. .json.gz), not just transport
            inner = gzip.GzipFile(fileobj=buffered)
        else:
            inner = buffered
        counter = CountingReader(inner, max_bytes)
        yield io.BufferedReader(counter, 1 << 16), counter
    finally:
        closer()


class _RawAdapter(io.RawIOBase):
    """Lets io.BufferedReader wrap objects that only implement read() (urllib3 responses, files)."""

    def __init__(self, inner):
        self._inner = inner

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        data = self._inner.read(len(b))
        n = len(data)
        b[:n] = data
        return n
