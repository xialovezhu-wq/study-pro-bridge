"""Native, local-only publication and authoritative identity checks with synthetic files."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import study_personalization as personal
import study_publication as pub
from study_bridge import Published
from study_mcp import LibraryStore


class EnglishWritingPublication(unittest.TestCase):
    def test_freshness_reads_live_identity_and_separates_formal_status(self):
        with tempfile.TemporaryDirectory() as td:
            repo=Path(td);state=repo/"publication"
            pub.write_json(state/"config.json",{"target":"local"})
            source=repo/"source";source.mkdir()
            path=source/"summary.json";pub.write_json(path,{"schema":"fixture","records":[]})
            publisher=pub.Publisher("english",source,state)
            info={"status":"available","version":"summary-one","projection_sha256":pub.file_hash(path),
                  "entrypoint":"context/personal-summary/current.json","memory_generation":"generation"}
            desc={"subject":"english","local_version":"v-fixture","formal_version":"formal-one","personalization":info,
                  "sources":[{"source_path":str(path),"publish_path":info["entrypoint"],"kind":"bounded_teaching_projection"}]}
            rec=publisher.execute("synthetic-writing-publication",desc)
            store=LibraryStore(repo,cache_parent=repo)
            try:
                snap=store.snapshot("english",rec["source_commit"])
                # Use this isolated publisher as the authoritative test service, preserving its real snapshot and checks.
                with patch("study_mcp.BASE",repo),patch.object(personal,"native_call") as live:
                    for identity,want in [({"status":"available","version":"summary-one"},"current"),
                                          ({"status":"available","version":"summary-two"},"stale"),
                                          ({"status":"unknown"},"unknown"),({"status":"pending"},"pending")]:
                        live.return_value={"status":"ready","formal_version":"formal-one","personal_summary":identity}
                        status=store.freshness(snap)
                        self.assertEqual((status["formal_freshness"],status["personal_summary_freshness"]),("current",want))
                result=store.fetch("english",info["entrypoint"],rec["source_commit"])
                self.assertTrue(result["eof"]);self.assertEqual(result["sha256"],pub.file_hash(path))
                self.assertEqual(publisher.execute("synthetic-writing-publication",desc)["source_commit"],rec["source_commit"])
            finally:
                store.close()
