import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, Mock

import pygit2
import pytest


ENTRY_POINT = Path(__file__).resolve().parents[2] / "deploy/installer/installer.py"


@pytest.fixture
def load_installer(tmp_path, monkeypatch):
    """Keep optional terminal UI and logging out of the headless service tests."""
    for name, exports in {
        "alive_progress": {"alive_bar": MagicMock()},
        "halo": {"Halo": Mock()},
        "loguru": {"logger": Mock()},
        "easydict": {"EasyDict": lambda values: SimpleNamespace(**values)},
    }.items():
        module = ModuleType(name)
        module.__dict__.update(exports)
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PDM_IGNORE_ACTIVE_VENV", "1")

    def load(*, standalone=False):
        name = "installer_entry_test" if standalone else "deploy.installer.installer_entry_test"
        if standalone:
            monkeypatch.syspath_prepend(str(ENTRY_POINT.parent))
        spec = importlib.util.spec_from_file_location(name, ENTRY_POINT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    return load


def test_legacy_service_installer_reuses_shared_implementation():
    from core.ocr.baas_ocr_client import server_installer
    from service import android_ocr_installer

    assert android_ocr_installer is server_installer


@pytest.mark.parametrize("standalone", [False, True])
def test_installer_imports_without_starting_an_install(load_installer, tmp_path, standalone):
    installer = load_installer(standalone=standalone)

    assert callable(installer.main)
    assert installer.clone_repository.__module__.endswith("git_backend")
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("launch", [False, True])
@pytest.mark.parametrize("dynamic", [False, True])
def test_install_and_launch_lifecycle_order(load_installer, tmp_path, monkeypatch, launch, dynamic):
    installer = load_installer()
    config = SimpleNamespace(
        baas_root=tmp_path,
        General=SimpleNamespace(launch=launch, use_dynamic_update=dynamic),
    )
    events = []
    environment = Mock()
    for method in ("prepare_runtime", "install_requirements", "fix_shebangs"):
        getattr(environment, method).side_effect = lambda name=method: events.append(name)
    updater = Mock()
    updater.run.side_effect = lambda: events.append("update")
    launcher = Mock()
    launcher.run_app.side_effect = lambda: events.append("launch")
    launcher.dynamic_update_installer.side_effect = lambda: events.append("dynamic")
    monkeypatch.setattr(installer, "GlobalConfig", Mock(return_value=config))
    monkeypatch.setattr(installer, "EnvironmentManager", Mock(return_value=environment))
    monkeypatch.setattr(installer, "UpdateOrchestrator", Mock(return_value=updater))
    monkeypatch.setattr(installer, "AppLauncher", Mock(return_value=launcher))

    installer.main()

    expected = [] if launch else ["prepare_runtime", "update", "install_requirements", "fix_shebangs"]
    assert events == expected + ["dynamic" if dynamic else "launch"]
    if launch:
        installer.EnvironmentManager.assert_not_called()
        installer.UpdateOrchestrator.assert_not_called()
    else:
        installer.EnvironmentManager.assert_called_once_with(config)
        installer.UpdateOrchestrator.assert_called_once_with(config, environment)
    installer.AppLauncher.assert_called_once_with(config)


@pytest.mark.parametrize("system", ["Windows", "Linux"])
def test_dynamic_update_keeps_entrypoint_and_cli_arguments(load_installer, tmp_path, monkeypatch, system):
    installer = load_installer()
    monkeypatch.setattr(installer, "__system__", system)
    target = tmp_path / "deploy/installer/installer.py"
    target.parent.mkdir(parents=True)
    target.touch()
    python = tmp_path / (".venv/Scripts/python.exe" if system == "Windows" else ".env/bin/python")
    python.parent.mkdir(parents=True)
    python.touch()
    arguments = ["--launch", "--existing-option", "value with spaces"]
    monkeypatch.setattr(sys, "argv", ["BlueArchiveAutoScript", *arguments])
    run = Mock()
    monkeypatch.setattr(installer.subprocess, "run", run)
    launcher = installer.AppLauncher(SimpleNamespace(baas_root=tmp_path))

    with pytest.raises(SystemExit):
        launcher.dynamic_update_installer()

    run.assert_called_once_with([str(python), str(target), *arguments])


def test_windows_launcher_rebuilds_the_canonical_installer(load_installer, tmp_path, monkeypatch):
    installer = load_installer()
    monkeypatch.setattr(installer, "__system__", "Windows")
    target = tmp_path / "deploy/installer/installer.py"
    target.parent.mkdir(parents=True)
    target.write_text("# canonical installer\n", encoding="utf-8")
    upx = tmp_path / "toolkit/upx-4.2.4-win64/upx.exe"
    upx.parent.mkdir(parents=True)
    upx.touch()
    (tmp_path / "backup.exe").write_bytes(b"old backup")
    (tmp_path / "BlueArchiveAutoScript.exe").write_bytes(b"old launcher")
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist/BlueArchiveAutoScript.exe").write_bytes(b"rebuilt launcher")
    pyinstaller = ModuleType("PyInstaller")
    build = ModuleType("PyInstaller.__main__")
    build.run = Mock()
    pyinstaller.__main__ = build
    monkeypatch.setitem(sys.modules, "PyInstaller", pyinstaller)
    monkeypatch.setitem(sys.modules, "PyInstaller.__main__", build)
    monkeypatch.setattr(installer.subprocess, "Popen", Mock(return_value=SimpleNamespace(pid=123)))
    config = SimpleNamespace(
        baas_root=tmp_path,
        General=SimpleNamespace(runtime_path="custom-python", no_build=False, force_launch=True),
    )

    installer.AppLauncher(config).run_app()

    assert build.run.call_args.args[0][0] == str(target)
    assert (tmp_path / "BlueArchiveAutoScript.exe").read_bytes() == b"rebuilt launcher"
    assert (tmp_path / "backup.exe").read_bytes() == b"old launcher"


def test_installer_clones_and_updates_a_local_repository_without_losing_user_data(load_installer, tmp_path):
    installer = load_installer()
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    repo = pygit2.init_repository(str(upstream), initial_head="master")
    signature = pygit2.Signature("Installer Test", "installer@example.invalid")

    def commit(version, parents):
        (upstream / "version.txt").write_text(version, encoding="utf-8")
        repo.index.add("version.txt")
        repo.index.write()
        return repo.create_commit("HEAD", signature, signature, version, repo.index.write_tree(), parents)

    try:
        first = commit("first", [])
        install = tmp_path / "installed"
        profile = install / "config/profile.json"
        profile.parent.mkdir(parents=True)
        profile.write_text("user data", encoding="utf-8")
        handler = installer.GitOperationHandler(install, str(upstream))

        handler.clone(branch="master")
        assert handler.get_local_sha() == str(first)
        assert (install / "version.txt").read_text(encoding="utf-8") == "first"
        second = commit("second", [first])
        handler.update(branch="master")

        assert handler.get_local_sha() == str(second)
        assert (install / "version.txt").read_text(encoding="utf-8") == "second"
        assert profile.read_text(encoding="utf-8") == "user data"
    finally:
        repo.free()
