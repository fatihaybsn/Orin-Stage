from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sys
import tarfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
DEPENDENCIES = REPO_ROOT / "release" / "dependencies"
MANIFEST = DEPENDENCIES / "cargo-sources.lock.json"
MATURIN_LOCK = DEPENDENCIES / "cargo-locks" / "maturin-1.9.0.Cargo.lock"
RPDS_LOCK = DEPENDENCIES / "cargo-locks" / "rpds-py-0.30.0.Cargo.lock"
TOOL = REPO_ROOT / "tools" / "release" / "fetch_cargo_sources.py"

spec = importlib.util.spec_from_file_location("fetch_cargo_sources", TOOL)
assert spec is not None and spec.loader is not None
cargo_sources = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = cargo_sources
spec.loader.exec_module(cargo_sources)


def _manifest_payload(entries: list[dict[str, object]]) -> dict[str, object]:
    return {"schema_version": 1, "cargo_locks": [], "sources": entries}


def _write_manifest(path: Path, entries: list[dict[str, object]]) -> None:
    path.write_text(json.dumps(_manifest_payload(entries)), encoding="utf-8")


def _entry(payload: bytes, name: str = "example", version: str = "1.0.0") -> dict[str, object]:
    filename = f"{name}-{version}.crate"
    return {
        "name": name,
        "version": version,
        "source": cargo_sources.CRATES_IO_SOURCE,
        "filename": filename,
        "url": f"https://static.crates.io/crates/{name}/{filename}",
        "sha256": hashlib.sha256(payload).hexdigest(),
        "consumers": ["maturin"],
    }


def _crate(name: str = "example", version: str = "1.0.0") -> bytes:
    output = io.BytesIO()
    files = {
        "Cargo.toml": (
            f'[package]\nname = "{name}"\nversion = "{version}"\n'
            'license = "MIT"\nrepository = "https://example.invalid/repo"\n'
        ).encode(),
        "src/lib.rs": b"pub fn answer() -> u8 { 42 }\n",
    }
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        root = tarfile.TarInfo(f"{name}-{version}")
        root.type = tarfile.DIRTYPE
        archive.addfile(root)
        for relative, payload in files.items():
            member = tarfile.TarInfo(f"{name}-{version}/{relative}")
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
    return output.getvalue()


def test_authoritative_cargo_locks_parse_to_exact_union() -> None:
    maturin = cargo_sources.parse_cargo_lock(MATURIN_LOCK, "maturin")
    rpds = cargo_sources.parse_cargo_lock(RPDS_LOCK, "rpds-py")
    union = cargo_sources.merge_lock_sources((maturin, rpds))

    assert (len(maturin.registry), maturin.local_count) == (371, 1)
    assert (len(rpds.registry), rpds.local_count) == (26, 1)
    assert len(union) == 395
    assert len({entry.identity for entry in union}) == 395
    assert union == cargo_sources.load_manifest(MANIFEST)


def test_cargo_lock_hashes_and_consumer_attribution_are_locked() -> None:
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    records = {record["consumer"]: record for record in payload["cargo_locks"]}
    assert hashlib.sha256(MATURIN_LOCK.read_bytes()).hexdigest() == records["maturin"]["lock_sha256"]
    assert hashlib.sha256(RPDS_LOCK.read_bytes()).hexdigest() == records["rpds-py"]["lock_sha256"]
    assert {tuple(source["consumers"]) for source in payload["sources"]} <= {
        ("maturin",),
        ("rpds-py",),
        ("maturin", "rpds-py"),
    }
    assert sum(source["consumers"] == ["maturin", "rpds-py"] for source in payload["sources"]) == 2
    assert all(len(source["sha256"]) == 64 for source in payload["sources"])
    assert all(source["url"].startswith("https://static.crates.io/") for source in payload["sources"])
    assert all(not source["source"].startswith("git+") for source in payload["sources"])


def test_git_dependency_fails_and_path_dependency_is_not_fetched(tmp_path: Path) -> None:
    git_lock = tmp_path / "git.lock"
    git_lock.write_text(
        '[[package]]\nname = "gitcrate"\nversion = "1.0.0"\n'
        'source = "git+https://example.invalid/repo#deadbeef"\n',
        encoding="utf-8",
    )
    with pytest.raises(cargo_sources.CargoSourceError, match="Git dependency"):
        cargo_sources.parse_cargo_lock(git_lock, "maturin")

    path_lock = tmp_path / "path.lock"
    path_lock.write_text(
        '[[package]]\nname = "local"\nversion = "1.0.0"\n', encoding="utf-8"
    )
    analysis = cargo_sources.parse_cargo_lock(path_lock, "rpds-py")
    assert analysis.registry == ()
    assert analysis.local_count == 1


def test_downloader_reuses_cache_and_rejects_wrong_hash(tmp_path: Path) -> None:
    payload = b"verified crate"
    entry = _entry(payload)
    manifest = tmp_path / "manifest.json"
    cache = tmp_path / "cache"
    cache.mkdir()
    cached = cache / entry["filename"]
    cached.write_bytes(payload)
    _write_manifest(manifest, [entry])

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("valid cache must not use network")

    results = cargo_sources.fetch_sources(manifest, cache, opener=forbidden)
    assert results[0].reused is True
    cached.write_bytes(b"wrong")
    with pytest.raises(cargo_sources.CargoSourceError, match="verification failed"):
        cargo_sources.fetch_sources(manifest, cache, opener=forbidden)


def test_failed_download_leaves_no_final_or_partial_file(tmp_path: Path) -> None:
    entry = _entry(b"expected")
    manifest = tmp_path / "manifest.json"
    cache = tmp_path / "cache"
    _write_manifest(manifest, [entry])

    def opener(*_args: object, **_kwargs: object) -> io.BytesIO:
        return io.BytesIO(b"truncated")

    with pytest.raises(cargo_sources.CargoSourceError, match="verification failed"):
        cargo_sources.fetch_sources(manifest, cache, opener=opener)
    assert list(cache.iterdir()) == []


def test_standard_vendor_tree_is_exact_and_deterministic(tmp_path: Path) -> None:
    crate = _crate()
    entry = _entry(crate)
    manifest = tmp_path / "manifest.json"
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / entry["filename"]).write_bytes(crate)
    _write_manifest(manifest, [entry])

    summaries = []
    for suffix in ("one", "two"):
        summaries.append(
            cargo_sources.materialize_vendor(
                manifest,
                cache,
                tmp_path / f"vendor-{suffix}",
                tmp_path / f"config-{suffix}.toml",
                tmp_path / f"inventory-{suffix}.json",
                tmp_path / f"vendor-{suffix}.json",
            )
        )
    assert summaries[0]["tree_sha256"] == summaries[1]["tree_sha256"]
    assert summaries[0]["crate_count"] == 1
    package = tmp_path / "vendor-one" / "example-1.0.0"
    assert (package / "Cargo.toml").is_file()
    assert (package / ".cargo-checksum.json").is_file()
    cargo_sources.verify_vendor(cargo_sources.load_manifest(manifest), tmp_path / "vendor-one")


def test_release_inventory_and_vendor_digest_cover_exact_manifest() -> None:
    entries = cargo_sources.load_manifest(MANIFEST)
    inventory = json.loads((DEPENDENCIES / "CARGO_THIRD_PARTY.json").read_text(encoding="utf-8"))
    vendor_lock = json.loads((DEPENDENCIES / "cargo-vendor.lock.json").read_text(encoding="utf-8"))
    assert [(item["name"], item["version"], item["source"]) for item in inventory["sources"]] == [
        entry.identity for entry in entries
    ]
    assert all(item["license"] or item["license_file"] for item in inventory["sources"])
    assert all(
        set(item)
        == {
            "name",
            "version",
            "source",
            "license",
            "license_file",
            "repository",
            "consumers",
        }
        for item in inventory["sources"]
    )
    assert vendor_lock["crate_count"] == len(entries) == 395
    assert vendor_lock["missing_license_metadata"] == []
    assert len(vendor_lock["tree_sha256"]) == 64


def test_python_locks_contain_no_crate_source_entries() -> None:
    crate_names = {entry.name for entry in cargo_sources.load_manifest(MANIFEST)}
    for lock_name in ("runtime.lock", "build-tools.lock"):
        python_names = {
            line.partition("==")[0]
            for line in (DEPENDENCIES / lock_name).read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
        assert python_names.isdisjoint(crate_names)
