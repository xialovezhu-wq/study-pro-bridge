"""Delivery policy for published originals exposed by the read-only bridge."""
from __future__ import annotations

from bridge_archives import require, safe_path

ANSWER_ROLES = frozenset({"answer", "answer_and_analysis", "analysis", "explanation",
    "explanation_image", "solution", "solution_image", "solution_text", "grading_capsule"})
ENGLISH_LEGACY_PRIVATE = ("intake/personal-summary/", "wiki/memory/", "raw/protected/",
                          "intake/packages/", "intake/events/", "articles/")


def protected(subject, path, row):
    return (row.get("visibility") == "protected" or row.get("role") in ANSWER_ROLES
            or (subject == "english" and path.startswith(ENGLISH_LEGACY_PRIVATE)))


def inventory_container(snapshot, path):
    # Old English inventory bytes carry private article and archive locators.
    # The library tool can enumerate the safe members without delivering them.
    return snapshot.subject == "english" and (
        path == "PUBLISHED_LIBRARY.json" or path in snapshot.metadata)


def listed(snapshot, path, row=None):
    row = snapshot._row(path) if row is None else row
    return row is not None and not inventory_container(snapshot, path) and not protected(snapshot.subject, path, row)


def authorize(snapshot, path, answer_scope):
    path = safe_path(path)
    row = snapshot._row(path)
    require(row is not None, "PATH_NOT_IN_PUBLISHED_LIBRARY", path)
    require(not inventory_container(snapshot, path), "PROTECTED_PUBLISHED_SOURCE")
    if not protected(snapshot.subject, path, row):
        return path
    require(isinstance(answer_scope, dict) and set(answer_scope) == {
        "view", "purpose", "allow_answer_text", "source_id", "item_id"}
        and answer_scope["view"] == "direct" and answer_scope["allow_answer_text"] is True
        and answer_scope["purpose"] in ("question_review", "formal_review")
        and all(isinstance(answer_scope[key], str) and 0 < len(answer_scope[key]) <= 256
                for key in ("source_id", "item_id")), "PROTECTED_PUBLISHED_SOURCE")
    # A whole mixed document cannot be released through one member's grant.
    # Use only the publisher's typed identity, not path shape or a caller SHA.
    identity = answer_scope["item_id"]
    item_ids = row.get("formal_ids") or []
    bound = row.get("question_id") == identity and (not item_ids or item_ids == [identity])
    bound = bound or (item_ids == [identity] and row.get("question_id") in (None, identity))
    require(row.get("source_id") == answer_scope["source_id"] and bound,
            "PUBLISHED_SOURCE_SCOPE_UNVERIFIED")
    return path
