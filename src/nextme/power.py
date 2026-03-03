"""macOS power assertion to prevent idle sleep.

When the display turns off (screen lock), macOS eventually enters idle sleep
which powers down Wi-Fi and kills WebSocket connections.  Creating a
``PreventUserIdleSystemSleep`` assertion keeps the system awake (but the
display stays off) so long-lived network connections survive.

On non-macOS platforms this module is a no-op.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import sys

logger = logging.getLogger(__name__)

_IOKIT_AVAILABLE = sys.platform == "darwin"


def _load_iokit():
    """Load IOKit framework and bind the assertion functions."""
    lib_path = ctypes.util.find_library("IOKit")
    if not lib_path:
        return None
    iokit = ctypes.cdll.LoadLibrary(lib_path)

    # IOReturn IOPMAssertionCreateWithName(
    #     CFStringRef type, IOPMAssertionLevel level,
    #     CFStringRef name, IOPMAssertionID *id)
    iokit.IOPMAssertionCreateWithName.argtypes = [
        ctypes.c_void_p,  # CFStringRef
        ctypes.c_uint32,  # IOPMAssertionLevel
        ctypes.c_void_p,  # CFStringRef
        ctypes.POINTER(ctypes.c_uint32),  # IOPMAssertionID *
    ]
    iokit.IOPMAssertionCreateWithName.restype = ctypes.c_int32

    # IOReturn IOPMAssertionRelease(IOPMAssertionID id)
    iokit.IOPMAssertionRelease.argtypes = [ctypes.c_uint32]
    iokit.IOPMAssertionRelease.restype = ctypes.c_int32

    return iokit


def _cfstr(s: str):
    """Create a CoreFoundation CFString from a Python string."""
    cf = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreFoundation"))
    cf.CFStringCreateWithCString.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32,
    ]
    cf.CFStringCreateWithCString.restype = ctypes.c_void_p
    # kCFStringEncodingUTF8 = 0x08000100
    return cf.CFStringCreateWithCString(None, s.encode("utf-8"), 0x08000100)


# IOPMAssertionLevel
_kIOPMAssertionLevelOn = 255


class PowerAssertion:
    """RAII wrapper for a macOS IOKit power assertion.

    Usage::

        pa = PowerAssertion.acquire("NextMe bot")
        # ... system will not idle-sleep ...
        pa.release()

    Or as a context manager::

        with PowerAssertion.acquire("NextMe bot"):
            ...

    On non-macOS platforms, ``acquire()`` returns a no-op instance.
    """

    def __init__(self, assertion_id: int | None = None, iokit=None):
        self._id = assertion_id
        self._iokit = iokit

    @classmethod
    def acquire(cls, reason: str = "NextMe WebSocket keepalive") -> "PowerAssertion":
        """Create a PreventUserIdleSystemSleep assertion."""
        if not _IOKIT_AVAILABLE:
            logger.debug("Power assertions not available (non-macOS)")
            return cls()

        try:
            iokit = _load_iokit()
            if iokit is None:
                logger.warning("Could not load IOKit; power assertion unavailable")
                return cls()

            assertion_type = _cfstr("PreventUserIdleSystemSleep")
            assertion_name = _cfstr(reason)
            assertion_id = ctypes.c_uint32(0)

            ret = iokit.IOPMAssertionCreateWithName(
                assertion_type,
                _kIOPMAssertionLevelOn,
                assertion_name,
                ctypes.byref(assertion_id),
            )

            if ret != 0:  # kIOReturnSuccess == 0
                logger.warning(
                    "IOPMAssertionCreateWithName failed (ret=%d); "
                    "system may idle-sleep and drop WebSocket", ret,
                )
                return cls()

            logger.info(
                "Power assertion acquired (PreventUserIdleSystemSleep, id=%d)",
                assertion_id.value,
            )
            return cls(assertion_id=assertion_id.value, iokit=iokit)

        except Exception:
            logger.exception("Failed to acquire power assertion")
            return cls()

    def release(self) -> None:
        """Release the power assertion (allow idle sleep again)."""
        if self._id is not None and self._iokit is not None:
            try:
                ret = self._iokit.IOPMAssertionRelease(self._id)
                if ret == 0:
                    logger.info("Power assertion released (id=%d)", self._id)
                else:
                    logger.warning(
                        "IOPMAssertionRelease failed (ret=%d, id=%d)", ret, self._id,
                    )
            except Exception:
                logger.exception("Error releasing power assertion")
            finally:
                self._id = None

    def __enter__(self) -> "PowerAssertion":
        return self

    def __exit__(self, *exc) -> None:
        self.release()

    def __del__(self) -> None:
        self.release()
