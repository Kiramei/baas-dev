# Python/C++ parity trace (schema v1)

The parity trace is an opt-in JSON Lines stream used to compare Python behavior
with the C++ rewrite. It is disabled unless code explicitly constructs a
`ParityTraceRecorder` and injects it with `Baas_thread.set_parity_trace()`.
There is no environment-variable or configuration-file auto-enable path.

## Envelope

Every line is one UTF-8 JSON object with deterministic key ordering:

```json
{
  "schema": "baas.parity-trace",
  "schema_version": 1,
  "session_id": "run-123",
  "task_id": "cafe",
  "seq": 2,
  "monotonic_ns": 981234,
  "event": "host.begin",
  "payload": {}
}
```

`seq` is unique, monotonic, and represents the actual append order across all
threads. `monotonic_ns` comes from the declared session clock and is suitable
for duration/order diagnostics, not as a wall-clock timestamp.

## Events

| Event | Required payload fields |
| --- | --- |
| `session.start` | `wall_time_ns`, `metadata`, `clock`, `rng` |
| `session.end` | `dropped_events`, `write_errors` |
| `task.*` | `metadata` |
| `host.begin` | `operation`, `operation_id`, `params`, `metadata` |
| `host.end` | `operation`, `operation_id`, `duration_ns`, `result` |
| `host.error` | `operation`, `operation_id`, `duration_ns`, `error` |
| `host.cancel` | `operation`, `operation_id`, `duration_ns`, `reason` |

`clock.id`, `clock.monotonic_injected`, `clock.wall_injected`, `rng.id`, and
`rng.injected` declare replay substitutions. They do not imply that all legacy
code already consumes those injected sources.

## Safe values and replay fixtures

Mappings and unordered collections larger than their item bound are summarized
without traversing their contents; bounded mappings are sorted by key, while
bounded sequences retain their prefix. Nesting, strings, binary/image hashing,
events, and event bytes have configured bounds. Unknown objects serialize as type names, avoiding
nondeterministic `repr()` output. Non-finite floats use tagged values.

Keys such as password, token, secret, authorization, cookie, and API/private or
access keys are recursively replaced with `<redacted>`. Common inline forms and
Bearer credentials in strings are redacted too.

Bytes within the configured hashing bound are represented by SHA-256 and size;
larger buffers are size-only truncated records. Numpy-compatible arrays are treated
as images and represented by SHA-256, byte size, shape, and dtype. Pixel data is
never written. Wrap an image in `ImageFixture(image, "fixtures/name.png")` to add
a stable fixture reference that a replay implementation can resolve.

## Current host facade integration

`Baas_thread` traces logical `baas.click`, `baas.swipe`, and `baas.screenshot`
operations after explicit recorder injection. Click completion means the legacy
call returned; only the default non-waiting threaded click path reports
`{"scheduled": true}` and does not claim the worker has completed. Nemu clicks
and calls with `wait_over=true` report `{"scheduled": false}`. Existing
clock/RNG calls inside legacy operations are only identified, not replaced in
this foundation stage.
