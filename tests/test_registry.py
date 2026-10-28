"""Optional integration test against a disposable TLS registry; never uses the real target."""
import argparse
import contextlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import dune_mirror as m
from test_mirror import archive


@unittest.skipUnless(os.getenv("DUNE_TEST_REGISTRY"), "Set DUNE_TEST_REGISTRY to a disposable TLS registry")
class RegistryIntegration(unittest.TestCase):
    def test_publish_skip_deep_verify_and_conflict(self):
        target = os.environ["DUNE_TEST_REGISTRY"].rstrip("/") + "/dune-test"
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(os.environ, {"CI": "", "CI_REGISTRY_IMAGE": target, "CI_REGISTRY": "",
                                     "CI_REGISTRY_USER": "", "CI_REGISTRY_PASSWORD": ""}):
            root = Path(tmp)
            lock = json.loads(Path(m.__file__).with_name("images.lock").read_text())
            lock_path = root / "images.lock"
            lock_path.write_text(json.dumps(lock))
            for relative, (name, version) in m.IMAGES.items():
                archive(root / relative, m.PREFIX + name + ":" + lock[version])
            args = argparse.Namespace(command="publish", lock=lock_path, package=root,
                                      report=root / "report.json", deep=False)
            m.run(args)
            first = json.loads(args.report.read_text())
            self.assertTrue(all(i["status"] == "published" for i in first["images"]))
            m.run(args)
            second = json.loads(args.report.read_text())
            self.assertTrue(all(i["status"] == "verified" and i["verification"] == "full-pull"
                                for i in second["images"]))
            self.assertEqual([i["destination_digest"] for i in first["images"]],
                             [i["destination_digest"] for i in second["images"]])
            args.command, args.deep = "verify", True
            m.run(args)
            wrong = dict(second["images"][0], config_digest="sha256:" + "0" * 64)
            with self.assertRaisesRegex(m.MirrorError, "conflict"):
                m.remote(wrong, None)
