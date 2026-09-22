"""Real Git pipe regressions: bounded requests and cleanup of an unresponsive child."""
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

import study_mcp as mcp
from bridge_archives import BridgeError


class PersonalStreamTests(unittest.TestCase):
    def make_snapshot(self, count):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        checkout = root / 'publication/math/checkout'
        checkout.mkdir(parents=True)
        subprocess.run(['git','init','-q',str(checkout)],check=True)
        raw = b'{"evidence":"' + b'x' * 16384 + b'"}\n'
        oid = subprocess.run(['git','-C',str(checkout),'hash-object','-w','--stdin'],
                             input=raw,capture_output=True,check=True).stdout.decode().strip()
        paths = [f'personalization/math/points/{i:04}.json' for i in range(count)]
        snap = object.__new__(mcp.Snapshot)
        snap.store = SimpleNamespace(base=root)
        snap.subject = 'math'
        snap.lock = threading.RLock()
        snap.cache = root / 'cache'
        snap._verified = {}
        snap._files = {p:{'storage':'git','git_blob_sha1':oid,'bytes':len(raw),
                          'sha256':hashlib.sha256(raw).hexdigest()} for p in paths}
        return snap, paths, raw

    def test_many_large_blobs_do_not_fill_request_pipe_before_reading(self):
        snap, paths, raw = self.make_snapshot(512)
        real_popen = subprocess.Popen
        writes = []

        class BoundedWriter:
            def __init__(self, stream):
                self.stream = stream
            def write(self, data):
                # Fail fast instead of hanging the suite when the writer tries
                # to overfill a small pipe while Git is writing a large reply.
                if len(data) > 4096:
                    raise AssertionError('request producer can deadlock under pipe backpressure')
                writes.append(len(data))
                return self.stream.write(data)
            def flush(self):
                return self.stream.flush()
            def close(self):
                return self.stream.close()

        def spawn(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            child.stdin = BoundedWriter(child.stdin)
            return child

        with mock.patch.object(mcp.subprocess, 'Popen', side_effect=spawn):
            snap.materialize_paths(paths)
        self.assertEqual(len(writes), len(paths))
        for path in paths:
            self.assertEqual((snap.cache/path).read_bytes(), raw)

    def test_stalled_git_is_killed_and_returns_a_bounded_error(self):
        snap, paths, _ = self.make_snapshot(1)
        real_popen = subprocess.Popen
        children = []

        def stall(*args, **kwargs):
            child = real_popen([sys.executable,'-c','import time; time.sleep(30)'],
                               stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
            children.append(child)
            return child

        with mock.patch.object(mcp.subprocess, 'Popen', side_effect=stall), \
             mock.patch.object(mcp, 'PERSONAL_BATCH_TIMEOUT_SECONDS', 0.15):
            with self.assertRaisesRegex(BridgeError, 'PERSONAL_SNAPSHOT_READ_TIMEOUT'):
                snap.materialize_paths(paths)
        self.assertTrue(children)
        self.assertTrue(all(child.poll() is not None for child in children))


if __name__ == '__main__':
    unittest.main()
