from __future__ import annotations

import io
import subprocess
import zipfile

from deploy.installer.const import GetShaMethod
from service.update import checks


def test_remote_sha_uses_temporary_bare_repo_without_git(monkeypatch, tmp_path):
    created = {}

    class FakeRemote:
        def ls_remotes(self):
            return [
                {"name": "refs/heads/other", "oid": "0" * 40},
                {"name": "refs/heads/master", "oid": "1" * 40},
            ]

    class FakeRemotes:
        def create_anonymous(self, url):
            created["url"] = url
            return FakeRemote()

    class FakeRepo:
        remotes = FakeRemotes()

    def fake_repository(_path):
        raise AssertionError("remote SHA probing must not require cwd to be a git repository")

    def fake_init_repository(path, bare):
        created["path"] = path
        created["bare"] = bare
        return FakeRepo()

    monkeypatch.setattr(checks.shutil, "which", lambda _name: None)
    monkeypatch.setattr(checks.pygit2, "Repository", fake_repository)
    monkeypatch.setattr(checks.pygit2, "init_repository", fake_init_repository)

    handler = checks.GitOperationHandler(tmp_path / "not-a-repo")
    sha = handler.get_remote_latest_sha("https://example.invalid/repo.git", "master")

    assert sha == "1" * 40
    assert created["url"] == "https://example.invalid/repo.git"
    assert created["bare"] is True


def test_android_repo_sha_uses_archive_probe_for_baas_cdn(monkeypatch):
    requested_urls = []

    class FakeResponse:
        def __init__(self, url):
            self.url = url

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def raise_for_status(self):
            return None

        def iter_content(self, chunk_size=1):
            yield b"x"

        def json(self):
            return {"commit": {"sha": "455caf" + "0" * 34}}

    def fake_get(url, **kwargs):
        requested_urls.append((url, kwargs))
        return FakeResponse(url)

    def fail_git_wrapper(_config, _timeout):
        raise AssertionError("Android GitHub proxy checks must not use pygit2/git")

    monkeypatch.setenv("BAAS_ANDROID", "1")
    monkeypatch.setattr(checks.requests, "get", fake_get)
    monkeypatch.setattr(checks, "_git_wrapper_get_latest_sha", fail_git_wrapper)

    result = checks.test_repo_sha(
        {
            "name": "baas_cdn",
            "method": GetShaMethod.PYGIT2,
            "url": "https://baas-cdn.kiramei.workers.dev/https://github.com/Kiramei/baas-dev.git",
            "branch": "master",
        },
        timeout=3.0,
    )

    assert result["success"] is True
    assert result["value"] == "455caf" + "0" * 34
    assert requested_urls[0][0] == (
        "https://baas-cdn.kiramei.workers.dev/"
        "https://github.com/Kiramei/baas-dev/archive/refs/heads/master.zip"
    )
    assert requested_urls[0][1]["stream"] is True
    assert requested_urls[1][0] == "https://api.github.com/repos/Kiramei/baas-dev/branches/master"


def test_githubfast_archive_url_is_generated_from_accelerated_repo_url():
    archive_url = checks._github_archive_url_for_config(
        {
            "name": "githubfast",
            "url": "https://githubfast.com/Kiramei/baas-dev.git",
            "branch": "master",
        }
    )

    assert archive_url == "https://githubfast.com/Kiramei/baas-dev/archive/refs/heads/master.zip"


def test_non_github_android_sha_keeps_git_wrapper(monkeypatch):
    monkeypatch.setenv("BAAS_ANDROID", "1")
    monkeypatch.setattr(checks, "_git_wrapper_get_latest_sha", lambda _config, _timeout: (False, "git failed"))

    result = checks.test_repo_sha(
        {
            "name": "gitee",
            "method": GetShaMethod.PYGIT2,
            "url": "https://gitee.com/kiramei/baas-dev.git",
            "branch": "master",
        },
        timeout=3.0,
    )

    assert result["success"] is False
    assert result["error"] == "git failed"
    assert result["order"] == -1


def test_repo_sha_marks_failed_source_with_disabled_order(monkeypatch):
    monkeypatch.setattr(checks, "_git_wrapper_get_latest_sha", lambda _config, _timeout: (False, "auth failed"))

    result = checks.test_repo_sha(
        {
            "name": "gitee",
            "method": GetShaMethod.PYGIT2,
            "url": "https://gitee.com/kiramei/baas-dev.git",
            "branch": "master",
            "order": 2,
        },
        timeout=3.0,
    )

    assert result["success"] is False
    assert result["order"] == -1


def test_remote_sha_auth_failure_does_not_fallback_to_pygit2(monkeypatch, tmp_path):
    def fake_run_git_cmd(_self, _args, timeout=None):
        raise subprocess.CalledProcessError(
            returncode=128,
            cmd="git ls-remote",
            stderr="fatal: could not read Username for 'https://gitee.com': terminal prompts disabled",
        )

    def fail_init_repository(*_args, **_kwargs):
        raise AssertionError("credential failures should be final for this source")

    monkeypatch.setattr(checks.shutil, "which", lambda _name: "git")
    monkeypatch.setattr(checks.GitOperationHandler, "_run_git_cmd", fake_run_git_cmd)
    monkeypatch.setattr(checks.pygit2, "init_repository", fail_init_repository)

    handler = checks.GitOperationHandler(tmp_path)

    try:
        handler.get_remote_latest_sha("https://gitee.com/kiramei/baas-dev.git", "master")
    except ValueError as exc:
        assert "authentication failed" in str(exc).lower()
    else:
        raise AssertionError("credential failure should be reported")


def test_check_for_update_switches_failed_saved_sha_method(monkeypatch, tmp_path):
    setup_path = tmp_path / "setup.toml"

    def fake_get_local_version():
        return (
            checks.VersionInfo(version="0" * 40, source="setup.toml", path=setup_path),
            {"general": {"no_update": False, "get_remote_sha_method": "gitee", "channel": "stable"}},
            "master",
        )

    methods = [
        {"name": "github", "method": GetShaMethod.GITHUB_API, "order": 0},
        {"name": "gitee", "method": GetShaMethod.PYGIT2, "order": 1},
    ]
    saved = {}

    def fake_test_repo_sha(config, timeout):
        if config["name"] == "gitee":
            return {"name": "gitee", "success": False, "value": None, "error": "auth failed", "order": -1}
        return {"name": "github", "success": True, "value": "1" * 40, "error": None, "order": 0}

    monkeypatch.setattr(checks, "get_local_version", fake_get_local_version)
    monkeypatch.setattr(checks, "get_remote_sha_methods_for_channel", lambda _channel: [dict(item) for item in methods])
    monkeypatch.setattr(checks, "test_repo_sha", fake_test_repo_sha)
    monkeypatch.setattr(checks, "write_setup_toml", lambda data, path: saved.update({"data": data, "path": path}))

    result = checks.check_for_update(timeout=3.0)

    assert result["remote"] == "1" * 40
    assert result["method"] == "github"
    assert saved["data"]["general"]["get_remote_sha_method"] == "github"


def test_android_update_to_latest_skips_archive_when_sha_is_current(monkeypatch, tmp_path):
    setup_path = tmp_path / "setup.toml"
    current_sha = "1" * 40
    setup_path.write_text(
        f"""
[general]
current_baas_sha = "{current_sha}"
channel = "stable"
no_update = false
get_remote_sha_method = "github"
""".strip(),
        encoding="utf-8",
    )

    def fail_get(*_args, **_kwargs):
        raise AssertionError("current Android backend must not download archive again")

    monkeypatch.setenv("BAAS_ANDROID", "1")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(checks, "_github_api_get_latest_sha", lambda _source, _timeout: (True, current_sha))
    monkeypatch.setattr(
        checks,
        "_android_archive_config",
        lambda _data, _channel: {
            "name": "github",
            "url": "https://github.com/Kiramei/baas-dev.git",
            "branch": "master",
        },
    )
    monkeypatch.setattr(checks.requests, "get", fail_get)

    events = []
    result = checks.update_to_latest_with_progress(setup_path, progress=lambda stage, payload: events.append(stage))

    assert result["status"] == "skipped"
    assert result["reason"] == "latest"
    assert "download_start" not in events


def test_android_update_to_latest_updates_old_backend_from_archive(monkeypatch, tmp_path):
    setup_path = tmp_path / "setup.toml"
    old_sha = "0" * 40
    new_sha = "2" * 40
    setup_path.write_text(
        f"""
[general]
current_baas_sha = "{old_sha}"
channel = "stable"
no_update = false
get_remote_sha_method = "github"

[paths]
baas_root_path = "."
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / "service").mkdir()
    (tmp_path / "service" / "old.py").write_text("old", encoding="utf-8")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "service_auth.json").write_text("keep", encoding="utf-8")

    archive_bytes = io.BytesIO()
    with zipfile.ZipFile(archive_bytes, "w") as archive:
        archive.writestr("baas-dev-master/service/new.py", "new")
        archive.writestr("baas-dev-master/setup.toml", "should not overwrite")
        archive.writestr("baas-dev-master/config/service_auth.json", "should not overwrite")
    archive_payload = archive_bytes.getvalue()

    class FakeResponse:
        headers = {"content-length": str(len(archive_payload))}

        def raise_for_status(self):
            return None

        def iter_content(self, chunk_size=1):
            for index in range(0, len(archive_payload), chunk_size):
                yield archive_payload[index:index + chunk_size]

    def fake_get(url, **kwargs):
        assert url == "https://codeload.github.com/Kiramei/baas-dev/zip/refs/heads/master"
        assert kwargs["stream"] is True
        return FakeResponse()

    monkeypatch.setenv("BAAS_ANDROID", "1")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(checks, "_github_api_get_latest_sha", lambda _source, _timeout: (True, new_sha))
    monkeypatch.setattr(
        checks,
        "_android_archive_config",
        lambda _data, _channel: {
            "name": "github",
            "url": "https://github.com/Kiramei/baas-dev.git",
            "branch": "master",
        },
    )
    monkeypatch.setattr(checks.requests, "get", fake_get)

    events = []
    result = checks.update_to_latest_with_progress(
        setup_path,
        progress=lambda stage, payload: events.append((stage, payload)),
    )

    assert result["status"] == "updated"
    assert result["current"] == new_sha
    assert (tmp_path / "service" / "new.py").read_text(encoding="utf-8") == "new"
    assert not (tmp_path / "service" / "old.py").exists()
    assert (tmp_path / "config" / "service_auth.json").read_text(encoding="utf-8") == "keep"
    assert setup_path.read_text(encoding="utf-8").count(new_sha) == 1
    assert [stage for stage, _payload in events] == [
        "read_setup",
        "fetch_sha",
        "remote_sha",
        "download_start",
        "download_done",
        "extract_start",
        "copy_start",
        "copy_done",
        "write_setup",
        "done",
    ]
