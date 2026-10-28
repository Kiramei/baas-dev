# Plugin system, API v1

The service owns plugin discovery, profile settings and jobs. Desktop, Android and WebUI use the same authenticated trigger commands and declarative interface. A plugin never supplies executable frontend JavaScript, HTML, Qt widgets or CSS. The host renders its names, schemas and results with the application's own components and theme.

The mechanisms in [upstream PR 583](https://github.com/pur1fying/blue_archive_auto_script/pull/583) informed this design. Its Qt UI and the UI changes in [PR 2](https://github.com/sakurairsora/blue_archive_auto_script/pull/2) are not part of this implementation. The old five tools are not silently imported or registered as compatible v1 extensions.

## Package and compatibility

A package contains `plugin.json` at its root and, for computation, ordinary Python source files. Bundled packages live under `service/plugins/bundled/<id>/`. User packages live under `plugins/<id>/`, outside the tracked source and outside other plugins' configuration namespaces. The client can upload a local ZIP after authentication; no pip commands, installation hooks or arbitrary download URLs are run.

`schema_version` describes the manifest format, `api_version` describes the host contract, and `version` is the plugin's own version. All three are separate. API v1 accepts schema/API integer `1`; incompatible versions are reported in the catalog. A stable, lowercase plugin ID may use namespaced dots, underscores or hyphens. IDs are never paths. Duplicate IDs cannot replace a bundled plugin or another installed plugin.

See `examples/plugins/example.notes/` for a complete installable package. Its computation entry point is `main:run`:

```python
def run(context, inputs):
    # Both arguments are plain JSON values. No service objects or auth data.
    prefix = context["settings"]["prefix"]
    return {"text": prefix + inputs["text"]}
```

The context contains `api_version`, `plugin_id`, `config_id` and the validated settings snapshot. It does not contain the login Key, session keys, core runtime, arbitrary configuration files or a device object. Return JSON-compatible values only; invalid, non-finite or excessively large output fails the job. Standard output is a worker protocol, not a UI rendering surface.

## Manifest

The required fields are `schema_version`, `api_version`, `id`, `version`, `name`, `description`, `permissions`, `settings_schema` and `actions`. Names and descriptions may be strings or locale maps such as `{"en": "Notes", "zh": "笔记"}`. Clients use the selected language, then English, then an available translation.

An action has a stable `id`, localized `name`, optional description, an `execution` kind and an `input_schema`. A `compute` action declares `entrypoint: "main:run"` and the `compute` permission. It executes in a separate, terminable Python process. A `device_task` action declares a host-supported `task_name` and the `device.tasks` permission. It has no Python entry point and cannot introduce an arbitrary core function name.

Settings and action inputs use a deliberately restricted JSON Schema subset: object properties, required keys and `additionalProperties: false`; bounded arrays with an item schema; strings, booleans, numbers and integers; defaults, enums, numeric bounds, titles and descriptions. Unsupported schema keywords or versions are rejected instead of silently interpreted with different semantics. The service validates every write and run; client-side validation only improves feedback.

An optional `contributions.home` array contains `{id, title, description?, action_id}` entries. Each entry references a declared action. The host may show a shortcut for an enabled plugin; opening it does not automatically execute the action. The manifest cannot inject arbitrary UI or override an existing application route. New execution kinds, permissions, schema features and contribution types require explicit host capability negotiation before use.

## Bundled automation capability

Hosts advertising `host.automation.v1` also accept `execution: "host_automation"` for a closed list of shipped tool/action pairs. These actions require `device.tasks` and declare neither an entry point nor a task name. External ZIPs cannot request this execution mode, even by copying a bundled ID. The host dispatches its own adapter, never Python loaded from the plugin package into the device process.

`baas.auto-push` provides independent mission lists, story episodes and formation attributes. Its current-page actions recognize supported mission/story screens and stop on unknown pages. `baas.hoard-ap` provides a pure `plan` action and a `start` action that owns the profile until completion or cancellation, with a maximum seven-day deadline. It does not rewrite scheduler events: another task in that profile receives `already-running`/`DEVICE_BUSY`. Temporary task overrides are restored on success, failure and cancellation, while actual inventory updates remain persistent. Long timelines do not occupy a compute-worker slot. Backend restart requires the user to replan and start again.

Hoarding results distinguish `available_at_target` from the projected remaining `summary`. The timeline tracks natural recovery caps, cafe capacity, server-day resets, already purchased AP and mail deadlines. Premium purchases, selective collection of an oversized mailbox and overflow gains that cannot be verified from the AP counter are manual steps. Such a plan reports `executable: false` with explicit warning codes; `start` rejects it before automation. The adapter requires fresh AP OCR and verifies each claimed gain, including small amounts, rather than treating the host's OCR fallback or a task's boolean return as proof of collection.

Club sign-in delivers AP to a mail message, rather than directly increasing character AP. A planned `club` step preserves character AP and known mail inventory, declaring `expected_mail_ap`, `delivery: "mail"` and `verification: "manual_mail"`. Top-level `pending_mail_ap` is the unverified delivery estimate and is excluded from target assets and totals. A plan containing such steps reports `CLUB_REWARD_REQUIRES_MAIL_VERIFICATION` and cannot start automatically. After manual sign-in, verify each mail's amount and actual expiry, enter it once in `mail`, set today's `club_ap` to zero, and replan. No fixed club-mail lifetime is inferred from coarse day labels or from another source's overflow assumptions. The plan action has a closed output schema; `available_at_target` is omitted when the timeline stops before reaching the target. A source ending at AP 999 also stops for mailbox verification because the capped counter cannot prove that the full reward was received.

String schemas may declare `format: "date-time"` as a rendering hint. The client edits local date/time and sends an ISO 8601 instant with an explicit time zone. The hoarding tool uses its separate `server_utc_offset_minutes` and `reset_hour` for game-day boundaries, so a browser sending UTC does not shift the reset time. Empty initial values remain editable; the automation adapter validates required dates, time zones and the bounded planning horizon. Other format names and nonstring formats are rejected.

## Authenticated commands

All commands use the existing `CommandMessage` envelope on the authenticated trigger channel. A successful response retains `status: "ok"` and returns the following `data`. Errors retain `status: "error"`, a stable `code` and a human-readable `error` string. There is no unauthenticated plugin HTTP endpoint.

| Command | Scope and payload | Result |
| --- | --- | --- |
| `get_plugin_catalog` | Optional `config_id` | `{api_version: 1, capabilities, plugins, errors}` |
| `get_plugin_settings` | `config_id`, `{plugin_id}` | `{plugin_id, config_id, revision, values}` |
| `update_plugin_settings` | `config_id`, `{plugin_id, revision, values}` | Updated settings and revision |
| `set_plugin_enabled` | `config_id`, `{plugin_id, enabled, trust_code?}` | Plugin descriptor |
| `run_plugin_action` | `config_id`, `{plugin_id, action_id, input, request_id}` | Job snapshot |
| `get_plugin_job` | `config_id`, `{job_id}` | Job snapshot |
| `cancel_plugin_job` | `config_id`, `{job_id}` | Job snapshot |
| `install_plugin` | Binary ZIP upload with `{binary: true, filename?}` | Installed descriptor |
| `uninstall_plugin` | `{plugin_id}` | Removal result |

A descriptor includes the manifest and `source`, `enabled`, `trusted`, `state`, and an optional structured error. The catalog can be read without selecting a profile, but still requires an authenticated connection. Profile-dependent operations require a real existing configuration ID. Old services return an unsupported-command error, which clients present as an upgrade requirement.

## Settings and lifecycle

Settings are stored in the profile's separate `plugins.json` namespace. They are not appended to `ConfigSet` attributes, global defaults, `setup.toml` or `service_auth.json`. Atomic writes and monotonically increasing revisions prevent two clients from silently overwriting each other. On `REVISION_CONFLICT`, reload and explicitly reapply edits. Invalid JSON is preserved and reported rather than overwritten with empty defaults. Copy/export/import operations preserve optional plugin settings with the profile; import clears code trust and enablement.

Catalog discovery parses bounded JSON only. Disabled external plugins are not imported. Installing an external package leaves it disabled. Enabling external Python requires an explicit `trust_code: true` decision; trust is tied to the installed package, not merely its display name. Python plugins are trusted backend code, **not a security sandbox**. A child process supplies cancellation and failure isolation; it does not revoke operating-system access to files or the network. Install only code you trust.

Jobs report `queued`, `running`, `succeeded`, `failed` or `cancelled`, with identity, timestamps, optional JSON result and a structured error. A `request_id` deduplicates retries within the service's retained job window. Polling and cancellation are profile-scoped; another profile cannot retrieve or cancel a job by guessing its ID. Completed jobs and output have bounded retention. Computations are concurrency-limited and terminate on timeout/cancel/shutdown. Device tasks use the existing profile runtime lock, initialization, logging, Android display guard and stop lifecycle. A task already running in that profile is never replaced by a plugin action.

Running jobs prevent uninstall or unsafe enable/disable changes. Uninstall removes installed code and its registration, not unrelated profiles or application credentials. Bundled plugins cannot be removed. There is no automatic plugin update or dependency installation in v1.

The initial limits are 10 MiB per uploaded ZIP, 32 MiB unpacked, 256 package files/nodes, 16 directory levels, 100 discovered packages, two concurrent workers, eight active jobs and 128 retained jobs. Inputs/settings/results are bounded JSON; results are limited to 64 KiB. Computation timeouts default to 30 seconds and may be declared from 0.1 to 120 seconds. Device tasks default to 30 minutes, with a maximum of one hour. Linux/macOS use an owned process group; Windows assigns each worker to a Job Object before it receives executable input. Cleanup is a runtime responsibility on every supported transport.

Android's embedded Chaquopy runtime uses two private native service processes instead of launching `sys.executable` or forking the JVM. They execute the same standard-library worker protocol through bounded private files. Native leases bind cancellation to a slot, random token and process identity; an old cancellation cannot stop a replacement job. Each service owns its process group, watches its backend owner and has an independent hard deadline. Worker services are not exported or automatically restarted. An older Android client reports `ANDROID_WORKER_UNAVAILABLE` and needs updating; desktop and WebUI workers keep their existing launch path.

## Included plugins

The bundled budget calculators demonstrate independent settings, schema-generated inputs, cancellable computation and structured results. Their mathematical estimates do not claim to implement the old hoarding tool's game automation. The task launcher demonstrates approved existing device tasks using the currently selected profile's Configuration settings. It only starts when the user explicitly runs an action.

Future equipment solvers, hoarding policy hooks or new task adapters should declare a compatible API and capabilities first. In particular, resource interception must specify failure and cancellation behavior; silently bypassing a broken interception hook could spend a user's saved resources.
