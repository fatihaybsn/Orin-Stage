from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_LOCK = REPO_ROOT / "release" / "dependencies" / "runtime.lock"
SOURCES_LOCK = REPO_ROOT / "release" / "dependencies" / "sources.lock.json"
FETCHER_PATH = REPO_ROOT / "tools" / "release" / "fetch_dependency_sources.py"
DIRECT_PACKAGES = {"jsonschema", "pyyaml"}

spec = importlib.util.spec_from_file_location("fetch_dependency_sources", FETCHER_PATH)
assert spec is not None and spec.loader is not None
fetcher = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = fetcher
spec.loader.exec_module(fetcher)


def _runtime_versions() -> dict[str, str]:
    return dict(
        line.split("==", 1)
        for line in RUNTIME_LOCK.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )


def _manifest() -> dict[str, object]:
    return json.loads(SOURCES_LOCK.read_text(encoding="utf-8"))


def _entry(payload: bytes) -> dict[str, object]:
    return {
        "name": "example",
        "version": "1.0",
        "filename": "example-1.0.tar.gz",
        "url": "https://files.pythonhosted.org/packages/example-1.0.tar.gz",
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size": len(payload),
        "requires_python": ">=3.10",
        "license": "MIT",
        "license_files": ["LICENSE"],
        "build_backend": "example.backend",
        "build_requires": ["example-builder>=1"],
        "runtime_role": "transitive",
    }


def _write_manifest(path: Path, entries: list[dict[str, object]]) -> None:
    path.write_text(
        json.dumps({"schema_version": 1, "sources": entries}),
        encoding="utf-8",
    )


def test_source_manifest_matches_runtime_lock_and_roles() -> None:
    manifest = _manifest()
    assert manifest["schema_version"] == 1
    sources = manifest["sources"]
    assert isinstance(sources, list)
    assert len(sources) == 7

    names = [entry["name"] for entry in sources]
    assert names == sorted(names)
    assert len(names) == len(set(names))
    assert {entry["name"]: entry["version"] for entry in sources} == (
        _runtime_versions()
    )
    assert {
        entry["name"]
        for entry in sources
        if entry["runtime_role"] == "direct"
    } == DIRECT_PACKAGES
    assert all(
        entry["runtime_role"] == "direct"
        if entry["name"] in DIRECT_PACKAGES
        else entry["runtime_role"] == "transitive"
        for entry in sources
    )


def test_source_manifest_contains_only_hashed_https_sdists() -> None:
    sources = _manifest()["sources"]
    assert isinstance(sources, list)
    for entry in sources:
        assert entry["url"].startswith("https://files.pythonhosted.org/")
        assert entry["filename"].endswith((".tar.gz", ".tar.bz2", ".tar.xz"))
        assert not entry["filename"].endswith(".whl")
        assert len(entry["sha256"]) == 64
        assert entry["sha256"] == entry["sha256"].lower()
        int(entry["sha256"], 16)
        assert entry["license_files"]
        assert entry["build_backend"]
        assert entry["build_requires"]


def test_manifest_loader_rejects_wheel_filename(tmp_path: Path) -> None:
    entry = _entry(b"archive")
    entry["filename"] = "example-1.0-py3-none-any.whl"
    entry["url"] = "https://files.pythonhosted.org/packages/example-1.0-py3-none-any.whl"
    manifest = tmp_path / "sources.json"
    _write_manifest(manifest, [entry])

    with pytest.raises(fetcher.SourceAcquisitionError, match="supported sdist"):
        fetcher.load_manifest(manifest)


def test_fetcher_reuses_verified_cached_source(tmp_path: Path) -> None:
    payload = b"verified source"
    entry = _entry(payload)
    manifest = tmp_path / "sources.json"
    downloads = tmp_path / "nested" / "downloads"
    downloads.mkdir(parents=True)
    cached = downloads / entry["filename"]
    cached.write_bytes(payload)
    _write_manifest(manifest, [entry])

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("verified cached source must not be downloaded")

    output = io.StringIO()
    assert fetcher.fetch_sources(
        manifest,
        downloads,
        opener=forbidden,
        output=output,
    ) == (cached,)
    assert "REUSE" in output.getvalue()
    assert "MATCH" in output.getvalue()


def test_fetcher_rejects_wrong_cached_hash(tmp_path: Path) -> None:
    entry = _entry(b"expected")
    manifest = tmp_path / "sources.json"
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    cached = downloads / entry["filename"]
    cached.write_bytes(b"wrong")
    _write_manifest(manifest, [entry])

    with pytest.raises(fetcher.SourceAcquisitionError, match="verification failed"):
        fetcher.fetch_sources(manifest, downloads)
    assert cached.read_bytes() == b"wrong"


def test_failed_download_leaves_no_final_or_partial_file(tmp_path: Path) -> None:
    entry = _entry(b"expected")
    manifest = tmp_path / "sources.json"
    downloads = tmp_path / "downloads"
    _write_manifest(manifest, [entry])

    def opener(*_args: object, **_kwargs: object) -> io.BytesIO:
        return io.BytesIO(b"truncated")

    with pytest.raises(fetcher.SourceAcquisitionError, match="verification failed"):
        fetcher.fetch_sources(manifest, downloads, opener=opener)
    assert not (downloads / entry["filename"]).exists()
    assert list(downloads.iterdir()) == []
