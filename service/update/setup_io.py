from __future__ import annotations

from pathlib import Path
from typing import Any, Union

try:  # Python 3.11+
    import tomllib  # type: ignore[import]
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 fallback
    import tomli as tomllib  # type: ignore[import]

from .setup_schema import CURRENT_DEFAULT_SETTINGS, migrate_to_current_schema


def _dump_toml(data: dict[str, Any], file) -> None:
    try:
        import tomli_w

        tomli_w.dump(data, file)
        return
    except ModuleNotFoundError:
        file.write(_fallback_toml(data).encode("utf-8"))


def _fallback_toml(data: dict[str, Any]) -> str:
    lines: list[str] = []
    scalar_items = {key: value for key, value in data.items() if not isinstance(value, dict)}
    for key, value in scalar_items.items():
        lines.append(f"{key} = {_toml_value(value)}")
    for section, values in data.items():
        if not isinstance(values, dict):
            continue
        if lines:
            lines.append("")
        lines.append(f"[{section}]")
        for key, value in values.items():
            if isinstance(value, dict):
                continue
            lines.append(f"{key} = {_toml_value(value)}")
    return "\n".join(lines) + "\n"


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if value is None:
        return '""'
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def read_setup_toml(setup_path: Union[Path, None] = None) -> tuple[dict[str, Any], Union[Path, None]]:
    """Load `setup.toml`, creating it from defaults when missing.

    Args:
        setup_path: Optional explicit TOML path. Defaults to `Path.cwd() / "setup.toml"`.

    Returns:
        A tuple of parsed TOML content and the path that was read.
    """
    path = setup_path or (Path.cwd() / "setup.toml")
    if not path.exists():
        with path.open("wb") as file:
            _dump_toml(CURRENT_DEFAULT_SETTINGS, file)

    with path.open("rb") as fp:
        data = migrate_to_current_schema(tomllib.load(fp))
    with path.open("wb") as file:
        _dump_toml(data, file)
    return data, path


def write_setup_toml(content: dict, setup_path: Union[Path, None] = None) -> None:
    """Persist setup configuration to TOML.

    Args:
        content: TOML-serializable setup configuration.
        setup_path: Optional explicit TOML path. Defaults to `Path.cwd() / "setup.toml"`.
    """
    path = setup_path or (Path.cwd() / "setup.toml")
    if not path.exists():
        with path.open("wb") as file:
            _dump_toml(CURRENT_DEFAULT_SETTINGS, file)

    with path.open("wb") as fp:
        _dump_toml(migrate_to_current_schema(content), fp)
