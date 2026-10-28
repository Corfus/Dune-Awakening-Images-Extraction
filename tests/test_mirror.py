import argparse
import gzip
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch
from contextlib import contextmanager

import dune_mirror as m


def archive(path, source, *, compressed=True, wrong_layer=False, extra_tag=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    plain = b"a fixture filesystem layer"
    blob = gzip.compress(plain, mtime=0) if compressed else plain
    config = json.dumps({"os": "linux", "architecture": "amd64", "rootfs": {
        "type": "layers", "diff_ids": [m.sha(plain)]}}).encode()
    config_name = "blobs/sha256/" + m.sha(config)[7:] if compressed else m.sha(config)[7:] + ".json"
    layer_name = "blobs/sha256/" + m.sha(blob)[7:] if compressed else "layer/layer.tar"
    manifest = [{"Config": config_name, "RepoTags": [source] + ([source + "extra"] if extra_tag else []),
                 "Layers": [layer_name]}]
    with tarfile.open(path, "w") as tar:
        for name, data in [(config_name, config), (layer_name, b"corrupt" if wrong_layer else blob),
                           ("manifest.json", json.dumps(manifest).encode())]:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return config


class MirrorTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {
            "CI_REGISTRY_IMAGE": os.getenv("CI_REGISTRY_IMAGE") or "registry.example/group/project"})
        environment.start()
        self.addCleanup(environment.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.lock, _ = m.lockfile(Path(m.__file__).with_name("images.lock"))
        self.args = argparse.Namespace(command="publish", lock=Path(m.__file__).with_name("images.lock"),
                                       package=self.root, report=self.root / "report.json",
                                       deep=False, depotdownloader="DepotDownloader")

    def fixture(self):
        for path, (name, version) in m.IMAGES.items():
            archive(self.root / path, m.PREFIX + name + ":" + self.lock[version])

    def test_modern_and_classic_archives(self):
        for compressed in [False, True]:
            p = self.root / "image.tar"
            config = archive(p, "image:1", compressed=compressed)
            self.assertEqual(m.inspect_archive(p, "image:1")["config_digest"], m.sha(config))

    def test_corrupt_layer_and_wrong_tags_rejected(self):
        for options in [{"wrong_layer": True}, {"extra_tag": True}]:
            p = self.root / "image.tar"
            archive(p, "image:1", **options)
            with self.assertRaises(m.MirrorError):
                m.inspect_archive(p, "image:1")

    def test_safe_skopeo_compatibility_links_and_unsafe_links(self):
        for target, allowed in [("../layer/layer.tar", True), ("../../outside", False),
                                ("/etc/passwd", False)]:
            p = self.root / "image.tar"
            archive(p, "image:1", compressed=False)
            with tarfile.open(p, "a") as tar:
                info = tarfile.TarInfo("compat/layer.tar")
                info.type, info.linkname = tarfile.SYMTYPE, target
                tar.addfile(info)
            if allowed:
                m.inspect_archive(p, "image:1")
            else:
                with self.assertRaises(m.MirrorError):
                    m.inspect_archive(p, "image:1")

    def test_all_eleven_and_exact_names(self):
        self.fixture()
        images = m.validate(self.root, self.lock)
        self.assertEqual(len(images), 11)
        self.assertEqual(images[0]["destination"], m.registry() + "/igw-k8s-battlegroup-operator:v1.7.0")
        self.assertEqual(images[4]["destination"], m.registry() + "/seabass-server:2162777-0-shipping")
        self.assertTrue(all(":latest" not in i["destination"] for i in images))

    def test_last_bad_archive_means_no_registry_calls(self):
        self.fixture()
        last = list(m.IMAGES)[-1]
        archive(self.root / last, m.PREFIX + "wrong-name:" + self.lock["postgres_version"])
        with patch.object(m, "skopeo") as skopeo:
            with self.assertRaises(m.MirrorError):
                m.run(self.args)
            skopeo.assert_not_called()
        self.assertEqual(json.loads(self.args.report.read_text())["status"], "failed")

    def test_plan_is_offline(self):
        self.fixture()
        self.args.command = "plan"
        with patch.object(m, "execute") as execute, patch.object(m, "credentials") as creds:
            m.run(self.args)
            execute.assert_not_called()
            creds.assert_not_called()

    def test_existing_verified_images_are_not_pushed(self):
        self.fixture()
        with patch.object(m, "credentials", return_value=context(None)), \
             patch.object(m, "remote", return_value={"destination_digest": "sha256:fixture"}) as remote, \
             patch.object(m, "skopeo") as skopeo:
            m.run(self.args)
            self.assertEqual(remote.call_count, 11)
            skopeo.assert_not_called()

    def test_conflict_on_last_image_prevents_all_pushes(self):
        self.fixture()
        with patch.object(m, "credentials", return_value=context(None)), \
             patch.object(m, "remote", side_effect=[None] * 10 + [m.MirrorError("conflict")]), \
             patch.object(m, "skopeo") as skopeo:
            with self.assertRaises(m.MirrorError):
                m.run(self.args)
            skopeo.assert_not_called()

    def test_interrupted_publish_resumes_from_registry_not_report(self):
        self.fixture()
        with patch.object(m, "credentials", return_value=context(None)), \
             patch.object(m, "remote", return_value=None), \
             patch.object(m, "skopeo", side_effect=m.MirrorError("interrupted")):
            with self.assertRaises(m.MirrorError):
                m.run(self.args)
        with patch.object(m, "credentials", return_value=context(None)), \
             patch.object(m, "remote", side_effect=[{"destination_digest": "sha256:done"}] +
                          [None] * 10 + [{"destination_digest": "sha256:new"}] * 10), \
             patch.object(m, "skopeo", return_value=b"") as skopeo:
            m.run(self.args)
            self.assertEqual(skopeo.call_count, 10)
        self.assertEqual(json.loads(self.args.report.read_text())["status"], "complete")

    def test_verify_missing_is_failure(self):
        image = {"destination": "registry.test/image:1"}
        with patch.object(m, "tool", return_value="skopeo"), \
             patch.object(m, "execute", return_value=subprocess.CompletedProcess([], 1, b"", b"manifest unknown")):
            self.assertIsNone(m.remote(image, None, missing_ok=True))
            with self.assertRaises(m.MirrorError):
                m.remote(image, None)

    def test_auth_and_network_failure_are_not_missing(self):
        for error in [b"unauthorized token=secret", b"TLS failed", b"timeout", b"404 Not Found"]:
            with patch.object(m, "tool", return_value="skopeo"), \
                 patch.object(m, "execute", return_value=subprocess.CompletedProcess([], 1, b"", error)):
                with self.assertRaises(m.MirrorError) as caught:
                    m.skopeo(["inspect", "docker://registry.test/image:1"], missing_ok=True)
                self.assertNotIn("secret", str(caught.exception))

    def test_wrong_remote_config_is_conflict(self):
        raw = json.dumps({"schemaVersion": 2, "config": {"digest": "sha256:wrong"}, "layers": []}).encode()
        with patch.object(m, "skopeo", return_value=raw):
            with self.assertRaisesRegex(m.MirrorError, "conflict"):
                m.remote({"destination": "registry.test/image:1", "config_digest": "sha256:expected"}, None)

    def test_metadata_mismatch(self):
        self.fixture()
        (self.root / "images/operators/version.txt").write_text("v99")
        with self.assertRaisesRegex(m.MirrorError, "metadata"):
            m.validate(self.root, self.lock)

    def test_latest_lock_is_rejected(self):
        self.lock["game_version"] = "latest"
        path = self.root / "images.lock"
        path.write_text(json.dumps(self.lock))
        with self.assertRaises(m.MirrorError):
            m.lockfile(path)

    def test_gitlab_registry_is_used_for_all_images(self):
        self.fixture()
        with patch.dict(os.environ, {"CI_REGISTRY_IMAGE": "registry.example:5050/group/project"}):
            images = m.validate(self.root, self.lock)
            self.assertTrue(all(i["destination"].startswith("registry.example:5050/group/project/")
                                for i in images))
            self.assertEqual(images[0]["source"], m.PREFIX + "igw-k8s-battlegroup-operator:v1.7.0")

    def test_local_credentials_use_existing_docker_auth(self):
        with patch.dict(os.environ, {"CI": "", "CI_REGISTRY_USER": "",
                                     "CI_REGISTRY_PASSWORD": "", "REGISTRY_AUTH_FILE": ""}), \
             patch.object(m, "execute") as execute:
            with m.credentials() as authfile:
                self.assertFalse(authfile)
            execute.assert_not_called()

    def test_gitlab_credentials_use_stdin_and_temporary_authfile(self):
        def login(argv, **kwargs):
            self.assertNotIn("secret-ci-token", " ".join(argv))
            self.assertEqual(kwargs["input"], b"secret-ci-token\n")
            self.assertEqual(argv[-1], "registry.example:5050")
            Path(argv[argv.index("--authfile") + 1]).write_text("{}")
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        with patch.dict(os.environ, {"CI": "true", "CI_REGISTRY": "registry.example:5050",
                                     "CI_REGISTRY_IMAGE": "registry.example:5050/group/project",
                                     "CI_REGISTRY_USER": "gitlab-ci-token",
                                     "CI_REGISTRY_PASSWORD": "secret-ci-token"}), \
             patch.object(m, "tool", return_value="skopeo"), patch.object(m, "execute", side_effect=login):
            with m.credentials() as authfile:
                self.assertTrue(authfile.exists())
                self.assertEqual(authfile.stat().st_mode & 0o777, 0o600)
            self.assertFalse(authfile.exists())

    def test_missing_gitlab_variables_and_host_mismatch_fail_before_login(self):
        base = {"CI": "true", "CI_REGISTRY": "registry.example",
                "CI_REGISTRY_IMAGE": "registry.example/group/project",
                "CI_REGISTRY_USER": "gitlab-ci-token", "CI_REGISTRY_PASSWORD": "token"}
        for change in [{"CI_REGISTRY_PASSWORD": ""}, {"CI_REGISTRY_IMAGE": ""},
                       {"CI_REGISTRY": "another.example"}]:
            with patch.dict(os.environ, {**base, **change}), patch.object(m, "execute") as execute:
                with self.assertRaises(m.MirrorError):
                    with m.credentials():
                        self.fail("Invalid CI registry configuration should fail")
                execute.assert_not_called()

    def test_registry_free_lock_and_offline_archive_validation(self):
        self.fixture()
        with patch.dict(os.environ, {"CI_REGISTRY_IMAGE": ""}):
            lock, _ = m.lockfile(self.args.lock)
            self.assertNotIn("registry", lock)
            images = m.validate(self.root, lock)
            self.assertEqual(len(images), 11)
            self.assertTrue(all(i["destination"] is None for i in images))

    def test_publishing_commands_require_gitlab_destination(self):
        with patch.dict(os.environ, {"CI_REGISTRY_IMAGE": ""}), patch.object(m, "validate") as validate:
            for command in ("plan", "publish", "verify"):
                self.args.command = command
                with self.assertRaisesRegex(m.MirrorError, "CI_REGISTRY_IMAGE"):
                    m.run(self.args)
            validate.assert_not_called()


@contextmanager
def context(value):
    yield value


if __name__ == "__main__":
    unittest.main()
