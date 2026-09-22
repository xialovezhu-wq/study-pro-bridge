"""Invoke the subject-owned English context reader against a staged snapshot.

The caller supplies a repository view containing only hash-verified publication
bytes.  The installed English package supplies the reader implementation, but
it never receives the live English repository as its data root.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
from typing import Any


NATIVE_ROOT = Path("/Users/YOUR_USER/Documents/kaoyan-english")
NATIVE_PYTHON = "/usr/local/bin/python3"
RUNNER = r'''
import json,sys
from pathlib import Path

request=json.load(sys.stdin)
sys.path.insert(0,sys.argv[1])
from english_pipeline.learning_state import query_learning_context

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
    allow_protected=request["view"] == "question_review",
)
json.dump(result,sys.stdout,ensure_ascii=False,separators=(",",":"))
'''


def query_snapshot(repo_root: Path, request: dict[str, Any], *, timeout: int = 30) -> dict[str, Any]:
    """Run the native deterministic reader with an explicit snapshot data root."""
    completed = subprocess.run(
        [NATIVE_PYTHON, "-c", RUNNER, str(NATIVE_ROOT), str(repo_root.resolve(strict=True))],
        input=json.dumps(request, ensure_ascii=False),
        text=True,
        capture_output=True,
        timeout=timeout,
        env={"PATH": "/usr/local/bin:/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1", "LANG": "en_US.UTF-8"},
    )
    if completed.returncode != 0:
        raise ValueError("native English context reader unavailable: " + completed.stderr[-1200:])
    value = json.loads(completed.stdout)
    if not isinstance(value, dict):
        raise ValueError("native English context reader did not return an object")
    return value
