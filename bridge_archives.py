"""Bounded disk extraction for explicit Study Bridge inputs; never truncate originals."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import unicodedata
import zipfile
from typing import Any, Iterator

CHUNK = 1024 * 1024
DEFAULT_LIMIT = 32 * 1024 ** 3
MAX_FILES = 100_000
MAX_JSON = 64 * 1024 ** 2


class BridgeError(ValueError):
    def __init__(self, code: str, detail: Any = None):
        super().__init__(code)
        self.code, self.detail = code, detail


def require(ok: Any, code: str, detail: Any = None) -> None:
    if not ok:
        raise BridgeError(code, detail)


def safe_path(raw: str) -> str:
    require(isinstance(raw, str) and raw and "\\" not in raw and all(ord(c) >= 32 for c in raw),
            "UNSAFE_PATH", raw)
    path = PurePosixPath(raw)
    require(not path.is_absolute() and not re.match(r"^[A-Za-z]:", raw)
            and all(p not in {"", ".", ".."} for p in raw.split("/")), "UNSAFE_PATH", raw)
    return str(path)


def source_file(path: Path, root: Path | None = None) -> Path:
    require(path.is_file() and not path.is_symlink(), "MISSING_OR_UNSAFE_FILE", str(path))
    if root:
        require(path.resolve().is_relative_to(root.resolve()), "PATH_ESCAPES_ROOT", str(path))
        for item in [path, *path.parents]:
            if item == root:
                break
            require(not item.is_symlink(), "SYMLINK_SOURCE", str(item))
    return path


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with source_file(path).open("rb") as stream:
        for block in iter(lambda: stream.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: Path) -> Any:
    source_file(path)
    require(path.stat().st_size <= MAX_JSON, "JSON_METADATA_TOO_LARGE", str(path))
    with path.open() as stream:
        return json.load(stream)


def copy_file(source: Path, destination: Path, immutable: bool = True) -> None:
    source_file(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    require(not any(p.is_symlink() for p in [destination, *destination.parents]), "SYMLINK_DESTINATION")
    if destination.exists() and immutable:
        require(file_hash(source) == file_hash(destination), "EXISTING_CONTENT_CONFLICT", str(destination))
        return
    fd, temporary = tempfile.mkstemp(prefix=".bridge-", dir=destination.parent)
    try:
        with source.open("rb") as inp, os.fdopen(fd, "wb") as out:
            shutil.copyfileobj(inp, out, CHUNK)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class ArchiveBudget:
    def __init__(self, destination: Path, max_bytes: int = DEFAULT_LIMIT,
                 max_files: int = MAX_FILES, max_depth: int = 6):
        require(max_bytes > 0 and max_files > 0, "INVALID_ARCHIVE_BUDGET")
        self.max_bytes, self.max_files, self.max_depth = max_bytes, max_files, max_depth
        self.bytes = self.files = 0
        self.destination = destination

    def charge(self, size: int) -> None:
        self.bytes += size
        self.files += 1
        require(self.bytes <= self.max_bytes and self.files <= self.max_files, "ARCHIVE_LIMIT_EXCEEDED",
                {"max_unpacked_bytes": self.max_bytes, "max_files": self.max_files,
                 "originals_preserved": True, "action": "Inspect the archive, then explicitly raise the CLI limit if legitimate."})


def extract_zip(source: Path, destination: Path, budget: ArchiveBudget) -> Path:
    source_file(source)
    destination.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(source) as archive:
            entries = archive.infolist()
            require(len(entries) <= budget.max_files, "ARCHIVE_FILE_COUNT_EXCEEDED")
            seen, files = set(), set()
            for entry in entries:
                path = safe_path(entry.filename.rstrip("/") if entry.is_dir() else entry.filename)
                normalized = unicodedata.normalize("NFC", path).casefold()
                require(normalized not in seen, "DUPLICATE_ARCHIVE_PATH", path)
                seen.add(normalized)
                mode = entry.external_attr >> 16
                require(not stat.S_ISLNK(mode) and (not mode or stat.S_IFMT(mode) in
                        {0, stat.S_IFREG, stat.S_IFDIR}), "ARCHIVE_SPECIAL_FILE", path)
                require(not entry.flag_bits & 1, "ENCRYPTED_ARCHIVE_UNSUPPORTED")
                if not entry.is_dir():
                    budget.charge(entry.file_size)
                    files.add(normalized)
            for path in seen:
                require(not any(str(parent) in files for parent in PurePosixPath(path).parents if str(parent) != "."),
                        "ARCHIVE_FILE_DIRECTORY_COLLISION", path)
            remaining = sum(e.file_size for e in entries if not e.is_dir())
            require(remaining + 64 * 1024 ** 2 <= shutil.disk_usage(destination).free,
                    "INSUFFICIENT_TEMP_DISK_SPACE", {"required_bytes": remaining, "originals_preserved": True})
            for entry in entries:
                target = destination / safe_path(entry.filename.rstrip("/") if entry.is_dir() else entry.filename)
                if entry.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                count = 0
                with archive.open(entry) as inp, target.open("xb") as out:
                    while True:
                        block = inp.read(CHUNK)
                        if not block:
                            break
                        count += len(block)
                        require(count <= entry.file_size, "ARCHIVE_SIZE_MISMATCH")
                        out.write(block)
                require(count == entry.file_size, "ARCHIVE_SIZE_MISMATCH")
    except (zipfile.BadZipFile, RuntimeError, EOFError) as exc:
        raise BridgeError("INVALID_ZIP") from exc
    return destination


def write_zip(files: dict[str, Path], destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".bridge-zip-", dir=destination.parent)
    os.close(fd)
    temporary = Path(name)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            for relative, path in sorted(files.items()):
                source_file(path)
                info = zipfile.ZipInfo(safe_path(relative), (1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (stat.S_IFREG | 0o600) << 16
                with path.open("rb") as inp, archive.open(info, "w", force_zip64=True) as out:
                    shutil.copyfileobj(inp, out, CHUNK)
        copy_file(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def inventory(files: dict[str, Path], roles: dict[str, str] | None = None) -> list[dict[str, Any]]:
    return [{"path": path, "sha256": file_hash(source), "size": source.stat().st_size,
             "role": (roles or {}).get(path, "support")} for path, source in sorted(files.items())]


def mac_metadata(path: Path) -> bool:
    return path.name == ".DS_Store" or ("__MACOSX" in path.parts and path.name.startswith("._"))


def check_manifest(root: Path) -> dict[str, Any]:
    manifest = read_json(root / "manifest.json")
    require(manifest.get("schema_version") == 1 and manifest.get("subject") in {"math", "english", "408"},
            "INVALID_MANIFEST_IDENTITY")
    require(isinstance(manifest.get("files"), list), "MISSING_FILE_INVENTORY")
    seen = set()
    for row in manifest["files"]:
        path = safe_path(row["path"])
        require(isinstance(row.get("role"), str) and row["role"], "FILE_ROLE_REQUIRED", path)
        require(path not in seen and path != "manifest.json", "DUPLICATE_FILE_BINDING", path)
        seen.add(path)
        file = source_file(root / path, root)
        require(row.get("sha256") == file_hash(file) and row.get("size") == file.stat().st_size,
                "OUTPUT_FILE_HASH_MISMATCH", path)
    actual = set()
    for path in root.rglob("*"):
        require(not path.is_symlink(), "SYMLINK_SOURCE")
        if path.is_file() and not mac_metadata(path):
            actual.add(path.relative_to(root).as_posix())
    require(seen == actual - {"manifest.json"}, "UNDECLARED_OUTPUT_FILES")
    if manifest.get("kind") == "quick_pack":
        native_path = root / "original/manifest.json"
        native = read_json(native_path)
        native_subject = "408" if native.get("subject") == "cs408" else native.get("subject")
        require(native_subject == manifest["subject"] and native.get("package_id") == manifest.get("package_id")
                and native.get("study_date") == manifest.get("study_date"), "QUICK_NATIVE_IDENTITY_MISMATCH")
        require(file_hash(native_path) == manifest.get("native_manifest_sha256")
                and (native.get("canonical_sha256") or native.get("package_canonical_sha256")) == manifest.get("version"),
                "QUICK_NATIVE_VERSION_MISMATCH")
        require(all((root / path).is_file() for path in ("original/conversation.json", "original/source.json", "original/receipt.json")),
                "QUICK_COMPLETE_NATIVE_PACKAGE_REQUIRED")
        questions = manifest.get("questions")
        require(isinstance(questions, list) and questions and all(isinstance(q, dict) and isinstance(q.get("question_id"), str)
                and q["question_id"] for q in questions), "QUICK_QUESTION_IDENTITIES_REQUIRED")
        require(len({q["question_id"] for q in questions}) == len(questions), "QUICK_DUPLICATE_QUESTION_ID")
        for question in questions:
            require(question.get("conversation_path") == "original/conversation.json", "QUICK_FULL_CONVERSATION_BINDING_REQUIRED")
            for path in question.get("problem_paths", []) + question.get("solution_paths", []):
                require(safe_path(path) in seen, "QUICK_QUESTION_ASSET_BINDING_MISSING", path)
    return manifest


@contextlib.contextmanager
def collect_inputs(inputs: list[Path], expected_subject: str | None = None,
             max_bytes: int = DEFAULT_LIMIT, *, allow_lessons: bool = True) -> Iterator[list[dict[str, Any]]]:
    """Yield checked typed inputs; temporary roots live until the caller returns."""
    with tempfile.TemporaryDirectory(prefix="study-bridge-input-") as temporary:
        work = Path(temporary).resolve()
        budget = ArchiveBudget(work, max_bytes=max_bytes)
        found: dict[tuple[str, str, str], dict[str, Any]] = {}
        serial = 0

        def visit(path: Path, scope: str | None, depth: int) -> None:
            nonlocal serial
            require(depth <= budget.max_depth, "ARCHIVE_NESTING_LIMIT")
            original_zip = None
            if path.is_file():
                require(path.suffix.lower() == ".zip", "UNEXPECTED_CONTAINER_FILE", str(path))
                original_zip = path
                serial += 1
                path = extract_zip(path, work / str(serial), budget)
            require(path.is_dir() and not path.is_symlink(), "UNSAFE_INPUT_DIRECTORY")
            if (path / "manifest.json").is_file():
                manifest = check_manifest(path)
                kind = manifest.get("kind")
                if kind == "math_topic_batch":
                    require(manifest.get("project") == "A" and manifest.get("subject") == "math",
                            "INVALID_MATH_TOPIC_BATCH_IDENTITY")
                    for field in ("batch_id", "group_id", "shard_id", "run_id"):
                        value = manifest.get(field)
                        require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}", value),
                                "INVALID_MATH_TOPIC_BATCH_ID", field)
                    require(isinstance(manifest.get("source_commit"), str)
                            and re.fullmatch(r"[0-9a-f]{40}", manifest["source_commit"])
                            and isinstance(manifest.get("snapshot_id"), str) and manifest["snapshot_id"],
                            "MATH_TOPIC_BATCH_SNAPSHOT_REQUIRED")
                    binding = manifest.get("batch_binding")
                    require(isinstance(binding, dict)
                            and binding.get("batch_id") == manifest["batch_id"]
                            and binding.get("shard_id") == manifest["shard_id"]
                            and isinstance(binding.get("plan_sha256"), str)
                            and re.fullmatch(r"[0-9a-f]{64}", binding["plan_sha256"]),
                            "MATH_TOPIC_BATCH_PLAN_BINDING_INVALID")
                    require(manifest.get("profile_merge_policy") == "deferred_batch_merge",
                            "MATH_TOPIC_BATCH_PROFILE_MERGE_POLICY_INVALID")
                    packages = manifest.get("input_packages")
                    subpackages = manifest.get("subpackages")
                    require(isinstance(packages, list) and packages
                            and all(isinstance(row, dict) and isinstance(row.get("package_id"), str)
                                    and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}", row["package_id"])
                                    and isinstance(row.get("version"), str) and re.fullmatch(r"[0-9a-f]{64}", row["version"])
                                    and isinstance(row.get("zip_sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", row["zip_sha256"])
                                    for row in packages), "MATH_TOPIC_BATCH_INPUT_PACKAGES_INVALID")
                    package_ids = [row["package_id"] for row in packages]
                    expected_subpackages = [f"quick-packs/{package_id}.zip" for package_id in package_ids]
                    require(len(package_ids) == len(set(package_ids))
                            and isinstance(subpackages, list) and subpackages == expected_subpackages,
                            "MATH_TOPIC_BATCH_SUBPACKAGES_INVALID")
                    declared = {row["path"] for row in manifest["files"]}
                    require(set(subpackages) <= declared, "MATH_TOPIC_BATCH_SUBPACKAGE_NOT_DECLARED")
                    require(not scope or scope == "math", "CROSS_SUBJECT_DIRECTORY_MISMATCH")
                    require(not expected_subject or expected_subject == "math", "CROSS_SUBJECT_TASK")
                    for nested in subpackages:
                        visit(path / safe_path(nested), "math", depth + 1)
                    for row in packages:
                        nested = found.get(("math", "quick_pack", row["package_id"]))
                        require(nested and nested["manifest"].get("version") == row["version"]
                                and nested["zip_sha256"] == row["zip_sha256"],
                                "MATH_TOPIC_BATCH_INNER_BINDING_MISMATCH", row["package_id"])
                    return
                require(kind == "quick_pack" or allow_lessons and kind == "lesson_review_bundle", "NOT_A_QUICK_PACKAGE")
                lesson = None
                if kind == "lesson_review_bundle":
                    from lesson_export_408 import verify_lesson_directory
                    lesson = verify_lesson_directory(path)
                    require(lesson["results"], "EMPTY_LESSON_INPUT")
                subject, package_id = manifest["subject"], manifest.get("package_id") if kind == "quick_pack" else manifest.get("lesson_id")
                require(isinstance(package_id, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}", package_id), "INVALID_ID")
                require(not scope or scope == subject, "CROSS_SUBJECT_DIRECTORY_MISMATCH")
                require(not expected_subject or expected_subject == subject, "CROSS_SUBJECT_TASK")
                if original_zip is None:
                    serial += 1
                    original_zip = work / f"repacked-{serial}.zip"
                    write_zip({p.relative_to(path).as_posix(): p for p in path.rglob("*") if p.is_file() and not mac_metadata(p)}, original_zip)
                item = {"manifest": manifest, "root": path, "zip_path": original_zip,
                        "zip_sha256": file_hash(original_zip)}
                if lesson is not None:
                    item["lesson"] = lesson
                key = (subject, kind, package_id)
                if key in found:
                    require(found[key]["manifest"] == manifest, "DUPLICATE_PACKAGE_ID_CONFLICT", package_id)
                else:
                    found[key] = item
                if lesson is not None:
                    for quick in lesson["manifest"].get("quick_packs", []):
                        visit(path / safe_path(quick["path"]), subject, depth + 1)
                return
            children = sorted(path.iterdir())
            require(children, "EMPTY_PACKAGE_DIRECTORY")
            for child in children:
                require(not child.is_symlink(), "SYMLINK_SOURCE")
                if child.is_file() and mac_metadata(child):
                    continue
                if child.is_dir() and child.name == "__MACOSX":
                    metadata_files = [p for p in child.rglob("*") if p.is_file()]
                    require(all(mac_metadata(p) and not p.is_symlink() for p in metadata_files), "UNEXPECTED_MACOSX_CONTENT")
                    continue
                child_scope = child.name if child.name in {"math", "english", "408"} else scope
                require(not scope or child_scope == scope, "CROSS_SUBJECT_DIRECTORY_MISMATCH")
                visit(child, child_scope, depth + 1)

        for path in inputs:
            visit(path, path.name if path.name in {"math", "english", "408"} else None, 0)
        require(found, "NO_QUICK_PACKAGES")
        yield [found[key] for key in sorted(found)]


@contextlib.contextmanager
def packages(inputs: list[Path], expected_subject: str | None = None,
             max_bytes: int = DEFAULT_LIMIT) -> Iterator[list[dict[str, Any]]]:
    """Compatibility API: only quick packages, with the original result shape."""
    with collect_inputs(inputs, expected_subject, max_bytes, allow_lessons=False) as found:
        yield found
