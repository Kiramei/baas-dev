from __future__ import annotations

import asyncio
import hashlib
import io
import json
import threading

import pytest

from core.Baas_thread import Baas_thread
from core.parity_trace import ImageFixture, ParityTraceRecorder, SafeSerializer


class StepClock:
    def __init__(self, start=0, step=10):
        self.value = start
        self.step = step
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            value = self.value
            self.value += self.step
            return value


class FakeImage:
    shape = (2, 2, 3)
    dtype = "uint8"

    def __init__(self, raw):
        self.raw = raw

    def tobytes(self, order="C"):
        assert order == "C"
        return self.raw


def records(stream):
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_baas_thread_trace_is_absent_by_default_and_screenshot_behavior_is_unchanged():
    calls = []

    class Screenshot:
        @staticmethod
        def screenshot():
            calls.append("capture")
            return "raw-image"

    thread = Baas_thread.__new__(Baas_thread)
    thread.flag_run = True
    thread.screenshot = Screenshot()
    thread.normalize_screenshot = lambda image: calls.append(("normalize", image)) or "normalized"

    assert not hasattr(thread, "parity_trace")
    assert thread.get_screenshot_array() == "normalized"
    assert calls == ["capture", ("normalize", "raw-image")]
    assert not hasattr(thread, "parity_trace")


def test_baas_thread_screenshot_hook_is_opt_in_and_hashes_result():
    raw = bytes(range(12))
    image = FakeImage(raw)
    stream = io.StringIO()
    recorder = ParityTraceRecorder(stream, session_id="hook", wall_time_ns=lambda: 0)

    class Screenshot:
        @staticmethod
        def screenshot():
            return image

    thread = Baas_thread.__new__(Baas_thread)
    thread.flag_run = True
    thread.screenshot = Screenshot()
    thread.normalize_screenshot = lambda value: value
    thread.set_parity_trace(recorder)

    assert thread.get_screenshot_array() is image
    recorder.close()
    end = next(item for item in records(stream) if item["event"] == "host.end")
    assert end["payload"]["operation"] == "baas.screenshot"
    assert end["payload"]["result"]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert "raw" not in end["payload"]["result"]


def test_schema_sequence_metadata_and_injected_clock_rng_markers():
    stream = io.StringIO()
    clock = StepClock(100, 10)
    recorder = ParityTraceRecorder(
        stream,
        session_id="session-1",
        task_id="task-7",
        metadata={"module": "cafe"},
        monotonic_ns=clock,
        wall_time_ns=lambda: 1234,
        clock_id="fixture.clock",
        rng_id="fixture.rng",
        rng_injected=True,
    )
    with recorder.host_operation("host.click", {"x": 1}) as span:
        span.set_result({"ok": True})
    recorder.record_task("task.end", {"status": "ok"})
    recorder.close()

    output = records(stream)
    assert [item["seq"] for item in output] == list(range(1, len(output) + 1))
    assert [item["event"] for item in output] == [
        "session.start",
        "host.begin",
        "host.end",
        "task.end",
        "session.end",
    ]
    assert all(item["schema"] == "baas.parity-trace" for item in output)
    assert all(item["schema_version"] == 1 for item in output)
    assert all(item["session_id"] == "session-1" for item in output)
    assert all(item["task_id"] == "task-7" for item in output)
    session = output[0]["payload"]
    assert session["clock"] == {
        "id": "fixture.clock",
        "monotonic_injected": True,
        "wall_injected": True,
    }
    assert session["rng"] == {"id": "fixture.rng", "injected": True}
    assert session["wall_time_ns"] == 1234
    assert output[1]["payload"]["operation_id"] == output[2]["payload"]["operation_id"]
    assert output[2]["payload"]["duration_ns"] == 10


def test_decorator_records_named_parameters_result_and_exception():
    stream = io.StringIO()
    recorder = ParityTraceRecorder(stream, session_id="decorator", wall_time_ns=lambda: 0)

    @recorder.trace_host_operation("math.divide")
    def divide(numerator, denominator=2):
        return numerator / denominator

    assert divide(8) == 4
    with pytest.raises(ZeroDivisionError):
        divide(1, denominator=0)
    recorder.close()

    output = records(stream)
    begins = [item for item in output if item["event"] == "host.begin"]
    assert begins[0]["payload"]["params"] == {"denominator": 2, "numerator": 8}
    assert any(item["event"] == "host.end" for item in output)
    error = next(item for item in output if item["event"] == "host.error")
    assert error["payload"]["error"]["class"] == "builtins.ZeroDivisionError"


def test_cancel_event_is_distinct_and_exception_is_not_swallowed():
    stream = io.StringIO()
    recorder = ParityTraceRecorder(stream, session_id="cancel", wall_time_ns=lambda: 0)
    with pytest.raises(asyncio.CancelledError):
        with recorder.host_operation("host.wait"):
            raise asyncio.CancelledError("stop")
    recorder.close()
    output = records(stream)
    assert "host.cancel" in [item["event"] for item in output]
    assert "host.error" not in [item["event"] for item in output]


def test_concurrent_writers_have_unique_monotonic_sequence_and_operation_pairs():
    stream = io.StringIO()
    recorder = ParityTraceRecorder(stream, session_id="threads", wall_time_ns=lambda: 0)
    barrier = threading.Barrier(9)

    def worker(index):
        barrier.wait()
        with recorder.host_operation("host.concurrent", {"index": index}) as span:
            span.set_result(index)

    workers = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
    for worker_thread in workers:
        worker_thread.start()
    barrier.wait()
    for worker_thread in workers:
        worker_thread.join()
    recorder.close()

    output = records(stream)
    assert [item["seq"] for item in output] == list(range(1, len(output) + 1))
    operations = [item for item in output if item["event"].startswith("host.")]
    operation_ids = [item["payload"]["operation_id"] for item in operations]
    assert len(set(operation_ids)) == 8
    assert all(operation_ids.count(operation_id) == 2 for operation_id in set(operation_ids))


def test_sensitive_values_are_redacted_and_images_are_hash_only_with_fixture_ref():
    raw = bytes(range(12))
    serializer = SafeSerializer()
    result = serializer.serialize(
        {
            "token": "do-not-write",
            "nested": {"api_key": "also-secret"},
            "message": "authorization=abcdef",
            "image": ImageFixture(FakeImage(raw), "fixtures/home-screen.png"),
        }
    )

    encoded = json.dumps(result, sort_keys=True)
    assert "do-not-write" not in encoded
    assert "also-secret" not in encoded
    assert "abcdef" not in encoded
    assert result["token"] == "<redacted>"
    assert result["message"] == "authorization=<redacted>"
    image = result["image"]
    assert image == {
        "$type": "image",
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
        "shape": [2, 2, 3],
        "dtype": "uint8",
        "fixture_ref": "fixtures/home-screen.png",
    }
    assert raw.hex() not in encoded


def test_bounds_and_flush_are_observable_without_unbounded_output():
    class FlushTrackingStream(io.StringIO):
        flush_count = 0

        def flush(self):
            self.flush_count += 1
            return super().flush()

    stream = FlushTrackingStream()
    recorder = ParityTraceRecorder(
        stream,
        session_id="bounded",
        wall_time_ns=lambda: 0,
        max_events=3,
        max_event_bytes=512,
    )
    with recorder.host_operation("host.large", {"text": "x" * 10_000}):
        pass
    recorder.record_task("task.end")
    recorder.flush()
    recorder.close()

    output = records(stream)
    assert len(output) == 3
    assert recorder.dropped_events >= 2
    assert stream.flush_count >= 2
    assert all(len(line.encode("utf-8")) <= 512 for line in stream.getvalue().splitlines())
    assert output[1]["payload"]["$truncated"] == "max_event_bytes"


def test_minimum_event_bound_also_caps_large_envelope_identifiers():
    stream = io.StringIO()
    recorder = ParityTraceRecorder(
        stream,
        session_id="s" * 10_000,
        task_id="t" * 10_000,
        wall_time_ns=lambda: 0,
        max_event_bytes=512,
    )
    recorder.record_task("task." + "event" * 10_000, {"value": "x" * 10_000})
    recorder.close()

    assert all(len(line.encode("utf-8")) <= 512 for line in stream.getvalue().splitlines())
