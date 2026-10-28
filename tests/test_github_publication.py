"""Verify publication trees without contacting a remote repository."""

from pathlib import Path
import subprocess
import tempfile
import unittest


PREPARE = Path(__file__).resolve().parents[1] / "scripts/prepare-github.sh"


class GithubPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git("init", "--initial-branch=main")
        for key, value in (
            ("user.name", "Test"), ("user.email", "test@example.test"),
            ("commit.gpgsign", "false"), ("core.hooksPath", "/dev/null"),
        ):
            self.git("config", key, value)
        for name, content in {
            ".gitlab-ci.yml": "internal pipeline\n",
            "scripts/prepare-github.sh": "publication helper\n",
            "tests/test_mirror.py": "GitLab registry tests\n",
            "tests/test_github_publication.py": "GitLab publication tests\n",
            "README.md": "usage guide\n",
            "images.lock": "released pins\n",
        }.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        self.git("add", ".")
        self.git("commit", "-m", "Release fixture")

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, check=True,
                              text=True, capture_output=True).stdout.strip()

    def test_snapshot_excludes_pipeline_and_preserves_local_changes(self):
        (self.root / "README.md").write_text("unreleased guide\n")
        self.git("add", "README.md")
        (self.root / "images.lock").write_text("unreleased pins\n")
        (self.root / "untracked.txt").write_text("local only\n")
        head = self.git("rev-parse", "HEAD")
        index = self.git("write-tree")
        status = self.git("status", "--porcelain")

        result = subprocess.run(["sh", str(PREPARE), head], cwd=self.root,
                                check=True, text=True, capture_output=True)
        snapshot = result.stdout.strip()

        self.assertEqual(set(self.git("ls-tree", "--name-only", snapshot).splitlines()),
                         {"README.md", "images.lock", "tests"})
        self.assertEqual(self.git("show", f"{snapshot}:README.md"), "usage guide")
        self.assertEqual(self.git("show", f"{snapshot}:images.lock"), "released pins")
        self.assertEqual(self.git("rev-list", "--count", snapshot), "1")
        snapshot_paths = set(self.git("ls-tree", "-r", "--name-only", snapshot).splitlines())
        self.assertNotIn(".gitlab-ci.yml", snapshot_paths)
        self.assertNotIn("scripts/prepare-github.sh", snapshot_paths)
        self.assertIn("tests/test_mirror.py", snapshot_paths)
        self.assertIn("tests/test_github_publication.py", snapshot_paths)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual(self.git("write-tree"), index)
        self.assertEqual(self.git("status", "--porcelain"), status)
        self.assertTrue((self.root / ".gitlab-ci.yml").exists())
        self.assertTrue((self.root / "scripts/prepare-github.sh").exists())

    def test_invalid_source_commit_fails_without_changes(self):
        status = self.git("status", "--porcelain")
        result = subprocess.run(["sh", str(PREPARE), "missing-release"],
                                cwd=self.root, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.git("status", "--porcelain"), status)


if __name__ == "__main__":
    unittest.main()
