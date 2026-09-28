# Copyright (c) 2026 Vika Integrity contributors | MIT License
"""Public API. Import from here, not from the modules directly."""

from .atomicio import (
    APPEND_INLINE_MAX,
    atomic_append_text,
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_text,
)
from .locks import FileLock, LockBusy, is_locked, lock_info
from .verify import verify_dir

__version__ = "0.1.0"
__all__ = [
    "atomic_write_text",
    "atomic_write_bytes",
    "atomic_write_json",
    "atomic_append_text",
    "APPEND_INLINE_MAX",
    "FileLock",
    "LockBusy",
    "is_locked",
    "lock_info",
    "verify_dir",
    "__version__",
]
