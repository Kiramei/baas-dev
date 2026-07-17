from __future__ import annotations

import asyncio
import hashlib
import io
import json
import threading
from collections.abc import Mapping
from types import SimpleNamespace

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
        self.nbytes = len(raw)

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


def test_baas_thread_click_trace_distinguishes_synchronous_nemu_from_scheduled_click():
    stream = io.StringIO()
    recorder = ParityTraceRecorder(stream, session_id="click", wall_time_ns=lambda: 0)
    thread = Baas_thread.__new__(Baas_thread)
    thread.flag_run = True
    calls = []
    thread.click_thread = lambda *args: calls.append(args)
    thread.set_parity_trace(recorder)

    thread.control = SimpleNamespace(method="nemu")
    thread.click(1, 2, wait_over=False)
    thread.control = SimpleNamespace(method="adb")
    thread.click(3, 4, wait_over=True)
    recorder.close()

    ends = [item for item in records(stream) if item["event"] == "host.end"]
    assert [item["payload"]["result"] for item in ends] == [
        {"scheduled": False},
        {"scheduled": False},
    ]
    assert calls == [(1, 2, 1, 0, 0), (3, 4, 1, 0, 0)]


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


def test_decorator_awaits_async_result_error_and_cancellation():
    stream = io.StringIO()
    recorder = ParityTraceRecorder(stream, session_id="async", wall_time_ns=lambda: 0)

    @recorder.trace_host_operation("async.ok")
    async def succeed(value):
        await asyncio.sleep(0)
        return value + 1

    @recorder.trace_host_operation("async.error")
    async def fail():
        await asyncio.sleep(0)
        raise RuntimeError("failure after await")

    @recorder.trace_host_operation("async.cancel")
    async def cancel():
        raise asyncio.CancelledError("stop")

    assert asyncio.run(succeed(4)) == 5
    with pytest.raises(RuntimeError):
        asyncio.run(fail())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(cancel())
    recorder.close()

    output = records(stream)
    assert [item["event"] for item in output].count("host.end") == 1
    assert [item["event"] for item in output].count("host.error") == 1
    assert [item["event"] for item in output].count("host.cancel") == 1


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
            "header_line": "Authorization: Bearer super-secret-token",
            "json_text": '{"password":"hunter2"}',
            "path": __import__("pathlib").Path("token=path-secret"),
            "image": ImageFixture(FakeImage(raw), "fixtures/token=fixture-secret.png"),
        }
    )

    encoded = json.dumps(result, sort_keys=True)
    assert "do-not-write" not in encoded
    assert "also-secret" not in encoded
    assert "abcdef" not in encoded
    assert "super-secret-token" not in encoded
    assert "hunter2" not in encoded
    assert "path-secret" not in encoded
    assert "fixture-secret" not in encoded
    late_sensitive_key = "x" * 5000 + "_token"
    bounded_key = serializer.serialize({late_sensitive_key: "late-key-secret"})
    assert "late-key-secret" not in json.dumps(bounded_key)
    assert result["token"] == "<redacted>"
    assert result["message"] == "authorization=<redacted>"
    image = result["image"]
    assert image == {
        "$type": "image",
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
        "shape": [2, 2, 3],
        "dtype": "uint8",
        "fixture_ref": "fixtures/token=<redacted>",
    }
    assert raw.hex() not in encoded

    stream = io.StringIO()
    recorder = ParityTraceRecorder(stream, session_id="safe", wall_time_ns=lambda: 0)
    recorder.record_task("task.token=event-secret")
    recorder.close()
    assert "event-secret" not in stream.getvalue()


def test_collection_bounds_do_not_copy_an_unbounded_sequence_prefix():
    class SliceOnlyList(list):
        def __iter__(self):
            raise AssertionError("bounded sequence serialization must use a slice")

    serializer = SafeSerializer(max_items=3)
    result = serializer.serialize(
        {
            "mapping": {str(index): index for index in range(10)},
            "sequence": SliceOnlyList(range(10)),
            "set": set(range(10)),
        }
    )

    assert result["sequence"] == [
        0,
        1,
        2,
        {"$truncated": "max_items", "omitted": 7},
    ]
    assert result["mapping"] == {
        "$type": "mapping",
        "$truncated": "max_items",
        "size": 10,
    }
    assert result["set"] == {
        "$type": "builtins.set",
        "$truncated": "max_items",
        "size": 10,
    }


def test_binary_hashing_has_an_input_work_bound():
    serializer = SafeSerializer(max_binary_bytes=4)
    assert serializer.serialize(b"12345") == {
        "$type": "binary",
        "$truncated": "max_binary_bytes",
        "size_bytes": 5,
    }


def test_global_node_budget_stops_repeated_shared_subtrees():
    serializer = SafeSerializer(max_items=128, max_nodes=5)
    shared = [[{"value": 1}] * 128] * 128

    result = serializer.serialize(shared)
    encoded = json.dumps(result, sort_keys=True)

    assert '"$truncated": "max_nodes"' in encoded
    assert len(encoded) < 512


def test_exhausted_node_budget_never_hides_omitted_sequence_items():
    serializer = SafeSerializer(max_items=1, max_nodes=2)

    assert serializer.serialize([1, 2]) == [
        1,
        {"$truncated": "max_items", "omitted": 1},
    ]


def test_mapping_keys_are_bounded_without_calling_unknown_stringifiers():
    class ExplosiveKey:
        def __str__(self):
            raise AssertionError("unknown mapping keys must not be stringified")

    serializer = SafeSerializer(max_string_chars=16)
    result = serializer.serialize(
        {
            "x" * 100_000 + "_token": "long-key-secret",
            ExplosiveKey(): "unknown-key-secret",
        }
    )
    encoded = json.dumps(result, sort_keys=True)

    assert "long-key-secret" not in encoded
    assert "unknown-key-secret" not in encoded
    assert "<truncated-key>" in encoded
    assert len(encoded) < 256


def test_normalized_mapping_key_collisions_are_insertion_order_independent():
    generated_key = 1
    matching_text_key = "<key:builtins.int>"
    serializer = SafeSerializer()

    first = serializer.serialize(
        {generated_key: "hidden", matching_text_key: "visible"}
    )
    second = serializer.serialize(
        {matching_text_key: "visible", generated_key: "hidden"}
    )

    assert first == second
    assert first == {
        matching_text_key: "visible",
        matching_text_key + "#2": "<redacted>",
    }


def test_large_metadata_is_not_copied_before_bounded_serialization():
    class OversizedMetadata(Mapping):
        def __len__(self):
            return 129

        def __iter__(self):
            raise AssertionError("oversized metadata must not be iterated")

        def __getitem__(self, key):
            raise AssertionError("oversized metadata must not be indexed")

    stream = io.StringIO()
    recorder = ParityTraceRecorder(
        stream,
        session_id="bounded-metadata",
        wall_time_ns=lambda: 0,
        metadata=OversizedMetadata(),
    )
    recorder.record_task("task.metadata", OversizedMetadata())
    recorder.close()

    output = records(stream)
    for event in output[:2]:
        assert event["payload"]["metadata"] == {
            "$type": "mapping",
            "$truncated": "max_items",
            "size": 129,
        }


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
