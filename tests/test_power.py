"""Unit tests for nextme.power (macOS power assertion)."""

from __future__ import annotations

import ctypes
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# PowerAssertion.__init__
# ---------------------------------------------------------------------------

def test_power_assertion_init_defaults():
    from nextme.power import PowerAssertion
    pa = PowerAssertion()
    assert pa._id is None
    assert pa._iokit is None


def test_power_assertion_init_with_values():
    from nextme.power import PowerAssertion
    mock_iokit = MagicMock()
    pa = PowerAssertion(assertion_id=42, iokit=mock_iokit)
    assert pa._id == 42
    assert pa._iokit is mock_iokit


# ---------------------------------------------------------------------------
# PowerAssertion.acquire() — non-macOS path
# ---------------------------------------------------------------------------

def test_acquire_non_macos_returns_noop():
    """On non-macOS, acquire() returns a no-op PowerAssertion with _id=None."""
    from nextme.power import PowerAssertion
    with patch("nextme.power._IOKIT_AVAILABLE", False):
        pa = PowerAssertion.acquire("test reason")
    assert pa._id is None
    assert pa._iokit is None


# ---------------------------------------------------------------------------
# PowerAssertion.acquire() — macOS paths (mocked IOKit)
# ---------------------------------------------------------------------------

def test_acquire_iokit_load_returns_none():
    """acquire() returns no-op when _load_iokit returns None."""
    from nextme.power import PowerAssertion
    with patch("nextme.power._IOKIT_AVAILABLE", True), \
         patch("nextme.power._load_iokit", return_value=None):
        pa = PowerAssertion.acquire("test reason")
    assert pa._id is None


def test_acquire_assertion_create_fails():
    """acquire() returns no-op when IOPMAssertionCreateWithName returns non-zero."""
    from nextme.power import PowerAssertion, _kIOPMAssertionLevelOn
    mock_iokit = MagicMock()
    mock_iokit.IOPMAssertionCreateWithName.return_value = 1  # error code

    with patch("nextme.power._IOKIT_AVAILABLE", True), \
         patch("nextme.power._load_iokit", return_value=mock_iokit), \
         patch("nextme.power._cfstr", return_value=0x1234):
        pa = PowerAssertion.acquire("test reason")
    assert pa._id is None


def test_acquire_success():
    """acquire() stores assertion_id and iokit on success (ret=0)."""
    from nextme.power import PowerAssertion
    mock_iokit = MagicMock()
    mock_iokit.IOPMAssertionCreateWithName.return_value = 0  # kIOReturnSuccess

    with patch("nextme.power._IOKIT_AVAILABLE", True), \
         patch("nextme.power._load_iokit", return_value=mock_iokit), \
         patch("nextme.power._cfstr", return_value=0xABCD):
        pa = PowerAssertion.acquire("test reason")

    # assertion_id.value is 0 (default of c_uint32) since mock doesn't set it
    assert pa._id == 0
    assert pa._iokit is mock_iokit


def test_acquire_exception_returns_noop():
    """acquire() catches exceptions and returns no-op."""
    from nextme.power import PowerAssertion
    with patch("nextme.power._IOKIT_AVAILABLE", True), \
         patch("nextme.power._load_iokit", side_effect=RuntimeError("crash")):
        pa = PowerAssertion.acquire("test reason")
    assert pa._id is None


# ---------------------------------------------------------------------------
# PowerAssertion.release()
# ---------------------------------------------------------------------------

def test_release_noop_when_id_is_none():
    """release() does nothing when _id is None."""
    from nextme.power import PowerAssertion
    pa = PowerAssertion()
    pa.release()  # should not raise
    assert pa._id is None


def test_release_success():
    """release() calls IOPMAssertionRelease when _id is set."""
    from nextme.power import PowerAssertion
    mock_iokit = MagicMock()
    mock_iokit.IOPMAssertionRelease.return_value = 0
    pa = PowerAssertion(assertion_id=7, iokit=mock_iokit)
    pa.release()
    mock_iokit.IOPMAssertionRelease.assert_called_once_with(7)
    assert pa._id is None  # cleared after release


def test_release_logs_warning_on_nonzero_return():
    """release() logs a warning when IOPMAssertionRelease returns non-zero."""
    from nextme.power import PowerAssertion
    mock_iokit = MagicMock()
    mock_iokit.IOPMAssertionRelease.return_value = 5  # error
    pa = PowerAssertion(assertion_id=3, iokit=mock_iokit)
    pa.release()  # should not raise
    assert pa._id is None  # still cleared in finally


def test_release_exception_swallowed():
    """release() swallows exceptions from IOPMAssertionRelease."""
    from nextme.power import PowerAssertion
    mock_iokit = MagicMock()
    mock_iokit.IOPMAssertionRelease.side_effect = RuntimeError("crash")
    pa = PowerAssertion(assertion_id=5, iokit=mock_iokit)
    pa.release()  # should not raise


# ---------------------------------------------------------------------------
# Context manager protocol
# ---------------------------------------------------------------------------

def test_context_manager_enter_returns_self():
    from nextme.power import PowerAssertion
    pa = PowerAssertion()
    result = pa.__enter__()
    assert result is pa


def test_context_manager_exit_calls_release():
    from nextme.power import PowerAssertion
    mock_iokit = MagicMock()
    mock_iokit.IOPMAssertionRelease.return_value = 0
    pa = PowerAssertion(assertion_id=10, iokit=mock_iokit)
    pa.__exit__(None, None, None)
    mock_iokit.IOPMAssertionRelease.assert_called_once_with(10)


def test_context_manager_as_with_statement():
    from nextme.power import PowerAssertion
    with patch("nextme.power._IOKIT_AVAILABLE", False):
        with PowerAssertion.acquire("test") as pa:
            assert pa._id is None
    # No exception should be raised


# ---------------------------------------------------------------------------
# __del__
# ---------------------------------------------------------------------------

def test_del_calls_release():
    """__del__ calls release(), which clears _id."""
    from nextme.power import PowerAssertion
    mock_iokit = MagicMock()
    mock_iokit.IOPMAssertionRelease.return_value = 0
    pa = PowerAssertion(assertion_id=11, iokit=mock_iokit)
    pa.__del__()
    mock_iokit.IOPMAssertionRelease.assert_called_once_with(11)


# ---------------------------------------------------------------------------
# _load_iokit and _cfstr (macOS-only internal helpers)
# ---------------------------------------------------------------------------

def test_load_iokit_returns_none_when_lib_not_found():
    """_load_iokit returns None when the library path cannot be found."""
    from nextme.power import _load_iokit
    with patch("ctypes.util.find_library", return_value=None):
        result = _load_iokit()
    assert result is None


def test_load_iokit_returns_library_on_success():
    """_load_iokit returns a library object when IOKit is available."""
    from nextme.power import _load_iokit
    mock_lib = MagicMock()
    with patch("ctypes.util.find_library", return_value="/path/to/IOKit"), \
         patch("ctypes.cdll.LoadLibrary", return_value=mock_lib):
        result = _load_iokit()
    assert result is mock_lib


def test_cfstr_calls_cfstring_create():
    """_cfstr calls CFStringCreateWithCString and returns the result."""
    from nextme.power import _cfstr
    mock_cf = MagicMock()
    mock_cf.CFStringCreateWithCString.return_value = 0xDEAD
    with patch("ctypes.util.find_library", return_value="/path/to/CF"), \
         patch("ctypes.cdll.LoadLibrary", return_value=mock_cf):
        result = _cfstr("hello")
    assert result == 0xDEAD
    mock_cf.CFStringCreateWithCString.assert_called_once_with(
        None, b"hello", 0x08000100
    )
