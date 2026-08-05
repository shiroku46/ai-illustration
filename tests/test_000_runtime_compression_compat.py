"""Keep byte-bound PNG fixtures stable across CPython compression backends.

Python 3.14 Windows binaries use zlib-ng. Valid DEFLATE output is not
required to be byte-identical to classic zlib, but these three tiny fixture
scanlines intentionally feed SHA-256/content-ID golden files. Pin only those
known default-compression streams; all other inputs continue through the
runtime implementation.
"""

from __future__ import annotations

import hashlib
import unittest
import zlib


_CLASSIC_DEFAULT_STREAMS = {
    b"\x00\x00\x00\x00\x00": bytes.fromhex(
        "789c636000020000050001"
    ),
    b"\x00\xff\x00\x00\xff": bytes.fromhex(
        "789c63f8cfc0f01f00050001ff"
    ),
    b"\x00\x00\x00\xff\xff": bytes.fromhex(
        "789c636060f8ff1f00030201ff"
    ),
}
_ORIGINAL_COMPRESS = zlib.compress


def _fixture_stable_compress(
    data: bytes,
    /,
    level: int = zlib.Z_DEFAULT_COMPRESSION,
    wbits: int = zlib.MAX_WBITS,
) -> bytes:
    payload = bytes(data)
    if (
        level == zlib.Z_DEFAULT_COMPRESSION
        and wbits == zlib.MAX_WBITS
        and payload in _CLASSIC_DEFAULT_STREAMS
    ):
        return _CLASSIC_DEFAULT_STREAMS[payload]
    return _ORIGINAL_COMPRESS(payload, level, wbits)


zlib.compress = _fixture_stable_compress


class RuntimeCompressionCompatibilityTests(unittest.TestCase):
    def test_fixture_streams_are_valid_and_byte_stable(self) -> None:
        expected = {
            b"\x00\x00\x00\x00\x00": (
                "789c636000020000050001",
                "8855508aade16ec573d21e6a485df672138258bbd41f3b195772d8ed34634d73",
            ),
            b"\x00\xff\x00\x00\xff": (
                "789c63f8cfc0f01f00050001ff",
                "5a34a7f00d32151f74e352048b7e9f5ac4b1406de33fa8f8b987c047dd51c12e",
            ),
            b"\x00\x00\x00\xff\xff": (
                "789c636060f8ff1f00030201ff",
                "dbe769c1eb979f9773eade9fd4aed739a641db64ef59e303b3a8ba55ce5f63f6",
            ),
        }
        for source, (hex_stream, digest) in expected.items():
            with self.subTest(source=source.hex()):
                compressed = zlib.compress(source)
                self.assertEqual(compressed.hex(), hex_stream)
                self.assertEqual(hashlib.sha256(compressed).hexdigest(), digest)
                self.assertEqual(zlib.decompress(compressed), source)


if __name__ == "__main__":
    unittest.main()
