#!/usr/bin/env python3
"""Private, read-only MCP access to Study Bridge's existing published libraries.

Read/search/image contracts are adapted from the design of Chat On Steroids.
See THIRD_PARTY_NOTICES-study-mcp.md. No terminal or formal writer is exposed.
"""
from __future__ import annotations

import argparse
import base64
import csv
from functools import partial, wraps
import datetime as dt
import hashlib
import io
import json
import mimetypes
import os
from pathlib import Path
from pathlib import PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import weakref
import zipfile
from urllib.parse import quote
from typing import Any, Literal

from bridge_archives import BridgeError, file_hash, require, safe_path, source_file
import study_bridge
import study_english_context
import study_personalization
import study_read_scope

BASE = Path(__file__).resolve().parent
SUBJECTS = ("math", "english", "408")
READ_LIMIT = 512 * 1024
PAGE_BYTES = 64 * 1024
TEXT_LIMIT = 32 * 1024 * 1024
# Native append-only math ledgers are parsed to EOF, not returned as one text
# response. Keep their bounded streaming budget separate from ordinary text.
NATIVE_JSONL_LIMIT = 256 * 1024 * 1024
NATIVE_JSONL_LINE_LIMIT = 8 * 1024 * 1024
NATIVE_JSONL_RECORD_LIMIT = 100_000
# One publication may declare a large number of personal objects. Bound both
# the count and the total bytes so a stale or hostile source_commit cannot make
# this server read an unbounded generation into memory.
PERSONAL_BATCH_PATHS = 512
PERSONAL_BATCH_TIMEOUT_SECONDS = 60
MAX_PERSONAL_SNAPSHOT_BYTES = 256 * 1024 * 1024
# Full 408 point histories plus exact-question templates exceed 20,000 small
# immutable objects. Retain a bounded count and the independent 256 MiB guard.
MAX_PERSONAL_SNAPSHOT_OBJECTS = 30000
ENGLISH_CONTEXT_FILES = 512
MAX_ENGLISH_CONTEXT_BYTES = 64 * 1024 * 1024
IMAGE_LIMIT = 12 * 1024 * 1024
SNAPSHOT_CACHE_LIMIT = 3
WORKFLOW_CACHE_LIMIT = 2
NATIVE_PYTHON = "/usr/local/bin/python3"
# These are transport provenance added by _calculate, not part of the native
# candidate_bundle_sha256 body. Keep them in resource metadata, never in a
# self-hashed native JSON file.
MATH_METHOD_CATALOG = "错题知识网络/个人知识点索引/做题方法/catalog.json"
MATH_METHOD_BINDINGS = "错题知识网络/个人知识点索引/做题方法/bindings.json"
C_WORKFLOW_ENVELOPE_FIELDS = ("native_algorithm_source", "native_algorithm_sha256",
    "native_workflow_version", "published_personalization", "formal_write_count",
    "learner_evidence_write_count", "published_inputs_verified")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def encoded(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def checked_day(value: str) -> str:
    require(bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)), "DATE_REQUIRED_YYYY_MM_DD")
    return dt.date.fromisoformat(value).isoformat()


class Snapshot:
    def __init__(self, store: "LibraryStore", subject: str, commit: str):
        self.store, self.subject, self.commit = store, subject, commit
        self.lock = threading.RLock()
        # The byte inventories list every published file and total several
        # megabytes. Keep them as shard descriptors and parse a shard only when
        # a call actually touches one of its paths.
        self.receipt, self.library = store.published.inspect_version(subject, commit, resolve_inventory=False)
        self._files: dict[str, dict[str, Any]] = {}
        self._loaded_shards: set[str] = set()
        self._shard_cache: dict[str, list[dict[str, Any]]] = {}
        self.metadata = {"PUBLISHED_LIBRARY.json": {"path": "PUBLISHED_LIBRARY.json",
                                                    "sha256": self.receipt["manifest_sha256"]}}
        # A small publication keeps its file list inline; a large one shards it.
        inline = self.library.get("files")
        self._files_complete = inline is not None
        if inline is not None:
            for record in inline:
                self._files[record["path"]] = record
        for key, _ in study_bridge.publication.INVENTORY_FIELDS:
            for row in self.library.get(key, []):
                self.metadata[row["path"]] = row
        self.cache = store.cache / subject / commit
        self.cache.mkdir(parents=True, exist_ok=True)
        self._domains: dict[str, list[dict[str, Any]]] | None = None
        self._question_refs: list[dict[str, Any]] | None = None
        self._math_catalog: dict[str, Any] | None = None
        self._scope: list[dict[str, Any]] | None = None
        self._personal_materialized = False
        self._personal_partial = False
        self._verified: dict[str, tuple[int, int]] = {}

    def _load_shards(self, groups: set[str] | None = None) -> None:
        """Parse inventory shards, optionally only those for named top-level groups."""
        with self.lock:
            if self._files_complete and groups is None:
                return
            for row in self.library.get("file_inventory", []):
                if groups is not None and row.get("source_directory") not in groups:
                    continue
                shard_path = row.get("path")
                if not isinstance(shard_path, str) or shard_path in self._loaded_shards:
                    continue
                records = self.store.published.inventory_shard(self.subject, self.commit, row)
                for record in records:
                    path = record.get("path")
                    require(isinstance(path, str) and path not in self._files,
                            "DUPLICATE_PUBLISHED_PATH", path)
                    self._files[path] = record
                self._loaded_shards.add(shard_path)

    @property
    def files(self) -> dict[str, dict[str, Any]]:
        self._load_shards()
        return self._files

    def _row(self, relative: str) -> dict[str, Any] | None:
        row = self._files.get(relative) or self.metadata.get(relative)
        if row is not None:
            return row
        group = PurePosixPath(relative).parts[0] if len(PurePosixPath(relative).parts) > 1 else "."
        self._load_shards({group})
        return self._files.get(relative)

    def path(self, relative: str) -> Path:
        relative = safe_path(relative)
        row = self._row(relative)
        require(row is not None, "PATH_NOT_IN_PUBLISHED_LIBRARY", relative)
        with self.lock:
            target = self.cache / relative
            if not target.is_file():
                self.store.published.materialize(self.subject, self.commit, relative, target, row["sha256"])
                target.chmod(0o400)
            source_file(target, self.cache)
            info = target.stat()
            # Re-hash only when the cached bytes actually changed. A tampered
            # cache is still caught because editing it moves size or mtime.
            stamp = (info.st_size, info.st_mtime_ns)
            if self._verified.get(relative) != stamp:
                require(file_hash(target) == row["sha256"], "CACHED_SOURCE_HASH_MISMATCH", relative)
                self._verified[relative] = stamp
            return target

    def text(self, relative: str) -> str:
        path = self.path(relative)
        require(path.stat().st_size <= TEXT_LIMIT, "TEXT_SOURCE_TOO_LARGE_USE_FETCH", relative)
        raw = path.read_bytes()
        require(b"\0" not in raw[:8192], "BINARY_SOURCE_USE_VIEW_IMAGE", relative)
        try:
            return raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise BridgeError("SOURCE_IS_NOT_UTF8", relative) from exc

    def native_jsonl(self, relative: str) -> list[dict[str, Any]]:
        entries = self.library.get("entrypoints", {})
        require(self.subject == "math" and relative in
                {entries.get("capture_events"), entries.get("review_events")},
                "NATIVE_JSONL_SCOPE_REQUIRED", relative)
        path = self.path(relative)  # Published membership and SHA-256 first.
        require(path.stat().st_size <= NATIVE_JSONL_LIMIT, "NATIVE_JSONL_TOO_LARGE", relative)
        records, total, line_number = [], 0, 0
        with path.open("rb") as stream:
            while raw := stream.readline(NATIVE_JSONL_LINE_LIMIT + 1):
                line_number += 1
                total += len(raw)
                require(total <= NATIVE_JSONL_LIMIT, "NATIVE_JSONL_TOO_LARGE", relative)
                require(len(raw) <= NATIVE_JSONL_LINE_LIMIT, "NATIVE_JSONL_LINE_TOO_LARGE", relative)
                try:
                    line = raw.decode("utf-8-sig" if line_number == 1 else "utf-8")
                except UnicodeDecodeError as exc:
                    raise BridgeError("SOURCE_IS_NOT_UTF8", relative) from exc
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except ValueError as exc:
                    raise BridgeError("INVALID_NATIVE_JSONL", {"path": relative, "line": line_number}) from exc
                require(isinstance(row, dict), "JSONL_OBJECT_REQUIRED", relative)
                require(len(records) < NATIVE_JSONL_RECORD_LIMIT, "NATIVE_JSONL_TOO_MANY_RECORDS", relative)
                records.append(row)
        self.path(relative)  # Detect cached source changes during the scan.
        return records

    def materialize_paths(self, paths) -> None:
        """Fetch exactly these published personal paths, one git process if possible."""
        prefix = 'personalization/' + self.subject + '/'
        ordered = sorted(dict.fromkeys(paths))
        require(bool(ordered) and all(p in self._files and p.startswith(prefix) for p in ordered),
                'PATH_NOT_IN_PUBLISHED_LIBRARY')
        with self.lock:
            missing = [p for p in ordered if not (self.cache / p).is_file()]
            if not missing:
                return
            checkout = self.store.base / 'publication' / self.subject / 'checkout'
            seen = set()
            # Refuse an oversized generation before reading anything. A stale
            # source_commit can declare tens of thousands of objects; without
            # this check the whole generation would be streamed into memory.
            declared_bytes = sum(int(self._files[p].get('bytes') or 0) for p in missing)
            require(len(missing) <= MAX_PERSONAL_SNAPSHOT_OBJECTS
                    and declared_bytes <= MAX_PERSONAL_SNAPSHOT_BYTES,
                    'PERSONAL_SNAPSHOT_TOO_LARGE',
                    {'objects': len(missing), 'bytes': declared_bytes,
                     'max_objects': MAX_PERSONAL_SNAPSHOT_OBJECTS,
                     'max_bytes': MAX_PERSONAL_SNAPSHOT_BYTES,
                     'hint': 're-pin the current source_commit from library'})
            # Fetch exactly the blobs this publication declares. Streaming by
            # blob id avoids both a whole-tree archive and a git process per
            # batch, and each batch is read incrementally so peak memory stays
            # bounded by one object rather than by the whole generation.
            streamable = [p for p in missing
                          if self._files[p].get('storage') == 'git' and self._files[p].get('git_blob_sha1')]
            read_bytes = 0
            for start in range(0, len(streamable), PERSONAL_BATCH_PATHS):
                batch = streamable[start:start + PERSONAL_BATCH_PATHS]
                process = subprocess.Popen(['git', '-C', str(checkout), 'cat-file', '--batch'],
                                           stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                           stderr=subprocess.DEVNULL)
                timed_out = threading.Event()

                def stop_stalled_read(child=process, marker=timed_out):
                    if child.poll() is None:
                        marker.set()
                        child.kill()

                watchdog = threading.Timer(PERSONAL_BATCH_TIMEOUT_SECONDS, stop_stalled_read)
                watchdog.daemon = True
                watchdog.start()
                try:
                    for path in batch:
                        # Do not fill stdin with hundreds of IDs before draining
                        # stdout: cat-file can block writing its first large blob
                        # while this writer blocks on a full request pipe.
                        process.stdin.write((self._files[path]['git_blob_sha1'] + "\n").encode())
                        process.stdin.flush()
                        header = process.stdout.readline().split()
                        require(len(header) == 3 and header[1] == b'blob',
                                'PERSONAL_BLOB_HEADER_INVALID', path)
                        size = int(header[2])
                        read_bytes += size
                        require(read_bytes <= MAX_PERSONAL_SNAPSHOT_BYTES,
                                'PERSONAL_SNAPSHOT_TOO_LARGE',
                                {'bytes_read': read_bytes, 'path': path,
                                 'max_bytes': MAX_PERSONAL_SNAPSHOT_BYTES})
                        data = process.stdout.read(size)
                        require(len(data) == size and process.stdout.read(1) == b'\n',
                                'PERSONAL_BLOB_TRUNCATED', path)
                        require(digest(data) == self._files[path]['sha256'],
                                'PERSONAL_ARCHIVE_HASH_MISMATCH', path)
                        target = self.cache / path
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(data)
                        target.chmod(0o400)
                        info = target.stat()
                        self._verified[path] = (info.st_size, info.st_mtime_ns)
                        seen.add(path)
                finally:
                    watchdog.cancel()
                    try:
                        process.stdin.close()
                    except OSError:
                        pass
                    process.stdout.close()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                        timed_out.set()
                    require(not timed_out.is_set(), 'PERSONAL_SNAPSHOT_READ_TIMEOUT',
                            {'subject': self.subject, 'objects': len(batch),
                             'timeout_seconds': PERSONAL_BATCH_TIMEOUT_SECONDS})
                require(process.returncode == 0, 'PERSONAL_SNAPSHOT_READ_FAILED')
            for path in missing:
                if path in seen:
                    continue
                # LFS pointers and any non-git storage keep the verified
                # per-path materialization route.
                self.path(path)
                seen.add(path)
            require(seen == set(missing), 'PERSONAL_SNAPSHOT_INCOMPLETE')

    def materialize_personalization(self) -> None:
        """Fetch the personal files this publication marks as query-required.

        Archiving every path under the prefix scales with the snapshot tree
        instead of the query. A publication that declares no partial set keeps
        the complete-prefix behaviour.
        """
        prefix = 'personalization/' + self.subject + '/'
        # Only the personalization shards are needed here; loading every
        # published inventory shard would read megabytes for one subject area.
        self._load_shards({prefix.split('/')[0]})
        entry = self.library.get('entrypoints', {}).get('personalization')
        require(isinstance(entry, str) and entry.startswith(prefix), 'PERSONALIZATION_NOT_PUBLISHED')
        manifest = json.loads(self.path(entry).read_text())
        declared = manifest.get('query_required')
        # Older v2 408 publications contain the same immutable hot-route
        # descriptors but predate query_required. Bootstrap those descriptors
        # instead of downloading every history and teacher object. The native
        # planner verifies their hashes before selecting this query's objects.
        if (declared is None and self.subject == '408'
                and manifest.get('export_schema') == 'study-personalization-408-v2'):
            buckets = (manifest.get('knowledge_hot_routes') or {}).get('buckets')
            require(isinstance(buckets, dict), 'INVALID_PERSONAL_HOT_ROUTES')
            refs = [manifest.get('native_manifest_ref'), manifest.get('receipt_ref')]
            refs.extend(row.get('ref') for row in buckets.values() if isinstance(row, dict))
            require(len(refs) == len(buckets) + 2
                    and all(isinstance(ref, str)
                        and re.fullmatch(r'(?:objects/[a-z-]+|receipts|manifests)/[0-9a-f]{64}\.json', ref)
                        for ref in refs), 'INVALID_QUERY_REQUIRED')
            declared = sorted(set(refs))
        if declared is None:
            required = [p for p in self._files if p.startswith(prefix)]
        else:
            require(isinstance(declared, list) and all(isinstance(item, str) for item in declared),
                    'INVALID_QUERY_REQUIRED')
            base = entry.rsplit('/', 1)[0] + '/'
            required = [entry] + [base + item for item in declared]
            require(all(p in self._files for p in required), 'PERSONALIZATION_NOT_PUBLISHED')
        self._personal_partial = declared is not None
        self.materialize_paths(required)
        self._personal_materialized = True
        # No blanket re-hash of the whole generation: every file that is
        # actually read goes through ``path``, which re-hashes exactly the
        # cached files whose size or mtime moved. Bytes that reach the model
        # are therefore always verified, and unread files cost nothing.

    def identity(self) -> dict[str, Any]:
        target = self.receipt.get("publication_target", "github")
        return {"subject": self.subject, "repository": self.library["repository"],
                "source_commit": self.commit, "local_version": self.library["local_version"],
                "snapshot_id": self.commit, "publication_target": target,
                "github_backup_required": target == "github",
                "source_role": "local_immutable_study_snapshot" if target == "local" else "same_version_private_github_publication",
                "formal_write_count": 0, "learner_evidence_write_count": 0}

    def record(self, identity: Any, body: Any, path: str, ordinal: int) -> dict[str, Any]:
        return {"id": identity, "source_path": path, "source_sha256": self._row(path)["sha256"],
                "record_ordinal": ordinal, "representation": "parsed_native_record", "record": body}

    def question_refs(self) -> list[dict[str, Any]]:
        """Return verified 408 question bindings without scanning native source files."""
        require(self.subject == "408", "UNKNOWN_DOMAIN")
        with self.lock:
            if self._question_refs is not None:
                return self._question_refs
            refs = self.library.get("question_refs")
            require(isinstance(refs, dict), "QUESTION_REFS_NOT_PUBLISHED")
            rows = []
            for ordinal, formal_id in enumerate(sorted(refs), 1):
                body = refs[formal_id]
                state = body.get("formal_state") if isinstance(body, dict) else None
                require(isinstance(body, dict) and body.get("formal_id") == formal_id,
                        "INVALID_QUESTION_REF", formal_id)
                require(isinstance(state, dict)
                        and all(isinstance(state.get(key), str)
                                and re.fullmatch(r"[0-9a-f]{64}", state[key])
                                for key in ("formal_row_sha256", "safe_card_sha256")),
                        "INVALID_QUESTION_REF_FORMAL_STATE", formal_id)
                row = self.record(formal_id, body, "PUBLISHED_LIBRARY.json", ordinal)
                row["representation"] = "published_question_reference"
                rows.append(row)
            self._question_refs = rows
            return rows

    def math_method_catalog(self) -> dict[str, Any]:
        """Resolve material IDs only from verified bytes of this fixed snapshot."""
        require(self.subject == "math", "MATH_METHODS_SUBJECT_REQUIRED")
        with self.lock:
            # Re-run path guards even when the parsed catalog is warm. A changed
            # cache must not bypass Snapshot's published-hash verification.
            paths = [self.path(p) for p in (MATH_METHOD_CATALOG, MATH_METHOD_BINDINGS)]
            require(all(p.stat().st_size <= TEXT_LIMIT for p in paths), "MATH_METHOD_REGISTRY_TOO_LARGE")
            if self._math_catalog is not None:
                return self._math_catalog
            try:
                catalog, bindings = [json.loads(p.read_bytes()) for p in paths]
            except (ValueError, UnicodeError) as exc:
                raise BridgeError("INVALID_MATH_METHOD_REGISTRY") from exc
            require(isinstance(catalog, dict) and catalog.get("schema") in
                    {"math-problem-methods-v1", "math-problem-methods-v2"}
                    and isinstance(bindings, dict) and bindings.get("schema") == "math-problem-bindings-v1",
                    "INVALID_MATH_METHOD_REGISTRY")
            objects, groups = {}, {}
            for field, id_field, role in (("methods", "method_id", "method_reference"),
                                         ("steps", "step_id", "step_reference"),
                                         ("conclusions", "conclusion_id", "conclusion_reference")):
                rows = catalog.get(field, [])
                require(isinstance(rows, list), "INVALID_MATH_METHOD_REGISTRY")
                group = {}
                for node in rows:
                    identity = node.get(id_field) if isinstance(node, dict) else None
                    require(isinstance(identity, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", identity)
                            and identity not in objects, "INVALID_MATH_METHOD_OBJECT_ID", identity)
                    group[identity] = node
                    objects[identity] = {"object_role": role, "record": node}
                groups[field] = group
            self._math_catalog = {"schema": catalog["schema"], "objects": objects, **groups,
                "source_refs": [{"path": p, "sha256": self._row(p)["sha256"]}
                                for p in (MATH_METHOD_CATALOG, MATH_METHOD_BINDINGS)]}
            return self._math_catalog

    def domains(self) -> dict[str, list[dict[str, Any]]]:
        with self.lock:
            if self._domains is not None:
                return self._domains
            # The native scanner expects the resolved file list, which is
            # loaded on demand rather than parsed with the root manifest.
            resolved = {**self.library, "files": list(self.files.values())}
            paths = study_bridge.scan_source_paths(self.subject, resolved)
            entries = self.library.get("entrypoints", {})
            ledger_paths = ({entries.get("capture_events"), entries.get("review_events")}
                            & set(paths) if self.subject == "math" else set())
            parsed_jsonl = {p: self.native_jsonl(p) for p in ledger_paths}
            texts = {p: self.text(p) for p in paths if p not in ledger_paths}
            # Reuse the same native identity checks as the existing Bridge gate.
            study_bridge.scan_id_domains(self.subject, resolved, texts, parsed_jsonl)
            domains: dict[str, list[dict[str, Any]]] = {}

            def jsonlines(name: str, path: str, id_field: str | None) -> None:
                rows = (parsed_jsonl[path] if path in parsed_jsonl else
                        (json.loads(v) for v in texts[path].split("\n") if v.strip()))
                domains[name] = [self.record(row.get(id_field) if id_field else None, row, path, n)
                                 for n, row in enumerate(rows, 1)]

            if self.subject == "math":
                path = self.library["history_index"]
                domains["formal_cards"] = [self.record(row["id"], row, path, n)
                                            for n, row in enumerate(json.loads(texts[path])["cards"], 1)]
                entries = self.library["entrypoints"]
                jsonlines("capture_events", entries["capture_events"], "event_id")
                jsonlines("review_events", entries["review_events"], None)
            elif self.subject == "408":
                entries = self.library["entrypoints"]
                path = entries["formal_nodes"]
                lines = [v for v in texts[path].split("\n") if v.startswith("|")]
                headers = [v.strip() for v in lines[0].split("|")[1:-1]]
                domains["formal_nodes"] = []
                for n, line in enumerate(lines[2:], 1):
                    cells = [v.strip() for v in line.split("|")[1:-1]]
                    domains["formal_nodes"].append(self.record(cells[0], dict(zip(headers, cells)), path, n))
                jsonlines("review_events", entries["learning_events"], "event_id")
                domains["history_packages"] = [self.record(row["package_id"], row, "PUBLISHED_LIBRARY.json", n)
                                                for n, row in enumerate(self.library["history_index"], 1)]
                domains["question_refs"] = self.question_refs()
                if entries.get("variant_bank"):
                    bank_path = entries["variant_bank"]
                    bank = json.loads(texts[bank_path])
                    # Surfaces only: the answer capsule stays private. Each row
                    # carries the published surface path so a caller reads the
                    # stem and options without re-deriving the join.
                    rows = []
                    for n, entry in enumerate(bank.get("entries", []), 1):
                        surface_sha = entry.get("surface_sha256")
                        if not isinstance(surface_sha, str):
                            continue
                        rows.append(self.record(entry.get("variant_id"),
                                                {**entry,
                                                 "answer_content_in_surface": False,
                                                 "surface_path": "variant-bank/surfaces/%s.json" % surface_sha},
                                                bank_path, n))
                    domains["variant_surfaces"] = rows
            else:
                path = "bank/master_bank.csv"
                domains["vocabulary"] = [self.record(row["id"], row, path, n)
                                          for n, row in enumerate(csv.DictReader(io.StringIO(texts[path])), 1)]
                path = "bank/sentence_patterns.md"
                matches = list(re.finditer(r"(?m)^##\s+(SP-\d+)\b", texts[path]))
                domains["sentence_patterns"] = [self.record(m[1], texts[path][m.start():
                    matches[n + 1].start() if n + 1 < len(matches) else len(texts[path])], path, n + 1)
                    for n, m in enumerate(matches)]
                article_paths = [p for p in paths if p.startswith("articles/")]
                domains["articles"] = [self.record(p, texts[p], p, 1) for p in article_paths]
                event_paths = [p for p in paths if p.startswith("intake/events/") and p.endswith(".json")]
                domains["intake_events"] = [self.record(json.loads(texts[p]).get("event_id"),
                                                       json.loads(texts[p]), p, 1) for p in event_paths]
                other = [p for p in paths if p not in article_paths + event_paths
                         and p not in ("bank/master_bank.csv", "bank/sentence_patterns.md")]
                domains["other_history_files"] = [self.record(None, texts[p], p, 1) for p in other]
            self._scope = [{"path": p, "bytes": self.path(p).stat().st_size,
                            "sha256": self._row(p)["sha256"], "server_read_to_eof": True} for p in paths]
            self._domains = domains
            return domains

    def scope(self) -> list[dict[str, Any]]:
        self.domains()
        return list(self._scope or [])

    def source_mappings(self, value: Any) -> list[dict[str, Any]]:
        refs = set()
        def visit(item):
            if isinstance(item, dict):
                if isinstance(item.get('path'), str) and isinstance(item.get('sha256'), str):
                    refs.add((item['path'], item['sha256']))
                for child in item.values():
                    visit(child)
            elif isinstance(item, list):
                for child in item:
                    visit(child)
        visit(value)
        mapped = []
        for original, sha in sorted(refs):
            # A native snapshot reference is usually the published path itself,
            # so answer from the one shard that holds it instead of parsing the
            # entire inventory.
            direct = self._row(original)
            if direct is not None and direct.get('sha256') == sha:
                matches = [direct]
            else:
                matches = [row for p, row in self.files.items() if row.get('sha256') == sha and
                           original in (p, row.get('original_reference'), row.get('source_relative_path'))]
            mapped.append({'original_path': original, 'sha256': sha,
                           'published_path': matches[0]['path'] if matches else None,
                           'status': 'available' if matches else 'not_in_snapshot'})
        return mapped


class LibraryStore:
    def __init__(self, base: Path = BASE, *, cache_parent: Path | None = None):
        self.base = base.resolve()
        self.published = study_bridge.Published(self.base / "publication")
        self.temporary = tempfile.TemporaryDirectory(prefix="study-mcp-", dir=cache_parent)
        self.cache = Path(self.temporary.name).resolve()
        self.snapshots: dict[tuple[str, str], Snapshot] = {}
        # Keep at most one warm slot per subject on average. Active requests
        # still own their snapshot after eviction; weak references let another
        # request reuse that same instance and its file-materialization lock.
        self._live_snapshots: weakref.WeakValueDictionary = weakref.WeakValueDictionary()
        self.lock = threading.RLock()
        self.workflow_cache: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._workflow_locks: weakref.WeakValueDictionary = weakref.WeakValueDictionary()
        self._native_hash_cache: dict[Path, tuple[tuple[int, ...], str]] = {}
        self._semantic_engine = None
        self._material_preprocessor = None

    def close(self) -> None:
        self.snapshots.clear()
        self._live_snapshots.clear()
        self.workflow_cache.clear()
        self.temporary.cleanup()

    def snapshot(self, subject: str, source_commit: str = "") -> Snapshot:
        require(subject in SUBJECTS, "UNKNOWN_SUBJECT")
        commit = source_commit or self.published.latest(subject)["source_commit"]
        require(bool(re.fullmatch(r"[0-9a-f]{40}", commit)), "INVALID_SOURCE_COMMIT")
        with self.lock:
            key = (subject, commit)
            snap = self.snapshots.pop(key, None)
            if snap is None:
                snap = self._live_snapshots.get(key)
            if snap is None:
                snap = Snapshot(self, subject, commit)
                self._live_snapshots[key] = snap
            self.snapshots[key] = snap
            while len(self.snapshots) > SNAPSHOT_CACHE_LIMIT:
                self.snapshots.pop(next(iter(self.snapshots)))
            return snap

    def library(self, subject: str, source_commit: str = "", prefix: str = "", offset: int = 0,
                limit: int = 100) -> dict[str, Any]:
        if offset > 0:
            require(bool(source_commit), "SOURCE_COMMIT_REQUIRED_FOR_CONTINUATION")
        snap = self.snapshot(subject, source_commit)
        require(0 <= offset and 1 <= limit <= 200, "INVALID_PAGE")
        domains = snap.domains()
        if prefix:
            snap._load_shards({prefix.split('/')[0]})
        paths = sorted(p for p, row in snap.files.items() if p.startswith(prefix)
                       and study_read_scope.listed(snap, p, row))
        page = paths[offset:offset + limit]
        visible_history = [row for row in snap.scope() if study_read_scope.listed(snap, row["path"])]
        text_history = {row["path"] for row in visible_history}
        non_text_history = ([{"path": path, "bytes": row["bytes"], "sha256": row["sha256"]}
                             for path, row in sorted(snap.files.items())
                             if path not in text_history and study_read_scope.listed(snap, path, row)
                             and any(path == p or path.startswith(p.rstrip('/') + '/')
                                 for p in snap.library.get("history_index", []))]
                            if subject == "english" else [])
        formula_reference = None
        if snap.subject == "math" and isinstance(snap.library.get("entrypoints"),dict) and snap.library["entrypoints"].get("math_methods"):
            from study_formula_methods import capability as formula_capability
            try:
                formula_reference = formula_capability(snap)
            except (OSError, ValueError, KeyError) as exc:
                formula_reference = {"status": "unavailable", "reason": str(exc)}
        entrypoints = snap.library["entrypoints"]
        if subject == "english" and isinstance(entrypoints, dict):
            entrypoints = {key: path for key, path in entrypoints.items()
                           if isinstance(path, str) and study_read_scope.listed(snap, path)}
        elif subject == "english" and isinstance(entrypoints, list):
            entrypoints = [path for path in entrypoints if isinstance(path, str) and study_read_scope.listed(snap, path)]
        return {**snap.identity(), "counts": snap.library["counts"], "entrypoints": entrypoints,
                "formula_reference": formula_reference,
                "domains": {k: {"records": len(v), "null_ids": sum(r["id"] is None for r in v)}
                            for k, v in domains.items()},
                "full_history_source_scan": visible_history, "model_semantic_scan_complete": False,
                "non_text_history_files": non_text_history,
                "coverage_status": snap.library["coverage_status"],
                "personalization": snap.library.get("personalization", {"status": "not_published"}),
                "freshness": self.freshness(snap),
                "semantic_discovery": {
                    "tool": "query", "domain": "semantic_candidates",
                    "subject_argument": "subject", "source_commit": snap.commit,
                    "request_argument": "contains JSON: query, rerank (auto/off/on), view (protected only)",
                    "example": {"query": "自然语言描述要找的知识点或旧记录", "rerank": "auto", "view": "protected"},
                    "dedicated_tool": "semantic_search", "limit_range": [1, 12],
                    "when": "Exact ID first. Use semantic candidates only when literal/alias lookup is insufficient; Decisions only for an ambiguous shortlist.",
                    "api_calls_possible": True, "api_provider": "OpenAI",
                    "writes": "derived local retrieval cache only; no formal or learner records",
                    "answer_protection": "Safe discovery metadata only; no answers, raw conversations, grades or mastery. Candidate relevance is not identity or learning evidence.",
                    "failure": "Explicit degraded status with safe metadata fallback; never substitute a stale index."
                },
                "material_preprocessing": {
                    "tool":"query","domain":"material_prepare","ids_argument":"one exact published PDF/image/HTML/text path",
                    "request_argument":"contains JSON: pages(one-based),mode(auto/text/vision),purpose(reference/question/user_work/solution),view(protected/after_attempt/direct),allow_answer_text,review(auto/off),review_pages(selected uncertain pages)",
                    "source_commit":snap.commit,"default_view":"protected","api_calls_possible":True,
                    "when":"Only explicitly requested material preparation. Not ordinary fetch, single-question tutoring, quick Capture, or reprocessing an already structured formal card.",
                    "answer_policy":"protected and after_attempt return metadata only. direct text requires explicit answer/content authorization and allow_answer_text=true; reference purpose does not unlock answers.",
                    "review":"auto checks only uncertainty-bearing pages with gpt-6-luna Decisions; review_pages marks known concerns. Original-page image required. Cached advisory flags, never approval; failures remain not_screened.",
                    "result":"Immutable original plus machine-derived review candidates; not original-image observation, formal intake or learning evidence."
                },
                "personalization_query": ({"tool": "query", "domain": "personal_context",
                    "knowledge_points_argument": "ids", "options_argument": ("contains as JSON: view,weak_knowledge_points,cursor,allow_historical,current_question_id,current_question_part,formal_id,source_id,personal_scope,materials,known_version"
                        if snap.subject == '408' else "contains as JSON: view,weak_knowledge_points,cursor,allow_historical"),
                    "views": ["protected", "after_attempt", "direct"],
                    "content_view_example": {"view": "after_attempt", "allow_historical": False},
                    "max_bytes_range": [1024, 1024 * 1024 if snap.subject == '408' else 65536],
                    **({"default_max_bytes": 256 * 1024, "default_personal_scope": "full", "default_materials": "core",
                        "completion": "personal_read_receipt.complete; never treat a summary or withheld Pxx parent as a full read",
                        "version_reuse": "known_version uses context_version; version remains the immutable snapshot identity"}
                       if snap.subject == '408' else {}),
                    "pagination": "personal_context.next_cursor; keep source_commit fixed"} if snap.subject in ('math', '408') else None),
                "math_methods_query": ({"tool": "query", "domain": "math_methods",
                    "ids_argument": "ids: exact published method_id, step_id or conclusion_id; no fuzzy aliases",
                    "options_argument": "contains as JSON: view,allow_historical",
                    "views": ["protected", "after_attempt", "direct"], "default_view": "protected",
                    "content_view_example": {"view": "after_attempt", "allow_historical": False},
                    "max_bytes_range": [1024, READ_LIMIT],
                    "pagination": "next_offset; preserve source_commit, ids and contains; whole objects only",
                    "dependencies": "method step_graph includes full prerequisite closure; conclusions retain conditions/exceptions/direct_use; refinements are exact-ID candidates, not expanded",
                    "evidence_policy": "reference material only; personal state comes from personal_context"}
                    if snap.subject == 'math' and snap._row(MATH_METHOD_CATALOG)
                    and snap._row(MATH_METHOD_BINDINGS) else None),
                "file_delivery": {"binary_fetch": "MCP embedded resource with exact original bytes",
                    "bundle_tool": "query", "bundle_domain": "source_files", "paths_argument": "ids",
                    "max_source_bytes": 8 * 1024 * 1024, "max_files": 30,
                    "protected_access": "Use answer_scope only after an explicit release. Bind view=direct, purpose=question_review or formal_review, allow_answer_text=true, source_id and item_id to a single published item. For ZIP put {answer_scope: ...} in contains. Mixed files remain withheld.",
                    "workflow_input": "First use workflow collection=bundle_identity to verify an uploaded candidate JSON at the same source_commit and target_date. Reuse it when its recomputed native self-hash matches; do not redownload the full bundle. Native self-hash is SHA-256 of UTF-8 JSON with ensure_ascii=false, sort_keys=true, separators=(',',':'), omitting only candidate_bundle_sha256.",
                    "workflow_file": "If no matching uploaded bundle exists, page named collections to EOF. Optional collection=bundle_file exports exact native self-hashed JSON (transport provenance stays in metadata), compressed when large. Verify resource sha256, content_sha256 and native_candidate_bundle_sha256. File-delivery failure must fall back to pagination, not repeated full-file requests.",
                    "host_boundary": "A resource is exported, not proof that the host saved it. Use the host file API to materialize; never transcribe Base64. If unavailable, continue semantic analysis and retain exact source asset bindings for local assembly."},
                "english_context_query": ({"tool": "query", "domain": "english_learning_context",
                    "request_argument": "contains as JSON: source_id,sentence_id,source_article,source_hash,sentence_sha256,bank_ids,sp_ids,concept_ids,aliases,view,collection,allow_historical,question_id,unlocked_question_ids",
                    "views": ["before_attempt", "question_review"],
                    "collections": ["formal_records", "sentence_patterns", "learning_history", "recorded_article_records", "sentence_support_records"],
                    "pagination": "use offset/limit; keep source_commit and request JSON fixed"} if snap.subject == 'english' else None),
                "files": [{k: snap.files[p].get(k) for k in ("path", "bytes", "sha256", "kind")} for p in page],
                "matching_file_count": len(paths), "next_offset": offset + len(page) if offset + len(page) < len(paths) else None,
                "use": "Query native records by domain/ID, fetch original text, or view_image. Always reuse source_commit."}

    def freshness(self, snap: Snapshot) -> dict[str, Any]:
        if self.base != BASE.resolve():
            return {"status": "unknown", "reason": "nonproduction_snapshot"}
        if snap.subject == "english" and snap.commit != self.published.latest("english").get("source_commit"):
            return dict(status="historical", formal_freshness="historical", personal_summary_freshness="historical",
                        snapshot_id=snap.commit, reason="not_latest_publication")
        current = {}
        try:
            current = study_personalization.native_call(snap.subject, 'identity')
            published = (snap.library.get('personalization', {}) if snap.subject != 'english'
                         else {'formal_version': snap.library.get('formal_version')})
            result = study_personalization.compare_identity(published, current,
                                                            personalized=snap.subject != 'english')
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            result = {'status': 'unknown', 'reason': 'native_identity_unavailable',
                      'error_type': type(exc).__name__}
        if snap.subject == 'english':
            from study_personalization import english_summary_freshness
            result.update(formal_freshness=result['status'], **english_summary_freshness(
                snap.library.get('personalization', {}), current.get('personal_summary', {})))
        result['snapshot_id'] = snap.commit
        # A failed final publication may matter even when formal bytes match.
        cutoff = str(snap.receipt.get('committed_at') or '')
        pending = []
        for row in self.published.local_pending_receipts(snap.subject):
            requested = str(row.get('requested_at') or row.get('created_at') or '')
            if row.get('request_origin') == 'postcommit' and requested > cutoff:
                pending.append({'event_id': row.get('event_id'), 'status': row.get('status'),
                                'error': row.get('error')})
        result['pending_publications'] = pending
        if pending and result['status'] == 'current':
            result.update(status='pending', reason='newer_publication_not_completed')
        return result

    def math_methods(self, subject: str, ids: list[str] | None, source_commit: str = "",
                     view: str = "protected", allow_historical: bool = False,
                     offset: int = 0, limit: int = 20, max_bytes: int | None = None) -> dict[str, Any]:
        """Return exact material objects; never infer a route or personal mastery."""
        if offset > 0:
            require(bool(source_commit), "SOURCE_COMMIT_REQUIRED_FOR_CONTINUATION")
        require(subject == "math", "MATH_METHODS_SUBJECT_REQUIRED")
        require(isinstance(ids, list) and 1 <= len(ids) <= 64
                and all(isinstance(x, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", x) for x in ids)
                and len(ids) == len(set(ids)), "INVALID_MATH_METHOD_IDS")
        require(isinstance(view, str) and view in {"protected", "after_attempt", "direct"}
                and isinstance(allow_historical, bool), "INVALID_MATH_METHOD_OPTIONS")
        require(type(offset) is int and 0 <= offset <= len(ids)
                and type(limit) is int and 1 <= limit <= 64, "INVALID_PAGE")
        budget = PAGE_BYTES if max_bytes is None else max_bytes
        require(type(budget) is int and 1024 <= budget <= READ_LIMIT, "INVALID_MATH_METHOD_BUDGET")
        snap = self.snapshot(subject, source_commit)
        freshness = self.freshness(snap)
        if not allow_historical and freshness.get("status") != "current":
            return {**snap.identity(), "ok": False, "error": "MATH_METHODS_NOT_CURRENT",
                    "freshness": freshness, "historical_read_requested": False}
        catalog = snap.math_method_catalog()
        if any(identity.startswith("FCE-") for identity in ids):
            from study_formula_methods import extend_catalog
            try:
                catalog = extend_catalog(snap, catalog, ids, view)
            except (OSError, ValueError, KeyError) as exc:
                raise BridgeError("MATH_FORMULA_REFERENCE_UNAVAILABLE", str(exc)) from exc
        missing = [identity for identity in ids if identity not in catalog["objects"]]
        require(not missing, "MATH_METHOD_IDS_NOT_FOUND", missing)

        def id_list(node, field):
            values = node.get(field, [])
            require(isinstance(values, list) and all(isinstance(x, str) for x in values)
                    and len(values) == len(set(values)), "INVALID_MATH_METHOD_DEPENDENCIES", field)
            return values

        def step_closure(roots):
            result, seen, active = [], set(), set()
            pending = [(identity, False) for identity in reversed(roots)]
            while pending:
                identity, expanded = pending.pop()
                require(identity in catalog["steps"], "MATH_METHOD_STEP_NOT_FOUND", identity)
                node = catalog["steps"][identity]
                if expanded:
                    active.remove(identity)
                    seen.add(identity)
                    result.append(node)
                    continue
                if identity in seen:
                    continue
                require(identity not in active, "MATH_METHOD_STEP_CYCLE", identity)
                active.add(identity)
                pending.append((identity, True))
                pending.extend((dependency, False) for dependency in
                               reversed(id_list(node, "prerequisite_step_ids")))
            return result

        def verify_sources(nodes):
            # A source hash matching the pinned inventory proves binding, not
            # that the model has visually read that textbook/PDF page.
            for node in nodes:
                refs = node.get("source_refs")
                require(isinstance(refs, list) and bool(refs), "MATH_METHOD_SOURCE_REFS_REQUIRED")
                for ref in refs:
                    require(isinstance(ref, dict) and isinstance(ref.get("path"), str)
                            and isinstance(ref.get("sha256"), str)
                            and re.fullmatch(r"[0-9a-f]{64}", ref["sha256"]), "INVALID_MATH_METHOD_SOURCE_REF")
                    path = safe_path(ref["path"])
                    source = snap._row(path)
                    require(source is not None and source.get("sha256") == ref["sha256"],
                            "MATH_METHOD_SOURCE_HASH_MISMATCH", path)

        rows = []
        for identity in ids:
            value = catalog["objects"][identity]
            row = {"id": identity, "object_role": value["object_role"]}
            if view == "protected":
                row["status"] = "protected"
                rows.append(row)
                continue
            node = value["record"]
            row.update(record=node, source_path=value.get("source_path", MATH_METHOD_CATALOG),
                       source_sha256=value.get("source_sha256", catalog["source_refs"][0]["sha256"]))
            nodes = [node]
            if value["object_role"] == "method_reference":
                graph = step_closure(id_list(node, "step_ids"))
                conclusion_ids = id_list(node, "conclusion_ids")
                require(all(x in catalog["conclusions"] for x in conclusion_ids),
                        "MATH_METHOD_CONCLUSION_NOT_FOUND", conclusion_ids)
                conclusions = [catalog["conclusions"][x] for x in conclusion_ids]
                refinements = id_list(node, "refinement_ids")
                require(all(x in catalog["methods"] for x in refinements),
                        "MATH_METHOD_REFINEMENT_NOT_FOUND", refinements)
                row.update(step_graph=graph, step_graph_ids=[x["step_id"] for x in graph],
                           conclusions=conclusions, refinement_ids=refinements,
                           refinement_policy="candidate_ids_only;query_exact_id_before_using_details")
                nodes += graph + conclusions
            elif value["object_role"] == "step_reference":
                graph = step_closure(id_list(node, "prerequisite_step_ids"))
                row.update(prerequisite_steps=graph, prerequisite_step_ids=[x["step_id"] for x in graph])
                nodes += graph
            verify_sources(nodes)
            row["source_binding_status"] = "matches_pinned_inventory;original_pages_not_read_by_this_query"
            rows.append(row)
        base = {**snap.identity(), "domain": "math_methods", "schema": "study-math-method-query-v1",
                "catalog_schema": catalog["schema"], "view": view, "freshness": freshness,
                "historical_read_requested": allow_historical, "requested_ids": ids,
                "evidence_policy": "reference_material_only;not_personal_learning_or_mastery;check_current_problem_conditions",
                "registry_source_refs": catalog["source_refs"], "total_records": len(rows),
                "offset": offset, "max_bytes": budget, "model_semantic_scan_complete": False}

        def page(items):
            end = offset + len(items)
            return {**base, "status": "protected" if view == "protected" else "available",
                    "records": items, "returned_records": len(items),
                    "next_offset": end if end < len(rows) else None, "eof": end == len(rows)}
        selected = []
        for row in rows[offset:offset + limit]:
            candidate = page(selected + [row])
            size = len(encoded(candidate))
            if size > budget:
                if not selected:
                    return {**page([]), "status": "budget_too_small", "required_min_bytes": size,
                            "resolution": "raise max_bytes or fetch registry_source_refs to EOF; objects/formulas are never cut"}
                break
            selected.append(row)
        return page(selected)

    def personal_context(self, subject: str, knowledge_points: list[str],
                         weak_knowledge_points: list[str] | None = None, source_commit: str = '',
                         view: str = 'protected', max_bytes: int | None = None, cursor: str = '',
                         allow_historical: bool = False, **question_options) -> dict[str, Any]:
        if cursor:
            require(bool(source_commit), "SOURCE_COMMIT_REQUIRED_FOR_CONTINUATION")
        require(subject in ('math', '408'), 'PERSONALIZATION_SUBJECT_UNSUPPORTED')
        require(view in ('protected', 'after_attempt', 'direct'), 'INVALID_PERSONAL_VIEW')
        require(set(question_options) <= {'exam_question_id', 'current_question_id', 'current_question_part', 'formal_id', 'source_id',
                                         'personal_scope', 'materials', 'known_version'}
                and (not question_options or subject == '408'), 'INVALID_PERSONAL_QUESTION_OPTIONS')
        require(all(v is None or (isinstance(v, str) and 0 < len(v) <= 200) for v in question_options.values()),
                'INVALID_PERSONAL_QUESTION_OPTIONS')
        require(question_options.get('personal_scope', 'full') in {'full', 'summary'}
                and question_options.get('materials', 'core') in {'all', 'core', 'personal', 'visual'},
                'INVALID_PERSONAL_MATERIAL_OPTIONS')
        if subject == '408':
            question_options.setdefault('personal_scope', 'full')
            question_options.setdefault('materials', 'core')
        has_question = any(question_options.get(k) for k in ('exam_question_id', 'current_question_id', 'formal_id', 'source_id'))
        require(isinstance(knowledge_points, list) and (0 if has_question else 1) <= len(knowledge_points) <= 64
                and all(isinstance(x, str) and 0 < len(x) <= 200 for x in knowledge_points), 'INVALID_KNOWLEDGE_POINTS')
        weak = weak_knowledge_points or []
        require(isinstance(weak, list) and len(weak) <= 64 and all(isinstance(x, str) and 0 < len(x) <= 200 for x in weak), 'INVALID_WEAK_POINTS')
        budget = (10000 if subject == 'math' else 256 * 1024) if max_bytes is None else max_bytes
        require(type(budget) is int and 1024 <= budget <= (65536 if subject == 'math' else 1024 * 1024)
                and len(cursor) <= 8192, 'INVALID_PERSONAL_BUDGET')
        snap = self.snapshot(subject, source_commit)
        freshness = self.freshness(snap)
        entry = snap.library.get('entrypoints', {}).get('personalization')
        require(isinstance(entry, str), 'PERSONALIZATION_NOT_PUBLISHED')
        if not allow_historical and freshness.get('status') != 'current':
            return {**snap.identity(), 'ok': False, 'error': 'PERSONALIZATION_NOT_CURRENT',
                    'freshness': freshness, 'fallback': 'current_question_only'}
        prefix = 'personalization/' + subject + '/'
        require(entry.startswith(prefix), 'INVALID_PERSONAL_ENTRYPOINT')
        # Only the published allowlist is materialized. Native readers never see
        # live source paths, and each source hash is verified by Snapshot.path.
        snap.materialize_personalization()
        # Resolve and verify the pinned bytes before the native call so a cache
        # integrity failure keeps its own error instead of being reported as an
        # unavailable native reader.
        manifest_path = str(snap.path(entry))
        try:
            # A sharded publication names the exact knowledge-point shards this
            # query needs, so one point never pulls in the rest of the library.
            if snap._personal_partial:
                plan = study_personalization.native_call(subject, 'plan', manifest_path=manifest_path,
                    knowledge_points=knowledge_points, weak_knowledge_points=weak,
                    **({'view': view, **question_options} if subject == '408' else {}))
                shards = [p for p in (plan.get('files') or []) if isinstance(p, str)]
                if shards:
                    snap.materialize_paths(shards)
            result = study_personalization.native_call(subject, 'query', manifest_path=manifest_path,
                knowledge_points=knowledge_points, weak_knowledge_points=weak, view=view,
                max_bytes=budget, cursor=cursor, **question_options)
        except BridgeError:
            raise
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            raise BridgeError('PERSONAL_QUERY_UNAVAILABLE', type(exc).__name__) from exc
        return {**snap.identity(), 'freshness': freshness, 'personal_context': result,
                'source_mappings': snap.source_mappings(result),
                'answer_view': view, 'historical_read_requested': allow_historical,
                'model_semantic_scan_complete': False}

    @staticmethod
    def _copy_english_paths(snap: Snapshot, root: Path, paths: list[str], budget: dict[str, Any]) -> None:
        ordered = sorted(set(paths) - budget['paths'])
        require(len(budget['paths']) + len(ordered) <= ENGLISH_CONTEXT_FILES,
                'ENGLISH_CONTEXT_SNAPSHOT_TOO_LARGE',
                {'files': len(budget['paths']) + len(ordered), 'max_files': ENGLISH_CONTEXT_FILES})
        declared = budget['bytes']
        for relative in ordered:
            row = snap._row(relative)
            require(row is not None, 'ENGLISH_CONTEXT_SOURCE_NOT_PUBLISHED', relative)
            declared += int(row.get('bytes') or 0)
        require(declared <= MAX_ENGLISH_CONTEXT_BYTES, 'ENGLISH_CONTEXT_SNAPSHOT_TOO_LARGE',
                {'bytes': declared, 'max_bytes': MAX_ENGLISH_CONTEXT_BYTES})
        for relative in ordered:
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(snap.path(relative), target)
        budget['paths'].update(ordered)
        budget['bytes'] = declared

    @staticmethod
    def _english_article_dependencies(snap: Snapshot, source_article: str) -> tuple[list[str], str | None]:
        relative = safe_path(source_article)
        require(relative.startswith('articles/') and relative.endswith('.md'),
                'INVALID_ENGLISH_SOURCE_ARTICLE')
        text = snap.text(relative)
        paths = [relative]
        metadata = lambda name: [m.group(1).strip() for m in re.finditer(
            rf'^\s*(?:-\s*)?{re.escape(name)}\s*[：:]\s*`?([^`\r\n]+?)`?\s*$', text, re.MULTILINE)]
        handoffs = metadata('pipeline_handoff')
        if handoffs:
            require(len(set(handoffs)) == 1, 'INVALID_ENGLISH_SOURCE_BINDING')
            handoff = safe_path(handoffs[0])
            paths.append(handoff)
            document = json.loads(snap.text(handoff))
            locator = str((document.get('canonical_payload') or {}).get('path') or '')
            require(locator and not Path(locator).is_absolute() and '..' not in Path(locator).parts,
                    'INVALID_ENGLISH_SOURCE_BINDING')
            base = PurePosixPath(handoff).parent
            candidates = []
            while True:
                candidate = (base / locator).as_posix()
                if snap._row(candidate) is not None:
                    candidates.append(candidate)
                if str(base) == '.':
                    break
                base = base.parent
            require(len(set(candidates)) == 1, 'INVALID_ENGLISH_SOURCE_BINDING')
            paths.append(candidates[0])
            ids = metadata('source_id')
            return paths, ids[0] if len(set(ids)) == 1 else None
        reference = re.search(r'EXAM-READING-(20\d{2})-T([1-4])', text[:6000])
        if reference is None:
            reference = re.search(r'(20\d{2})-english-i-text-([1-4])', Path(relative).name)
        if reference is None:
            reference = re.search(r'(20\d{2})\s*(?:年|/|English).*?Text\s*([1-4])', text[:3000], re.IGNORECASE)
        if reference:
            canonical = f'raw/articles/exam-reading-corpus/{reference.group(1)}/text-{reference.group(2)}.json'
            paths.append(canonical)
            record = json.loads(snap.text(canonical))
            return paths, str(record.get('source_id') or '') or None
        return paths, 'RAW-ARTICLE-20260424-001' if relative == 'articles/2026-04-24-article.md' else None

    def english_context(self, request: dict[str, Any], *, source_commit: str = '', offset: int = 0,
                        limit: int = 20, max_bytes: int = PAGE_BYTES) -> dict[str, Any]:
        if offset > 0:
            require(bool(source_commit), "SOURCE_COMMIT_REQUIRED_FOR_CONTINUATION")
        require(isinstance(request, dict), 'ENGLISH_CONTEXT_REQUEST_MUST_BE_JSON')
        allowed = {'source_id', 'sentence_id', 'source_article', 'source_hash', 'sentence_sha256',
                   'bank_ids', 'sp_ids', 'concept_ids', 'aliases', 'view', 'collection', 'allow_historical',
                   'question_id', 'unlocked_question_ids'}
        require(set(request) <= allowed, 'INVALID_ENGLISH_CONTEXT_REQUEST', sorted(set(request) - allowed))
        view = request.get('view', 'before_attempt')
        collection = request.get('collection', '')
        allow_historical = request.get('allow_historical', False)
        require(view in ('before_attempt', 'question_review') and isinstance(collection, str)
                and isinstance(allow_historical, bool), 'INVALID_ENGLISH_CONTEXT_REQUEST')
        question_id = request.get('question_id')
        unlocked = request.get('unlocked_question_ids', [])
        require((question_id is None or isinstance(question_id, str) and re.fullmatch(r'Q[0-9]{1,3}', question_id))
                and isinstance(unlocked, list) and len(unlocked) <= 32
                and all(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,256}:Q[0-9]{1,3}', value)
                        for value in unlocked),
                'INVALID_ENGLISH_QUESTION_SCOPE')
        collections_allowed = {'', 'formal_records', 'sentence_patterns', 'learning_history',
                               'recorded_article_records', 'sentence_support_records'}
        require(collection in collections_allowed, 'UNKNOWN_ENGLISH_CONTEXT_COLLECTION')
        for key in ('source_id', 'sentence_id', 'source_article', 'source_hash', 'sentence_sha256'):
            require(key not in request or isinstance(request[key], str) and len(request[key]) <= 500,
                    'INVALID_ENGLISH_CONTEXT_REQUEST', key)
        for key in ('bank_ids', 'sp_ids', 'concept_ids', 'aliases'):
            values = request.get(key, [])
            require(isinstance(values, list) and len(values) <= 16
                    and all(isinstance(x, str) and 0 < len(x) <= 200 for x in values),
                    'INVALID_ENGLISH_CONTEXT_REQUEST', key)
        identity_count = sum(len(request.get(key, [])) for key in ('bank_ids', 'sp_ids', 'concept_ids', 'aliases'))
        require(identity_count <= 16 and 0 <= offset and 1 <= limit <= 100
                and 1024 <= max_bytes <= 65536, 'INVALID_ENGLISH_CONTEXT_PAGE')
        require(any(request.get(key) for key in ('source_id', 'source_article', 'bank_ids', 'sp_ids', 'concept_ids', 'aliases')),
                'ENGLISH_CONTEXT_SELECTOR_REQUIRED')
        for key in ('source_hash', 'sentence_sha256'):
            require(not request.get(key) or bool(re.fullmatch(r'[0-9a-f]{64}', request[key])),
                    'INVALID_ENGLISH_CONTEXT_SOURCE_HASH', key)
        snap = self.snapshot('english', source_commit)
        latest_commit = self.published.latest('english').get('source_commit')
        # An explicitly pinned older publication is self-contained.  Its data
        # must not be mixed with a live-repository identity read merely to label
        # it historical.
        freshness = ({'status': 'historical', 'reason': 'not_latest_publication',
                      'snapshot_id': snap.commit, 'latest_snapshot_id': latest_commit}
                     if snap.commit != latest_commit else self.freshness(snap))
        if not allow_historical and freshness.get('status') != 'current':
            return {**snap.identity(), 'ok': False, 'error': 'ENGLISH_CONTEXT_NOT_CURRENT',
                    'freshness': freshness, 'fallback': 'current_input_only'}
        expected = snap.library.get('formal_source_hashes')
        formal_version = snap.library.get('formal_version')
        formal_paths = ('bank/master_bank.csv', 'bank/mastered_items.csv', 'bank/sentence_patterns.md',
                        'bank/review_exclusion_ledger.jsonl', 'bank/learning_events.jsonl', 'bank/concept_aliases.json')
        require(isinstance(expected, dict) and set(expected) == set(formal_paths)
                and isinstance(formal_version, str) and re.fullmatch(r'[0-9a-f]{64}', formal_version),
                'ENGLISH_FORMAL_IDENTITY_NOT_PUBLISHED')
        selected_paths = []
        for path in formal_paths:
            expected_sha = expected[path]
            require(expected_sha is None or isinstance(expected_sha, str) and re.fullmatch(r'[0-9a-f]{64}', expected_sha),
                    'INVALID_ENGLISH_FORMAL_IDENTITY', path)
            if expected_sha is not None:
                row = snap._row(path)
                require(row is not None and row.get('sha256') == expected_sha,
                        'ENGLISH_FORMAL_SOURCE_DIFFERS_FROM_SNAPSHOT', path)
                selected_paths.append(path)
        effective_source_id = request.get('source_id')
        article_dependencies: list[str] = []
        if request.get('source_article'):
            dependencies, resolved_id = self._english_article_dependencies(snap, request['source_article'])
            selected_paths.extend(dependencies)
            article_dependencies = dependencies
            effective_source_id = effective_source_id or resolved_id
        question_scope = ({'source_id': effective_source_id, 'question_id': question_id}
                          if view == 'question_review' and effective_source_id and question_id
                          and f'{effective_source_id}:{question_id}' in unlocked else None)
        event_path = 'bank/learning_events.jsonl'
        package_ids: set[str] = set()
        if expected.get(event_path) is not None:
            events = [json.loads(line) for line in snap.text(event_path).split("\n") if line.strip()]
            bank_ids = set(request.get('bank_ids', []))
            sp_ids = set(request.get('sp_ids', []))
            concepts = set(request.get('concept_ids', []))
            aliases = request.get('aliases', [])
            if aliases:
                normalized = lambda value: ' '.join(str(value).strip().casefold().split())
                known: dict[str, set[str]] = {}
                for row in csv.DictReader(io.StringIO(snap.text('bank/master_bank.csv'))):
                    known.setdefault(normalized(row['item']), set()).add('bank:' + row['id'])
                patterns = snap.text('bank/sentence_patterns.md')
                for match in re.finditer(r'^## (SP-\d{3,})｜(.+)$', patterns, re.MULTILINE):
                    known.setdefault(normalized(match.group(2)), set()).add('sp:' + match.group(1))
                if expected.get('bank/concept_aliases.json') is not None:
                    for alias, targets in json.loads(snap.text('bank/concept_aliases.json')).get('aliases', {}).items():
                        known.setdefault(normalized(alias), set()).update(targets)
                for alias in aliases:
                    concepts.update(known.get(normalized(alias), set()))
            bank_ids.update(x[5:] for x in concepts if x.startswith('bank:'))
            sp_ids.update(x[3:] for x in concepts if x.startswith('sp:'))
            permitted = [row for row in events if row.get('visibility') == 'answer_free']
            source_matches = [row for row in permitted if effective_source_id and request.get('sentence_id')
                              and row.get('event', {}).get('source_id') == effective_source_id
                              and row.get('event', {}).get('unit_id') == request.get('sentence_id')]
            for row in source_matches:
                event = row.get('event', {})
                bank_ids.update(event.get('bank_ids', []))
                sp_ids.update(event.get('sentence_pattern_ids', []))
                concepts.update(event.get('concept_ids', []))
            related = []
            for row in permitted:
                event = row.get('event', {})
                if (set(event.get('bank_ids', [])) & bank_ids
                        or set(event.get('sentence_pattern_ids', [])) & sp_ids
                        or set(event.get('concept_ids', [])) & concepts
                        or row in source_matches):
                    related.append(row)
            if question_scope:
                exact = study_english_context.question_events(events, **question_scope)
                related.extend(exact if len(exact) <= 8 else [exact[0], *exact[-7:]])
            require(len(related) <= 64, 'ENGLISH_CONTEXT_HISTORY_TOO_BROAD', {'matching_events': len(related)})
            for row in related:
                for ref in row.get('evidence_refs', []):
                    package_id = str(ref.get('package_id') or '')
                    require(bool(re.fullmatch(r'EN-PKG-\d{8}-[0-9A-F]{16}', package_id)),
                            'INVALID_ENGLISH_CONTEXT_EVIDENCE')
                    package_ids.add(package_id)
        with tempfile.TemporaryDirectory(prefix='study-mcp-english-context-') as temporary:
            root = Path(temporary).resolve()
            budget = {'paths': set(), 'bytes': 0}
            self._copy_english_paths(snap, root, selected_paths, budget)
            for package_id in sorted(package_ids):
                day = f'{package_id[7:11]}-{package_id[11:13]}-{package_id[13:15]}'
                prefix = f'intake/packages/{day}/{package_id}/'
                manifest_path = prefix + 'manifest.json'
                self._copy_english_paths(snap, root, [manifest_path], budget)
                manifest = json.loads((root / manifest_path).read_text())
                needed = [prefix + name for name in ('conversation.json', 'source.json')]
                needed.extend(prefix + row['path'] for row in manifest.get('attachments', [])
                              if row.get('role') == 'user_work_image')
                self._copy_english_paths(snap, root, needed, budget)
            native_request = {key: request.get(key) for key in ('source_id', 'sentence_id', 'source_article',
                              'source_hash', 'sentence_sha256', 'bank_ids', 'sp_ids', 'concept_ids', 'aliases')}
            native_request.update(question_review_scope=question_scope, native_max_bytes=256 * 1024)
            try:
                result = study_english_context.query_snapshot(root, native_request)
            except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                raise BridgeError('ENGLISH_CONTEXT_QUERY_UNAVAILABLE', type(exc).__name__) from exc
        if result.get('status') == 'ready':
            require(result.get('formal_version') == formal_version and result.get('source_hashes') == expected,
                    'ENGLISH_CONTEXT_VERSION_DIFFERS_FROM_SNAPSHOT')
            if request.get('source_article') and not study_read_scope.listed(snap, request['source_article']):
                # The native historical article can contain several questions.
                # The single-question grant only covers typed event rows.
                if result.get('recorded_article_episode') is not None:
                    result['recorded_article_episode'] = None
                    result['recorded_article_status'] = 'withheld_mixed_source'
        nested_support = result.get('sentence_support') if isinstance(result.get('sentence_support'), dict) else {}
        nested_episode = result.get('recorded_article_episode') if isinstance(result.get('recorded_article_episode'), dict) else {}
        collections = {
            'formal_records': result.get('formal_records', []),
            'sentence_patterns': result.get('sentence_patterns', []),
            'learning_history': result.get('learning_history', []),
            'recorded_article_records': nested_episode.get('records', []),
            'sentence_support_records': nested_support.get('records', []),
        }
        require(all(isinstance(rows, list) for rows in collections.values()), 'INVALID_ENGLISH_CONTEXT_RESULT')
        base = {**snap.identity(), 'freshness': freshness,
                'answer_view': 'question_review' if question_scope else 'before_attempt',
                'question_scope_status': ('scoped' if question_scope else 'withheld'
                                          if view == 'question_review' else 'not_requested'),
                'historical_read_requested': allow_historical, 'english_learning_context': result,
                'formal_source_refs': [{'path': path, 'sha256': expected[path], 'source_commit': snap.commit}
                                       for path in formal_paths if expected[path] is not None],
                'article_source_refs': [{'path': path, 'sha256': snap._row(path)['sha256'],
                                         'source_commit': snap.commit} for path in article_dependencies
                                        if study_read_scope.listed(snap, path)],
                'evidence_package_refs': [{'package_id': package_id,
                    'manifest_path': f'intake/packages/{package_id[7:11]}-{package_id[11:13]}-{package_id[13:15]}/{package_id}/manifest.json',
                    'manifest_sha256': snap._row(f'intake/packages/{package_id[7:11]}-{package_id[11:13]}-{package_id[13:15]}/{package_id}/manifest.json')['sha256'],
                    'source_commit': snap.commit} for package_id in sorted(package_ids)
                    if study_read_scope.listed(snap, f'intake/packages/{package_id[7:11]}-{package_id[11:13]}-{package_id[13:15]}/{package_id}/manifest.json')],
                'model_semantic_scan_complete': False}
        if collection:
            compact = {key: value for key, value in result.items()
                       if key not in ('formal_records', 'sentence_patterns', 'learning_history')}
            if isinstance(compact.get('sentence_support'), dict):
                compact['sentence_support'] = {key: value for key, value in compact['sentence_support'].items() if key != 'records'}
            if isinstance(compact.get('recorded_article_episode'), dict):
                compact['recorded_article_episode'] = {key: value for key, value in compact['recorded_article_episode'].items() if key != 'records'}
            base['english_learning_context'] = compact
            return {**base, 'collection': collection, **self.page(collections[collection], offset, limit, max_bytes)}
        require(offset == 0, 'ENGLISH_CONTEXT_SUMMARY_OFFSET_MUST_BE_ZERO')
        compact = {key: value for key, value in result.items()
                   if key not in ('formal_records', 'sentence_patterns', 'learning_history')}
        if isinstance(compact.get('sentence_support'), dict):
            compact['sentence_support'] = {key: value for key, value in compact['sentence_support'].items() if key != 'records'}
        if isinstance(compact.get('recorded_article_episode'), dict):
            compact['recorded_article_episode'] = {key: value for key, value in compact['recorded_article_episode'].items() if key != 'records'}
        base['english_learning_context'] = compact
        base['collections'] = {key: len(value) for key, value in collections.items()}
        return base

    def fetch(self, subject: str, path: str, source_commit: str = "", start_byte: int = 0,
              max_bytes: int = PAGE_BYTES, *, answer_scope: dict[str, Any] | None = None) -> dict[str, Any]:
        if start_byte > 0:
            require(bool(source_commit), "SOURCE_COMMIT_REQUIRED_FOR_CONTINUATION")
        snap = self.snapshot(subject, source_commit)
        require(0 <= start_byte and 1 <= max_bytes <= READ_LIMIT, "INVALID_READ_RANGE")
        path = study_read_scope.authorize(snap, path, answer_scope)
        local = snap.path(path)
        size = local.stat().st_size
        require(start_byte <= size, "READ_START_PAST_EOF")
        with local.open("rb") as stream:
            require(b"\0" not in stream.read(8192), "BINARY_SOURCE_USE_VIEW_IMAGE")
            stream.seek(start_byte)
            raw = stream.read(max_bytes)
        try:
            body = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            if exc.reason == "unexpected end of data" and exc.start > 0:
                raw = raw[:exc.start]
                body = raw.decode("utf-8")
            else:
                raise BridgeError("NOT_UTF8_OR_OFFSET_INSIDE_CHARACTER", {"path": path, "start_byte": start_byte}) from exc
        require(bool(raw) or start_byte == size, "READ_BUDGET_SMALLER_THAN_CHARACTER")
        end = start_byte + len(raw)
        full_sha256 = snap._row(path)["sha256"]
        return {**snap.identity(), "path": path, "sha256": full_sha256,
                "source_ref": {"path": f"study-library://{snap.commit}/{path}", "sha256": full_sha256},
                "hash_scope": {"sha256": "complete_file", "source_ref.sha256": "complete_file",
                               "returned_sha256": "returned_utf8_bytes"},
                "total_bytes": size, "start_byte": start_byte, "end_byte_exclusive": end,
                "returned_sha256": digest(raw), "text": body, "eof": end == size,
                "next_start_byte": end if end < size else None, "truncated": False,
                "range_is_partial_file": start_byte != 0 or end != size}

    def original_file(self, subject: str, path: str, source_commit: str = "", *,
                      answer_scope: dict[str, Any] | None = None):
        """Export only a verified member of the pinned library; no live arbitrary paths."""
        snap = self.snapshot(subject, source_commit)
        path = study_read_scope.authorize(snap, path, answer_scope)
        local = snap.path(path)
        require(local.stat().st_size <= 8 * 1024 * 1024, "SOURCE_FILE_EXCEEDS_RESOURCE_LIMIT",
                {"path": path, "source_bytes": local.stat().st_size, "max_bytes": 8 * 1024 * 1024})
        raw = local.read_bytes()
        sha = snap._row(path)["sha256"]
        require(digest(raw) == sha, "SOURCE_FILE_CHANGED")
        mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
        uri = f"study-library://{snap.commit}/{quote(path, safe='/')}"
        meta = {**snap.identity(), "path": path, "file_name": Path(path).name,
                "mime_type": mime, "sha256": sha, "bytes": len(raw), "resource_uri": uri,
                "source_ref": {"path": path, "sha256": sha, "source_commit": snap.commit},
                "file_resource_exported": True, "host_file_materialization_verified": False,
                "use": "Save the attached original file with the host file API and verify SHA-256. If that API is unavailable, retain this exact source_ref for local assembly and continue analysis; view_image remains the visual-reading tool."}
        return meta, raw, mime, uri

    def source_files(self, subject: str, paths: list[str], source_commit: str = "", *,
                     answer_scope: dict[str, Any] | None = None):
        require(isinstance(paths, list) and 1 <= len(paths) <= 30 and len(paths) == len(set(paths)),
                "UNIQUE_SOURCE_PATHS_REQUIRED")
        snap = self.snapshot(subject, source_commit)
        paths = [study_read_scope.authorize(snap, path, answer_scope) for path in paths]
        files, total = [], 0
        for path in paths:
            local = snap.path(path)
            total += local.stat().st_size
            require(total <= 8 * 1024 * 1024, "SOURCE_BUNDLE_TOO_LARGE", {"max_bytes": 8 * 1024 * 1024})
            files.append(self.original_file(subject, path, snap.commit, answer_scope=answer_scope))
        manifest = {"schema": "study-source-files-v1", **snap.identity(),
                    "files": [{"path": m["path"], "sha256": m["sha256"], "size": len(raw)}
                              for m, raw, _, _ in files]}
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, raw in [("source_manifest.json", encoded(manifest))] + [("sources/"+m["path"], raw) for m, raw, _, _ in files]:
                info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, raw)
        raw = buffer.getvalue(); sha = digest(raw)
        # Hosts materialize successive resources by their basename. A fixed
        # name can overwrite an earlier, different export in the same chat.
        file_name = f"source-files-{sha[:16]}.zip"
        uri = f"study-library-export://{snap.commit}/{sha}/{file_name}"
        return ({**snap.identity(), "schema": "study-source-file-export-v1", "file_name": file_name,
                 "sha256": sha, "bytes": len(raw), "source_files": manifest["files"],
                 "file_resource_exported": True, "host_file_materialization_verified": False,
                 "resource_uri": uri}, raw, "application/zip", uri)

    def workflow_file(self, project: str, subject: str, target_date: str, source_commit: str = ""):
        snap, day, result = self._workflow_result(project, subject, target_date, source_commit)
        # This local reference owns the immutable result even if another date
        # evicts its cache slot. Serialization must not hold the store-wide
        # lock and delay unrelated exact reads.
        key = (project, snap.commit, day)
        summary_view = self._d_workflow_view(result) if project == "D" else result
        summary = self._workflow_summary(snap, project, day, summary_view)
        native = result
        envelope = {}
        if project == "C" and result.get("schema_version") in {"warmup-candidate-bundle-v2", "warmup-candidate-bundle-v3"}:
            envelope = {k: result[k] for k in C_WORKFLOW_ENVELOPE_FIELDS if k in result}
            native = {k: v for k, v in result.items() if k not in envelope}
            body = {k: v for k, v in native.items() if k != "candidate_bundle_sha256"}
            actual = digest(json.dumps(body, ensure_ascii=False, sort_keys=True,
                                       separators=(",", ":")).encode("utf-8"))
            require(actual == native.get("candidate_bundle_sha256"),
                    "NATIVE_CANDIDATE_BUNDLE_SELF_HASH_MISMATCH")
        raw = encoded(native)
        require(len(raw) <= 32 * 1024 * 1024, "WORKFLOW_FILE_TOO_LARGE")
        content_sha = digest(raw)
        content_bytes = len(raw)
        file_name, mime, suffix = "candidate-bundle.json", "application/json", "json"
        compression = "none"
        # Preserve every native result byte, but avoid multi-megabyte Base64
        # responses when the same JSON compresses into a small file resource.
        if len(raw) > 256 * 1024:
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
                info = zipfile.ZipInfo("candidate-bundle.json", (1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, raw)
            raw = buffer.getvalue()
            file_name, mime, suffix = "candidate-bundle.zip", "application/zip", "zip"
            compression = "zip-deflate"
        sha = digest(raw); uri = f"study-workflow://{key[1]}/{project}/{target_date}/{sha}.{suffix}"
        return ({**{k:v for k,v in summary.items() if k != "result"},
                 **envelope,
                 "file_name": file_name, "sha256": sha, "bytes": len(raw),
                 "compression": compression, "content_file_name": "candidate-bundle.json",
                 "content_sha256": content_sha, "content_bytes": content_bytes,
                 "native_candidate_bundle_sha256": summary["result"].get("candidate_bundle_sha256"),
                 "collections": summary["result"].get("collections"), "resource_uri": uri,
                 "file_resource_exported": True, "host_file_materialization_verified": False,
                 "model_semantic_scan_complete": False,
                 "fallback": "If file delivery fails, page the advertised collections to eof using next_offset; do not retry the full file indefinitely."}, raw, mime, uri)

    @staticmethod
    def page(rows: list[Any], offset: int, limit: int, max_bytes: int = PAGE_BYTES) -> dict[str, Any]:
        require(0 <= offset <= len(rows) and 1 <= limit <= 200, "INVALID_PAGE")
        page, used = [], 0
        for row in rows[offset:offset + limit]:
            size = len(encoded(row))
            if used + size > max_bytes:
                require(page, "RECORD_EXCEEDS_PAGE_USE_FETCH", {
                    "source_path": row.get("source_path") if isinstance(row, dict) else None, "record_bytes": size})
                break
            page.append(row)
            used += size
        end = offset + len(page)
        return {"total_records": len(rows), "offset": offset, "returned_records": len(page),
                "records": page, "next_offset": end if end < len(rows) else None,
                "eof": end == len(rows), "server_evaluated_complete_domain": True,
                "model_semantic_scan_complete": False}

    def semantic_candidates(self, subject: str, query: str, source_commit: str = "",
                            limit: int = 8, rerank: str = "auto", view: str = "protected") -> dict[str, Any]:
        """Discover pinned safe candidates; only this explicit path may call model APIs."""
        from study_semantic import SemanticEngine, SemanticError
        require(isinstance(query, str) and 0 < len(query.strip()) <= 500
                and isinstance(limit, int) and not isinstance(limit, bool) and 1 <= limit <= 12
                and isinstance(rerank, str) and rerank in {"auto", "off", "on"}
                and view == "protected", "INVALID_SEMANTIC_REQUEST")
        snap = self.snapshot(subject, source_commit)
        with self.lock:
            if self._semantic_engine is None:
                self._semantic_engine = SemanticEngine(self.base)
            engine = self._semantic_engine
        try:
            result = engine.search(snap, query.strip(), limit=limit, rerank=rerank, view=view)
        except SemanticError as exc:
            raise BridgeError(getattr(exc, "code", "SEMANTIC_UNAVAILABLE")) from exc
        return {**result, **snap.identity(), "freshness": self.freshness(snap),
                "answer_view": "protected", "formal_write_count": 0, "learner_evidence_write_count": 0,
                "use": "Candidates only. Read the selected exact ID through the original protected context reader at this source_commit; do not infer answers, identity, mastery or eligibility from relevance."}

    def prepare_material(self, subject: str, path: str, source_commit: str = "", *,
                         pages: list[int] | None = None, mode: str = "auto", purpose: str = "reference",
                         view: str = "protected", allow_answer_text: bool = False,
                         review: str = "auto", review_pages: list[int] | None = None,
                         answer_scope: dict[str, Any] | None = None) -> dict[str, Any]:
        from study_materials import MaterialPreprocessor
        from study_material_inputs import output_for,register_output
        snap=self.snapshot(subject,source_commit)
        require(isinstance(path,str) and bool(path),"MATERIAL_PATH_REQUIRED")
        require(type(allow_answer_text) is bool and isinstance(view,str) and view in {"protected","after_attempt","direct"},"INVALID_MATERIAL_VIEW")
        require(view!="direct" or allow_answer_text,"MATERIAL_DIRECT_AUTHORIZATION_REQUIRED")
        path=study_read_scope.authorize(snap,path,answer_scope if view=='direct' and allow_answer_text else None)
        local=snap.path(path)
        expected=snap._row(path)['sha256']
        require(file_hash(local)==expected,"MATERIAL_SOURCE_HASH_MISMATCH")
        with self.lock:
            if self._material_preprocessor is None:self._material_preprocessor=MaterialPreprocessor(self.base)
            processor=self._material_preprocessor
        output=output_for(subject,local)
        try:
            result=processor.prepare(local,subject=subject,pages=pages,output_dir=output,mode=mode,
                                     purpose=purpose,view=view,allow_answer_text=allow_answer_text,
                                     review=review,review_pages=review_pages)
        except Exception as exc:
            code=getattr(exc,'code','MATERIAL_PREPARATION_UNAVAILABLE')
            raise BridgeError(code if isinstance(code,str) else 'MATERIAL_PREPARATION_UNAVAILABLE') from None
        require(result.get('source_sha256')==expected,"MATERIAL_RESULT_SOURCE_MISMATCH")
        try:registration=register_output(output,subject)
        except Exception as exc:registration={'status':'pending','error_type':type(exc).__name__,'retry_registration_only':True}
        return {**result,**snap.identity(),"source_binding":{"path":path,"sha256":expected,"source_commit":snap.commit},
                "storage_registration":registration,"formal_write_count":0,"learner_evidence_write_count":0,
                "machine_extraction_not_verified":True,"original_image_observation_satisfied":False}

    def query(self, subject: str, domain: str, source_commit: str = "", ids: list[str] | None = None,
              contains: str = "", offset: int = 0, limit: int = 20, ids_only: bool = False,
              max_bytes: int | None = None) -> dict[str, Any]:
        if offset > 0:
            require(bool(source_commit), "SOURCE_COMMIT_REQUIRED_FOR_CONTINUATION")
        if domain == 'material_prepare':
            require(isinstance(ids,list) and len(ids)==1 and offset==0 and not ids_only
                    and isinstance(contains,str) and len(contains)<=3000,'INVALID_MATERIAL_QUERY_OPTIONS')
            try:options=json.loads(contains) if contains else {}
            except ValueError as exc:raise BridgeError('MATERIAL_OPTIONS_MUST_BE_JSON') from exc
            require(isinstance(options,dict) and set(options)<={'pages','mode','purpose','view','allow_answer_text','review','review_pages','answer_scope'},'INVALID_MATERIAL_QUERY_OPTIONS')
            budget=PAGE_BYTES if max_bytes is None else max_bytes
            require(type(budget) is int and 1024<=budget<=READ_LIMIT,'INVALID_MATERIAL_RESPONSE_BUDGET')
            result=self.prepare_material(subject,ids[0],source_commit=source_commit,**options)
            required=len(encoded(result))
            require(required<=budget,'MATERIAL_RESPONSE_BUDGET_TOO_SMALL',
                    {'required_min_bytes':required,'artifact_saved':True,'retry':'Use fewer pages or a larger max_bytes; successful extraction is cached.'})
            return result
        if domain == 'semantic_candidates':
            require(not ids and not ids_only and offset == 0 and isinstance(contains, str)
                    and len(contains) <= 3000, 'INVALID_SEMANTIC_QUERY_OPTIONS')
            try:
                options = json.loads(contains) if contains else {}
            except ValueError as exc:
                raise BridgeError('SEMANTIC_OPTIONS_MUST_BE_JSON') from exc
            require(isinstance(options, dict) and set(options) <= {'query', 'rerank', 'view'}
                    and isinstance(options.get('query'), str), 'INVALID_SEMANTIC_QUERY_OPTIONS')
            return self.semantic_candidates(subject, source_commit=source_commit,
                                            limit=min(limit, 12), **options)
        if domain == 'math_methods':
            require(not ids_only and isinstance(contains, str) and len(contains) <= 1000,
                    'INVALID_MATH_METHOD_OPTIONS')
            try:
                options = json.loads(contains) if contains else {}
            except ValueError as exc:
                raise BridgeError('MATH_METHOD_OPTIONS_MUST_BE_JSON') from exc
            require(isinstance(options, dict) and set(options) <= {'view', 'allow_historical'},
                    'INVALID_MATH_METHOD_OPTIONS')
            return self.math_methods(subject, ids, source_commit=source_commit,
                offset=offset, limit=limit, max_bytes=max_bytes, **options)
        if domain == 'personal_context':
            # Preserve the installed six-tool connector's exact input schema.
            # A tool-list refresh is optional, not required for this new domain.
            require(offset == 0 and not ids_only and len(contains) <= 10000, 'INVALID_PERSONAL_QUERY_OPTIONS')
            try:
                options = json.loads(contains) if contains else {}
            except ValueError as exc:
                raise BridgeError('PERSONAL_OPTIONS_MUST_BE_JSON') from exc
            require(isinstance(options, dict) and set(options) <= {'view', 'weak_knowledge_points', 'cursor', 'allow_historical',
                    'exam_question_id', 'current_question_id', 'current_question_part', 'formal_id', 'source_id',
                    'personal_scope', 'materials', 'known_version'},
                    'INVALID_PERSONAL_QUERY_OPTIONS')
            require(isinstance(options.get('cursor', ''), str) and isinstance(options.get('allow_historical', False), bool),
                    'INVALID_PERSONAL_QUERY_OPTIONS')
            return self.personal_context(subject, ids or [], source_commit=source_commit,
                max_bytes=max_bytes, **options)
        page_bytes = PAGE_BYTES if max_bytes is None else max_bytes
        if domain == 'english_learning_context':
            require(subject == 'english' and not ids and not ids_only and len(contains) <= 20000,
                    'INVALID_ENGLISH_CONTEXT_QUERY')
            try:
                request = json.loads(contains) if contains else {}
            except ValueError as exc:
                raise BridgeError('ENGLISH_CONTEXT_REQUEST_MUST_BE_JSON') from exc
            return self.english_context(request, source_commit=source_commit, offset=offset,
                                        limit=limit, max_bytes=page_bytes)
        snap = self.snapshot(subject, source_commit)
        if subject == "408" and domain == "question_refs":
            require(len(ids or []) <= 200 and 1 <= page_bytes <= READ_LIMIT, "QUERY_TOO_LARGE")
            # 2026-09-27: a web duplicate check sent contains=EXAM408-2010-Q35 and got the
            # misleading QUERY_TOO_LARGE. contains is one exact formal/source/canonical ID.
            needle = contains.strip()
            require(len(needle) <= 80 and (not needle or re.fullmatch(r"[A-Za-z0-9_.:-]+", needle)),
                    "QUESTION_REFS_CONTAINS_MUST_BE_ONE_EXACT_ID")
            rows = snap.question_refs()
            if ids:
                wanted = set(ids)
                rows = [row for row in rows if row["id"] in wanted]
                found = {row["id"] for row in rows}
                require(wanted <= found, "IDS_NOT_IN_SUBJECT_DOMAIN", sorted(wanted - found))
            if needle:
                # Exact match only; an empty page means no published formal node has this identity.
                rows = [row for row in rows if needle == row["id"]
                        or needle == row["record"].get("source_id")
                        or needle in (row["record"].get("canonical_ids") or [])]
            if ids_only:
                rows = [{key: row[key] for key in ("id", "source_path", "record_ordinal")}
                        for row in rows]
            return {**snap.identity(), "domain": domain,
                    "hash_semantics": {
                        "source_sha256": "sha256 of the verified pinned PUBLISHED_LIBRARY.json manifest",
                        "formal_state.formal_row_sha256": "sha256 of the canonical formal-node row",
                        "formal_state.safe_card_sha256": "sha256 of the published safe-card file"},
                    "server_evaluated_complete_domain": True,
                    "native_source_scan_performed": False,
                    **self.page(rows, offset, limit, page_bytes)}
        domains = snap.domains()
        require(domain in domains, "UNKNOWN_DOMAIN", list(domains))
        require(len(ids or []) <= 200 and len(contains) <= 1000 and 1 <= page_bytes <= READ_LIMIT, "QUERY_TOO_LARGE")
        rows = domains[domain]
        if subject == "english" and domain not in {"vocabulary", "sentence_patterns"}:
            rows = [row for row in rows if study_read_scope.listed(snap, row["source_path"])]
        if ids:
            wanted = set(ids)
            rows = [r for r in rows if r["id"] in wanted]
            require(wanted <= {r["id"] for r in rows}, "IDS_NOT_IN_SUBJECT_DOMAIN",
                    sorted(wanted - {r["id"] for r in rows}))
        if contains:
            needle = contains.casefold()
            rows = [r for r in rows if needle in encoded(r["record"]).decode().casefold()]
        if ids_only:
            rows = [{k: r[k] for k in ("id", "source_path", "record_ordinal")} for r in rows]
        return {**snap.identity(), "domain": domain, **self.page(rows, offset, limit, page_bytes)}

    def search(self, subject: str, query: str, source_commit: str = "", scope: str = "formal",
               prefix: str = "", offset: int = 0, limit: int = 30) -> dict[str, Any]:
        if offset > 0:
            require(bool(source_commit), "SOURCE_COMMIT_REQUIRED_FOR_CONTINUATION")
        snap = self.snapshot(subject, source_commit)
        require(0 < len(query) <= 500 and 0 <= offset and 1 <= limit <= 100, "INVALID_SEARCH")
        require(scope in {"formal", "paths", "content"}, "UNKNOWN_SEARCH_SCOPE")
        if scope == "formal":
            results = []
            for name, rows in snap.domains().items():
                for row in rows:
                    if (subject == "english" and name not in {"vocabulary", "sentence_patterns"}
                            and not study_read_scope.listed(snap, row["source_path"])):
                        continue
                    text = encoded(row["record"]).decode()
                    at = text.casefold().find(query.casefold())
                    if at >= 0:
                        results.append({"domain": name, "id": row["id"], "path": row["source_path"],
                                        "record_ordinal": row["record_ordinal"],
                                        "excerpt": text[max(0, at - 100):at + len(query) + 250]})
            return {**snap.identity(), "scope": scope, **self.page(results, offset, limit)}
        paths = sorted(p for p, row in snap.files.items() if p.startswith(prefix)
                       and study_read_scope.listed(snap, p, row))
        if scope == "paths":
            results = [{"path": p, "sha256": snap.files[p]["sha256"]} for p in paths if query.casefold() in p.casefold()]
            return {**snap.identity(), "scope": scope, **self.page(results, offset, limit)}
        # Content searches have an explicit continuation through every published
        # file. Large/binary files are named as excluded, never counted as read.
        results, skipped, scanned = [], [], []
        stop = min(len(paths), offset + limit)
        for path in paths[offset:stop]:
            if Path(path).suffix.lower() not in {".md", ".txt", ".json", ".jsonl", ".csv", ".py", ".js", ".ts", ".toml", ".yaml", ".yml"}:
                skipped.append({"path": path, "reason": "not_text_extension"})
                continue
            if snap.files[path].get("bytes", 0) > TEXT_LIMIT:
                skipped.append({"path": path, "reason": "large_source_use_fetch"})
                continue
            text = snap.text(path)
            matches = list(re.finditer(re.escape(query), text, re.IGNORECASE))
            scanned.append(path)
            if matches:
                results.append({"path": path, "sha256": snap.files[path]["sha256"], "match_count": len(matches),
                                "excerpts": [text[max(0, m.start() - 60):m.end() + 160] for m in matches[:3]],
                                "excerpts_are_complete_match_set": len(matches) <= 3})
        return {**snap.identity(), "scope": scope, "files_in_scope": len(paths), "scanned_files": scanned,
                "skipped_files": skipped, "results": results,
                "next_offset": stop if stop < len(paths) else None, "eof": stop == len(paths)}

    def image(self, subject: str, path: str, source_commit: str = "", page: int | None = None, *,
              answer_scope: dict[str, Any] | None = None) -> tuple[dict, bytes, str]:
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = 40_000_000
        snap = self.snapshot(subject, source_commit)
        path = study_read_scope.authorize(snap, path, answer_scope)
        local = snap.path(path)
        rendered = False
        if local.suffix.lower() == ".pdf":
            require(page is not None and 1 <= page <= 10000, "PDF_REQUIRES_ONE_BASED_PAGE")
            renderer = shutil.which("pdftoppm")
            require(renderer, "PDF_RENDERER_UNAVAILABLE")
            with tempfile.TemporaryDirectory(prefix="study-mcp-page-") as temporary:
                target = Path(temporary) / "page"
                process = subprocess.run([renderer, "-f", str(page), "-l", str(page), "-singlefile", "-scale-to", "2400",
                                          "-png", str(local), str(target)], capture_output=True, timeout=45)
                require(process.returncode == 0 and target.with_suffix(".png").is_file(), "PDF_PAGE_RENDER_FAILED")
                data = target.with_suffix(".png").read_bytes()
                rendered = True
        else:
            require(page is None, "PAGE_ONLY_APPLIES_TO_PDF")
            require(local.stat().st_size <= IMAGE_LIMIT, "IMAGE_TOO_LARGE")
            data = local.read_bytes()
        require(len(data) <= IMAGE_LIMIT, "IMAGE_TOO_LARGE")
        try:
            with Image.open(io.BytesIO(data)) as image:
                image.load()
                fmt, width, height = image.format, image.width, image.height
            mime = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp", "GIF": "image/gif"}.get(fmt)
            require(mime, "IMAGE_FORMAT_UNSUPPORTED")
        except (OSError, ValueError, Image.DecompressionBombError) as exc:
            raise BridgeError("IMAGE_DECODING_FAILED") from exc
        return ({**snap.identity(), "path": path, "source_sha256": snap._row(path)["sha256"],
                 "image_sha256": digest(data), "image_bytes": len(data), "width": width, "height": height,
                 "pdf_page": page, "rendered_from_pdf": rendered, "image_content_returned": True}, data, mime)

    def _workflow_disk_path(self, snap: Snapshot, project: str, day: str) -> Path | None:
        # The existing local handoff exporter and the long-running MCP process
        # use the same native query cache. These are derived runtime bytes,
        # never formal history, grades, or a replacement for binding checks.
        version = snap.library.get("native_math_workflow", {}).get("version")
        if project != "C" or not isinstance(version, str) or not re.fullmatch(r"[0-9a-f]{64}", version):
            return None
        key = digest(encoded(["native-workflow-result-v1", project, snap.commit, day, version]))
        return self.base / "runtime/study-mcp/workflow-cache" / (key + ".json")

    @staticmethod
    def _check_workflow_disk_value(snap: Snapshot, day: str, value: dict[str, Any]) -> None:
        require(isinstance(value, dict) and value.get("schema_version") in {"warmup-candidate-bundle-v2", "warmup-candidate-bundle-v3"}
                and value.get("date") == day
                and value.get("native_workflow_version") == snap.library["native_math_workflow"]["version"]
                and value.get("formal_write_count") == 0 and value.get("learner_evidence_write_count") == 0,
                "WORKFLOW_CACHE_BINDING_MISMATCH")
        if value["schema_version"] == "warmup-candidate-bundle-v3":
            require(value.get("requested_count") == 10
                    and all(re.fullmatch(r"[0-9a-f]{64}", str(value.get(k, ""))) for k in
                            ("formula_registry_version", "exam_source_version")),
                    "WORKFLOW_CACHE_MORNING_MODE_MISMATCH")
        plan_path = "数学一回滚复习系统/公式记忆配置.json"
        if plan_path in getattr(snap, "files", {}):
            plan = json.loads(snap.text(plan_path))
            expected_schema = "warmup-candidate-bundle-v3" if plan.get("enabled") is True else "warmup-candidate-bundle-v2"
            require(value["schema_version"] == expected_schema, "WORKFLOW_CACHE_MORNING_MODE_MISMATCH")
        native = {k: v for k, v in value.items() if k not in C_WORKFLOW_ENVELOPE_FIELDS}
        expected = native.get("candidate_bundle_sha256")
        body = {k: v for k, v in native.items() if k != "candidate_bundle_sha256"}
        actual = digest(json.dumps(body, ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":")).encode("utf-8"))
        require(actual == expected, "WORKFLOW_CACHE_SELF_HASH_MISMATCH")

    def _load_workflow_disk(self, snap: Snapshot, project: str, day: str) -> dict[str, Any] | None:
        path = self._workflow_disk_path(snap, project, day)
        if path is None or not path.exists():
            return None
        require(not path.is_symlink() and path.is_file() and path.stat().st_size <= 64 * 1024 * 1024,
                "WORKFLOW_CACHE_FILE_UNSAFE")
        with path.open("rb") as source:
            raw = source.read(64 * 1024 * 1024 + 1)
        require(len(raw) <= 64 * 1024 * 1024, "WORKFLOW_CACHE_TOO_LARGE")
        try:
            record = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise BridgeError("WORKFLOW_CACHE_INVALID_JSON") from exc
        require(isinstance(record, dict) and record.get("source_commit") == snap.commit and record.get("project") == project
                and record.get("target_date") == day, "WORKFLOW_CACHE_BINDING_MISMATCH")
        value = record.get("result")
        require(isinstance(value, dict) and digest(encoded(value)) == record.get("result_sha256"),
                "WORKFLOW_CACHE_CONTENT_HASH_MISMATCH")
        self._check_workflow_disk_value(snap, day, value)
        return value

    def _save_workflow_disk(self, snap: Snapshot, project: str, day: str, value: dict[str, Any]) -> None:
        path = self._workflow_disk_path(snap, project, day)
        if path is None or value.get("schema_version") not in {"warmup-candidate-bundle-v2", "warmup-candidate-bundle-v3"}:
            return
        self._check_workflow_disk_value(snap, day, value)
        record = {"schema": "native-workflow-result-cache-v1", "source_commit": snap.commit,
                  "project": project, "target_date": day, "result_sha256": digest(encoded(value)), "result": value}
        raw = encoded(record)
        require(len(raw) <= 64 * 1024 * 1024, "WORKFLOW_CACHE_TOO_LARGE")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        require(not path.is_symlink() and not path.parent.is_symlink(), "WORKFLOW_CACHE_FILE_UNSAFE")
        fd, temporary = tempfile.mkstemp(prefix=".workflow-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _workflow_result(self, project: str, subject: str, target_date: str,
                         source_commit: str = "") -> tuple[Snapshot, str, dict[str, Any]]:
        snap = self.snapshot(subject, source_commit)
        day = checked_day(target_date)
        require((project, subject) in {("C", "math"), ("D", "408")}, "ONLY_NATIVE_C_D_READ_CALCULATIONS")
        key = (project, snap.commit, day)
        with self.lock:
            # Weak locks stay alive while requests use them, without growing
            # permanently for every historical date ever requested.
            if not hasattr(self, "_workflow_locks"):
                self._workflow_locks = weakref.WeakValueDictionary()
            work_lock = self._workflow_locks.get(key)
            if work_lock is None:
                work_lock = threading.RLock()
                self._workflow_locks[key] = work_lock
        with work_lock:
            # Recheck installed sources even on a cache hit. A slow native
            # calculation only serializes requests for this exact result.
            if project == "C":
                self._math_bindings(snap)
            with self.lock:
                result = self.workflow_cache.pop(key, None)
            if result is None:
                result = self._load_workflow_disk(snap, project, day)
            if result is None:
                result = self._calculate(snap, project, day)
                self._save_workflow_disk(snap, project, day, result)
            with self.lock:
                self.workflow_cache[key] = result
                while len(self.workflow_cache) > WORKFLOW_CACHE_LIMIT:
                    self.workflow_cache.pop(next(iter(self.workflow_cache)))
        return snap, day, result

    @staticmethod
    def _workflow_summary(snap: Snapshot, project: str, day: str, result: dict[str, Any]) -> dict[str, Any]:
        summary = {k: v for k, v in result.items() if not isinstance(v, list)}
        summary["collections"] = {k: len(v) for k, v in result.items() if isinstance(v, list)}
        return {**snap.identity(), "project": project, "target_date": day, "result": summary,
                "generated_questions": False, "queue_imported": False}

    def workflow(self, project: str, subject: str, target_date: str, source_commit: str = "",
                 collection: str = "", offset: int = 0, limit: int = 20) -> dict[str, Any]:
        if offset > 0:
            require(bool(source_commit), "SOURCE_COMMIT_REQUIRED_FOR_CONTINUATION")
        snap, day, result = self._workflow_result(project, subject, target_date, source_commit)
        if project == "D":
            result = self._d_workflow_view(result)
        if collection == "bundle_identity":
            require(project == "C", "BUNDLE_IDENTITY_ONLY_APPLIES_TO_MATH_C")
            points = result.get("concept_evidence_contract", {})
            return {**snap.identity(), "project": project, "target_date": day,
                    "schema_version": result.get("schema_version"),
                    "candidate_bundle_sha256": result.get("candidate_bundle_sha256"),
                    **{k: result[k] for k in ("requested_count", "formula_registry_version", "exam_source_version",
                                              "formal_lane_basis", "mixed_stats") if k in result},
                    "collections": {k: len(v) for k, v in result.items() if isinstance(v, list)},
                    "stats": result.get("stats", {}), "delivery_gate": result.get("delivery_gate", {}),
                    "concept_evidence_scope": {k: points.get(k) for k in
                        ("included_point_count", "total_point_count", "source_version",
                         "complete_events_for_included_points")},
                    **{k: result[k] for k in C_WORKFLOW_ENVELOPE_FIELDS if k in result},
                    "uploaded_file_verified": False, "model_semantic_scan_complete": False,
                    "generated_questions": False, "queue_imported": False,
                    "use": "Parse the uploaded native candidate JSON to EOF and recompute its self-hash before comparing this identity. Reuse matching bytes; retain complete evidence for relevant points without printing the full JSON into model context."}
        if not collection:
            return self._workflow_summary(snap, project, day, result)
        require(isinstance(result.get(collection), list), "UNKNOWN_WORKFLOW_COLLECTION")
        # Complete C concept events can exceed the general 64 KiB page even
        # when a single record is requested. Keep each record whole, using the
        # existing large-record transport bound rather than an unusable fetch
        # hint with source_path=None for a computed workflow collection.
        budget = 512 * 1024 if project == "C" else PAGE_BYTES
        return {**snap.identity(), "project": project, "target_date": day, "collection": collection,
                **self.page(result[collection], offset, limit, max_bytes=budget)}

    @staticmethod
    def _d_workflow_view(result: dict[str, Any]) -> dict[str, Any]:
        """Expose native arrays as paginated collections without changing evidence.

        The cached canonical context stays intact. Nested metadata keeps explicit
        collection/count references instead of embedding every row in a summary.
        """
        collections = {key: value for key, value in result.items() if isinstance(value, list)}
        # Preserve legacy names such as due_items/blocked/included even when the
        # complete context wraps them under native_due_run.
        native = result.get("native_due_run", {})
        for key, value in native.items() if isinstance(native, dict) else ():
            if isinstance(value, list) and key not in collections:
                collections[key] = value

        def compact(value: Any, path: tuple[str, ...]) -> Any:
            if isinstance(value, list):
                name = path[-1]
                if name in collections and collections[name] != value:
                    name = ".".join(path)
                require(name not in result or isinstance(result[name], list) or name != path[-1],
                        "NATIVE_DUE_COLLECTION_NAME_CONFLICT", name)
                if name in collections and collections[name] != value:
                    raise BridgeError("NATIVE_DUE_COLLECTION_NAME_CONFLICT", name)
                collections[name] = value
                return {"collection": name, "count": len(value)}
            if isinstance(value, dict):
                return {key: compact(child, path + (key,)) for key, child in value.items()}
            return value

        metadata = {key: compact(value, (key,)) for key, value in result.items() if not isinstance(value, list)}
        return {**metadata, **collections}

    def _calculate(self, snap: Snapshot, project: str, day: str) -> dict[str, Any]:
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONDONTWRITEBYTECODE": "1", "LANG": "en_US.UTF-8"}
        if project == "D":
            support_path = "support/d408/manifest.json"
            support = json.loads(snap.text(support_path))
            with tempfile.TemporaryDirectory(prefix="study-mcp-due-") as temporary:
                root, output = Path(temporary) / "source", Path(temporary) / "result"
                needed = {"PUBLISHED_LIBRARY.json", support_path}
                needed.update(r["path"] for r in snap.library["file_inventory"])
                needed.update(r["published_path"] for r in support["components"] + support["inputs"])
                for path in needed:
                    target = root / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(snap.path(path), target)
                process = subprocess.run([NATIVE_PYTHON, str(root / "support/d408/read_only_due.py"),
                    "--repo", str(root), "--target-date", day, "--output-dir", str(output)],
                    capture_output=True, text=True, timeout=60, env=env)
                require(process.returncode == 0, "NATIVE_DUE_FAILED", process.stderr[-1500:])
                context = output / "due-context.json"
                result = json.loads((context if context.is_file() else output / "native-due-run.json").read_text())
                require(result.get("formal_write_count") == 0 and result.get("learner_evidence_write_count") == 0,
                        "NATIVE_READ_ONLY_CONTRACT_FAILED")
                return result
        # C reuses the installed native read-only selector. Require its complete
        # data inputs to still equal the requested publication; never mix versions.
        native_root = study_bridge.REPOS["math"]
        before = self._math_bindings(snap)
        script = native_root / "数学一回滚复习系统/scripts/scheduler.py"
        plan_path = "数学一回滚复习系统/公式记忆配置.json"
        plan = json.loads(snap.text(plan_path)) if plan_path in snap.files else {}
        count = 10 if plan.get("enabled") is True and plan.get("requested_count") == 10 else 5
        process = subprocess.run([NATIVE_PYTHON, str(script), "warmup-candidates", "--date", day, "--count", str(count)],
                                 capture_output=True, text=True, timeout=90, env=env, cwd=native_root)
        require(self._math_bindings(snap) == before, "NATIVE_MATH_CHANGED_DURING_READ")
        require(process.returncode == 0, "NATIVE_MATH_CANDIDATES_FAILED", process.stderr[-1500:])
        result = json.loads(process.stdout)
        require(result.get("schema_version") == ("warmup-candidate-bundle-v3" if count == 10 else "warmup-candidate-bundle-v2")
                and result.get("requested_count") == count, "NATIVE_MATH_MORNING_MODE_MISMATCH")
        contract = self._math_contract(snap)
        declared = {row["path"]: row["sha256"] for row in contract["inputs"]}
        for point in result.get("concept_evidence", []):
            for event in point.get("events", []):
                for reference in event.get("source_refs", []):
                    require(reference.get("path") in declared and declared[reference["path"]] == reference.get("sha256"),
                            "NATIVE_MATH_CONCEPT_SOURCE_NOT_BOUND", reference.get("path"))
        result["native_algorithm_source"] = "installed_read_only_warmup_candidates"
        result["native_algorithm_sha256"] = before[script]
        result["native_workflow_version"] = snap.library["native_math_workflow"]["version"]
        result["published_personalization"] = contract["personalization"]
        result["formal_write_count"] = result["learner_evidence_write_count"] = 0
        result["published_inputs_verified"] = len(before)
        return result

    @staticmethod
    def _math_contract(snap: Snapshot) -> dict[str, Any]:
        contract = snap.library.get("native_math_workflow", {})
        if contract.get("path"):
            entry = contract
            require(entry["path"] == "support/cmath/manifest.json"
                    and snap.files.get(entry["path"], {}).get("sha256") == entry.get("sha256"),
                    "NATIVE_MATH_WORKFLOW_MANIFEST_BINDING_MISMATCH")
            contract = json.loads(snap.text(entry["path"]))
            require(contract.get("version") == entry.get("version"), "NATIVE_MATH_WORKFLOW_MANIFEST_VERSION_MISMATCH")
        require(contract.get("schema") == "native-math-workflow-bindings-v1",
                "NATIVE_MATH_WORKFLOW_BINDINGS_NOT_PUBLISHED", "Publish the complete C inputs and algorithm before selecting this snapshot")
        body = {k: v for k, v in contract.items() if k != "version"}
        require(digest(json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()) == contract.get("version"),
                "NATIVE_MATH_WORKFLOW_BINDINGS_VERSION_MISMATCH")
        return contract

    def _verified_native_hash(self, path: Path) -> str:
        """Reuse a verified hash only while the complete file identity is stable."""
        def stamp():
            value = path.stat()
            return (value.st_dev, value.st_ino, value.st_size,
                    value.st_mtime_ns, value.st_ctime_ns)
        before = stamp()
        # Some isolated fixture stores bypass __init__; use the same safe empty
        # cache there. Every request still checks membership, path and stat.
        cache = getattr(self, "_native_hash_cache", None)
        if cache is None:
            self._native_hash_cache = cache = {}
        cached = cache.get(path)
        if cached is not None and cached[0] == before:
            return cached[1]
        value = file_hash(path)
        require(stamp() == before, "NATIVE_MATH_SOURCE_CHANGED_DURING_HASH", str(path))
        cache[path] = (before, value)
        return value

    def _math_bindings(self, snap: Snapshot) -> dict[Path, str]:
        native_root = study_bridge.REPOS["math"]
        contract = self._math_contract(snap)
        fixed = {"数学一回滚复习系统/复习单元.json", "数学一回滚复习系统/复习记录.jsonl",
                 "数学一回滚复习系统/学习进度.json", "数学一回滚复习系统/学习前5题记录.jsonl",
                 "数学一回滚复习系统/快速入库事件.jsonl"}
        fixed.update(p for p in snap.files if p.startswith("错题知识网络/错题卡/") and p.endswith(".md"))
        inputs, components = contract.get("inputs", []), contract.get("components", [])
        declared = {row["path"]: row["sha256"] for row in inputs + components}
        require(len(declared) == len(inputs) + len(components) and fixed <= {r["path"] for r in inputs},
                "NATIVE_MATH_WORKFLOW_INPUTS_INCOMPLETE")
        directories = ["数学一回滚复习系统/scripts", "错题知识网络/scripts"]
        require(contract.get("component_directories") == directories, "NATIVE_MATH_ALGORITHM_SCOPE_MISMATCH")
        actual_components = {path.relative_to(native_root).as_posix()
                             for directory in directories for path in (native_root / directory).glob("*.py")}
        require(actual_components == {row["path"] for row in components}, "NATIVE_MATH_ALGORITHM_SET_CHANGED")
        fixed = set(declared)
        require(fixed <= snap.files.keys(), "NATIVE_MATH_INPUT_NOT_PUBLISHED", sorted(fixed - snap.files.keys()))
        bindings = self.published.current_sources("math", snap.files, sorted(fixed))
        before = {}
        for path, binding in bindings.items():
            source = Path(binding["source_path"])
            source_file(source, native_root)
            require(declared[path] == snap.files[path]["sha256"], "NATIVE_MATH_BINDING_HASH_MISMATCH", path)
            require(self._verified_native_hash(source) == snap.files[path]["sha256"], "NATIVE_MATH_DIFFERS_FROM_PUBLICATION", path)
            before[source] = snap.files[path]["sha256"]
        index = contract.get("installed_index", {})
        require(index.get("path") == "错题知识网络/个人知识点索引/index.sqlite3"
                and index.get("published") is False and re.fullmatch(r"[0-9a-f]{64}", str(index.get("sha256", ""))),
                "NATIVE_MATH_INDEX_BINDING_REQUIRED")
        index_path = source_file(native_root / index["path"], native_root)
        require(self._verified_native_hash(index_path) == index["sha256"], "NATIVE_MATH_INDEX_DIFFERS_FROM_PUBLICATION")
        before[index_path] = index["sha256"]
        exam_index = contract.get("installed_exam_index")
        if exam_index is not None:
            require(isinstance(exam_index, dict) and exam_index.get("path") == "真题数据库/papers.sqlite3"
                    and exam_index.get("published") is False
                    and re.fullmatch(r"[0-9a-f]{64}", str(exam_index.get("sha256", ""))),
                    "NATIVE_MATH_EXAM_INDEX_BINDING_INVALID")
            exam_path = source_file(native_root / exam_index["path"], native_root)
            require(self._verified_native_hash(exam_path) == exam_index["sha256"],
                    "NATIVE_MATH_EXAM_INDEX_DIFFERS_FROM_PUBLICATION")
            before[exam_path] = exam_index["sha256"]
            # Catch added shards/activity files, not just changed existing bytes.
            script_root = str(native_root / "数学一回滚复习系统/scripts")
            if script_root not in sys.path:
                sys.path.insert(0, script_root)
            import math_morning_ten
            required_formula = {str(p) for p in math_morning_ten.extra_input_paths(native_root)}
            require(required_formula == set(contract.get("morning_formula_inputs", []))
                    and required_formula <= {r["path"] for r in inputs},
                    "NATIVE_MATH_FORMULA_INPUT_SET_CHANGED")
        current = study_personalization.native_call("math", "identity")
        expected = contract.get("personalization", {})
        require(current.get("status") == "ready" and expected.get("version") and expected.get("formal_version")
                and all(current.get(k) == expected[k] == snap.library.get("personalization", {}).get(k)
                        for k in ("version", "formal_version")),
                "NATIVE_MATH_PERSONALIZATION_DIFFERS_FROM_PUBLICATION", current)
        return before


def _threaded_tool(fn):
    """Keep blocking Git/native/API work off the MCP transport event loop."""
    @wraps(fn)
    async def invoke(*args, **kwargs):
        from anyio import to_thread
        return await to_thread.run_sync(partial(fn, *args, **kwargs))
    return invoke


def build_server(base: Path = BASE):
    from mcp.server.fastmcp import FastMCP
    from mcp.types import CallToolResult, ImageContent, TextContent, ToolAnnotations, EmbeddedResource, BlobResourceContents
    store = LibraryStore(base)
    server = FastMCP("Study Library", instructions=(
        "Read-only math/english/408 formal libraries for A/B/C/D. Call library to pin source_commit/snapshot_id; reuse it for every call. "
        "publication_target=local means a consistent local Git snapshot, not a GitHub upload. GitHub backup is not required. "
        "query searches/parses whole native domains on the server and returns complete paginated records. query domain=source_files exports an exact ZIP file resource for ids containing published paths. workflow collection=bundle_file exports the complete native result, compressing large JSON into a ZIP; verify content_sha256 after extraction. On file failure page named collections to eof. fetch returns binary originals as embedded file resources and original UTF-8 "
        "bytes with exact continuation and a copy-ready source_ref pinned to the complete file. Use source_ref unchanged for citations; "
        "returned_sha256 hashes only this returned byte range. view_image returns actual image content or one PDF page, never manually reconstruct Base64. "
        "workflow runs only existing read-only C/math and D/408 calculations. Server byte coverage is not model semantic reading. "
        "personal_context reads published knowledge-key personal records for math/408; check freshness before claiming current data. "
        "query domain math_methods reads exact published method/step/conclusion IDs with protected default; after_attempt/direct returns whole objects and prerequisite closure from the same snapshot, never personal mastery. "
        "query domain question_refs reads 408 question/source/asset bindings and formal-state hashes directly from the verified pinned "
        "manifest without scanning the library; use exact IDs and paginate with the same source_commit. "
        "query domain english_learning_context reads bounded English formal context from the same pinned snapshot; before_attempt filters "
        "After explicit authorization, English question_review requires question_id and an article-qualified unlock in unlocked_question_ids (source_id:question_id). An attempt alone does not grant access. Paginate its named collections. "
        "For fuzzy lookup after exact/literal lookup is insufficient, use semantic_search or existing query domain=semantic_candidates "
        "with contains JSON {query,rerank:auto/off/on,view:protected}, limit<=12. This explicit retrieval path uses OpenAI Embeddings "
        "and optional Decisions for safe candidate metadata, maintains derived caches, and never reads answers for ranking. "
        "Check degraded/coverage/freshness status and then read selected exact IDs through the original protected readers. "
        "For explicitly requested PDF/image/HTML/text material preparation, use query domain=material_prepare with one exact published path in ids "
        "and contains JSON pages/mode/purpose/view/allow_answer_text/review/review_pages. review=auto uses Decisions only on suspected pages; review_pages explicitly marks selected uncertain pages; off disables review. Advisory results never prove correctness. Default protected and after_attempt return metadata only; direct requires explicit "
        "content/answer authorization and allow_answer_text=true. This API-assisted derived artifact never substitutes for original-image observation, "
        "formal intake, scoring or mastery evidence. Do not invoke it during ordinary fetch or quick Capture. "
        "Keep pending/retired history and answer protection unchanged. No shell, formal writer, mastery update or queue import is exposed. "
        "Politics uses the named politics/politics_view_image tools for live local sources, state, genuine learning history, and read-only Xiao1000 planning. "
        "It is not part of the three-subject Git snapshot enum. Call politics(action=library) to get politics_version; pin version for related reads. "
        "politics_record and politics_practice delegate to the same scoped political append-only writer for genuine evidence and political summaries, "
        "with expected_version and idempotent event_id. It never changes math/English/408 data or global mastery. "
        "All political interpretations come from user-supplied corpus; question bodies must be verified exact Xiao1000 originals. Missing originals are explicit, never invented."),
        log_level="WARNING")
    readonly = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
    api_readonly = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=False, openWorldHint=True)
    politics_append = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)
    _politics_store = None

    def political_store():
        nonlocal _politics_store
        if _politics_store is None:
            from study_politics import PoliticsStore
            _politics_store = PoliticsStore()
        return _politics_store

    def guarded(fn, **kwargs):
        try:
            return fn(**kwargs)
        except BridgeError as exc:
            return {"ok": False, "error": exc.code, "detail": exc.detail, "formal_write_count": 0}

    def file_result(value):
        meta, raw, mime, uri = value
        return CallToolResult(content=[TextContent(type="text", text=encoded(meta).decode()),
            EmbeddedResource(type="resource", resource=BlobResourceContents(
                uri=uri, mimeType=mime, blob=base64.b64encode(raw).decode()))], structuredContent=meta)

    @server.tool(annotations=readonly)
    @_threaded_tool
    def library(subject: Literal["math", "english", "408"], source_commit: str = "", prefix: str = "",
                offset: int = 0, limit: int = 100) -> dict[str, Any]:
        """Open a pinned library and inspect native-domain counts before list visibility filtering. A visible list (including ids_only) may be empty for a nonempty domain. Domains are parsed server-side to EOF."""
        return guarded(store.library, subject=subject, source_commit=source_commit, prefix=prefix, offset=offset, limit=limit)

    @server.tool(annotations=readonly, structured_output=False)
    @_threaded_tool
    def fetch(subject: Literal["math", "english", "408"], path: str, source_commit: str = "",
              start_byte: int = 0, max_bytes: int = PAGE_BYTES, answer_scope: dict[str, Any] | None = None):
        """Read allowed exact original text or an embedded binary at start_byte=0 (maximum 8 MiB). Practice is the default. Only after explicit user release, set answer_scope with view=direct, purpose=question_review or formal_review, allow_answer_text=true, source_id and item_id for the exact single published item. Continue at next_start_byte with the same commit and scope. Copy source_ref unchanged for its complete-file hash and provenance; returned_sha256 hashes this range. Save an embedded resource through the host file API. No search index is required."""
        try:
            value = store.fetch(subject, path, source_commit, start_byte, max_bytes, answer_scope=answer_scope)
            return CallToolResult(content=[TextContent(type="text", text=encoded(value).decode())], structuredContent=value)
        except BridgeError as exc:
            if exc.code in {"BINARY_SOURCE_USE_VIEW_IMAGE", "NOT_UTF8_OR_OFFSET_INSIDE_CHARACTER"} and start_byte == 0:
                try:
                    return file_result(store.original_file(subject, path, source_commit, answer_scope=answer_scope))
                except BridgeError as file_error:
                    exc = file_error
            value = {"ok": False, "error": exc.code, "detail": exc.detail, "formal_write_count": 0}
            return CallToolResult(content=[TextContent(type="text", text=encoded(value).decode())], structuredContent=value)

    @server.tool(annotations=api_readonly)
    @_threaded_tool
    def query(subject: Literal["math", "english", "408"], domain: str, source_commit: str = "", ids: list[str] | None = None,
              contains: str = "", offset: int = 0, limit: int = 20, ids_only: bool = False,
              max_bytes: int | None = None) -> dict[str, Any]:
        """Query complete native formal/history domains by exact IDs or text. For fuzzy discovery use domain=semantic_candidates, contains JSON {query,rerank:auto/off/on,view:protected}, limit<=12; this explicit domain can call OpenAI Embeddings/Decisions and writes only derived retrieval caches. Check its coverage/degraded status; candidates are not answers or identity/mastery judgments. For explicit material preparation use domain=material_prepare, ids=[one exact published path], contains JSON pages/mode/purpose/view/allow_answer_text/review/review_pages. review=auto uses Decisions only on suspected pages; review_pages explicitly marks selected uncertain pages; off disables review. Advisory results never prove correctness. protected/after_attempt return metadata only; direct requires explicit content/answer authorization and allow_answer_text=true. Material candidates never satisfy original-image observation or formal intake. Other domains do not call models. For 408 bindings use domain=question_refs with exact formal IDs in ids, or one exact formal/source/canonical ID such as EXAM408-2010-Q35 in contains (empty result = no published node); it reads the verified root manifest without a full-library scan. For English context use domain=english_learning_context and put the advertised request JSON in contains. Records are whole; paginate to eof. For exact files use domain=source_files, ids=published paths (max 30 files, 8 MiB), to return an embedded ZIP resource. For math_methods use exact method_id/step_id/conclusion_id in ids and contains JSON with view protected (default)/after_attempt/direct and allow_historical; paginate whole objects with next_offset. For personal_context put view protected/after_attempt/direct and cursor in contains JSON. For 408 also pass verified current_question_id (or exam_question_id), optional current_question_part P01, formal_id/source_id; ids may be empty when an exact question is supplied. Explicit current_question_id:null prevents prototype fallback."""
        if domain == "source_files":
            try:
                require(not ids_only and offset == 0 and len(contains) <= 3000, "INVALID_SOURCE_FILES_OPTIONS")
                try:
                    options = json.loads(contains) if contains else {}
                except ValueError as exc:
                    raise BridgeError("INVALID_SOURCE_FILES_OPTIONS") from exc
                require(isinstance(options, dict) and set(options) <= {"answer_scope"}, "INVALID_SOURCE_FILES_OPTIONS")
                return file_result(store.source_files(subject, ids or [], source_commit, **options))
            except BridgeError as exc:
                return {"ok": False, "error": exc.code, "detail": exc.detail, "formal_write_count": 0}
        return guarded(store.query, subject=subject, domain=domain, source_commit=source_commit, ids=ids,
                       contains=contains, offset=offset, limit=limit, ids_only=ids_only, max_bytes=max_bytes)


    @server.tool(annotations=readonly)
    @_threaded_tool
    def search(subject: Literal["math", "english", "408"], query: str, source_commit: str = "",
               scope: Literal["formal", "paths", "content"] = "formal", prefix: str = "", offset: int = 0, limit: int = 30) -> dict[str, Any]:
        """Search the full formal/history domains, filenames, or published text. Content mode continues by file offset and reports skipped binaries explicitly."""
        return guarded(store.search, subject=subject, query=query, source_commit=source_commit,
                       scope=scope, prefix=prefix, offset=offset, limit=limit)

    @server.tool(annotations=readonly, structured_output=False)
    @_threaded_tool
    def view_image(subject: Literal["math", "english", "408"], path: str, source_commit: str = "", page: int | None = None,
                   answer_scope: dict[str, Any] | None = None):
        """View an allowed published image or one-based PDF page. Protected originals require the same single-item answer_scope as fetch, with explicit user release."""
        try:
            meta, data, mime = store.image(subject, path, source_commit, page, answer_scope=answer_scope)
            return CallToolResult(content=[TextContent(type="text", text=encoded(meta).decode()),
                ImageContent(type="image", data=base64.b64encode(data).decode(), mimeType=mime)], structuredContent=meta)
        except BridgeError as exc:
            return CallToolResult(isError=True, content=[TextContent(type="text", text=encoded({"error": exc.code, "detail": exc.detail}).decode())])

    @server.tool(annotations=readonly)
    @_threaded_tool
    def personal_context(subject: Literal['math', '408'], knowledge_points: list[str],
                         weak_knowledge_points: list[str] | None = None, source_commit: str = '',
                         view: Literal['protected', 'after_attempt', 'direct'] = 'protected',
                         max_bytes: int | None = None, cursor: str = '', allow_historical: bool = False) -> dict[str, Any]:
        """Read bounded personal history by actual knowledge points from a fixed snapshot. Default protected view; after_attempt for supplied work, direct only with answer authorization. Check freshness; historical reads require explicit opt-in. Never builds, writes or calls a model."""
        return guarded(store.personal_context, subject=subject, knowledge_points=knowledge_points,
                       weak_knowledge_points=weak_knowledge_points, source_commit=source_commit,
                       view=view, max_bytes=max_bytes, cursor=cursor, allow_historical=allow_historical)

    @server.tool(annotations=readonly)
    @_threaded_tool
    def workflow(project: Literal["C", "D"], subject: Literal["math", "408"], target_date: str,
                 source_commit: str = "", collection: str = "", offset: int = 0, limit: int = 20) -> dict[str, Any]:
        """Run native read-only math eligibility or 408 due calculations over full inputs. Page named collections to EOF using next_offset. collection=bundle_file returns the complete native JSON; large results are ZIP resources containing candidate-bundle.json, with both resource and content SHA-256. If file delivery fails, continue via pagination instead of repeatedly requesting the full file. File export is not semantic reading. Never imports or grades."""
        if collection == "bundle_file":
            try:
                require(offset == 0, "WORKFLOW_FILE_OFFSET_MUST_BE_ZERO")
                return file_result(store.workflow_file(project, subject, target_date, source_commit))
            except BridgeError as exc:
                return {"ok": False, "error": exc.code, "detail": exc.detail, "formal_write_count": 0}
        return guarded(store.workflow, project=project, subject=subject, target_date=target_date,
                       source_commit=source_commit, collection=collection, offset=offset, limit=limit)






    return server, store


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, default=BASE)
    args = parser.parse_args()
    server, store = build_server(args.base)
    try:
        server.run(transport="stdio")
    finally:
        store.close()


if __name__ == "__main__":
    main()
