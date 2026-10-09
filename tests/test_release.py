"""Distribution guards used by CI and the publishing workflow."""

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("release_tools", Path(__file__).resolve().parents[1] / "scripts/release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


@pytest.mark.parametrize(
    "name",
    [
        "/tmp/leak", "../leak", "pkg/../../leak", "C:/leak", "pkg\\leak", "pkg/.env.local", "pkg/.meta/data", "pkg/a.pyc",
        "evals/data/decisions.jsonl", "evals/data/trial.semantic-v1.jsonl",
    ],
)
def test_archive_rejects_unsafe_or_private_members(name):
    with pytest.raises(ValueError):
        release.safe_path(name)


@pytest.mark.parametrize("tag", ["main", "v0.1.0", "0.1.0a1", "v0.1.0a1/extra"])
def test_release_tag_must_match_literal_package_version(tmp_path, monkeypatch, tag):
    monkeypatch.setattr(release, "version", lambda: "0.1.0a1")
    with pytest.raises(ValueError, match="tag"):
        release.check(tmp_path, tag)


def test_extra_artifact_refuses_release_before_opening_archives(tmp_path, monkeypatch):
    monkeypatch.setattr(release, "version", lambda: "0.1.0a1")
    for name in ("ex_regex-0.1.0a1-py3-none-any.whl", "ex_regex-0.1.0a1.tar.gz", "ex_regex-0.1.0.tar.gz"):
        (tmp_path / name).touch()
    with pytest.raises(ValueError, match="exactly"):
        release.check(tmp_path)
