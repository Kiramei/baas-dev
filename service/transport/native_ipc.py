from __future__ import annotations

import ctypes
import ctypes.util
import errno
import os
import sys
import time
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator
from typing import Optional


class NativeIpcError(RuntimeError):
    pass


_NATIVE_PLATFORM = sys.platform
_HAS_WINDOWS_NATIVE = _NATIVE_PLATFORM == "win32"
_HAS_POSIX_NATIVE = _NATIVE_PLATFORM in {"linux", "darwin"}
_UMASK_LOCK = threading.Lock()


if _HAS_WINDOWS_NATIVE:
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateFileMappingW.argtypes = (
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_wchar_p,
    )
    _kernel32.CreateFileMappingW.restype = ctypes.c_void_p
    _kernel32.OpenFileMappingW.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_wchar_p)
    _kernel32.OpenFileMappingW.restype = ctypes.c_void_p
    _kernel32.MapViewOfFile.argtypes = (
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_size_t,
    )
    _kernel32.MapViewOfFile.restype = ctypes.c_void_p
    _kernel32.UnmapViewOfFile.argtypes = (ctypes.c_void_p,)
    _kernel32.UnmapViewOfFile.restype = ctypes.c_int
    _kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    _kernel32.CloseHandle.restype = ctypes.c_int
    _kernel32.CreateEventW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p)
    _kernel32.CreateEventW.restype = ctypes.c_void_p
    _kernel32.OpenEventW.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_wchar_p)
    _kernel32.OpenEventW.restype = ctypes.c_void_p
    _kernel32.SetEvent.argtypes = (ctypes.c_void_p,)
    _kernel32.SetEvent.restype = ctypes.c_int
    _kernel32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    _kernel32.WaitForSingleObject.restype = ctypes.c_uint32

    _FILE_MAP_READ_WRITE = 0x0006
    _PAGE_READWRITE = 0x04
    _SYNCHRONIZE = 0x00100000
    _EVENT_MODIFY_STATE = 0x0002
    _WAIT_OBJECT_0 = 0x00000000
    _WAIT_TIMEOUT = 0x00000102
elif _HAS_POSIX_NATIVE:
    _libc = ctypes.CDLL(ctypes.util.find_library("c") or None, use_errno=True)
    _libc.shm_open.argtypes = (ctypes.c_char_p, ctypes.c_int, ctypes.c_uint)
    _libc.shm_open.restype = ctypes.c_int
    _libc.ftruncate.argtypes = (ctypes.c_int, ctypes.c_longlong)
    _libc.ftruncate.restype = ctypes.c_int
    _libc.fchmod.argtypes = (ctypes.c_int, ctypes.c_uint)
    _libc.fchmod.restype = ctypes.c_int
    _libc.mmap.argtypes = (
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_longlong,
    )
    _libc.mmap.restype = ctypes.c_void_p
    _libc.munmap.argtypes = (ctypes.c_void_p, ctypes.c_size_t)
    _libc.munmap.restype = ctypes.c_int
    _libc.close.argtypes = (ctypes.c_int,)
    _libc.close.restype = ctypes.c_int
    _libc.shm_unlink.argtypes = (ctypes.c_char_p,)
    _libc.shm_unlink.restype = ctypes.c_int
    _libc.sem_open.argtypes = (ctypes.c_char_p, ctypes.c_int, ctypes.c_uint, ctypes.c_uint)
    _libc.sem_open.restype = ctypes.c_void_p
    _libc.sem_post.argtypes = (ctypes.c_void_p,)
    _libc.sem_post.restype = ctypes.c_int
    _libc.sem_trywait.argtypes = (ctypes.c_void_p,)
    _libc.sem_trywait.restype = ctypes.c_int
    _libc.sem_close.argtypes = (ctypes.c_void_p,)
    _libc.sem_close.restype = ctypes.c_int
    _libc.sem_unlink.argtypes = (ctypes.c_char_p,)
    _libc.sem_unlink.restype = ctypes.c_int

    _PROT_READ = 0x1
    _PROT_WRITE = 0x2
    _MAP_SHARED = 0x01
    _MAP_FAILED = ctypes.c_void_p(-1).value
    _SEM_FAILED = ctypes.c_void_p(-1).value


def _raise_last_error(operation: str) -> None:
    code = ctypes.get_last_error()
    raise NativeIpcError(f"{operation} failed with Win32 error {code}")


def _raise_errno(operation: str) -> None:
    code = ctypes.get_errno()
    raise NativeIpcError(f"{operation} failed with errno {code}: {os.strerror(code)}")


def _posix_name(name: str) -> bytes:
    if "\x00" in name:
        raise NativeIpcError("native IPC name must not contain NUL bytes")
    if not name.startswith("/"):
        raise NativeIpcError("POSIX native IPC names must start with '/'")
    return name.encode("utf-8")


@contextmanager
def _temporary_umask(mask: int) -> Iterator[None]:
    with _UMASK_LOCK:
        previous = os.umask(mask)
        try:
            yield
        finally:
            os.umask(previous)


@dataclass
class WindowsSharedMemoryRegion:
    name: str
    size: int
    _handle: int
    _address: int

    @classmethod
    def create(cls, name: str, size: int) -> "WindowsSharedMemoryRegion":
        if not _HAS_WINDOWS_NATIVE:
            raise NativeIpcError("native shared memory is only implemented for Windows in this build")
        if size <= 0:
            raise NativeIpcError("shared-memory region size must be greater than zero")
        handle = _kernel32.CreateFileMappingW(
            ctypes.c_void_p(-1).value,
            None,
            _PAGE_READWRITE,
            (size >> 32) & 0xFFFFFFFF,
            size & 0xFFFFFFFF,
            name,
        )
        if not handle:
            _raise_last_error("CreateFileMappingW")
        address = _kernel32.MapViewOfFile(handle, _FILE_MAP_READ_WRITE, 0, 0, size)
        if not address:
            _kernel32.CloseHandle(handle)
            _raise_last_error("MapViewOfFile")
        return cls(name=name, size=size, _handle=handle, _address=address)

    @classmethod
    def open(cls, name: str, size: int) -> "WindowsSharedMemoryRegion":
        if not _HAS_WINDOWS_NATIVE:
            raise NativeIpcError("native shared memory is only implemented for Windows in this build")
        if size <= 0:
            raise NativeIpcError("shared-memory region size must be greater than zero")
        handle = _kernel32.OpenFileMappingW(_FILE_MAP_READ_WRITE, False, name)
        if not handle:
            _raise_last_error("OpenFileMappingW")
        address = _kernel32.MapViewOfFile(handle, _FILE_MAP_READ_WRITE, 0, 0, size)
        if not address:
            _kernel32.CloseHandle(handle)
            _raise_last_error("MapViewOfFile")
        return cls(name=name, size=size, _handle=handle, _address=address)

    def read(self, offset: int, length: int) -> bytes:
        self._validate_range(offset, length)
        return ctypes.string_at(self._address + offset, length)

    def write(self, offset: int, data: bytes) -> None:
        self._validate_range(offset, len(data))
        ctypes.memmove(self._address + offset, bytes(data), len(data))

    def close(self) -> None:
        if self._address:
            _kernel32.UnmapViewOfFile(self._address)
            self._address = 0
        if self._handle:
            _kernel32.CloseHandle(self._handle)
            self._handle = 0

    def _validate_range(self, offset: int, length: int) -> None:
        if offset < 0 or length < 0 or offset + length > self.size:
            raise NativeIpcError("shared-memory read/write range is out of bounds")

    def __enter__(self) -> "WindowsSharedMemoryRegion":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


@dataclass
class WindowsNotificationEvent:
    name: str
    _handle: int

    @classmethod
    def create(cls, name: str) -> "WindowsNotificationEvent":
        if not _HAS_WINDOWS_NATIVE:
            raise NativeIpcError("native notification events are only implemented for Windows in this build")
        handle = _kernel32.CreateEventW(None, False, False, name)
        if not handle:
            _raise_last_error("CreateEventW")
        return cls(name=name, _handle=handle)

    @classmethod
    def open(cls, name: str) -> "WindowsNotificationEvent":
        if not _HAS_WINDOWS_NATIVE:
            raise NativeIpcError("native notification events are only implemented for Windows in this build")
        handle = _kernel32.OpenEventW(_SYNCHRONIZE | _EVENT_MODIFY_STATE, False, name)
        if not handle:
            _raise_last_error("OpenEventW")
        return cls(name=name, _handle=handle)

    def set(self) -> None:
        if not _kernel32.SetEvent(self._handle):
            _raise_last_error("SetEvent")

    def wait(self, timeout_ms: int) -> bool:
        result = _kernel32.WaitForSingleObject(self._handle, timeout_ms)
        if result == _WAIT_OBJECT_0:
            return True
        if result == _WAIT_TIMEOUT:
            return False
        _raise_last_error("WaitForSingleObject")

    def close(self) -> None:
        if self._handle:
            _kernel32.CloseHandle(self._handle)
            self._handle = 0

    def __enter__(self) -> "WindowsNotificationEvent":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


@dataclass
class PosixSharedMemoryRegion:
    name: str
    size: int
    _fd: int
    _address: int
    _owner: bool = False

    @classmethod
    def create(cls, name: str, size: int) -> "PosixSharedMemoryRegion":
        if not _HAS_POSIX_NATIVE:
            raise NativeIpcError("native shared memory is only implemented for Windows/Linux/macOS in this build")
        if size <= 0:
            raise NativeIpcError("shared-memory region size must be greater than zero")
        name_bytes = _posix_name(name)
        with _temporary_umask(0):
            fd = _libc.shm_open(name_bytes, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        if fd < 0:
            _raise_errno("shm_open")
        if _libc.fchmod(fd, 0o600) != 0:
            error = ctypes.get_errno()
            _libc.close(fd)
            _libc.shm_unlink(name_bytes)
            raise NativeIpcError(f"fchmod failed with errno {error}: {os.strerror(error)}")
        if _libc.ftruncate(fd, size) != 0:
            error = ctypes.get_errno()
            _libc.close(fd)
            _libc.shm_unlink(name_bytes)
            raise NativeIpcError(f"ftruncate failed with errno {error}: {os.strerror(error)}")
        return cls._map_fd(name, size, fd, owner=True)

    @classmethod
    def open(cls, name: str, size: int) -> "PosixSharedMemoryRegion":
        if not _HAS_POSIX_NATIVE:
            raise NativeIpcError("native shared memory is only implemented for Windows/Linux/macOS in this build")
        if size <= 0:
            raise NativeIpcError("shared-memory region size must be greater than zero")
        name_bytes = _posix_name(name)
        fd = _libc.shm_open(name_bytes, os.O_RDWR, 0o600)
        if fd < 0:
            _raise_errno("shm_open")
        return cls._map_fd(name, size, fd, owner=False)

    @classmethod
    def _map_fd(cls, name: str, size: int, fd: int, *, owner: bool) -> "PosixSharedMemoryRegion":
        address = _libc.mmap(None, size, _PROT_READ | _PROT_WRITE, _MAP_SHARED, fd, 0)
        if address == _MAP_FAILED:
            error = ctypes.get_errno()
            _libc.close(fd)
            if owner:
                _libc.shm_unlink(_posix_name(name))
            raise NativeIpcError(f"mmap failed with errno {error}: {os.strerror(error)}")
        return cls(name=name, size=size, _fd=fd, _address=address, _owner=owner)

    def read(self, offset: int, length: int) -> bytes:
        self._validate_range(offset, length)
        return ctypes.string_at(self._address + offset, length)

    def write(self, offset: int, data: bytes) -> None:
        self._validate_range(offset, len(data))
        ctypes.memmove(self._address + offset, bytes(data), len(data))

    def close(self) -> None:
        if self._address:
            _libc.munmap(self._address, self.size)
            self._address = 0
        if self._fd >= 0:
            _libc.close(self._fd)
            self._fd = -1
        if self._owner:
            _libc.shm_unlink(_posix_name(self.name))
            self._owner = False

    def _validate_range(self, offset: int, length: int) -> None:
        if offset < 0 or length < 0 or offset + length > self.size:
            raise NativeIpcError("shared-memory read/write range is out of bounds")

    def __enter__(self) -> "PosixSharedMemoryRegion":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


@dataclass
class PosixNotificationEvent:
    name: str
    _sem: int
    _owner: bool = False

    @classmethod
    def create(cls, name: str) -> "PosixNotificationEvent":
        if not _HAS_POSIX_NATIVE:
            raise NativeIpcError("native notification events are only implemented for Windows/Linux/macOS in this build")
        name_bytes = _posix_name(name)
        with _temporary_umask(0):
            sem = _libc.sem_open(name_bytes, os.O_CREAT | os.O_EXCL, 0o600, 0)
        if sem == _SEM_FAILED:
            _raise_errno("sem_open")
        return cls(name=name, _sem=sem, _owner=True)

    @classmethod
    def open(cls, name: str) -> "PosixNotificationEvent":
        if not _HAS_POSIX_NATIVE:
            raise NativeIpcError("native notification events are only implemented for Windows/Linux/macOS in this build")
        name_bytes = _posix_name(name)
        sem = _libc.sem_open(name_bytes, 0, 0, 0)
        if sem == _SEM_FAILED:
            _raise_errno("sem_open")
        return cls(name=name, _sem=sem, _owner=False)

    def set(self) -> None:
        if _libc.sem_post(self._sem) != 0:
            _raise_errno("sem_post")

    def wait(self, timeout_ms: int) -> bool:
        deadline = time.monotonic() + max(timeout_ms, 0) / 1000
        while True:
            if _libc.sem_trywait(self._sem) == 0:
                return True
            code = ctypes.get_errno()
            if code == errno.EINTR:
                continue
            if code != errno.EAGAIN:
                raise NativeIpcError(f"sem_trywait failed with errno {code}: {os.strerror(code)}")
            now = time.monotonic()
            if now >= deadline:
                return False
            time.sleep(min(0.01, deadline - now))

    def close(self) -> None:
        if self._sem:
            _libc.sem_close(self._sem)
            self._sem = 0
        if self._owner:
            _libc.sem_unlink(_posix_name(self.name))
            self._owner = False

    def __enter__(self) -> "PosixNotificationEvent":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def open_shared_memory_region(name: str, size: int):
    if _HAS_WINDOWS_NATIVE:
        return WindowsSharedMemoryRegion.open(name, size)
    if _HAS_POSIX_NATIVE:
        return PosixSharedMemoryRegion.open(name, size)
    raise NativeIpcError("native shared memory is not implemented on this platform")


def create_shared_memory_region(name: str, size: int):
    if _HAS_WINDOWS_NATIVE:
        return WindowsSharedMemoryRegion.create(name, size)
    if _HAS_POSIX_NATIVE:
        return PosixSharedMemoryRegion.create(name, size)
    raise NativeIpcError("native shared memory is not implemented on this platform")


def open_notification_event(name: Optional[str]):
    if not name:
        return None
    if _HAS_WINDOWS_NATIVE:
        return WindowsNotificationEvent.open(name)
    if _HAS_POSIX_NATIVE:
        return PosixNotificationEvent.open(name)
    raise NativeIpcError("native notification events are not implemented on this platform")


def create_notification_event(name: str):
    if _HAS_WINDOWS_NATIVE:
        return WindowsNotificationEvent.create(name)
    if _HAS_POSIX_NATIVE:
        return PosixNotificationEvent.create(name)
    raise NativeIpcError("native notification events are not implemented on this platform")
