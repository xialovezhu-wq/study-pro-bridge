#!/usr/bin/env python3
"""Create consistent local study snapshots, with optional configured GitHub publication.

Only this disposable checkout is written. Source repositories keep their origins
and dirty files. Receipts describe publication, never formal intake or mastery.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any

import study_web_read
from study_paths import subject_root

BASE = Path(__file__).resolve().parent
PDF_RUNTIME = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"
SUBJECTS = {s: {"root": Path.home() / "Documents" / f"kaoyan-{s}",
                "remote": f"xialovezhu-wq/kaoyan-{s}"}
            for s in ("math", "english", "408")}
SUBJECTS["math"]["root"] = subject_root("math")
SUBJECTS["english"]["root"] = subject_root("english")
SUBJECTS["408"]["root"] = subject_root("408")
LIMIT = 100 * 1024 * 1024
CHUNK = 1024 * 1024
# 2026-09-27: the 408 root reached 755 KB (question_refs ~1 KB per formal node, history_index
# ~185 KB); 12 new nodes crossed the old 750 KiB guard. Readers parse the root locally and the
# web pages question_refs through MCP, so a larger guard only bounds runaway growth. Sharding
# question_refs like original_mappings is the structural follow-up.
ROOT_MANIFEST_LIMIT = 2 * 1024 * 1024
INVENTORY_LIMIT = 512 * 1024
INVENTORY_FIELDS = (("file_inventory", "files"),
                    ("original_mapping_inventory", "original_mappings"),
                    ("exclusion_inventory", "exclusions"))
DENIED_PARTS = {".git", "__pycache__", "node_modules", ".venv", "privatebackups",
                ".DS_Store", ".ssh", ".aws", ".obsidian", "Computer History", "backups"}
DENIED_NAMES = {"AGENTS.md", "config.toml", "credentials", "credentials.json",
                "id_rsa", "id_ed25519", ".npmrc", ".netrc"}
SECRET = re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
                    rb"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|"
                    rb"sk-(?:proj-)?[A-Za-z0-9_-]{32,}|AKIA[A-Z0-9]{16})\b")


class PublicationError(RuntimeError):
    def __init__(self, code: str, detail: Any = None):
        super().__init__(code)
        self.code, self.detail = code, detail


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as out:
            json.dump(value, out, ensure_ascii=False, indent=2)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_json(path: Path, default: Any = None) -> Any:
    return json.loads(path.read_text()) if path.exists() else default


def configured_target(state_dir: Path) -> str:
    """Publication target for explicit publisher operations."""
    target = read_json(Path(state_dir) / "config.json", {}).get("target", "local")
    if target not in {"local", "github"}:
        raise PublicationError("INVALID_PUBLICATION_TARGET")
    return target


def local_lfs_object(state: Path, sha: str, size: int | None = None) -> Path:
    """Resolve verified local bytes; this function never fetches an LFS object."""
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise PublicationError("INVALID_LFS_OBJECT_ID")
    for root in (state / "checkout/.git/lfs/objects", state / "lfs-readback/objects"):
        path = root / sha[:2] / sha[2:4] / sha
        if (path.is_file() and not path.is_symlink()
                and path.resolve().is_relative_to(state.resolve())
                and (size is None or path.stat().st_size == size)
                and file_hash(path) == sha):
            return path
    raise PublicationError("LOCAL_LFS_ORIGINAL_UNAVAILABLE", sha)


def committed_library(checkout: Path, commit: str) -> tuple[bytes, dict[str, Any]]:
    """Read one immutable local Git version without consulting any remote."""
    raw = run(["git", "-C", str(checkout), "show", f"{commit}:PUBLISHED_LIBRARY.json"]).stdout
    manifest = json.loads(raw)
    for key, output in INVENTORY_FIELDS:
        if manifest.get(key):
            records = []
            for shard in manifest[key]:
                path_check(shard["path"])
                data = run(["git", "-C", str(checkout), "show", f"{commit}:{shard['path']}"]).stdout
                if hashlib.sha256(data).hexdigest() != shard["sha256"]:
                    raise PublicationError("INVENTORY_HASH_MISMATCH", shard["path"])
                rows = [json.loads(line) for line in data.splitlines() if line.strip()]
                if len(rows) != shard["count"]:
                    raise PublicationError("INVENTORY_COUNT_MISMATCH", shard["path"])
                records.extend(rows)
            manifest[output] = records
    return raw, manifest


def load_library(checkout: Path, manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    manifest = dict(manifest or read_json(checkout / "PUBLISHED_LIBRARY.json", {}))
    for key, output in INVENTORY_FIELDS:
        if not manifest.get(key):
            continue
        records = []
        for shard in manifest[key]:
            path = checkout / path_check(shard["path"])
            if file_hash(path) != shard["sha256"]:
                raise PublicationError("INVENTORY_HASH_MISMATCH", shard["path"])
            with path.open() as source:
                rows = [json.loads(line) for line in source if line.strip()]
            if len(rows) != shard["count"]:
                raise PublicationError("INVENTORY_COUNT_MISMATCH", shard["path"])
            records.extend(rows)
        manifest[output] = records
    return manifest


def write_library(checkout: Path, manifest: dict[str, Any]) -> None:
    """Keep the one web entrypoint small; large byte inventories are not duplicated."""
    manifest = {k: v for k, v in manifest.items() if k not in {key for key, _ in INVENTORY_FIELDS}}
    encoded = json.dumps(manifest, ensure_ascii=False, indent=2).encode()
    if len(encoded) <= ROOT_MANIFEST_LIMIT:
        write_json(checkout / "PUBLISHED_LIBRARY.json", manifest)
        return
    root = dict(manifest)
    rows = root.pop("files")
    shards, buffer, group, number = [], [], None, 0
    size = 0
    def flush() -> None:
        nonlocal buffer, size, number
        if not buffer:
            return
        number += 1
        relative = f"published-inventory/{hashlib.sha256(group.encode()).hexdigest()[:12]}-{number:03d}.jsonl"
        content = b"".join(buffer)
        path = checkout / relative
        if path.is_symlink() or path.parent.is_symlink():
            raise PublicationError("REMOTE_SYMLINK_COLLISION", relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        shards.append({"path": relative, "sha256": hashlib.sha256(content).hexdigest(),
                       "count": len(buffer), "bytes": len(content), "source_directory": group})
        buffer, size = [], 0
    def inventory_group(row: dict[str, Any]) -> str:
        parts = PurePosixPath(row["path"]).parts
        return str(parts[0]) if len(parts) > 1 else "."
    for row in sorted(rows, key=lambda row: (inventory_group(row), row["path"])):
        next_group = inventory_group(row)
        line = (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        if next_group != group or size + len(line) > INVENTORY_LIMIT:
            flush()
            if next_group != group:
                number = 0
            group = next_group
        buffer.append(line)
        size += len(line)
    flush()
    root["file_inventory"] = shards
    root["inventory_note"] = "Mechanical byte coverage. Use existing history_index and entrypoints for study evidence."
    for field, inventory_key, prefix in (
            ("original_mappings", "original_mapping_inventory", "original-mappings"),
            ("exclusions", "exclusion_inventory", "exclusions")):
        if (len(json.dumps(root, ensure_ascii=False, indent=2).encode()) <= ROOT_MANIFEST_LIMIT
                or not isinstance(root.get(field), list)):
            continue
        metadata_shards, buffer, size = [], [], 0
        def flush_metadata() -> None:
            nonlocal buffer, size
            if not buffer:
                return
            relative = f"published-inventory/{prefix}-{len(metadata_shards) + 1:03d}.jsonl"
            path = checkout / relative
            if path.is_symlink() or path.parent.is_symlink():
                raise PublicationError("REMOTE_SYMLINK_COLLISION", relative)
            content = b"".join(buffer)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            metadata_shards.append({"path": relative, "sha256": hashlib.sha256(content).hexdigest(),
                                    "count": len(buffer), "bytes": len(content)})
            buffer, size = [], 0
        for row in root.pop(field):
            line = (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
            if size + len(line) > INVENTORY_LIMIT:
                flush_metadata()
            buffer.append(line)
            size += len(line)
        flush_metadata()
        root[inventory_key] = metadata_shards
    if len(json.dumps(root, ensure_ascii=False, indent=2).encode()) > ROOT_MANIFEST_LIMIT:
        raise PublicationError("ROOT_ENTRYPOINT_TOO_LARGE")
    write_json(checkout / "PUBLISHED_LIBRARY.json", root)


def run(args: list[str], cwd: Path | None = None, data: bytes | None = None,
        timeout: int = 180, check: bool = True,
        extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_LFS_SKIP_SMUDGE="1", PYTHONDONTWRITEBYTECODE="1")
    env.update(extra_env or {})
    try:
        result = subprocess.run(args, cwd=cwd, input=data, capture_output=True,
                                timeout=timeout, env=env)
    except subprocess.TimeoutExpired as exc:
        raise PublicationError("COMMAND_TIMEOUT", {"command": args[:2]}) from exc
    if check and result.returncode:
        # Errors can echo authentication URLs or source bytes. Never persist them.
        raise PublicationError("COMMAND_FAILED", {"command": args[:2],
                                                   "exit_code": result.returncode})
    return result


def git(checkout: Path, *args: str, **kw: Any) -> str:
    # Scoped helper uses the user's existing gh login, with no credential export.
    cmd = ["git", "-c", "credential.helper=",
           "-c", "credential.https://github.com.helper=!gh auth git-credential",
           "-c", "core.quotepath=false", "-C", str(checkout), *args]
    return run(cmd, **kw).stdout.decode().strip()


@contextlib.contextmanager
def locked(path: Path, blocking: bool = False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError as exc:
            raise PublicationError("PUBLICATION_BUSY") from exc
        yield


def path_check(raw: str) -> str:
    path = PurePosixPath(raw)
    if (not raw or path.is_absolute() or any(p in {"..", "."} for p in path.parts)
            or "\\" in raw or any(ord(c) < 32 for c in raw)):
        raise PublicationError("UNSAFE_PUBLISH_PATH", raw)
    if (any(p in DENIED_PARTS for p in path.parts) or path.name in DENIED_NAMES
            or path.name == ".env" or path.name.startswith(".env.")
            or path.name.endswith((".lock", ".sqlite", ".sqlite3", ".db"))):
        raise PublicationError("EXCLUDED_SOURCE", raw)
    return str(path)


def stamp(path: Path) -> list[int]:
    info = path.stat()
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def file_hash(path: Path, scan: bool = False, with_git_oid: bool = False) -> Any:
    sha, tail = hashlib.sha256(), b""
    git_sha = hashlib.sha1(f"blob {path.stat().st_size}\0".encode())
    with path.open("rb") as source:
        for part in iter(lambda: source.read(CHUNK), b""):
            sha.update(part)
            git_sha.update(part)
            if scan and SECRET.search(tail + part):
                raise PublicationError("SECRET_PATTERN", {"path": str(path),
                                                           "value": "REDACTED"})
            tail = part[-256:]
    return (sha.hexdigest(), git_sha.hexdigest()) if with_git_oid else sha.hexdigest()


def normalize(description: Any, subject: str) -> dict[str, Any]:
    if isinstance(description, list):
        description = {"subject": subject, "sources": description}
    if description.get("subject", subject) != subject:
        raise PublicationError("SUBJECT_MISMATCH")
    rows = description.get("sources", description.get("files"))
    if not isinstance(rows, list) or not rows:
        raise PublicationError("EMPTY_EXPORT")
    output, seen = [], set()
    for row in rows:
        source = Path(row["source_path"]).expanduser()
        target = path_check(row.get("publish_path", row.get("relative_path", "")))
        if target in {"PUBLISHED_LIBRARY.json", "WEB_READ_START.json", ".gitattributes"} or target.startswith(("published-inventory/", "published-web/")):
            raise PublicationError("RESERVED_PUBLISH_PATH", target)
        if target in seen:
            raise PublicationError("DUPLICATE_PUBLISH_PATH", target)
        if not source.is_absolute() or source.is_symlink() or not source.is_file():
            raise PublicationError("SOURCE_UNAVAILABLE", str(source))
        # Exact descriptors are the whitelist. Reject machine-secret sources even
        # when a descriptor tries to rename them to an innocent destination.
        path_check(str(source).lstrip("/"))
        seen.add(target)
        item = dict(row, source_path=str(source), publish_path=target,
                    kind=row.get("kind", row.get("role", "formal-material")))
        if item["kind"] in {"machine_config", "runtime_cache", "secret", "backup", "temporary_prompt", "computer_history"}:
            raise PublicationError("EXCLUDED_SOURCE_ROLE", target)
        output.append(item)
    return dict(description, subject=subject, sources=sorted(output, key=lambda x: x["publish_path"]))


def describe(subject: str, repo: Path, event: str) -> dict[str, Any]:
    if subject == "math":
        command = [sys.executable, str(repo / "数学一回滚复习系统/scripts/study_publication_adapter.py"),
                   "describe", "--repo", str(repo), "--event-id", event]
    elif subject == "english":
        command = [sys.executable, str(repo / "scripts/english_learning_pipeline.py"),
                   "export-publication-sources", "--repo-root", str(repo)]
    else:
        command = [sys.executable, str(repo / "scripts/study_bridge_408.py"),
                   "descriptor", "--repo", str(repo)]
    raw = run(command, cwd=repo, timeout=300).stdout
    try:
        return normalize(json.loads(raw), subject)
    except (ValueError, KeyError, TypeError) as exc:
        raise PublicationError("INVALID_ADAPTER_JSON") from exc


def export_pdf_pages(description_path: Path, cache_root: Path, output_path: Path) -> dict[str, Any]:
    """Rebuildable exact-page publication copies; no OCR or image downsampling."""
    from pypdf import PdfReader, PdfWriter
    desc = read_json(description_path)
    index_path = cache_root / "index.json"
    index = read_json(index_path, {})
    output, created = [], 0
    for original in desc.get("original_mappings", []):
        pages = original.get("pages", [])
        if not pages:
            continue
        source = Path(original["source_path"])
        original_sha = original["sha256"]
        before = stamp(source)
        reader = None
        for page in pages:
            number = page["page_number"]
            if not isinstance(number, int) or number < 1:
                raise PublicationError("INVALID_ORIGINAL_PAGE", page)
            key = f"{original_sha}:{number}"
            row = index.get(key, {})
            path = Path(page["existing_page_file"]) if page.get("existing_page_file") else cache_root / original_sha / f"page-{number:04d}.pdf"
            reuse = (path.is_file() and row.get("path") == str(path)
                     and row.get("stamp") == stamp(path))
            if not reuse:
                if reader is None:
                    if file_hash(source) != original_sha:
                        raise PublicationError("ORIGINAL_PDF_HASH_CHANGED", original["source_id"])
                    reader = PdfReader(str(source))
                if number > len(reader.pages):
                    raise PublicationError("ORIGINAL_PAGE_OUT_OF_RANGE", page)
                src_page = reader.pages[number - 1]
                if not page.get("existing_page_file"):
                    path.parent.mkdir(parents=True, exist_ok=True)
                    writer = PdfWriter()
                    writer.add_page(src_page)
                    with path.open("wb") as out:
                        writer.write(out)
                    writer.close()
                    created += 1
                with path.open("rb") as inp:
                    actual = PdfReader(inp)
                    if len(actual.pages) != 1:
                        raise PublicationError("PAGE_EXPORT_COUNT_MISMATCH", str(path))
                    dst_page = actual.pages[0]
                    if (list(src_page.mediabox) != list(dst_page.mediabox)
                            or list(src_page.cropbox) != list(dst_page.cropbox)
                            or src_page.rotation != dst_page.rotation
                            or hashlib.sha256(src_page.get_contents().get_data()).digest()
                            != hashlib.sha256(dst_page.get_contents().get_data()).digest()):
                        raise PublicationError("PAGE_EXPORT_CONTENT_MISMATCH", str(path))
                row = {"path": str(path), "stamp": stamp(path), "sha256": file_hash(path), "bytes": path.stat().st_size}
                index[key] = row
            output.append({"source_path": str(path), "publish_path": page["publish_path"],
                           "kind": "original_page_pdf", "sha256": row["sha256"], "bytes": row["bytes"],
                           "source_id": original["source_id"], "page_number": number,
                           "original_sha256": original_sha, "required": True, "storage": "git",
                           "visibility": original.get("visibility"),
                           "original_reference": str(source) + f"#page={number}"})
        if reader is not None:
            reader.close()
        if stamp(source) != before:
            raise PublicationError("ORIGINAL_PDF_CHANGED_DURING_EXPORT", str(source))
        write_json(index_path, index)
    write_json(output_path, {"sources": output, "created_pages": created, "total_pages": len(output)})
    return {"created_pages": created, "total_pages": len(output), "bytes": sum(x["bytes"] for x in output)}


def _math_quality_module(repo):
    scripts = Path(repo) / "数学一回滚复习系统/scripts"
    if not (scripts / "math_formal_quality.py").is_file():
        return None
    sys.path.insert(0, str(scripts))
    try:
        import math_formal_quality
        return math_formal_quality
    finally:
        sys.path.remove(str(scripts))


@contextlib.contextmanager
def _formal_quality_window(subject, repo, event):
    if subject == "math":
        quality = _math_quality_module(repo)
        if quality is not None:
            try:
                with quality.publication_window(repo, event) as binding:
                    yield binding
            except quality.QualityError as exc:
                raise PublicationError(str(exc)) from exc
            return
    scripts = Path(repo) / "scripts"
    if subject != "408" or not (scripts / "formal_quality_408.py").is_file():
        yield None
        return
    sys.path.insert(0, str(scripts))
    try:
        import formal_quality_408 as quality
        with quality.publication_window(repo, event) as binding:
            yield binding
    finally:
        sys.path.remove(str(scripts))


class Publisher:
    def __init__(self, subject: str, repo: Path, state: Path,
                 remote: str | None = None, test_mode: bool = False, *, local_only: bool = True):
        self.subject, self.repo = subject, repo.resolve()
        self.remote = remote or SUBJECTS[subject]["remote"]
        self.test_mode = test_mode
        self.target = "local" if local_only else configured_target(state)
        self.subject_state = state.resolve() / subject
        # A backup has its own Git ancestry, worktree, receipt history, hash/LFS
        # caches and latest pointer. It never resets the learning snapshot branch.
        self.state = self.subject_state if self.target == "local" else self.subject_state / "github-backup"
        self.checkout = self.state / "checkout"

    def remote_metadata(self) -> dict[str, Any]:
        if self.target == "local":
            return {"publication_target": "local", "remote_contacted": False}
        if self.test_mode:
            if not Path(self.remote).is_absolute():
                raise PublicationError("TEST_REQUIRES_LOCAL_REMOTE")
            return {"nameWithOwner": self.remote, "isPrivate": True,
                    "defaultBranchRef": {"name": "main"}}
        if self.remote != SUBJECTS[self.subject]["remote"]:
            raise PublicationError("REMOTE_NOT_AUTHORIZED")
        result = json.loads(run(["gh", "repo", "view", self.remote, "--json",
                                "nameWithOwner,isPrivate,defaultBranchRef,url"]).stdout)
        if not result["isPrivate"] or result["defaultBranchRef"]["name"] != "main":
            raise PublicationError("REMOTE_PRIVACY_OR_BRANCH_CHANGED")
        return result

    def initialize(self) -> None:
        url = self.remote if self.test_mode else f"https://github.com/{self.remote}.git"
        self.state.mkdir(parents=True, exist_ok=True)
        if not (self.checkout / ".git").exists():
            self.checkout.mkdir(parents=True, exist_ok=True)
            git(self.checkout, "init", "-b", "main")
            git(self.checkout, "remote", "add", "origin", url)
        if git(self.checkout, "remote", "get-url", "origin") != url:
            raise PublicationError("STAGING_REMOTE_CHANGED")
        git(self.checkout, "config", "user.name", "Study Library Publisher")
        git(self.checkout, "config", "user.email", "xialovezhu-wq@users.noreply.github.com")
        git(self.checkout, "config", "core.autocrlf", "false")
        if self.target == "local":
            if not git(self.checkout, "rev-parse", "--verify", "HEAD", check=False):
                git(self.checkout, "commit", "--allow-empty", "-m", "Initialize local study snapshot store")
            return
        if not self.test_mode:
            git(self.checkout, "config", "lfs.url", url + "/info/lfs")
        # User work never resides here; a dirty checkout is still kept on failure.
        git(self.checkout, "fetch", "--no-tags", "origin", "main", timeout=300)

    def remote_head(self) -> str:
        if self.target == "local":
            raise PublicationError("REMOTE_ACCESS_DISABLED_IN_LOCAL_MODE")
        row = git(self.checkout, "ls-remote", "origin", "refs/heads/main")
        value = row.split()[0] if row else ""
        if not re.fullmatch(r"[0-9a-f]{40}", value):
            raise PublicationError("REMOTE_REF_UNAVAILABLE")
        return value

    def receipt_path(self, event: str) -> Path:
        path = self.state / "receipts" / (hashlib.sha256(event.encode()).hexdigest() + ".json")
        if self.target == "local":
            # A local postcommit can finish under its original event ID while a
            # historical external operation keeps its own exact retry receipt.
            local_path = path.with_suffix(".local.json")
            if local_path.is_file():
                return local_path
            prior = read_json(path)
            if prior and prior.get("publication_target", "github") != "local" and prior.get("status") != "PUBLISHED":
                return local_path
        return path

    def verified_lfs_cache(self, entries: list[dict[str, Any]]) -> dict[str, Any]:
        """Only earlier successful remote readbacks authorize a zero-transfer reuse."""
        verified = {}
        roots = [self.state]
        if self.target == "github":
            roots.append(self.subject_state)  # Reuse a proven pre-isolation readback only.
        for root in roots:
            latest = read_json(root / "latest.json", {})
            if latest.get("status") != "PUBLISHED" or latest.get("publication_target", "github") != "github":
                continue
            known = latest.get("lfs_verified_objects", {})
            for entry in entries:
                sha = entry["sha256"]
                if sha in verified:
                    continue
                record = known.get(sha, {})
                relative = Path("lfs-readback/objects") / sha[:2] / sha[2:4] / sha
                path = root / relative
                if (record.get("bytes") != entry["bytes"] or not record.get("remote_verified_at")
                        or not path.is_file() or record.get("stamp") != stamp(path)):
                    continue
                if root != self.state:
                    destination = self.state / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if not destination.is_file():
                        shutil.copyfile(path, destination)
                    if file_hash(destination) != sha:
                        raise PublicationError("LFS_REMOTE_BYTES_UNVERIFIED", str(relative))
                    record = dict(record, stamp=stamp(destination))
                verified[sha] = record
        return verified

    def prepare(self, event: str, desc: dict[str, Any], lfs_verified: bool = False,
                extra_web_binary_paths: set[str] | None = None) -> dict[str, Any]:
        """Stage a read-only source snapshot; no network mutation or git commit."""
        if self.target == "local":
            # Recover even if an older manual operation left HEAD on a
            # divergent branch while the last acknowledged snapshot stayed put.
            latest = read_json(self.state / "latest.json", {})
            anchor = latest.get("source_commit") if latest.get("status") == "PUBLISHED" else None
            if anchor and not re.fullmatch(r"[0-9a-f]{40}", anchor):
                raise PublicationError("INVALID_LOCAL_SNAPSHOT_ANCHOR")
            base = git(self.checkout, "rev-parse", "HEAD")
            if anchor and run(["git", "-C", str(self.checkout), "merge-base", "--is-ancestor",
                               anchor, base], check=False).returncode:
                base = git(self.checkout, "rev-parse", "--verify", f"{anchor}^{{commit}}")
        else:
            base = git(self.checkout, "rev-parse", "origin/main")
        # A previously interrupted owned snapshot is retained for diagnosis; reset
        # only tracked paths in this publisher-owned disposable checkout.
        git(self.checkout, "reset", "--hard", base)
        if (self.checkout / "PUBLISHED_LIBRARY.json").is_symlink():
            raise PublicationError("REMOTE_SYMLINK_COLLISION", "PUBLISHED_LIBRARY.json")
        old = load_library(self.checkout)
        previous = {x["path"]: x for x in old.get("files", [])}
        remote_blobs = {}
        for record in git(self.checkout, "ls-tree", "-r", "-z", "HEAD").split("\0"):
            if record:
                meta, path = record.split("\t", 1)
                remote_blobs[path] = meta.split()[2]
        cache = read_json(self.state / "hash-cache.json", {})
        new_cache, files, stamps, large, changed = {}, [], [], [], []
        extra_bindings = []
        for binding in desc.get("additional_source_bindings", []):
            source = Path(binding["source_path"])
            before = stamp(source)
            if before[2] != binding["bytes"] or file_hash(source) != binding["sha256"]:
                raise PublicationError("DUE_SUPPORT_INPUT_DRIFT", str(source))
            extra_bindings.append((source, before))
        absent_bindings = [Path(binding["source_path"]) for binding in desc.get("additional_absent_bindings", [])]
        for source in absent_bindings:
            if source.exists() or source.is_symlink():
                raise PublicationError("DUE_SUPPORT_ABSENT_INPUT_APPEARED", str(source))
        watched_dirs = {}
        for row in desc["sources"]:
            path = Path(row["source_path"])
            watched_dirs[path.parent] = stamp(path.parent)
            if path.is_relative_to(self.repo):
                for parent in path.parents:
                    watched_dirs[parent] = stamp(parent)
                    if parent == self.repo:
                        break
        for row in desc["sources"]:
            source, target = Path(row["source_path"]), row["publish_path"]
            before = stamp(source)
            expected = row.get("sha256", row.get("expected_sha256"))
            cached = cache.get(str(source), {})
            if cached.get("stamp") == before and cached.get("secret_scan") == "v1" and cached.get("git_oid"):
                sha, git_oid = cached["sha256"], cached["git_oid"]
            else:
                sha, git_oid = file_hash(source, scan=True, with_git_oid=True)
            if expected and sha != expected:
                raise PublicationError("SOURCE_HASH_CHANGED", target)
            if row.get("bytes") is not None and row["bytes"] != before[2]:
                raise PublicationError("SOURCE_SIZE_CHANGED", target)
            destination = self.checkout / target
            # Never follow an existing remote symlink during assembly.
            for ancestor in [destination, *destination.parents]:
                if ancestor == self.checkout:
                    break
                if ancestor.is_symlink():
                    raise PublicationError("REMOTE_SYMLINK_COLLISION", target)
            destination.parent.mkdir(parents=True, exist_ok=True)
            prior = previous.get(target, {})
            storage = ("git-lfs" if before[2] >= LIMIT or row.get("storage") in {"lfs", "git-lfs"} else "git")
            expected_git_oid = git_oid
            if storage == "git-lfs":
                pointer = f"version https://git-lfs.github.com/spec/v1\noid sha256:{sha}\nsize {before[2]}\n".encode()
                expected_git_oid = hashlib.sha1(f"blob {len(pointer)}\0".encode() + pointer).hexdigest()
            same = (prior.get("sha256") == sha and destination.exists()
                    and remote_blobs.get(target) == expected_git_oid)
            if not same and prior.get("sha256") == sha and destination.is_file():
                # Reuse an earlier PREPARED snapshot as well as published blobs.
                # First adoption hashes the staged bytes; subsequent unchanged
                # paths use their inode/size/mtime/ctime stamp, never a blind copy.
                known_stage = cached.get("stage_stamps", {}).get(target)
                same = (known_stage == stamp(destination) or
                        (destination.stat().st_size == before[2] and file_hash(destination) == sha))
            if not same:
                with source.open("rb") as inp, destination.open("wb") as out:
                    shutil.copyfileobj(inp, out, CHUNK)
                if file_hash(destination) != sha:
                    raise PublicationError("COPY_HASH_MISMATCH", target)
                changed.append(target)
            if stamp(source) != before:
                raise PublicationError("SOURCE_CHANGED_DURING_COPY", target)
            stamps.append((source, before))
            stage_stamps = dict(cached.get("stage_stamps", {}))
            stage_stamps[target] = stamp(destination)
            new_cache[str(source)] = {"stamp": before, "sha256": sha, "git_oid": git_oid,
                                      "secret_scan": "v1", "stage_stamps": stage_stamps}
            entry = {"path": target, "kind": row["kind"], "sha256": sha, "bytes": before[2],
                     "git_blob_sha1": expected_git_oid}
            for key in ("formal_ids", "original_reference", "source_id", "page", "page_number",
                        "original_sha256", "required", "role", "source_relative_path", "state_source_version", "visibility"):
                if key in row:
                    entry[key] = row[key]
            if not source.is_relative_to(self.repo) or target.startswith("published-originals/"):
                entry["original_reference"] = row.get("original_reference", str(source))
            if storage == "git-lfs":
                entry["storage"] = "git-lfs"
                large.append({"path": target, "bytes": before[2], "sha256": sha})
            else:
                entry["storage"] = "git"
            files.append(entry)
        for source, before in stamps:
            if stamp(source) != before:
                raise PublicationError("SOURCE_CHANGED_AFTER_COPY", str(source))
        for directory, before in watched_dirs.items():
            if stamp(directory) != before:
                raise PublicationError("SOURCE_INVENTORY_CHANGED", str(directory))
        # Preserve all earlier exported historical files even if a later adapter
        # no longer enumerates them. Their original bytes and git history remain.
        # Personalization is the exception: every export re-enumerates its whole
        # reachable generation, so an earlier generation is dead weight that
        # would otherwise accumulate inside the snapshot tree forever.
        current = {x["path"] for x in files}
        personal_prefix = "personalization/%s/" % self.subject
        retained = []
        for path, entry in previous.items():
            if path not in current:
                if path.startswith(personal_prefix):
                    continue
                path_check(path)
                if not (self.checkout / path).is_file():
                    raise PublicationError("PUBLISHED_HISTORY_MISSING", path)
                retained.append(dict(entry, retained_from_earlier_publication=True))
        files.extend(retained)
        gaps = list(desc.get("gaps", []))
        verified_lfs = self.verified_lfs_cache(large)
        new_lfs = [row for row in large if row["sha256"] not in verified_lfs]
        if self.target == "github" and new_lfs and not lfs_verified:
            gaps.append({"code": "LFS_BUDGET_UNVERIFIED", "files": len(large),
                         "bytes": sum(x["bytes"] for x in new_lfs)})
        manifest = {"schema_version": 1, "subject": self.subject,
                    "repository": self.remote, "branch": "main",
                    "publication_target": self.target,
                    "github_backup_required": self.target == "github",
                    "local_version": desc.get("local_version") or digest(files),
                    "event_id": event, "history_index": desc.get("history_index", []),
                    "entrypoints": desc.get("entrypoints", {}),
                    "counts": {"files": len(files), "bytes": sum(x["bytes"] for x in files),
                               "current_files": len(current), "retained_files": len(retained),
                               "lfs_files": len(large)},
                    "source_counts": desc.get("counts", {}),
                    "saved_materials_status": "ALL_DECLARED_EXISTING_FILES_INCLUDED",
                    "coverage_status": "PARTIAL" if gaps else "COMPLETE",
                    "coverage_scope": "declared-existing-subject-history-and-originals",
                    "web_read_status": "NOT_YET_VERIFIED", "gaps": gaps,
                    "files": sorted(files, key=lambda x: x["path"])}
        for key in ("originals", "original_mappings", "question_refs", "source_stability", "exclusions", "history_state",
                    "personalization", "formal_version", "formal_source_hashes", "native_math_workflow"):
            if key in desc:
                manifest[key] = desc[key]
        for key in ("formal_node_count", "canonical_exam_count", "file_count"):
            if key in desc:
                manifest["source_counts"][key] = desc[key]
        manifest["web_read"] = ({"schema": "study-mcp-local-read-v1", "entrypoint": "Study Library.library",
                                 "status": "NOT_YET_VERIFIED", "manual_base64_transport_required": False}
                                if self.target == "local" else
                                {"schema": "study-web-read-v1", "entrypoint": "WEB_READ_START.json",
                                 "status": "NOT_YET_VERIFIED", "fetch_max_lines": 8})
        write_library(self.checkout, manifest)
        # These are deterministic transport copies of the same snapshot. They
        # neither replace originals nor certify model reading or image viewing.
        published_manifest = load_library(self.checkout)
        web = (study_web_read.build_web_read(self.checkout, published_manifest, extra_web_binary_paths)
               if self.target == "github" else None)
        for source, before in extra_bindings:
            if stamp(source) != before:
                raise PublicationError("DUE_SUPPORT_INPUT_DRIFT", str(source))
            new_cache.setdefault(str(source), {"stamp": before, "binding_only": True})
        for source in absent_bindings:
            if source.exists() or source.is_symlink():
                raise PublicationError("DUE_SUPPORT_ABSENT_INPUT_APPEARED", str(source))
            new_cache[str(source)] = {"absent": True, "binding_only": True}
        if web is not None:
            web_cache = {"generated_files": web["generated_files"], "counts": web["counts"], "start": web["start"]}
            web_cache_name = "web-read-manifests/" + digest(web_cache) + ".json"
            write_json(self.state / web_cache_name, web_cache)
        write_json(self.state / "hash-cache.json", new_cache)
        prior_request = read_json(self.receipt_path(event), {})
        result = {"status": "PREPARED", "subject": self.subject, "event_id": event,
                  "repository": self.remote, "branch": "main",
                  "publication_target": self.target, "network_started": False,
                  "request_origin": prior_request.get("request_origin", "manual"),
                  "requested_at": prior_request.get("requested_at"),
                  "base_commit": base, "local_version": manifest["local_version"],
                  "manifest_sha256": file_hash(self.checkout / "PUBLISHED_LIBRARY.json"),
                  "counts": manifest["counts"], "changed_files": len(changed),
                  "coverage_status": manifest["coverage_status"], "gaps": gaps,
                  "lfs": large, "source_stable": True, "secret_scan": "PASS",
                  "stage": str(self.checkout), "prepared_at": now(),
                  "source_cache": {"path": "hash-cache.json", "sha256": file_hash(self.state / "hash-cache.json"),
                                   "count": len(new_cache)}}
        if web is not None:
            result["web_transport"] = {"path": web_cache_name, "sha256": file_hash(self.state / web_cache_name),
                                       "start": web["start"], "counts": web["counts"]}
        write_json(self.receipt_path(event), result)
        return result

    def publish_prepared(self, receipt: dict[str, Any], lfs_verified: bool) -> dict[str, Any]:
        event = receipt["event_id"]
        manifest_path = self.checkout / "PUBLISHED_LIBRARY.json"
        if file_hash(manifest_path) != receipt["manifest_sha256"]:
            raise PublicationError("STAGED_MANIFEST_CHANGED")
        manifest = load_library(self.checkout)
        if receipt.get("source_cache"):
            reference = receipt["source_cache"]
            cache_path = self.state / reference["path"]
            if reference["path"] != "hash-cache.json" or file_hash(cache_path) != reference["sha256"]:
                raise PublicationError("SOURCE_SNAPSHOT_CHANGED")
            cache = read_json(cache_path)
            if len(cache) != reference["count"]:
                raise PublicationError("SOURCE_SNAPSHOT_CHANGED")
            stamps = ((path, None if row.get("absent") else row["stamp"]) for path, row in cache.items())
        else:
            stamps = ((row["source_path"], row["stamp"]) for row in receipt.get("sources", []))
        for source, expected_stamp in stamps:
            if expected_stamp is None:
                if Path(source).exists() or Path(source).is_symlink():
                    raise PublicationError("DUE_SUPPORT_ABSENT_INPUT_APPEARED", source)
                continue
            if stamp(Path(source)) != expected_stamp:
                raise PublicationError("SOURCE_CHANGED_SINCE_PREPARE", source)
        verified_lfs = self.verified_lfs_cache(receipt.get("lfs", []))
        new_lfs = [row for row in receipt.get("lfs", []) if row["sha256"] not in verified_lfs]
        if self.target == "github" and new_lfs and not lfs_verified:
            raise PublicationError("LFS_BUDGET_UNVERIFIED", {"bytes": sum(x["bytes"] for x in new_lfs)})
        if self.target == "local":
            if git(self.checkout, "rev-parse", "HEAD") != receipt["base_commit"]:
                raise PublicationError("LOCAL_HEAD_ADVANCED_REPREPARE_REQUIRED")
        elif self.remote_head() != receipt["base_commit"]:
            raise PublicationError("REMOTE_ADVANCED_REPREPARE_REQUIRED")
        explicit = [x["path"] for x in manifest["files"]] + ["PUBLISHED_LIBRARY.json"]
        explicit.extend(x["path"] for key, _ in INVENTORY_FIELDS for x in manifest.get(key, []))
        web_files = []
        if receipt.get("web_transport"):
            reference = receipt["web_transport"]
            relative = path_check(reference["path"])
            if not relative.startswith("web-read-manifests/"):
                raise PublicationError("INVALID_WEB_TRANSPORT_CACHE")
            web_path = self.state / relative
            if file_hash(web_path) != reference["sha256"]:
                raise PublicationError("WEB_TRANSPORT_CACHE_CHANGED")
            web_files = read_json(web_path)["generated_files"]
            for entry in web_files:
                path = self.checkout / path_check(entry["path"])
                if path.stat().st_size != entry["bytes"] or file_hash(path) != entry["sha256"]:
                    raise PublicationError("WEB_TRANSPORT_BYTES_CHANGED", entry["path"])
            explicit.extend(entry["path"] for entry in web_files)
        # The published tree mirrors the current personalization generation.
        # Deleted paths stay readable through their own historical commit.
        personal_prefix = "personalization/%s/" % self.subject
        allowed = set(explicit)
        stale = []
        for record in git(self.checkout, "ls-tree", "-r", "-z", "HEAD").split("\0"):
            if record:
                _meta, path = record.split("\t", 1)
                if path.startswith(personal_prefix) and path not in allowed:
                    stale.append(path)
        if stale:
            for path in stale:
                target = self.checkout / path_check(path)
                if target.is_file():
                    target.unlink()
            git(self.checkout, "--literal-pathspecs", "update-index", "--force-remove", "-z", "--stdin",
                data=b"\0".join(p.encode() for p in stale) + b"\0", timeout=600)
            allowed.update(stale)
        if receipt.get("lfs"):
            git(self.checkout, "lfs", "install", "--local")
        attrs = self.checkout / ".gitattributes"
        if attrs.is_symlink():
            raise PublicationError("REMOTE_SYMLINK_COLLISION", ".gitattributes")
        existing = attrs.read_text() if attrs.exists() else ""
        begin, end = "# BEGIN STUDY PUBLICATION byte-preserving paths", "# END STUDY PUBLICATION byte-preserving paths"
        existing = re.sub(re.escape(begin) + r"\n.*?" + re.escape(end) + r"\n?", "", existing, flags=re.S)
        rules = []
        source_paths = {x["path"] for x in manifest["files"]}
        attr_entries = manifest["files"] + [{"path": path, "storage": "git"} for path in explicit if path not in source_paths]
        for entry in attr_entries:
            # C-style quoting handles whitespace; glob metacharacters remain
            # explicitly escaped after Git unquotes the path token.
            pattern = "".join("\\" + char if char in "*?[]\\" else char for char in entry["path"])
            rule = json.dumps(pattern, ensure_ascii=False)
            rule += " filter=lfs diff=lfs merge=lfs -text" if entry.get("storage") == "git-lfs" else " -text -filter"
            rules.append(rule)
        attrs.write_text(existing.rstrip("\n") + "\n" + begin + "\n" + "\n".join(rules) + "\n" + end + "\n")
        explicit.append(".gitattributes")
        allowed.add(".gitattributes")
        # Explicit NUL-delimited paths avoid argv limits, ignored business data,
        # wildcard interpretation, and accidental staging of unrelated files.
        git(self.checkout, "--literal-pathspecs", "add", "-f", "--pathspec-from-file=-",
            "--pathspec-file-nul", data=b"\0".join(p.encode() for p in explicit) + b"\0", timeout=600)
        actual = git(self.checkout, "diff", "--cached", "--name-only", "-z").split("\0")
        unexpected = sorted(set(actual) - {""} - allowed)
        if unexpected:
            raise PublicationError("UNEXPECTED_STAGED_FILE", unexpected[:5])
        staged = {}
        for record in git(self.checkout, "ls-files", "--stage", "-z").split("\0"):
            if record:
                meta, path = record.split("\t", 1)
                staged[path] = meta.split()[1]
        for entry in manifest["files"] + web_files:
            if entry.get("git_blob_sha1") and staged.get(entry["path"]) != entry["git_blob_sha1"]:
                raise PublicationError("STAGED_BYTES_CHANGED", entry["path"])
        # Large originals use local LFS objects in either mode; only GitHub mode
        # uploads them. Snapshot pointers must always bind their actual bytes.
        for entry in receipt.get("lfs", []):
            pointer = git(self.checkout, "show", ":" + entry["path"])
            expected = f"oid sha256:{entry['sha256']}\nsize {entry['bytes']}"
            if not pointer.startswith("version https://git-lfs.github.com/spec/v1\n") or expected not in pointer:
                raise PublicationError("LFS_POINTER_MISMATCH", entry["path"])
        if actual != [""]:
            git(self.checkout, "commit", "-m", f"Publish {self.subject} study library ({event})", timeout=600)
        candidate = git(self.checkout, "rev-parse", "HEAD")
        receipt.update(status="COMMITTED_PENDING_LOCAL_VERIFY" if self.target == "local" else "COMMITTED_PENDING_PUSH",
                       candidate_commit=candidate, committed_at=now())
        write_json(self.receipt_path(event), receipt)
        return self.resume_push(receipt, lfs_verified=lfs_verified)

    def finish_local(self, receipt: dict[str, Any]) -> dict[str, Any]:
        candidate, event = receipt["candidate_commit"], receipt["event_id"]
        raw, manifest = committed_library(self.checkout, candidate)
        if hashlib.sha256(raw).hexdigest() != receipt["manifest_sha256"]:
            raise PublicationError("LOCAL_SNAPSHOT_MANIFEST_CHANGED")
        if self.subject == "math" and (quality := _math_quality_module(self.repo)):
            if quality.active(self.repo) or receipt.get("formal_quality_generation_sha256"):
                try:
                    personal = json.loads(git(self.checkout, "show", f"{candidate}:personalization/math/manifest.json"))
                    binding = quality.binding_is_expected(self.repo, event, personal)
                    if (not binding or binding["generation_sha256"] != receipt.get("formal_quality_generation_sha256")
                            or any(personal[k] != manifest["personalization"][k]
                                   for k in ("version", "formal_version"))):
                        raise PublicationError("MATH_FORMAL_QUALITY_COMMIT_MISMATCH")
                except (quality.QualityError, KeyError) as exc:
                    raise PublicationError("MATH_FORMAL_QUALITY_COMMIT_MISMATCH", str(exc)) from exc
        actual = {}
        for row in git(self.checkout, "ls-tree", "-r", "-z", candidate).split("\0"):
            if row:
                meta, path = row.split("\t", 1)
                actual[path] = meta.split()[2]
        lfs_objects = {}
        for entry in manifest["files"]:
            if actual.get(entry["path"]) != entry.get("git_blob_sha1"):
                raise PublicationError("LOCAL_SNAPSHOT_BYTES_CHANGED", entry["path"])
            if entry.get("storage") == "git-lfs":
                sha = entry["sha256"]
                if sha not in lfs_objects:
                    path = local_lfs_object(self.state, sha, entry["bytes"])
                    lfs_objects[sha] = {"bytes": entry["bytes"], "stamp": stamp(path), "local_verified_at": now()}
        # Keep every acknowledged snapshot reachable even if a later optional
        # backup operation changes the staging branch. Never expose partial HEAD.
        git(self.checkout, "update-ref", f"refs/study-snapshots/{candidate}", candidate)
        receipt.update(status="PUBLISHED", publication_target="local", source_commit=candidate,
                       snapshot_id=candidate, local_snapshot_verified_at=now(), network_started=False,
                       github_backup_required=False, github_backup_performed=False,
                       local_lfs_verified_objects=lfs_objects, web_read_status="NOT_YET_VERIFIED")
        write_json(self.receipt_path(event), receipt)
        latest = read_json(self.state / "latest.json", {})
        previous = latest.get("source_commit")
        if (not previous or previous == candidate or run(["git", "-C", str(self.checkout), "merge-base",
                "--is-ancestor", previous, candidate], check=False).returncode == 0):
            write_json(self.state / "latest.json", public_receipt(receipt))
        return receipt

    def resume_push(self, receipt: dict[str, Any], lfs_verified: bool = False) -> dict[str, Any]:
        if self.target == "local":
            if receipt.get("status") == "PUBLISHED":
                return receipt
            return self.finish_local(receipt)
        candidate, event = receipt["candidate_commit"], receipt["event_id"]
        observed = self.remote_head()  # Always resolve an unknown previous push first.
        entries = receipt.get("lfs", [])
        cached_lfs = self.verified_lfs_cache(entries)
        new_lfs = [row for row in entries if row["sha256"] not in cached_lfs]
        if new_lfs and not lfs_verified:
            raise PublicationError("LFS_BUDGET_UNVERIFIED", {"bytes": sum(row["bytes"] for row in new_lfs)})
        if observed != candidate:
            git(self.checkout, "fetch", "--no-tags", "origin", "main", timeout=300)
            ancestor = run(["git", "-C", str(self.checkout), "merge-base", "--is-ancestor",
                            candidate, "origin/main"], check=False).returncode == 0
            if not ancestor:
                if observed != receipt["base_commit"]:
                    raise PublicationError("REMOTE_ADVANCED_WITH_PENDING_COMMIT",
                                           {"candidate_commit": candidate, "remote_commit": observed})
                receipt.update(status="PUSH_RESULT_UNKNOWN", push_attempted_at=now())
                write_json(self.receipt_path(event), receipt)
                try:
                    git(self.checkout, "-c", "pack.threads=2", "-c", "pack.windowMemory=64m",
                        "push", "origin", f"{candidate}:refs/heads/main", timeout=1200,
                        extra_env={"GIT_LFS_SKIP_PUSH": "1"} if entries and not new_lfs else None)
                except PublicationError:
                    if self.remote_head() != candidate:
                        raise
                observed = self.remote_head()
                if observed != candidate:
                    raise PublicationError("REMOTE_VERIFICATION_FAILED")
        receipt.update(status="PUBLISHED", source_commit=candidate, publication_target="github",
                       github_backup_performed=True, remote_head_observed=observed, remote_ref_verified_at=now(),
                       web_read_status="NOT_YET_VERIFIED")
        if entries:
            # A separate empty-first cache proves actual remote object download;
            # upload-side bytes or a successful Git pointer push are insufficient.
            storage = self.state / "lfs-readback"
            if new_lfs:
                previous_storage = git(self.checkout, "config", "--local", "--get", "lfs.storage", check=False)
                git(self.checkout, "config", "--local", "lfs.storage", str(storage))
                try:
                    git(self.checkout, "lfs", "fetch", "--include=", "--exclude=", "origin", candidate, timeout=1200)
                finally:
                    if previous_storage:
                        git(self.checkout, "config", "--local", "lfs.storage", previous_storage)
                    else:
                        git(self.checkout, "config", "--local", "--unset", "lfs.storage", check=False)
            proofs = dict(read_json(self.state / "latest.json", {}).get("lfs_verified_objects", {}))
            for entry in entries:
                sha = entry["sha256"]
                downloaded = storage / "objects" / sha[:2] / sha[2:4] / sha
                if sha in cached_lfs:
                    proofs[sha] = cached_lfs[sha]
                    continue
                if (not downloaded.is_file() or downloaded.stat().st_size != entry["bytes"]
                        or file_hash(downloaded) != sha):
                    raise PublicationError("LFS_REMOTE_BYTES_UNVERIFIED", entry["path"])
                proofs[sha] = {"bytes": entry["bytes"], "stamp": stamp(downloaded), "remote_verified_at": now()}
            receipt.update(lfs_remote_bytes_verified_at=max(proofs[row["sha256"]]["remote_verified_at"] for row in entries),
                           lfs_object_count=len(entries), lfs_verified_objects=proofs,
                           lfs_readback_reused=not bool(new_lfs))
        write_json(self.receipt_path(event), receipt)
        latest = read_json(self.state / "latest.json", {})
        previous_commit = latest.get("source_commit")
        newest = (not previous_commit or previous_commit == candidate or run(
            ["git", "-C", str(self.checkout), "merge-base", "--is-ancestor", previous_commit, candidate],
            check=False).returncode == 0)
        if newest:
            write_json(self.state / "latest.json", public_receipt(receipt))
        return receipt

    def _legacy_backup(self, event: str, prepared_hash: str | None) -> dict[str, Any] | None:
        """Reconcile an explicitly requested historical backup, leaving its original evidence in place."""
        legacy = self.subject_state / "receipts" / self.receipt_path(event).name
        row = read_json(legacy)
        if not row or row.get("publication_target", "github") != "github":
            return None
        if (row.get("event_id") != event or row.get("subject") != self.subject
                or row.get("repository") != self.remote or row.get("branch") != "main"):
            raise PublicationError("HISTORICAL_BACKUP_IDENTITY_MISMATCH")
        # Uncommitted, unapproved requests can be prepared independently.
        candidate = row.get("candidate_commit") or row.get("source_commit")
        if not candidate and prepared_hash is None:
            return None
        with locked(self.subject_state / "publication.lock"):
            row_raw = legacy.read_bytes()
            if json.loads(row_raw) != row:
                raise PublicationError("HISTORICAL_BACKUP_RECEIPT_CHANGED")
            old_checkout = self.subject_state / "checkout"
            recovered = dict(row, publication_target="github", legacy_receipt_path=str(legacy),
                             legacy_receipt_sha256=hashlib.sha256(row_raw).hexdigest())

            def copy_verified(relative, expected, old_root, new_root, *, lfs_bytes=None):
                name = path_check(relative)
                old, new = old_root / name, new_root / name
                if not old.is_file() or old.is_symlink() or not old.resolve().is_relative_to(old_root.resolve()):
                    raise PublicationError("HISTORICAL_BACKUP_SOURCE_UNAVAILABLE", name)
                if new.is_symlink() or not new.resolve().is_relative_to(new_root.resolve()):
                    raise PublicationError("HISTORICAL_BACKUP_DESTINATION_UNSAFE", name)
                if file_hash(old) != expected:
                    if lfs_bytes is None:
                        raise PublicationError("HISTORICAL_BACKUP_SOURCE_CHANGED", name)
                    old = local_lfs_object(self.subject_state, expected, lfs_bytes)
                new.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(old, new)
                if file_hash(new) != expected:
                    raise PublicationError("HISTORICAL_BACKUP_COPY_MISMATCH", name)

            web = row.get("web_transport")
            if web:
                if not web["path"].startswith("web-read-manifests/"):
                    raise PublicationError("INVALID_WEB_TRANSPORT_CACHE")
                copy_verified(web["path"], web["sha256"], self.subject_state, self.state)
            if candidate:
                if not re.fullmatch(r"[0-9a-f]{40}", candidate):
                    raise PublicationError("INVALID_HISTORICAL_BACKUP_COMMIT")
                if run(["git", "-C", str(self.checkout), "cat-file", "-e", candidate], check=False).returncode:
                    # Fetch the already committed bytes from our prior LOCAL store.
                    git(self.checkout, "fetch", "--no-tags", str(old_checkout), candidate)
                committed, _ = committed_library(self.checkout, candidate)
                if hashlib.sha256(committed).hexdigest() != row["manifest_sha256"]:
                    raise PublicationError("HISTORICAL_BACKUP_MANIFEST_CHANGED")
                recovered["candidate_commit"] = candidate
                remote_contains = run(["git", "-C", str(self.checkout), "merge-base", "--is-ancestor",
                                       candidate, "origin/main"], check=False).returncode == 0
                if row.get("status") == "PUBLISHED":
                    if not remote_contains:
                        raise PublicationError("HISTORICAL_BACKUP_NO_LONGER_ON_REMOTE")
                    return recovered
                # A candidate already present remotely only needs the normal
                # budget-authorized download/readback. Upload bytes are needed
                # solely to finish a candidate that still has to be pushed.
                for asset in ([] if remote_contains else row.get("lfs", [])):
                    sha = asset["sha256"]
                    old = local_lfs_object(self.subject_state, sha, asset["bytes"])
                    new = self.checkout / ".git/lfs/objects" / sha[:2] / sha[2:4] / sha
                    new.parent.mkdir(parents=True, exist_ok=True)
                    if not new.exists():
                        shutil.copyfile(old, new)
                    local_lfs_object(self.state, sha, asset["bytes"])
            else:
                if row.get("status") != "PREPARED" or row.get("manifest_sha256") != prepared_hash:
                    raise PublicationError("PREPARED_MANIFEST_BINDING_MISMATCH")
                base = row.get("base_commit")
                if not isinstance(base, str) or not re.fullmatch(r"[0-9a-f]{40}", base):
                    raise PublicationError("INVALID_HISTORICAL_BACKUP_COMMIT")
                git(self.checkout, "reset", "--hard", base)
                copy_verified("PUBLISHED_LIBRARY.json", prepared_hash, old_checkout, self.checkout)
                manifest = read_json(self.checkout / "PUBLISHED_LIBRARY.json")
                for shard in (row for key, _ in INVENTORY_FIELDS for row in manifest.get(key, [])):
                    copy_verified(shard["path"], shard["sha256"], old_checkout, self.checkout)
                files = load_library(self.checkout, manifest)["files"]
                if web:
                    files = files + read_json(self.state / web["path"])["generated_files"]
                for entry in files:
                    copy_verified(entry["path"], entry["sha256"], old_checkout, self.checkout,
                                  lfs_bytes=entry["bytes"] if entry.get("storage") == "git-lfs" else None)
                if row.get("source_cache"):
                    ref = row["source_cache"]
                    if ref["path"] != "hash-cache.json":
                        raise PublicationError("SOURCE_SNAPSHOT_CHANGED")
                    copy_verified(ref["path"], ref["sha256"], self.subject_state, self.state)
            write_json(self.receipt_path(event), recovered)
            return recovered

    def execute(self, event: str, description: dict[str, Any] | None = None,
                prepare_only: bool = False, lfs_verified: bool = False,
                prepared_manifest_sha256: str | None = None,
                wait_for_lock: bool = False, extra_web_binary_paths: set[str] | None = None) -> dict[str, Any]:
        if not event.strip() or len(event) > 200 or any(ord(c) < 32 for c in event):
            raise PublicationError("INVALID_EVENT_ID")
        with locked(self.state / "publication.lock", blocking=wait_for_lock), \
                _formal_quality_window(self.subject, self.repo, event) as formal_quality:
            metadata = self.remote_metadata()
            self.initialize()
            prior = read_json(self.receipt_path(event))
            if prior is None and self.target == "github":
                prior = self._legacy_backup(event, prepared_manifest_sha256)
                if prior and prior.get("status") == "PUBLISHED":
                    return public_receipt(prior)
            if prior and prior.get("status") == "PUBLISHED":
                # Idempotency belongs to this formal event, not to today's files.
                return public_receipt(self.resume_push(prior, lfs_verified=lfs_verified))
            if prior and prior.get("candidate_commit"):
                return public_receipt(self.resume_push(prior, lfs_verified=lfs_verified))
            if prepared_manifest_sha256 is not None:
                if (not prior or prior.get("status") != "PREPARED"
                        or prior.get("manifest_sha256") != prepared_manifest_sha256):
                    raise PublicationError("PREPARED_MANIFEST_BINDING_MISMATCH")
                return public_receipt(self.publish_prepared(prior, lfs_verified))
            desc = normalize(description, self.subject) if description is not None else describe(self.subject, self.repo, event)
            if self.subject == "408" and not self.test_mode:
                due_script = self.repo / "scripts/study_publication_due_408.py"
                support = json.loads(run([sys.executable, str(due_script), "export", "--repo", str(self.repo),
                                          "--output-dir", str(self.state / "due-support-export")], timeout=300).stdout)
                sources = {row["publish_path"]: row for row in desc["sources"]}
                for row in support["sources"]:
                    target = row["publish_path"]
                    if target in sources:
                        previous = sources[target]
                        original = Path(previous["source_path"])
                        actual_sha = previous.get("sha256") or file_hash(original)
                        actual_size = previous.get("bytes", original.stat().st_size)
                        if actual_sha != row["sha256"] or actual_size != row["bytes"]:
                            raise PublicationError("DUE_SUPPORT_INPUT_DRIFT", target)
                        row = dict(previous, source_path=row["source_path"], sha256=row["sha256"], bytes=row["bytes"])
                    sources[target] = row
                desc["sources"] = list(sources.values())
                desc["entrypoints"].update(support["merge_entrypoints"])
                desc["additional_source_bindings"] = support["input_bindings"]
                desc["additional_absent_bindings"] = support.get("absent_input_bindings", [])
                desc = normalize(desc, self.subject)
            if self.subject == "english" and any(row.get("pages") for row in desc.get("original_mappings", [])):
                source_description = self.state / "page-source-description.json"
                page_description = self.state / "page-descriptors.json"
                write_json(source_description, desc)
                run([str(PDF_RUNTIME), str(BASE / "study_publication.py"), "export-pdf-pages",
                     "--description", str(source_description), "--cache-root", str(self.state / "page-cache"),
                     "--output", str(page_description)], timeout=1200)
                pages = read_json(page_description)["sources"]
                desc["sources"].extend(pages)
                desc = normalize(desc, self.subject)
            prepared = self.prepare(event, desc, lfs_verified, extra_web_binary_paths)
            if formal_quality and self.subject == "math":
                personal = read_json(self.checkout / "personalization/math/manifest.json", {})
                if personal.get("formal_quality") != formal_quality:
                    raise PublicationError("MATH_FORMAL_QUALITY_PREPARED_MISMATCH")
                prepared["formal_quality_generation_sha256"] = formal_quality["generation_sha256"]
            elif formal_quality:
                generation = read_json(self.repo / formal_quality["generation_ref"])
                personal = read_json(self.checkout / "personalization/408/manifest.json", {})
                if (personal.get("source_version") != generation["source_version"]
                        or personal.get("version") != generation["personal_version"]):
                    raise PublicationError("FORMAL_QUALITY_PERSONAL_VERSION_MISMATCH")
                prepared["formal_quality_generation_sha256"] = Path(formal_quality["generation_ref"]).stem
            prepared["local_snapshot_metadata" if self.target == "local" else "remote_metadata"] = metadata
            write_json(self.receipt_path(event), prepared)
            if prepare_only:
                return public_receipt(prepared)
            return public_receipt(self.publish_prepared(prepared, lfs_verified))

    def compact_prepared_receipt(self, receipt: dict[str, Any]) -> dict[str, Any]:
        """Reuse the existing hash cache; do not copy its full source list into receipts."""
        if receipt.get("status") != "PREPARED" or not receipt.get("sources"):
            return receipt
        cache_path = self.state / "hash-cache.json"
        cache = read_json(cache_path, {})
        expected = {row["source_path"]: row["stamp"] for row in receipt["sources"]}
        if expected != {path: row["stamp"] for path, row in cache.items()}:
            raise PublicationError("SOURCE_SNAPSHOT_CHANGED")
        receipt = dict(receipt)
        receipt.pop("sources")
        receipt["source_cache"] = {"path": "hash-cache.json", "sha256": file_hash(cache_path), "count": len(cache)}
        write_json(self.receipt_path(receipt["event_id"]), receipt)
        return receipt


def public_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in receipt.items() if k not in {"sources", "lfs"}}


def mark_pending(subject: str, event_id: str, state_dir: Path = BASE / "publication") -> dict[str, Any]:
    """Zero-network postcommit marker using the publication's one event receipt."""
    if subject not in SUBJECTS or not event_id.strip() or len(event_id) > 200:
        raise PublicationError("INVALID_EVENT_ID")
    publisher = Publisher(subject, SUBJECTS[subject]["root"], state_dir, local_only=True)
    with locked(publisher.state / "request.lock", blocking=True):
        path = publisher.receipt_path(event_id)
        prior = read_json(path)
        if prior:
            return public_receipt(prior)
        receipt = {"status": "PENDING", "subject": subject, "event_id": event_id,
                   "repository": publisher.remote, "branch": "main", "created_at": now(),
                   "publication_target": publisher.target}
        write_json(path, receipt)
        return receipt


def verify_published(subject: str, source_commit: str,
                     state_dir: Path = BASE / "publication", *, verify_remote: bool = False) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        raise PublicationError("INVALID_SOURCE_COMMIT")
    if verify_remote and configured_target(state_dir) != "github":
        raise PublicationError("GITHUB_BACKUP_NOT_CONFIGURED")
    publisher = Publisher(subject, SUBJECTS[subject]["root"], state_dir, local_only=not verify_remote)
    roots = [publisher.state]
    if verify_remote:
        roots.append(publisher.subject_state)  # Pre-isolation, already published GitHub history.
    receipts = []
    for root in roots:
        for path in sorted((root / "receipts").glob("*.json")):
            row = read_json(path)
            if (row.get("subject") == subject and row.get("status") == "PUBLISHED"
                    and row.get("source_commit") == source_commit
                    and row.get("repository") == publisher.remote and row.get("branch") == "main"
                    and (not verify_remote or row.get("publication_target", "github") == "github")):
                receipts.append((path, row))
        if receipts:
            break
    if not receipts:
        raise PublicationError("NO_CANONICAL_PUBLICATION_RECEIPT")
    with locked(publisher.state / "publication.lock"):
        offline = not verify_remote
        if offline:
            if not (publisher.checkout / ".git").exists():
                raise PublicationError("LOCAL_SNAPSHOT_STORE_UNAVAILABLE")
            result = run(["git", "-C", str(publisher.checkout), "cat-file", "-t", source_commit], check=False)
            if result.returncode or result.stdout.strip() != b"commit":
                raise PublicationError("LOCAL_SNAPSHOT_COMMIT_UNAVAILABLE")
        else:
            publisher.remote_metadata()
            publisher.initialize()
            result = run(["git", "-C", str(publisher.checkout), "merge-base", "--is-ancestor",
                          source_commit, "origin/main"], check=False)
            if result.returncode:
                raise PublicationError("SOURCE_COMMIT_NOT_ON_REMOTE_MAIN")
        raw_manifest = run(["git", "-C", str(publisher.checkout), "show",
                            f"{source_commit}:PUBLISHED_LIBRARY.json"]).stdout
        manifest = json.loads(raw_manifest)
        path, receipt = receipts[0]
        if (manifest.get("local_version") != receipt.get("local_version") or
                hashlib.sha256(raw_manifest).hexdigest() != receipt.get("manifest_sha256")):
            raise PublicationError("RECEIPT_VERSION_MISMATCH")
        manifest_path = publisher.state / "verified-manifests" / f"{source_commit}.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_bytes(raw_manifest)
        resolved = dict(manifest)
        for key, output in INVENTORY_FIELDS:
            if not manifest.get(key):
                continue
            records = []
            for shard in manifest[key]:
                path_check(shard["path"])
                raw = run(["git", "-C", str(publisher.checkout), "show", f"{source_commit}:{shard['path']}"]).stdout
                if hashlib.sha256(raw).hexdigest() != shard["sha256"]:
                    raise PublicationError("INVENTORY_HASH_MISMATCH", shard["path"])
                rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
                if len(rows) != shard["count"]:
                    raise PublicationError("INVENTORY_COUNT_MISMATCH", shard["path"])
                records.extend(rows)
            resolved[output] = records
        resolved_path = manifest_path.with_name(f"{source_commit}.resolved.json")
        write_json(resolved_path, resolved)
        verified_transport_paths = 0
        if receipt.get("web_transport"):
            reference = receipt["web_transport"]
            relative = path_check(reference["path"])
            if not relative.startswith("web-read-manifests/") or file_hash(path.parent.parent / relative) != reference["sha256"]:
                raise PublicationError("WEB_TRANSPORT_CACHE_CHANGED")
            expected = read_json(path.parent.parent / relative)["generated_files"]
            actual = {}
            tree = git(publisher.checkout, "ls-tree", "-r", "-z", source_commit)
            for record in tree.split("\0"):
                if record:
                    meta, relative_path = record.split("\t", 1)
                    actual[relative_path] = meta.split()[2]
            for entry in expected:
                if actual.get(entry["path"]) != entry["git_blob_sha1"]:
                    raise PublicationError("REMOTE_WEB_TRANSPORT_MISMATCH", entry["path"])
            start = reference["start"]
            start_bytes = run(["git", "-C", str(publisher.checkout), "show", f"{source_commit}:{start['path']}"]).stdout
            if len(start_bytes) != start["bytes"] or hashlib.sha256(start_bytes).hexdigest() != start["sha256"]:
                raise PublicationError("REMOTE_WEB_START_MISMATCH")
            verified_transport_paths = len(expected)
        return dict(public_receipt(receipt), verified=True, receipt_path=str(path),
                    manifest_path=str(manifest_path), resolved_manifest_path=str(resolved_path),
                    verified_at=now(), verified_transport_paths=verified_transport_paths,
                    verification_transport="local" if offline else "github", network_started=not offline,
                    web_read_status="NOT_YET_VERIFIED")


def request_publication(subject: str, event_id: str, repo_root: Path,
                        state_dir: Path = BASE / "publication") -> dict[str, Any]:
    """Call after releasing the subject transaction lock. Never wait on network.

    One inherited launch lock prevents both duplicate waiting processes and the
    lost wakeup between the worker's final pending check and process exit.
    """
    if subject not in SUBJECTS or Path(repo_root).resolve() != SUBJECTS[subject]["root"].resolve():
        return {"status": "NOT_PRODUCTION_REPO", "subject": subject, "network_started": False}
    if not event_id.strip() or len(event_id) > 200 or any(ord(c) < 32 for c in event_id):
        raise PublicationError("INVALID_EVENT_ID")
    if subject == "math" and (quality := _math_quality_module(repo_root)):
        marker = quality.active(repo_root)
        if marker and (marker["status"] != "READY_TO_PUBLISH" or marker["publication_event_id"] != event_id):
            return {"status": quality.HOLD, "subject": subject, "event_id": event_id, "network_started": False}
    publisher = Publisher(subject, Path(repo_root), state_dir, local_only=True)
    with locked(publisher.state / "request.lock", blocking=True):
        path = publisher.receipt_path(event_id)
        receipt = read_json(path, {"status": "PENDING", "subject": subject, "event_id": event_id,
                                  "repository": publisher.remote, "branch": "main", "created_at": now(),
                                  "publication_target": publisher.target})
        if receipt["status"] == "PUBLISHED":
            return dict(public_receipt(receipt), receipt_path=str(path), network_started=False)
        receipt.update(automatic_requested=True, request_origin="postcommit", requested_at=now(),
                       publication_target=publisher.target)
        write_json(path, receipt)
        if read_json(publisher.state / "latest.json", {}).get("status") != "PUBLISHED":
            return {"status": "PENDING", "subject": subject, "event_id": event_id,
                    "receipt_path": str(path), "network_started": False,
                    "baseline_review_required": True}
        launch = os.open(publisher.state / "launch.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(launch, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {"status": "ALREADY_RUNNING", "subject": subject, "event_id": event_id,
                        "receipt_path": str(path), "network_started": False}
            command = [sys.executable, str(BASE / "study_publication.py"), "run-pending",
                       "--subject", subject, "--state-dir", str(state_dir), "--lock-fd", str(launch)]
            log = (publisher.state / "worker.log").open("ab")
            try:
                worker_env = dict(os.environ)
                if subject == "math":
                    worker_env["KAOYAN_MATH_PROJECT_ROOT"] = str(Path(repo_root).resolve())
                elif subject == "english":
                    worker_env["KAOYAN_ENGLISH_PROJECT_ROOT"] = str(Path(repo_root).resolve())
                elif subject == "408":
                    worker_env["KAOYAN_408_PROJECT_ROOT"] = str(Path(repo_root).resolve())
                child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                         pass_fds=(launch,), start_new_session=True, cwd=BASE, env=worker_env)
            finally:
                log.close()
            return {"status": "STARTED", "subject": subject, "event_id": event_id,
                    "receipt_path": str(path), "pid": child.pid, "publication_target": publisher.target,
                    "snapshot_worker_started": True, "network_started": publisher.target == "github"}
        finally:
            os.close(launch)  # Child keeps the same flock open-file description.


def drain_pending(subject: str, state_dir: Path, launch_fd: int) -> int:
    """One finite postcommit worker. No schedule, polling, model, or error retry."""
    publisher = Publisher(subject, SUBJECTS[subject]["root"], state_dir, local_only=True)
    while True:
        with locked(publisher.state / "request.lock", blocking=True):
            all_receipts = [(path, read_json(path)) for path in (publisher.state / "receipts").glob("*.json")]
            # Optional remote backups never run or get acknowledged by this local worker.
            all_receipts = [(path, row) for path, row in all_receipts if row.get("publication_target") == "local"]
            pending = [(path, r) for path, r in all_receipts if r.get("automatic_requested")
                       and r.get("status") != "PUBLISHED"]
            if not pending:
                os.close(launch_fd)
                return 0
            pending.sort(key=lambda pair: pair[1].get("requested_at", ""))
            # An unknown push is resolved before a newer snapshot can replace it.
            candidates = [pair for pair in all_receipts if pair[1].get("candidate_commit")
                          and pair[1].get("status") != "PUBLISHED"]
            selected = candidates[0] if candidates else pending[-1]
            selected_path, selected_row = selected
            batch = [selected] if candidates else pending
            snapshot_cutoff = now()
            for path, row in batch:
                row["automatic_requested"] = False
                write_json(path, row)
        try:
            evidence = read_json(state_dir / "lfs-budget-evidence.json", {})
            lfs_verified = valid_lfs_evidence(evidence)
            # This one worker already owns launch.lock. It may wait for a
            # short readback lock without creating one waiter per study event.
            result = publisher.execute(selected_row["event_id"], lfs_verified=lfs_verified, wait_for_lock=True)
        except (PublicationError, OSError) as exc:
            with locked(publisher.state / "request.lock", blocking=True):
                for path, original in batch:
                    latest = read_json(path, original)
                    busy = isinstance(exc, PublicationError) and exc.code == "PUBLICATION_BUSY"
                    latest.update(status="PENDING" if busy else "LOCAL_SNAPSHOT_PENDING" if publisher.target == "local" else "CLOUD_PENDING",
                                  automatic_requested=busy,
                                  error=getattr(exc, "code", type(exc).__name__),
                                  detail=getattr(exc, "detail", None), failed_at=now())
                    write_json(path, latest)
                # A failure ends this invocation. New source work can request a
                # new attempt; no sleeping retry loop is created here.
                os.close(launch_fd)
            return 1
        with locked(publisher.state / "request.lock", blocking=True):
            for path, original in batch:
                if path == selected_path:
                    continue
                row = dict(result, event_id=original["event_id"],
                           coalesced_into_event_id=selected_row["event_id"],
                           automatic_requested=False)
                write_json(path, row)
            # A later complete snapshot also fulfills older automatic requests
            # that failed before creating a commit. Keep unknown commits intact:
            # they must be reconciled with the remote before any replacement.
            if not candidates:
                for path in (publisher.state / "receipts").glob("*.json"):
                    original = read_json(path)
                    if (original.get("publication_target") == "local"
                            and original.get("request_origin") == "postcommit"
                            and original.get("status") != "PUBLISHED"
                            and not original.get("candidate_commit")
                            and (original.get("requested_at") or "~") <= snapshot_cutoff):
                        row = dict(result, event_id=original["event_id"], request_origin="postcommit",
                                   coalesced_into_event_id=selected_row["event_id"], automatic_requested=False)
                        write_json(path, row)


def dispatch_pending(subject: str, repo_root: Path, state_dir: Path) -> dict[str, Any] | None:
    """Resume only an already requested event after a direct CLI releases ownership."""
    publisher = Publisher(subject, repo_root, state_dir, local_only=True)
    with locked(publisher.state / "request.lock", blocking=True):
        pending = [read_json(path) for path in (publisher.state / "receipts").glob("*.json")]
        pending = [row for row in pending if row.get("publication_target") == "local"
                   and row.get("automatic_requested") and row.get("status") != "PUBLISHED"]
    if not pending:
        return None
    selected = max(pending, key=lambda row: row.get("requested_at", ""))
    return request_publication(subject, selected["event_id"], repo_root, state_dir)


def valid_lfs_evidence(evidence: Any) -> bool:
    if not isinstance(evidence, dict):
        return False
    try:
        age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(evidence.get("verified_at", ""))
    except (ValueError, TypeError):
        return False
    return bool(evidence.get("account") == "xialovezhu-wq" and evidence.get("budget_usd") == 0
                and evidence.get("prevent_further_usage") is True and evidence.get("evidence_source")
                and dt.timedelta(0) <= age < dt.timedelta(days=1))


def _verify_personalization_batches(snap, paths, *, max_bytes=None, max_objects=None):
    """Cold whole-generation verification with bounded temporary batches.

    Interactive readers keep their per-request limits. This explicit audit
    verifies every declared byte without asking one request to hold a whole
    growing generation, and releases only copies created by this audit.
    """
    from study_mcp import MAX_PERSONAL_SNAPSHOT_BYTES, MAX_PERSONAL_SNAPSHOT_OBJECTS
    from bridge_archives import require
    byte_limit = min(max_bytes or 64 * 1024 * 1024, MAX_PERSONAL_SNAPSHOT_BYTES)
    count_limit = min(max_objects or 2048, MAX_PERSONAL_SNAPSHOT_OBJECTS)
    batch, size, count = [], 0, 0

    def verify(batch):
        newly_cached = [p for p in batch if not (snap.cache / p).is_file()]
        snap.materialize_paths(batch)
        for path in batch:
            snap.path(path)
        for path in newly_cached:
            (snap.cache / path).unlink()
            snap._verified.pop(path, None)

    for path in paths:
        row = snap.files[path]
        declared = int(row.get('bytes') or 0)
        # Original evidence can legitimately be a large LFS textbook. It is
        # never a JSON object for an interactive personalization request. Use
        # the existing exact-original reader (streamed copy/hash) for that
        # role, without changing the JSON batch limits or splitting an object.
        original_lfs = (row.get('kind') == 'personalization_source_evidence'
                        and row.get('storage') == 'git-lfs'
                        and path.startswith(f'personalization/{snap.subject}/sources/'))
        if declared > MAX_PERSONAL_SNAPSHOT_BYTES and original_lfs:
            if batch:
                verify(batch); count += len(batch); batch, size = [], 0
            newly_cached = not (snap.cache / path).is_file()
            try:
                source = snap.path(path)
                require(source.stat().st_size == declared,
                        'PERSONAL_SOURCE_SIZE_MISMATCH', path)
            finally:
                if newly_cached and (snap.cache / path).is_file():
                    (snap.cache / path).unlink()
                    snap._verified.pop(path, None)
            count += 1
            continue
        require(0 <= declared <= MAX_PERSONAL_SNAPSHOT_BYTES,
                'PERSONAL_SNAPSHOT_SINGLE_OBJECT_TOO_LARGE', path)
        if batch and (len(batch) >= count_limit or size + declared > byte_limit):
            verify(batch); count += len(batch); batch, size = [], 0
        batch.append(path); size += declared
    if batch:
        verify(batch); count += len(batch)
    return count


def verify_current(subject: str) -> dict[str, Any]:
    """Verify published data and the installed math calculation bindings."""
    from study_mcp import LibraryStore
    from bridge_archives import BridgeError
    # Keep cold verification copies beside the configured publication. Strict
    # resolution refuses a disconnected archive rather than making a fake root.
    cache_parent = (BASE / 'publication' / subject).resolve(strict=True)
    store = LibraryStore(BASE, cache_parent=cache_parent)
    try:
        snap = store.snapshot(subject)
        freshness = store.freshness(snap)
        prefix = 'personalization/' + subject + '/'
        verified = sorted(p for p in snap.files if p.startswith(prefix))
        if subject != 'english' and snap.library.get('entrypoints', {}).get('personalization'):
            _verify_personalization_batches(snap, verified)
        if subject == 'english' and snap.library.get('personalization', {}).get('entrypoint'):
            contract = snap.library['personalization']
            entry = 'context/personal-summary/current.json'
            if contract['entrypoint'] != entry or file_hash(snap.path(entry)) != contract['projection_sha256']:
                raise PublicationError("ENGLISH_TEACHING_INDEX_MISMATCH")
            index = json.loads(snap.text(entry))
            if (index['version'] != contract['version'] or index['memory_generation'] != contract['memory_generation']
                    or index['coverage'] != contract['coverage']):
                raise PublicationError("ENGLISH_SUMMARY_IDENTITY_MISMATCH")
            verified = [entry]
            for member in index['members'].values():
                path = member['path']
                if not path.startswith('context/personal-summary/') or file_hash(snap.path(path)) != member['sha256']:
                    raise PublicationError("ENGLISH_TEACHING_MEMBER_MISMATCH")
                verified.append(path)
            if set(verified) != {p for p in snap.files if p.startswith('context/personal-summary/')}:
                raise PublicationError("ENGLISH_TEACHING_CLOSURE_MISMATCH")
        if subject != 'english' and not verified:
            freshness.update(status='missing', reason='personalization_not_published')
        native = {'status': 'not_applicable'}
        if subject == 'math':
            try:
                bindings = store._math_bindings(snap)
                native = {'status': 'matched', 'verified_bindings': len(bindings)}
            except BridgeError as exc:
                native = {'status': 'mismatch', 'error': exc.code, 'detail': exc.detail}
        quality_result = {}
        if subject == 'math' and (quality := _math_quality_module(SUBJECTS[subject]["root"])):
            try:
                personal = json.loads(snap.text("personalization/math/manifest.json"))
                binding = quality.binding_is_expected(SUBJECTS[subject]["root"], snap.receipt.get("event_id"), personal)
                if binding:
                    if (snap.receipt.get("formal_quality_generation_sha256") != binding["generation_sha256"]
                            or any(personal[k] != snap.library["personalization"][k]
                                   for k in ("version", "formal_version"))):
                        raise quality.QualityError("quality_replica_binding_mismatch")
                    quality_result["formal_quality"] = binding
            except (OSError, ValueError, KeyError, BridgeError) as exc:
                quality_result["formal_quality_error"] = getattr(exc, "code", str(exc))
        ready = (freshness['status'] == 'current' and native['status'] != 'mismatch'
                 and not quality_result.get("formal_quality_error")
                 and (subject != 'english' or freshness.get('personal_summary_freshness') == 'current'))
        return {**snap.identity(), 'status': 'PUBLISHED_CURRENT' if ready else 'SYNC_PENDING',
                'freshness': freshness, 'verified_personalization_files': len(verified),
                'native_calculation_bindings': native,
                'publication_event_id': snap.receipt.get('event_id'), **quality_result}
    finally:
        store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    publish = sub.add_parser("publish")
    publish.add_argument("--subject", choices=SUBJECTS, required=True)
    publish.add_argument("--event-id", required=True)
    publish.add_argument("--repo", type=Path)
    publish.add_argument("--state-dir", type=Path, default=BASE / "publication")
    publish.add_argument("--prepare-only", action="store_true")
    publish.add_argument("--prepared-manifest-sha256", help="Publish this already reviewed candidate without re-exporting sources")
    publish.add_argument("--github-backup", action="store_true", help="Manually use an explicitly configured GitHub backup target")
    publish.add_argument("--web-binary-paths", type=Path,
                         help="Optional UTF-8 list of extra non-LFS original paths requiring bounded transport")
    publish.add_argument("--lfs-budget-evidence", type=Path,
                         help="Local verified current account budget/remaining quota evidence")
    pending = sub.add_parser("mark-pending")
    pending.add_argument("--subject", choices=SUBJECTS, required=True)
    pending.add_argument("--event-id", required=True)
    pending.add_argument("--state-dir", type=Path, default=BASE / "publication")
    verify = sub.add_parser("verify-published")
    verify.add_argument("--subject", choices=SUBJECTS, required=True)
    verify.add_argument("--source-commit", required=True)
    verify.add_argument("--verify-remote", action="store_true", help="Check the remote for an explicitly requested historical GitHub publication")
    verify.add_argument("--state-dir", type=Path, default=BASE / "publication")
    current = sub.add_parser('verify-current', help='Read-only final check of current formal/personal versions against the MCP snapshot')
    current.add_argument('--subject', choices=SUBJECTS, required=True)
    request = sub.add_parser("request")
    request.add_argument("--subject", choices=SUBJECTS, required=True)
    request.add_argument("--event-id", required=True)
    request.add_argument("--repo", type=Path, required=True)
    request.add_argument("--state-dir", type=Path, default=BASE / "publication")
    drain = sub.add_parser("run-pending")
    drain.add_argument("--subject", choices=SUBJECTS, required=True)
    drain.add_argument("--state-dir", type=Path, default=BASE / "publication")
    drain.add_argument("--lock-fd", type=int, required=True)
    pages = sub.add_parser("export-pdf-pages")
    pages.add_argument("--description", type=Path, required=True)
    pages.add_argument("--cache-root", type=Path, required=True)
    pages.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "export-pdf-pages":
            print(json.dumps(export_pdf_pages(args.description, args.cache_root, args.output), ensure_ascii=False))
            return 0
        if args.command == "mark-pending":
            print(json.dumps(mark_pending(args.subject, args.event_id, args.state_dir), ensure_ascii=False))
            return 0
        if args.command == "verify-published":
            print(json.dumps(verify_published(args.subject, args.source_commit, args.state_dir, verify_remote=args.verify_remote), ensure_ascii=False))
            return 0
        if args.command == 'verify-current':
            result = verify_current(args.subject)
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result['status'] == 'PUBLISHED_CURRENT' else 1
        if args.command == "request":
            print(json.dumps(request_publication(args.subject, args.event_id, args.repo, args.state_dir), ensure_ascii=False))
            return 0
        if args.command == "run-pending":
            return drain_pending(args.subject, args.state_dir, args.lock_fd)
        verified = False
        if args.github_backup and configured_target(args.state_dir) != "github":
            raise PublicationError("GITHUB_BACKUP_NOT_CONFIGURED")
        if args.github_backup and args.lfs_budget_evidence:
            evidence = read_json(args.lfs_budget_evidence)
            verified = valid_lfs_evidence(evidence)
            if not verified:
                raise PublicationError("INVALID_LFS_BUDGET_EVIDENCE")
        publisher = Publisher(args.subject, args.repo or SUBJECTS[args.subject]["root"], args.state_dir,
                              local_only=not args.github_backup)
        with locked(publisher.state / "launch.lock"):
            result = publisher.execute(args.event_id, prepare_only=args.prepare_only, lfs_verified=verified,
                                       prepared_manifest_sha256=args.prepared_manifest_sha256,
                                       extra_web_binary_paths={line.strip() for line in args.web_binary_paths.read_text().splitlines() if line.strip()} if args.web_binary_paths else None)
        if not args.github_backup:
            followup = dispatch_pending(args.subject, publisher.repo, args.state_dir)
            if followup:
                result["pending_followup"] = followup
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (PublicationError, study_web_read.WebReadError, OSError) as exc:
        external = getattr(args, "github_backup", False) or getattr(args, "verify_remote", False)
        result = {"status": "CLOUD_PENDING" if external else "LOCAL_SNAPSHOT_PENDING",
                  "subject": getattr(args, "subject", None),
                  "event_id": getattr(args, "event_id", None), "local_formal_state": "PRESERVED",
                  "error": getattr(exc, "code", type(exc).__name__),
                  "detail": str(exc) if isinstance(exc, study_web_read.WebReadError) else getattr(exc, "detail", None),
                  "retry": "publication_only"}
        print(json.dumps(result, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
