"""Opt-in JSONL traces for Python/C++ behavior parity work.

The recorder intentionally has no global instance and reads no environment
variables.  Callers must construct and inject a :class:`ParityTraceRecorder`.
This keeps the production path completely disabled unless tracing is requested.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import datetime as _datetime
import functools
import hashlib
import inspect
import json
import math
import os
import re
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, IO, Optional, Union


SCHEMA = "baas.parity-trace"
SCHEMA_VERSION = 1
REDACTED = "<redacted>"

_SENSITIVE_KEY = re.compile(
    r"(?:password|passwd|pwd|token|secret|authorization|cookie|"
    r"api[_-]?key|private[_-]?key|access[_-]?key)",
    re.IGNORECASE,
)
_SENSITIVE_TEXT = re.compile(
    r"(?i)\b(password|passwd|pwd|token|secret|"
    r"api[_-]?key|private[_-]?key|access[_-]?key)\b"
    r"([\"']?\s*[:=]\s*)"
    r"(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s,;}\]]+)"
)
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_AUTH_HEADER = re.compile(
    r"(?i)\b(authorization|proxy-authorization|cookie|set-cookie)\b"
    r"([\"']?\s*[:=]\s*)[^\r\n]+"
)


@dataclass(frozen=True)
class ImageFixture:
    """Marks image-like data for hash-only serialization.

    ``fixture_ref`` is a stable replay fixture identifier or repository-relative
    path.  The wrapped pixels are never written to the trace.
    """

    value: Any
    fixture_ref: Optional[str] = None


class SafeSerializer:
    """Deterministic, bounded serialization that defaults to revealing less."""

    def __init__(
        self,
        *,
        max_depth: int = 8,
        max_items: int = 128,
        max_nodes: int = 4096,
        max_string_chars: int = 4096,
        max_binary_bytes: int = 16 * 1024 * 1024,
    ) -> None:
        if (
            max_depth < 1
            or max_items < 1
            or max_nodes < 1
            or max_string_chars < 1
            or max_binary_bytes < 1
        ):
            raise ValueError("serializer bounds must be positive")
        self.max_depth = max_depth
        self.max_items = max_items
        self.max_nodes = max_nodes
        self.max_string_chars = max_string_chars
        self.max_binary_bytes = max_binary_bytes

    def serialize(self, value: Any) -> Any:
        return self._serialize(
            value, depth=0, seen=set(), remaining_nodes=[self.max_nodes]
        )

    def _serialize(
        self,
        value: Any,
        *,
        depth: int,
        seen: set[int],
        remaining_nodes: list[int],
    ) -> Any:
        if remaining_nodes[0] == 0:
            return {"$truncated": "max_nodes", "$type": self._type_name(value)}
        remaining_nodes[0] -= 1
        if value is None or isinstance(value, (bool, int)):
            return value
        if isinstance(value, float):
            if math.isfinite(value):
                return value
            return {"$type": "float", "value": str(value)}
        if isinstance(value, str):
            return self._safe_string(value)
        if isinstance(value, ImageFixture):
            return self._image(value.value, value.fixture_ref)
        if isinstance(value, (bytes, bytearray, memoryview)):
            return self._hashed_buffer(memoryview(value), kind="binary")
        if isinstance(value, (Path, os.PathLike)):
            return {
                "$type": "path",
                "value": self._safe_string(os.fspath(value)),
            }
        if isinstance(value, (_datetime.datetime, _datetime.date, _datetime.time)):
            return {"$type": type(value).__name__, "value": value.isoformat()}
        if isinstance(value, BaseException):
            return {
                "$type": "exception",
                "class": self._type_name(value),
                "message": self._safe_string(str(value)),
            }
        if depth >= self.max_depth:
            return {"$truncated": "max_depth", "$type": self._type_name(value)}

        identity = id(value)
        if identity in seen:
            return {"$ref": "cycle", "$type": self._type_name(value)}

        if isinstance(value, Mapping):
            seen.add(identity)
            try:
                if len(value) > self.max_items:
                    return {
                        "$type": "mapping",
                        "$truncated": "max_items",
                        "size": len(value),
                    }
                items = [
                    (*self._mapping_key(key), child)
                    for key, child in value.items()
                ]
                items.sort(key=lambda item: (item[0], item[1]))
                result: dict[str, Any] = {}
                key_counts: dict[str, int] = {}
                for text_key, sensitive, child in items:
                    if remaining_nodes[0] == 0:
                        result["$truncated"] = "max_nodes"
                        break
                    occurrence = key_counts.get(text_key, 0) + 1
                    key_counts[text_key] = occurrence
                    if occurrence > 1:
                        text_key = f"{text_key}#{occurrence}"
                    if sensitive:
                        result[text_key] = REDACTED
                    else:
                        result[text_key] = self._serialize(
                            child,
                            depth=depth + 1,
                            seen=seen,
                            remaining_nodes=remaining_nodes,
                        )
                return result
            finally:
                seen.remove(identity)

        if isinstance(value, (list, tuple, set, frozenset)):
            seen.add(identity)
            try:
                if isinstance(value, (set, frozenset)):
                    if len(value) > self.max_items:
                        return {
                            "$type": self._type_name(value),
                            "$truncated": "max_items",
                            "size": len(value),
                        }
                    encoded = []
                    for item in value:
                        if remaining_nodes[0] == 0:
                            encoded.append({"$truncated": "max_nodes"})
                            break
                        encoded.append(
                            self._serialize(
                                item,
                                depth=depth + 1,
                                seen=seen,
                                remaining_nodes=remaining_nodes,
                            )
                        )
                    encoded.sort(
                        key=lambda item: json.dumps(
                            item,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    )
                else:
                    encoded = []
                    for item in value[: self.max_items]:
                        if remaining_nodes[0] == 0:
                            encoded.append({"$truncated": "max_nodes"})
                            break
                        encoded.append(
                            self._serialize(
                                item,
                                depth=depth + 1,
                                seen=seen,
                                remaining_nodes=remaining_nodes,
                            )
                        )
                if len(value) > self.max_items:
                    encoded.append(
                        {
                            "$truncated": "max_items",
                            "omitted": len(value) - self.max_items,
                        }
                    )
                return encoded
            finally:
                seen.remove(identity)

        # numpy arrays and compatible image buffers are recognized without a
        # numpy import, keeping this core utility usable in minimal test envs.
        if all(hasattr(value, attr) for attr in ("shape", "dtype", "tobytes")):
            return self._image(value, fixture_ref=None)

        # Unknown instances are represented by type only. repr() is avoided:
        # it is commonly nondeterministic and may leak credentials or addresses.
        return {"$type": self._type_name(value)}

    def _image(self, value: Any, fixture_ref: Optional[str]) -> dict[str, Any]:
        declared_size = getattr(value, "nbytes", None)
        if declared_size is not None and int(declared_size) > self.max_binary_bytes:
            result = self._oversized_buffer(int(declared_size), kind="image")
            return self._add_image_metadata(result, value, fixture_ref)
        try:
            raw = memoryview(value)
            result = self._hashed_buffer(raw, kind="image")
        except TypeError:
            if declared_size is None:
                result = {
                    "$type": "image",
                    "$truncated": "unknown_buffer_size",
                }
                return self._add_image_metadata(result, value, fixture_ref)
            try:
                raw = value.tobytes(order="C")
            except TypeError:
                raw = value.tobytes()
            except AttributeError:
                raw = bytes(value)
            result = self._hashed_buffer(memoryview(raw), kind="image")
        return self._add_image_metadata(result, value, fixture_ref)

    def _add_image_metadata(
        self, result: dict[str, Any], value: Any, fixture_ref: Optional[str]
    ) -> dict[str, Any]:
        shape = getattr(value, "shape", None)
        dtype = getattr(value, "dtype", None)
        if shape is not None:
            result["shape"] = [int(part) for part in shape]
        if dtype is not None:
            result["dtype"] = self._safe_string(str(dtype))
        if fixture_ref is not None:
            result["fixture_ref"] = self._safe_string(fixture_ref)
        return result

    def _hashed_buffer(self, raw: memoryview, *, kind: str) -> dict[str, Any]:
        if raw.nbytes > self.max_binary_bytes:
            return self._oversized_buffer(raw.nbytes, kind=kind)
        try:
            octets = raw.cast("B")
        except TypeError:
            octets = memoryview(raw.tobytes())
        return {
            "$type": kind,
            "sha256": hashlib.sha256(octets).hexdigest(),
            "size_bytes": octets.nbytes,
        }

    @staticmethod
    def _oversized_buffer(size: int, *, kind: str) -> dict[str, Any]:
        return {
            "$type": kind,
            "$truncated": "max_binary_bytes",
            "size_bytes": size,
        }

    def _safe_string(self, value: str) -> Union[str, dict[str, Any]]:
        original_length = len(value)
        prefix = self._redact_text(value[: self.max_string_chars])
        if original_length <= self.max_string_chars:
            return prefix
        return {
            "$type": "string",
            "prefix": prefix,
            "length": original_length,
            "truncated": True,
        }

    def _mapping_key(self, value: Any) -> tuple[str, bool]:
        if type(value) is not str:
            type_name = self._type_name(value)[: self.max_string_chars]
            return f"<key:{type_name}>", True
        prefix = value[: self.max_string_chars]
        text_key = self._redact_text(prefix)
        if len(value) > self.max_string_chars:
            return text_key + "<truncated-key>", True
        return text_key, _SENSITIVE_KEY.search(value) is not None

    @staticmethod
    def _type_name(value: Any) -> str:
        value_type = type(value)
        return f"{value_type.__module__}.{value_type.__qualname__}"

    @staticmethod
    def _redact_text(value: str) -> str:
        value = _AUTH_HEADER.sub(
            lambda match: match.group(1) + match.group(2) + REDACTED,
            value,
        )
        value = _BEARER.sub("Bearer " + REDACTED, value)
        value = _SENSITIVE_TEXT.sub(
            lambda match: match.group(1) + match.group(2) + REDACTED,
            value,
        )
        return value


class HostOperationSpan:
    """Context manager for one begin/end, error, or cancellation pair."""

    def __init__(
        self,
        recorder: "ParityTraceRecorder",
        operation: str,
        params: Any,
        metadata: Any,
    ) -> None:
        self._recorder = recorder
        self.operation = operation
        self.params = params
        self.metadata = metadata
        self.operation_id: Optional[str] = None
        self._started_ns: Optional[int] = None
        self._result: Any = None
        self._cancel_reason: Optional[str] = None

    def __enter__(self) -> "HostOperationSpan":
        self._started_ns = self._recorder._read_monotonic_ns()
        self.operation_id = self._recorder._new_operation_id()
        self._recorder._emit(
            "host.begin",
            monotonic_ns=self._started_ns,
            operation=self.operation,
            operation_id=self.operation_id,
            params=self.params,
            metadata=self.metadata,
        )
        return self

    def set_result(self, result: Any) -> Any:
        self._result = result
        return result

    def cancel(self, reason: str = "cancelled") -> None:
        self._cancel_reason = reason

    def __exit__(self, exc_type: Any, exc: Any, _traceback: Any) -> bool:
        ended_ns = self._recorder._read_monotonic_ns()
        started_ns = ended_ns if self._started_ns is None else self._started_ns
        duration_ns = max(0, ended_ns - started_ns)
        common = {
            "operation": self.operation,
            "operation_id": self.operation_id,
            "duration_ns": duration_ns,
        }
        if exc is None and self._cancel_reason is None:
            self._recorder._emit(
                "host.end",
                monotonic_ns=ended_ns,
                result=self._result,
                **common,
            )
        elif self._cancel_reason is not None or _is_cancellation(exc):
            self._recorder._emit(
                "host.cancel",
                monotonic_ns=ended_ns,
                reason=self._cancel_reason or exc,
                **common,
            )
        else:
            self._recorder._emit(
                "host.error",
                monotonic_ns=ended_ns,
                error=exc,
                **common,
            )
        return False


class ParityTraceRecorder:
    """Thread-safe, bounded JSONL recorder.

    A recorder writes synchronously so an event is durable in sequence order.
    ``max_events`` and ``max_event_bytes`` bound output; over-limit events are
    counted in :attr:`dropped_events`.  Recorder failures never replace a host
    operation's exception.
    """

    def __init__(
        self,
        destination: Union[str, os.PathLike[str], IO[str]],
        *,
        session_id: Optional[str] = None,
        task_id: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
        monotonic_ns: Optional[Callable[[], int]] = None,
        wall_time_ns: Optional[Callable[[], int]] = None,
        clock_id: str = "python.time",
        rng_id: str = "python.numpy.default",
        rng_injected: bool = False,
        serializer: Optional[SafeSerializer] = None,
        max_events: int = 100_000,
        max_event_bytes: int = 64 * 1024,
        auto_flush: bool = False,
    ) -> None:
        if max_events < 1 or max_event_bytes < 512:
            raise ValueError("trace bounds are too small")
        self.session_id = SafeSerializer._redact_text(
            (session_id or uuid.uuid4().hex)[:64]
        )
        self.task_id = (
            SafeSerializer._redact_text(task_id[:64])
            if task_id is not None
            else None
        )
        self.serializer = serializer or SafeSerializer()
        self.max_events = max_events
        self.max_event_bytes = max_event_bytes
        self.auto_flush = auto_flush
        self._monotonic_ns = monotonic_ns or time.monotonic_ns
        self._wall_time_ns = wall_time_ns or time.time_ns
        self._lock = threading.RLock()
        self._seq = 0
        self._operation_seq = 0
        self._event_count = 0
        self._dropped_events = 0
        self._write_errors = 0
        self._closed = False
        self._owns_stream = not hasattr(destination, "write")
        if self._owns_stream:
            self._stream = open(destination, "w", encoding="utf-8", newline="\n")
        else:
            self._stream = destination  # type: ignore[assignment]

        self._emit(
            "session.start",
            wall_time_ns=int(self._wall_time_ns()),
            metadata=metadata or {},
            clock={
                "id": clock_id,
                "monotonic_injected": monotonic_ns is not None,
                "wall_injected": wall_time_ns is not None,
            },
            rng={"id": rng_id, "injected": bool(rng_injected)},
        )

    @property
    def dropped_events(self) -> int:
        with self._lock:
            return self._dropped_events

    @property
    def write_errors(self) -> int:
        with self._lock:
            return self._write_errors

    def host_operation(
        self,
        operation: str,
        params: Any = None,
        *,
        metadata: Any = None,
    ) -> HostOperationSpan:
        return HostOperationSpan(self, operation, params, metadata)

    def trace_host_operation(
        self,
        operation: Optional[str] = None,
        *,
        params: Optional[Callable[..., Any]] = None,
        result: Optional[Callable[[Any], Any]] = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Decorate a synchronous function as a host operation.

        By default, bound named arguments excluding ``self``/``cls`` are used
        as parameters.  Factories allow callers to expose a smaller safe view.
        """

        def decorate(function: Callable[..., Any]) -> Callable[..., Any]:
            signature = inspect.signature(function)
            operation_name = operation or function.__qualname__

            def call_parameters(args: Any, kwargs: Any) -> Any:
                if params is not None:
                    return params(*args, **kwargs)
                bound = signature.bind_partial(*args, **kwargs)
                bound.apply_defaults()
                return {
                    key: value
                    for key, value in bound.arguments.items()
                    if key not in ("self", "cls")
                }

            if inspect.iscoroutinefunction(function):

                @functools.wraps(function)
                async def async_wrapped(*args: Any, **kwargs: Any) -> Any:
                    with self.host_operation(
                        operation_name, call_parameters(args, kwargs)
                    ) as span:
                        return_value = await function(*args, **kwargs)
                        span.set_result(result(return_value) if result else return_value)
                        return return_value

                return async_wrapped

            @functools.wraps(function)
            def wrapped(*args: Any, **kwargs: Any) -> Any:
                with self.host_operation(
                    operation_name, call_parameters(args, kwargs)
                ) as span:
                    return_value = function(*args, **kwargs)
                    span.set_result(result(return_value) if result else return_value)
                    return return_value

            return wrapped

        return decorate

    def record_task(self, event: str, metadata: Optional[Mapping[str, Any]] = None) -> None:
        """Record task lifecycle metadata (for example ``task.start``)."""

        if not event.startswith("task."):
            raise ValueError("task event names must start with 'task.'")
        self._emit(event, metadata=metadata or {})

    def flush(self) -> None:
        with self._lock:
            if not self._closed:
                try:
                    self._stream.flush()
                except Exception:
                    self._write_errors += 1

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._emit_locked(
                "session.end",
                monotonic_ns=self._read_monotonic_ns(),
                dropped_events=self._dropped_events,
                write_errors=self._write_errors,
            )
            try:
                self._stream.flush()
            except Exception:
                self._write_errors += 1
            if self._owns_stream:
                try:
                    self._stream.close()
                except Exception:
                    self._write_errors += 1
            self._closed = True

    def __enter__(self) -> "ParityTraceRecorder":
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> bool:
        self.close()
        return False

    def _read_monotonic_ns(self) -> int:
        try:
            return int(self._monotonic_ns())
        except Exception:
            with self._lock:
                self._write_errors += 1
            return 0

    def _new_operation_id(self) -> str:
        with self._lock:
            self._operation_seq += 1
            return f"op-{self._operation_seq}"

    def _emit(
        self,
        event: str,
        *,
        monotonic_ns: Optional[int] = None,
        **payload: Any,
    ) -> Optional[int]:
        with self._lock:
            return self._emit_locked(event, monotonic_ns=monotonic_ns, **payload)

    def _emit_locked(
        self,
        event: str,
        *,
        monotonic_ns: Optional[int] = None,
        **payload: Any,
    ) -> Optional[int]:
        if self._closed or self._event_count >= self.max_events:
            self._dropped_events += 1
            return None
        try:
            self._seq += 1
            record = {
                "schema": SCHEMA,
                "schema_version": SCHEMA_VERSION,
                "session_id": self.session_id,
                "task_id": self.task_id,
                "seq": self._seq,
                "monotonic_ns": (
                    self._read_monotonic_ns() if monotonic_ns is None else monotonic_ns
                ),
                "event": SafeSerializer._redact_text(event[:128]),
                "payload": self.serializer.serialize(payload),
            }
            encoded = self._encode_bounded(record)
            self._stream.write(encoded + "\n")
            if self.auto_flush:
                self._stream.flush()
            self._event_count += 1
            return self._seq
        except Exception:
            self._write_errors += 1
            self._dropped_events += 1
            return None

    def _encode_bounded(self, record: dict[str, Any]) -> str:
        encoded = json.dumps(
            record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        size = len(encoded.encode("utf-8"))
        if size <= self.max_event_bytes:
            return encoded
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        record["payload"] = {
            "$truncated": "max_event_bytes",
            "original_size_bytes": size,
            "sha256": digest,
        }
        record["event"] = record["event"][:32]
        encoded = json.dumps(
            record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        if len(encoded.encode("utf-8")) <= self.max_event_bytes:
            return encoded
        # Multibyte identifiers can still overflow the smallest supported
        # envelope. Preserve stable correlations through short hashes.
        record["session_id"] = "sha256:" + hashlib.sha256(
            record["session_id"].encode("utf-8")
        ).hexdigest()[:16]
        if record["task_id"] is not None:
            record["task_id"] = "sha256:" + hashlib.sha256(
                record["task_id"].encode("utf-8")
            ).hexdigest()[:16]
        record["event"] = "trace.truncated"
        return json.dumps(
            record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )


def _is_cancellation(exc: Any) -> bool:
    return isinstance(
        exc,
        (
            asyncio.CancelledError,
            concurrent.futures.CancelledError,
        ),
    )


__all__ = [
    "ImageFixture",
    "ParityTraceRecorder",
    "SafeSerializer",
    "SCHEMA",
    "SCHEMA_VERSION",
]
