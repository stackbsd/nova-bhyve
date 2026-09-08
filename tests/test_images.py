"""Tests for AlignedDeviceWriter."""

import io
import os
import unittest
from unittest import mock

from nova_bhyve import images


class AlignedDeviceWriterTestCase(unittest.TestCase):
    """Unaligned Glance chunks become aligned device writes."""

    def _writer(self, block=16):
        """Return a BytesIO sink and a writer over it."""
        sink = io.BytesIO()
        sink.fileno = lambda: -1
        return sink, images.AlignedDeviceWriter(sink, block=block)

    def _close(self, writer):
        """Close the writer with fsync stubbed out for the BytesIO sink."""
        with mock.patch.object(os, "fsync"):
            writer.close()

    def test_nothing_is_written_before_a_full_block(self):
        """A partial block stays buffered."""
        sink, writer = self._writer()
        writer.write(b"a" * 15)
        self.assertEqual(b"", sink.getvalue())

    def test_a_full_block_is_written_through(self):
        """A whole block goes straight to the device."""
        sink, writer = self._writer()
        writer.write(b"a" * 16)
        self.assertEqual(b"a" * 16, sink.getvalue())

    def test_chunks_are_reassembled_into_aligned_blocks(self):
        """Arbitrary chunk sizes come out as whole blocks plus a padded tail."""
        sink, writer = self._writer()
        for _ in range(10):
            writer.write(b"x" * 5)  # 50 bytes in 5-byte chunks
        self.assertEqual(48, len(sink.getvalue()))  # three whole blocks
        self._close(writer)
        self.assertEqual(64, len(sink.getvalue()))  # tail padded to four

    def test_the_tail_is_zero_padded_not_truncated(self):
        """The final partial block is padded with zeros."""
        sink, writer = self._writer()
        writer.write(b"abc")
        self._close(writer)
        self.assertEqual(b"abc" + b"\0" * 13, sink.getvalue())

    def test_close_is_idempotent(self):
        """Closing twice writes the tail once."""
        sink, writer = self._writer()
        writer.write(b"abc")
        self._close(writer)
        self._close(writer)
        self.assertEqual(16, len(sink.getvalue()))

    def test_written_counts_padding(self):
        """The written counter describes the device, padding included."""
        _sink, writer = self._writer()
        writer.write(b"abc")
        self._close(writer)
        self.assertEqual(16, writer.written)

    def test_write_returns_the_length_it_was_given(self):
        """write() honours the file-object contract Glance relies on."""
        _sink, writer = self._writer()
        self.assertEqual(5, writer.write(b"abcde"))
