import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "sync-conventions.sh"
CANONICAL = ROOT / "docs" / "conventions.md"
HEADER = "<!-- vendored from platform-edge@"


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                           *args], check=True, capture_output=True, text=True).stdout.strip()


class SyncConventionsTest(unittest.TestCase):
    def setUp(self):
        # A committed copy of the checkout isolates the header revision from this working tree.
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.repo = self.tmp / "repo"
        (self.repo / "scripts").mkdir(parents=True)
        (self.repo / "docs").mkdir()
        shutil.copy(SCRIPT, self.repo / "scripts" / "sync-conventions.sh")
        shutil.copy(CANONICAL, self.repo / "docs" / "conventions.md")
        git(self.repo, "init", "-q")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "canonical")
        self.sha = git(self.repo, "rev-parse", "--short", "HEAD")
        self.sibling = self.tmp / "sibling"
        (self.sibling / "docs").mkdir(parents=True)
        self.copy = self.sibling / "docs" / "conventions.md"

    def run_script(self, *args, repo=None):
        script = (repo or self.repo) / "scripts" / "sync-conventions.sh"
        return subprocess.run([str(script), *args], capture_output=True, text=True)

    def test_check_exits_1_on_drift_or_wrong_direction_and_0_on_fresh_copy(self):
        with self.subTest("copy then check a fresh copy"):
            self.assertEqual(self.run_script(str(self.sibling)).returncode, 0)
            self.assertEqual(self.copy.read_text().splitlines()[0], f"{HEADER}{self.sha} ; do not edit here -->")
            result = self.run_script("--check", str(self.sibling))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"ok: {self.copy} (platform-edge@{self.sha})", result.stdout)
        with self.subTest("modified line"):
            fresh = self.copy.read_text()
            lines = fresh.splitlines(keepends=True)
            lines[5] = "edited locally\n"
            self.copy.write_text("".join(lines))
            result = self.run_script("--check", str(self.sibling))
            self.assertEqual(result.returncode, 1)
            self.assertIn("differs:", result.stderr)
        with self.subTest("unrelated revision in the header"):
            self.copy.write_text(fresh.replace(self.sha, "deadbeef", 1))
            result = self.run_script("--check", str(self.sibling))
            self.assertEqual(result.returncode, 1)
            self.assertIn("deadbeef", result.stderr)
        with self.subTest("no header"):
            self.copy.write_text(CANONICAL.read_text())
            self.assertEqual(self.run_script("--check", str(self.sibling)).returncode, 1)
        with self.subTest("vendoring into the canonical checkout is refused"):
            result = self.run_script(str(self.repo))
            self.assertEqual(result.returncode, 1)
            self.assertIn("canonical checkout", result.stderr)
        with self.subTest("canonical file with a header is refused"):
            # A vendoring header on the canonical file means a sibling copy was pasted back.
            (self.repo / "docs" / "conventions.md").write_text(fresh)
            result = self.run_script("--check")
            self.assertEqual(result.returncode, 1)
            self.assertIn("refused:", result.stderr)
        with self.subTest("this checkout's canonical file passes the self-check"):
            self.assertEqual(self.run_script("--check", repo=ROOT).returncode, 0)


if __name__ == "__main__":
    unittest.main()
