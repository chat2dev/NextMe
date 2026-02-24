"""Context subsystem — per-session context file storage with optional compression."""

from .compression import (
    CompressionAlgorithm,
    CompressionResult,
    choose_algorithm,
    compress,
    decompress,
)
from .manager import ContextManager

__all__ = [
    "choose_algorithm",
    "compress",
    "decompress",
    "CompressionAlgorithm",
    "CompressionResult",
    "ContextManager",
]
