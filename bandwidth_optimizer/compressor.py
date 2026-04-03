"""
Data compressor / decompressor.

Uses zlib (DEFLATE) for in-memory compression of packet payloads.
A magic header (``SBOC``) is prepended to every compressed payload so the
decompressor can detect double-compression and skip payloads that are already
compressed or that didn't benefit from compression.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass
from typing import Optional

from .config import OptimizerConfig, TrafficPriority

# Magic bytes that prefix every compressed payload produced by this module
_MAGIC = b"SBOC"
_MAGIC_LEN = len(_MAGIC)


@dataclass
class CompressionResult:
    """Outcome of a single compress() call."""
    original_size: int
    compressed_size: int
    data: bytes
    was_compressed: bool

    @property
    def ratio(self) -> float:
        """Compression ratio (< 1.0 means data shrank)."""
        if self.original_size == 0:
            return 1.0
        return self.compressed_size / self.original_size

    @property
    def space_saved_bytes(self) -> int:
        return max(0, self.original_size - self.compressed_size)


class PayloadCompressor:
    """
    Compress / decompress packet payloads.

    Rules applied before compression:
      * payload must be at least *threshold* bytes
      * payload must not already carry the ``SBOC`` magic header
      * compressed result must be smaller than the original

    If any rule fails, the original payload is returned unchanged and
    ``CompressionResult.was_compressed`` is ``False``.
    """

    def __init__(self, config: Optional[OptimizerConfig] = None) -> None:
        self._config = config or OptimizerConfig()

    # ── public API ────────────────────────────────────────────────────────

    def compress(
        self,
        data: bytes,
        level: Optional[int] = None,
        threshold: Optional[int] = None,
    ) -> CompressionResult:
        """
        Attempt to compress *data*.

        :param data:      Raw bytes to compress.
        :param level:     zlib compression level (1–9). Defaults to config value.
        :param threshold: Minimum byte size to attempt compression.
                          Defaults to config value.
        :returns: CompressionResult with the (possibly unchanged) payload.
        """
        if level is None:
            level = self._config.compression_level
        if threshold is None:
            threshold = self._config.compression_threshold_bytes

        original_size = len(data)

        # Skip empty, too-small, or already-compressed payloads
        if original_size < threshold or data[:_MAGIC_LEN] == _MAGIC:
            return CompressionResult(
                original_size=original_size,
                compressed_size=original_size,
                data=data,
                was_compressed=False,
            )

        compressed_body = zlib.compress(data, level=level)
        candidate = _MAGIC + compressed_body

        if len(candidate) >= original_size:
            # Compression didn't help (e.g., data is already random/encrypted)
            return CompressionResult(
                original_size=original_size,
                compressed_size=original_size,
                data=data,
                was_compressed=False,
            )

        return CompressionResult(
            original_size=original_size,
            compressed_size=len(candidate),
            data=candidate,
            was_compressed=True,
        )

    def decompress(self, data: bytes) -> bytes:
        """
        Decompress *data* if it carries the SBOC magic header.

        :returns: Decompressed bytes, or the original bytes if not compressed
                  by this module.
        :raises ValueError: If the compressed body is corrupt.
        """
        if data[:_MAGIC_LEN] != _MAGIC:
            return data
        try:
            return zlib.decompress(data[_MAGIC_LEN:])
        except zlib.error as exc:
            raise ValueError(f"Failed to decompress payload: {exc}") from exc

    def is_compressed(self, data: bytes) -> bool:
        """Return True if *data* was compressed by this module."""
        return data[:_MAGIC_LEN] == _MAGIC

    # ── statistics helper ─────────────────────────────────────────────────

    def compress_batch(self, payloads: list[bytes]) -> list[CompressionResult]:
        """Compress a list of payloads and return their results."""
        return [self.compress(p) for p in payloads]

    def total_savings(self, results: list[CompressionResult]) -> dict:
        """Summarise compression savings across a batch."""
        total_orig = sum(r.original_size for r in results)
        total_comp = sum(r.compressed_size for r in results)
        compressed_count = sum(1 for r in results if r.was_compressed)
        return {
            "total_original_bytes": total_orig,
            "total_compressed_bytes": total_comp,
            "bytes_saved": max(0, total_orig - total_comp),
            "overall_ratio": (total_comp / total_orig) if total_orig else 1.0,
            "packets_compressed": compressed_count,
            "packets_total": len(results),
        }
