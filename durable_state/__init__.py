# Copyright (c) 2026 Durable State contributors | MIT License
"""Public API. Import from here, not from the modules directly."""

from .atomicio import (
    APPEND_INLINE_MAX,
    atomic_append_text,
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_text,
)
from .locks import FileLock, LockBusy, is_locked, lock_info


def __getattr__(name: str):
    """verify_dir is imported lazily, on purpose.

    Importing .verify here made `python -m durable_state.verify <dir>` print
    RuntimeWarning: the package imported the submodule, then runpy executed it
    again as __main__. The function is lazy so the documented CLI runs clean.

    PEP 562, so this works on the Python 3.9 floor we claim.
    """
    if name == "verify_dir":
        from .verify import verify_dir

        return verify_dir
    raise AttributeError("module %r has no attribute %r" % (__name__, name))

__version__ = "0.1.3"
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
