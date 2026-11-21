"""Invoke the subject-owned English context reader against a staged snapshot.

The caller supplies a repository view containing only hash-verified publication
bytes.  The installed English package supplies the reader implementation, but
it never receives the live English repository as its data root.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any
from study_paths import subject_root


NATIVE_ROOT = subject_root("english")
NATIVE_PYTHON = sys.executable


def question_events(rows, source_id, question_id):
    """Match the ledger's exact source and unit, without expanding word or SP IDs."""
    if not source_id or not re.fullmatch(r"Q[0-9]{1,3}", question_id or ""):
        return []
    return [row for row in rows if row.get("visibility") == "protected"
            and row["event"].get("source_id") == source_id and row["event"].get("unit_id") == question_id]


RUNNER = r'''
import json,sys
from pathlib import Path

request=json.load(sys.stdin)
sys.path.insert(0,sys.argv[1])
from english_pipeline.learning_state import query_learning_context, load_learning_events, _verify_event_evidence
from english_pipeline.errors import ValidationError
from study_english_context import question_events

root=Path(sys.argv[2]).resolve(strict=True)
result=query_learning_context(
    root,
    root / "intake",
    source_id=request.get("source_id"),
    sentence_id=request.get("sentence_id"),
    source_article=request.get("source_article"),
    source_hash=request.get("source_hash"),
    sentence_sha256_value=request.get("sentence_sha256"),
    bank_ids=request.get("bank_ids"),
    sp_ids=request.get("sp_ids"),
    concept_ids=request.get("concept_ids"),
    aliases=request.get("aliases"),
    max_bytes=request["native_max_bytes"],
    allow_protected=False,
)
scope=request.get("question_review_scope")
if scope and result.get("status") == "ready":
    from english_pipeline.util import canonical_bytes

    # Keep the full snapshot ledger for chain/version verification. Only the
    # published typed rows in the unlocked question enter the answer view.
    scoped=question_events(load_learning_events(root / "bank/learning_events.jsonl"),
                           scope["source_id"], scope["question_id"])
    selected=scoped if len(scoped) <= 8 else [scoped[0], *scoped[-7:]]
    status="available" if selected else "no_record"
    try:
        _verify_event_evidence(root, root / "intake", selected)
    except (OSError, ValueError, ValidationError, KeyError, TypeError):
        status="source_unavailable"
        selected=[]
    projected=[{"event_id": r["event_id"], "source_record_sha256": r["record_sha256"],
                "visibility": "protected", "event": r["event"], "evidence_refs": []} for r in selected]
    review={"status": status, "source_id": scope["source_id"], "question_id": scope["question_id"],
            "matched_events": len(scoped), "returned_events": len(projected)}
    trial={**result, "question_review": review,
           "learning_history": result["learning_history"] + projected}
    if len(canonical_bytes(trial)) <= request["native_max_bytes"]:
        result=trial
    else:
        result["question_review"]={**review, "status": "omitted_for_budget", "returned_events": 0}
json.dump(result,sys.stdout,ensure_ascii=False,separators=(",",":"))
'''


def query_snapshot(repo_root: Path, request: dict[str, Any], *, timeout: int = 30) -> dict[str, Any]:
    """Run the native deterministic reader with an explicit snapshot data root."""
    env = {"PATH": os.environ.get("PATH", os.defpath), "HOME": str(Path.home()),
           "PYTHONDONTWRITEBYTECODE": "1", "LANG": "en_US.UTF-8"}
    for name in ("CODEX_HOME", "STUDY_STORAGE_MAP_ROOT", "STUDY_BRIDGE_ROOT", "XDG_DATA_HOME",
                 "ENGLISH_MEMORY_RUNTIME_ROOT", "KAOYAN_ENGLISH_PROJECT_ROOT", "KAOYAN_ENGLISH_ROOT"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    completed = subprocess.run(
        [NATIVE_PYTHON, "-c", RUNNER, str(NATIVE_ROOT), str(repo_root.resolve(strict=True))],
        cwd=Path(__file__).resolve().parent,
        input=json.dumps(request, ensure_ascii=False),
        text=True,
        capture_output=True,
        timeout=timeout,
        env=env,
    )
    if completed.returncode != 0:
        raise ValueError("native English context reader unavailable: " + completed.stderr[-1200:])
    value = json.loads(completed.stdout)
    if not isinstance(value, dict):
        raise ValueError("native English context reader did not return an object")
    return value
