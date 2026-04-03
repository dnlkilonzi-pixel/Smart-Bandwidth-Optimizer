"""
Tests for bandwidth_optimizer.compressor
"""

import zlib

import pytest

from bandwidth_optimizer.compressor import PayloadCompressor, _MAGIC
from bandwidth_optimizer.config import OptimizerConfig


class TestPayloadCompressor:
    def setup_method(self):
        self.cfg = OptimizerConfig(
            compression_threshold_bytes=10,
            compression_level=6,
        )
        self.comp = PayloadCompressor(config=self.cfg)

    # ── compress ──────────────────────────────────────────────────────────

    def test_compress_large_repetitive_data(self):
        data = b"A" * 1000
        result = self.comp.compress(data)
        assert result.was_compressed
        assert result.compressed_size < result.original_size
        assert result.data[:4] == _MAGIC
        assert result.space_saved_bytes > 0

    def test_compress_below_threshold_skips(self):
        data = b"tiny"   # < 10 bytes
        result = self.comp.compress(data)
        assert not result.was_compressed
        assert result.data == data

    def test_compress_already_compressed_skips(self):
        data = b"A" * 500
        first = self.comp.compress(data)
        assert first.was_compressed
        second = self.comp.compress(first.data)
        assert not second.was_compressed   # magic header detected

    def test_compress_random_data_may_not_compress(self):
        import os
        data = os.urandom(500)
        result = self.comp.compress(data)
        # Random data often doesn't compress; verify the API doesn't crash
        # and that if not compressed, original data is returned
        if not result.was_compressed:
            assert result.data == data
        else:
            assert result.data[:4] == _MAGIC

    def test_ratio_property(self):
        data = b"B" * 1000
        result = self.comp.compress(data)
        assert result.was_compressed
        assert result.ratio < 1.0

    def test_ratio_uncompressed_is_one(self):
        result = self.comp.compress(b"hi")
        assert result.ratio == pytest.approx(1.0)

    # ── decompress ────────────────────────────────────────────────────────

    def test_roundtrip(self):
        original = b"Hello, World! " * 100
        compressed = self.comp.compress(original)
        assert compressed.was_compressed
        recovered = self.comp.decompress(compressed.data)
        assert recovered == original

    def test_decompress_uncompressed_returns_as_is(self):
        data = b"plain text"
        assert self.comp.decompress(data) == data

    def test_decompress_corrupt_raises(self):
        bad = _MAGIC + b"\xff\xff\xff\xff"
        with pytest.raises(ValueError, match="decompress"):
            self.comp.decompress(bad)

    def test_is_compressed_true(self):
        data = b"C" * 500
        result = self.comp.compress(data)
        assert self.comp.is_compressed(result.data) == result.was_compressed

    def test_is_compressed_false_for_plain(self):
        assert not self.comp.is_compressed(b"not compressed")

    # ── batch helpers ─────────────────────────────────────────────────────

    def test_compress_batch(self):
        payloads = [b"D" * 500, b"E" * 500, b"hi"]
        results = self.comp.compress_batch(payloads)
        assert len(results) == 3

    def test_total_savings(self):
        payloads = [b"F" * 1000, b"G" * 1000]
        results = self.comp.compress_batch(payloads)
        summary = self.comp.total_savings(results)
        assert "bytes_saved" in summary
        assert summary["bytes_saved"] >= 0
        assert summary["packets_total"] == 2
        assert 0.0 <= summary["overall_ratio"] <= 1.0

    def test_custom_threshold_override(self):
        data = b"H" * 20   # 20 bytes
        # Threshold=10 (default) → should compress
        result_low = self.comp.compress(data, threshold=10)
        # Threshold=100 → should skip
        result_high = self.comp.compress(data, threshold=100)
        assert result_low.was_compressed
        assert not result_high.was_compressed
