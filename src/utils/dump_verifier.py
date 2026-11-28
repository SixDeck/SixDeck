from __future__ import annotations

import hashlib
import json
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

SUPPORTED_ALGORITHMS = ("sha256", "sha1", "md5", "crc32")


@dataclass
class FileChecksumResult:
    relative_path: str
    status: str  # "valid" | "corrupted" | "missing" | "unverified"
    algorithm: str
    actual: str = ""
    expected: str = ""
    size_bytes: int = 0
    expected_size: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "relative_path": self.relative_path,
            "status": self.status,
            "algorithm": self.algorithm,
            "actual": self.actual,
            "expected": self.expected,
            "size_bytes": self.size_bytes,
            "expected_size": self.expected_size,
        }


@dataclass
class DumpVerificationReport:
    status: str  # "valid" | "incomplete" | "corrupted" | "unverified" | "missing"
    dump_dir: str
    algorithm: str
    manifest_source: str = ""
    total_files: int = 0
    verified_files: int = 0
    valid_count: int = 0
    corrupted_count: int = 0
    missing_count: int = 0
    extra_count: int = 0
    checked_at: str = ""
    summary: str = ""
    files: list[FileChecksumResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "dump_dir": self.dump_dir,
            "algorithm": self.algorithm,
            "manifest_source": self.manifest_source,
            "total_files": self.total_files,
            "verified_files": self.verified_files,
            "valid_count": self.valid_count,
            "corrupted_count": self.corrupted_count,
            "missing_count": self.missing_count,
            "extra_count": self.extra_count,
            "checked_at": self.checked_at,
            "summary": self.summary,
            "files": [f.to_dict() for f in self.files[:250]],
        }


def compute_file_checksum(
    file_path: Path,
    algorithm: str = "sha256",
    *,
    chunk_size: int = 1024 * 1024,
    on_bytes: Callable[[int], None] | None = None,
) -> str:
    """Computes a streaming checksum (sha256, sha1, md5, or crc32) for a file without loading it in RAM."""
    algo = (algorithm or "sha256").strip().lower()
    if algo not in SUPPORTED_ALGORITHMS:
        raise ValueError(f"Unsupported checksum algorithm: {algorithm}")

    if algo == "crc32":
        crc = 0
        with file_path.open("rb") as fh:
            while chunk := fh.read(chunk_size):
                crc = zlib.crc32(chunk, crc)
                if on_bytes is not None:
                    on_bytes(len(chunk))
        return f"{crc & 0xFFFFFFFF:08x}"

    hasher = hashlib.new(algo)
    with file_path.open("rb") as fh:
        while chunk := fh.read(chunk_size):
            hasher.update(chunk)
            if on_bytes is not None:
                on_bytes(len(chunk))
    return hasher.hexdigest().lower()


def _parse_manifest_entries(manifest_data: dict[str, Any], default_algo: str) -> tuple[str, list[dict[str, Any]]]:
    algo = str(manifest_data.get("algorithm") or default_algo or "sha256").strip().lower()
    if algo not in SUPPORTED_ALGORITHMS:
        algo = "sha256"

    raw_files = manifest_data.get("files")
    entries: list[dict[str, Any]] = []
    if isinstance(raw_files, dict):
        for rel_path, spec in raw_files.items():
            if isinstance(spec, str):
                entries.append({"path": rel_path, "checksum": spec.strip().lower(), "algorithm": algo, "size": None})
            elif isinstance(spec, dict):
                entry_algo = algo
                expected = ""
                for candidate_algo in (spec.get("algorithm"), algo, *SUPPORTED_ALGORITHMS):
                    if candidate_algo and str(candidate_algo).lower() in SUPPORTED_ALGORITHMS:
                        ca = str(candidate_algo).lower()
                        val = spec.get(ca) or spec.get("checksum") or spec.get("hash")
                        if val:
                            entry_algo = ca
                            expected = str(val).strip().lower()
                            break
                entries.append(
                    {
                        "path": rel_path,
                        "checksum": expected,
                        "algorithm": entry_algo,
                        "size": int(spec["size"]) if isinstance(spec.get("size"), int) else None,
                    }
                )
    elif isinstance(raw_files, list):
        for item in raw_files:
            if not isinstance(item, dict) or not item.get("path"):
                continue
            entry_algo = str(item.get("algorithm") or algo).strip().lower()
            if entry_algo not in SUPPORTED_ALGORITHMS:
                entry_algo = algo
            expected = str(
                item.get(entry_algo) or item.get("checksum") or item.get("hash") or ""
            ).strip().lower()
            entries.append(
                {
                    "path": str(item["path"]),
                    "checksum": expected,
                    "algorithm": entry_algo,
                    "size": int(item["size"]) if isinstance(item.get("size"), int) else None,
                }
            )
    return algo, entries


def build_local_manifest(
    dump_dir: Path,
    *,
    algorithm: str = "sha256",
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Generates a local checksum manifest JSON for a user's verified dump directory (no copyrighted files included)."""
    algo = (algorithm or "sha256").strip().lower()
    if algo not in SUPPORTED_ALGORITHMS:
        algo = "sha256"

    target_files = sorted(
        (
            fp
            for fp in dump_dir.rglob("*")
            if fp.is_file()
            and (
                fp.name.lower() in {"game_scarlett_master.exe", "eboot.bin"}
                or fp.suffix.lower() in {".rpf", ".elf", ".sprx", ".xvd"}
            )
        ),
        key=lambda p: str(p.relative_to(dump_dir)).lower(),
    )
    total_bytes = sum(fp.stat().st_size for fp in target_files)
    done_bytes = 0
    files_map: dict[str, dict[str, Any]] = {}

    for idx, fp in enumerate(target_files, start=1):
        rel = fp.relative_to(dump_dir).as_posix()

        def _cb(n: int) -> None:
            nonlocal done_bytes
            done_bytes += n
            if on_progress is not None:
                pct = round((done_bytes * 100.0) / total_bytes, 1) if total_bytes > 0 else 100.0
                on_progress(
                    {
                        "stage": "hashing",
                        "current_file": rel,
                        "file_index": idx,
                        "total_files": len(target_files),
                        "bytes_done": done_bytes,
                        "bytes_total": total_bytes,
                        "percent": min(99.0, pct),
                    }
                )

        digest = compute_file_checksum(fp, algo, on_bytes=_cb)
        files_map[rel] = {"size": fp.stat().st_size, algo: digest}

    return {
        "schema": "sixdeck-dump-manifest-v1",
        "algorithm": algo,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "files": files_map,
    }


def verify_dump_directory(
    dump_dir: Path,
    *,
    manifest_data: dict[str, Any] | None = None,
    manifest_path: Path | None = None,
    algorithm: str = "sha256",
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> DumpVerificationReport:
    """Verifies dump integrity via streaming checksums against a reference JSON manifest (or local scan)."""
    now_iso = datetime.now(timezone.utc).isoformat()
    algo = (algorithm or "sha256").strip().lower()
    if algo not in SUPPORTED_ALGORITHMS:
        algo = "sha256"

    if not dump_dir.is_dir():
        return DumpVerificationReport(
            status="missing",
            dump_dir=str(dump_dir),
            algorithm=algo,
            checked_at=now_iso,
            summary=f"Dump directory not found: {dump_dir}",
        )

    manifest_source = ""
    loaded_manifest = manifest_data
    if loaded_manifest is None and manifest_path is not None and manifest_path.is_file():
        loaded_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest_source = str(manifest_path)
    elif loaded_manifest is None:
        auto_manifest = dump_dir / "dump_manifest.json"
        if auto_manifest.is_file():
            loaded_manifest = json.loads(auto_manifest.read_text(encoding="utf-8"))
            manifest_source = str(auto_manifest)
    else:
        manifest_source = str(manifest_path or "inline/community manifest")

    results: list[FileChecksumResult] = []

    if loaded_manifest is not None:
        algo, entries = _parse_manifest_entries(loaded_manifest, algo)
        existing_sizes: list[tuple[dict[str, Any], Path, int]] = []
        total_bytes = 0
        for ent in entries:
            fp = dump_dir / ent["path"]
            if fp.is_file():
                sz = fp.stat().st_size
                total_bytes += sz
                existing_sizes.append((ent, fp, sz))
            else:
                existing_sizes.append((ent, fp, 0))

        bytes_done = 0
        for idx, (ent, fp, sz) in enumerate(existing_sizes, start=1):
            if cancel_check and cancel_check():
                break
            rel = ent["path"]
            ent_algo = ent["algorithm"]
            expected = ent["checksum"]
            exp_size = ent["size"]

            if not fp.is_file():
                results.append(
                    FileChecksumResult(
                        relative_path=rel,
                        status="missing",
                        algorithm=ent_algo,
                        expected=expected,
                        expected_size=exp_size,
                    )
                )
                continue

            if on_progress is not None:
                pct = round((bytes_done * 100.0) / total_bytes, 1) if total_bytes > 0 else 0.0
                on_progress(
                    {
                        "stage": "verifying",
                        "current_file": rel,
                        "file_index": idx,
                        "total_files": len(existing_sizes),
                        "bytes_done": bytes_done,
                        "bytes_total": total_bytes,
                        "percent": min(99.0, pct),
                    }
                )

            def _cb(n: int) -> None:
                nonlocal bytes_done
                bytes_done += n
                if on_progress is not None:
                    pct = round((bytes_done * 100.0) / total_bytes, 1) if total_bytes > 0 else 100.0
                    on_progress(
                        {
                            "stage": "verifying",
                            "current_file": rel,
                            "file_index": idx,
                            "total_files": len(existing_sizes),
                            "bytes_done": bytes_done,
                            "bytes_total": total_bytes,
                            "percent": min(99.0, pct),
                        }
                    )

            actual = compute_file_checksum(fp, ent_algo, on_bytes=_cb)
            size_ok = exp_size is None or sz == exp_size
            hash_ok = (not expected) or (actual.lower() == expected.lower())
            status = "valid" if (size_ok and hash_ok) else "corrupted"
            results.append(
                FileChecksumResult(
                    relative_path=rel,
                    status=status,
                    algorithm=ent_algo,
                    actual=actual,
                    expected=expected,
                    size_bytes=sz,
                    expected_size=exp_size,
                )
            )

        valid_cnt = sum(1 for r in results if r.status == "valid")
        corrupt_cnt = sum(1 for r in results if r.status == "corrupted")
        miss_cnt = sum(1 for r in results if r.status == "missing")

        if corrupt_cnt > 0:
            overall = "corrupted"
            summary = f"Corrupted: {corrupt_cnt} invalid file(s), {miss_cnt} missing, {valid_cnt}/{len(entries)} valid."
        elif miss_cnt > 0:
            overall = "incomplete"
            summary = f"Incomplete: {miss_cnt} missing file(s), {valid_cnt}/{len(entries)} valid."
        elif len(entries) == 0:
            overall = "unverified"
            summary = "Empty manifest: no files to verify."
        else:
            overall = "valid"
            summary = f"Valid: {valid_cnt}/{len(entries)} file(s) verified ({algo.upper()})."

        if on_progress is not None:
            on_progress(
                {
                    "stage": "done",
                    "current_file": "",
                    "file_index": len(entries),
                    "total_files": len(entries),
                    "bytes_done": total_bytes,
                    "bytes_total": total_bytes,
                    "percent": 100.0,
                }
            )

        return DumpVerificationReport(
            status=overall,
            dump_dir=str(dump_dir),
            algorithm=algo,
            manifest_source=manifest_source,
            total_files=len(entries),
            verified_files=len(results),
            valid_count=valid_cnt,
            corrupted_count=corrupt_cnt,
            missing_count=miss_cnt,
            checked_at=now_iso,
            summary=summary,
            files=results,
        )

    # No manifest provided: compute local checksums for key dump files and validate structural integrity
    target_files = sorted(
        (
            fp
            for fp in dump_dir.rglob("*")
            if fp.is_file()
            and (
                fp.name.lower() in {"game_scarlett_master.exe", "eboot.bin"}
                or fp.suffix.lower() in {".rpf", ".elf", ".sprx", ".xvd"}
            )
        ),
        key=lambda p: str(p.relative_to(dump_dir)).lower(),
    )
    has_exe = any(fp.name.lower() in {"game_scarlett_master.exe", "eboot.bin"} for fp in target_files)
    total_bytes = sum(fp.stat().st_size for fp in target_files)
    bytes_done = 0

    for idx, fp in enumerate(target_files, start=1):
        if cancel_check and cancel_check():
            break
        rel = fp.relative_to(dump_dir).as_posix()
        sz = fp.stat().st_size

        def _cb_local(n: int) -> None:
            nonlocal bytes_done
            bytes_done += n
            if on_progress is not None:
                pct = round((bytes_done * 100.0) / total_bytes, 1) if total_bytes > 0 else 100.0
                on_progress(
                    {
                        "stage": "verifying",
                        "current_file": rel,
                        "file_index": idx,
                        "total_files": len(target_files),
                        "bytes_done": bytes_done,
                        "bytes_total": total_bytes,
                        "percent": min(99.0, pct),
                    }
                )

        actual = compute_file_checksum(fp, algo, on_bytes=_cb_local)
        f_status = "corrupted" if sz == 0 else "unverified"
        results.append(
            FileChecksumResult(
                relative_path=rel,
                status=f_status,
                algorithm=algo,
                actual=actual,
                size_bytes=sz,
            )
        )

    corrupt_cnt = sum(1 for r in results if r.status == "corrupted")
    if corrupt_cnt > 0:
        overall = "corrupted"
        summary = f"Corrupted: {corrupt_cnt} empty file(s) detected out of {len(results)} scanned."
    elif not has_exe or not results:
        overall = "incomplete"
        summary = f"Incomplete: main executable missing ({len(results)} archive(s) found)."
    else:
        overall = "unverified"
        summary = (
            f"{algo.upper()} checksums computed for {len(results)} file(s) — "
            "provide a JSON manifest to validate against a reference."
        )

    if on_progress is not None:
        on_progress(
            {
                "stage": "done",
                "current_file": "",
                "file_index": len(target_files),
                "total_files": len(target_files),
                "bytes_done": total_bytes,
                "bytes_total": total_bytes,
                "percent": 100.0,
            }
        )

    return DumpVerificationReport(
        status=overall,
        dump_dir=str(dump_dir),
        algorithm=algo,
        manifest_source="local-scan",
        total_files=len(target_files),
        verified_files=len(results),
        valid_count=0,
        corrupted_count=corrupt_cnt,
        missing_count=0 if has_exe else 1,
        checked_at=now_iso,
        summary=summary,
        files=results,
    )
