#!/usr/bin/env python3
"""Exercise the actual MCP transport against current published study libraries."""
import argparse
import asyncio
import base64
import hashlib
import json
from pathlib import Path
import sys
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

BASE = Path(__file__).resolve().parents[1]


async def check(include_workflows: bool) -> dict:
    started = time.monotonic()
    result = {"transport": "actual MCP stdio", "subjects": {}, "formal_write_count": 0,
              "learner_evidence_write_count": 0, "web_chat_verified": False}
    params = StdioServerParameters(command=sys.executable, args=[str(BASE / "study_mcp.py")], cwd=str(BASE))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            result["tools"] = [{"name": t.name, "read_only": t.annotations.readOnlyHint} for t in tools]
            assert all(t.annotations.readOnlyHint for t in tools)
            async def call(tool, args):
                raw = await session.call_tool(tool, args)
                assert not raw.isError, str(raw)
                body = raw.structuredContent
                assert body is not None and body.get("ok") is not False, body
                return body
            for subject in ("math", "english", "408"):
                status = await call("library", {"subject": subject, "limit": 1})
                row = result["subjects"][subject] = {"source_commit": status["source_commit"],
                    "publication_target": status.get("publication_target", "github"),
                    "source_role": status.get("source_role"), "github_backup_required": status.get("github_backup_required"),
                    "domains": {}, "source_files": status["full_history_source_scan"]}
                for domain, expected in status["domains"].items():
                    offset, records, identities, page_count = 0, [], [], 0
                    while True:
                        page = await call("query", {"subject": subject, "source_commit": status["source_commit"],
                                                   "domain": domain, "offset": offset, "limit": 20, "max_bytes": 524288})
                        records.extend(page["records"])
                        identities.extend(r["id"] for r in page["records"])
                        page_count += 1
                        if page["eof"]:
                            break
                        assert page["next_offset"] > offset
                        offset = page["next_offset"]
                    assert len(records) == expected["records"]
                    nonnull = [v for v in identities if v is not None]
                    assert len(nonnull) == len(set(nonnull))
                    row["domains"][domain] = {"records_received": len(records), "pages": page_count, "eof": True,
                        "ids_sha256": hashlib.sha256(("\n".join(sorted(nonnull)) + "\n").encode()).hexdigest()}
                if subject == "math":
                    path = "错题知识网络/生成/wrong_questions.json"
                    offset, content, page_count = 0, bytearray(), 0
                    while True:
                        part = await call("fetch", {"subject": subject, "source_commit": status["source_commit"],
                            "path": path, "start_byte": offset, "max_bytes": 65536})
                        raw = part["text"].encode()
                        assert hashlib.sha256(raw).hexdigest() == part["returned_sha256"]
                        content.extend(raw)
                        page_count += 1
                        if part["eof"]:
                            break
                        assert part["next_start_byte"] > offset
                        offset = part["next_start_byte"]
                    assert hashlib.sha256(content).hexdigest() == part["sha256"]
                    assert len(content) == part["total_bytes"]
                    row["large_original"] = {"bytes": len(content), "sha256": part["sha256"], "pages": page_count, "eof": True}
            examples = [("math", "错题知识网络/assets/visual_wrong_questions/GS-001/question_01.png", None),
                        ("english", "published-originals/SRC-EXAM-001/pages/page-0004.pdf", 1)]
            result["images"] = []
            for subject, path, page in examples:
                args = {"subject": subject, "path": path, "source_commit": result["subjects"][subject]["source_commit"]}
                if page:
                    args["page"] = page
                raw = await session.call_tool("view_image", args)
                assert not raw.isError, str(raw)
                image = next(c for c in raw.content if c.type == "image")
                assert hashlib.sha256(base64.b64decode(image.data)).hexdigest() == raw.structuredContent["image_sha256"]
                result["images"].append(raw.structuredContent)
            if include_workflows:
                target = "2026-09-09"
                result["workflows"] = {}
                for project, subject in (("C", "math"), ("D", "408")):
                    raw = await session.call_tool("workflow", {"project": project, "subject": subject,
                        "target_date": target, "source_commit": result["subjects"][subject]["source_commit"]})
                    result["workflows"][project] = raw.structuredContent
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--workflows", action="store_true")
    args = parser.parse_args()
    result = asyncio.run(check(args.workflows))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"ok": True, "elapsed_seconds": result["elapsed_seconds"],
        "subjects": {s: r["domains"] for s, r in result["subjects"].items()},
        "image_count": len(result["images"]), "web_chat_verified": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
