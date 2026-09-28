import hashlib
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "sync-ui.sh"
CANONICAL = ROOT / "docker" / "console" / "platform.css"


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                           *args], check=True, capture_output=True, text=True).stdout.strip()


class SyncUiTest(unittest.TestCase):
    def setUp(self):
        # A committed copy of the checkout isolates the header revision from this working tree.
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.repo = self.tmp / "repo"
        (self.repo / "scripts").mkdir(parents=True)
        (self.repo / "docker" / "console").mkdir(parents=True)
        shutil.copy(SCRIPT, self.repo / "scripts" / "sync-ui.sh")
        shutil.copy(CANONICAL, self.repo / "docker" / "console" / "platform.css")
        git(self.repo, "init", "-q")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "canonical")
        self.sha = git(self.repo, "rev-parse", "--short", "HEAD")

    def run_script(self, *args):
        return subprocess.run([str(self.repo / "scripts" / "sync-ui.sh"), *args], capture_output=True, text=True)

    def sibling(self, name, console):
        (self.tmp / name / console).mkdir(parents=True)
        return self.tmp / name, self.tmp / name / console / "platform.css"

    def test_copy_check_and_drift(self):
        gateway, copy = self.sibling("gateway", "docker/caddy/console")
        backplane, web_copy = self.sibling("backplane", "apps/web")
        result = self.run_script(str(gateway), str(backplane))
        self.assertEqual(result.returncode, 0, result.stderr)
        canonical = CANONICAL.read_text()
        for target in (copy, web_copy):
            header, body = target.read_text().split("\n", 1)
            self.assertEqual(body, canonical)
            # Siblings verify the checksum alone, without a platform-edge checkout.
            self.assertEqual(header, f"/* vendored from platform-edge@{self.sha} sha256:"
                                     f"{hashlib.sha256(canonical.encode()).hexdigest()} ; do not edit here */")
        result = self.run_script("--check", str(gateway), str(backplane))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"ok: {copy} (platform-edge@{self.sha})", result.stdout)
        fresh = copy.read_text()
        cases = {
            "checksum": fresh.replace("--pk-bg: #0d1525", "--pk-bg: #000000", 1),
            "canonical": re.sub("sha256:[0-9a-f]{64}", "sha256:" + hashlib.sha256(
                fresh.split("\n", 1)[1].replace("#0d1525", "#000000", 1).encode()).hexdigest(), fresh)
            .replace("--pk-bg: #0d1525", "--pk-bg: #000000", 1),
            "unknown here": fresh.replace(self.sha, "deadbeef", 1),
            "no valid vendoring header": canonical,
        }
        for expected, content in cases.items():
            with self.subTest(expected):
                copy.write_text(content)
                result = self.run_script("--check", str(gateway))
                self.assertEqual(result.returncode, 1)
                self.assertIn(expected, result.stderr)
        with self.subTest("a sibling without a console directory"):
            (self.tmp / "empty").mkdir()
            result = self.run_script(str(self.tmp / "empty"))
            self.assertEqual(result.returncode, 1)
            self.assertIn("missing:", result.stderr)

    def test_refuses_uncommitted_or_headed_canonical(self):
        gateway, _ = self.sibling("gateway", "docker/caddy/console")
        canonical = self.repo / "docker" / "console" / "platform.css"
        canonical.write_text(canonical.read_text() + "\n")
        result = self.run_script(str(gateway))
        self.assertEqual(result.returncode, 1)
        self.assertIn("uncommitted", result.stderr)
        canonical.write_text("/* vendored from platform-edge@abc1234 */\n" + CANONICAL.read_text())
        result = self.run_script("--check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("vendoring header", result.stderr)
        self.assertEqual(subprocess.run([str(SCRIPT), "--check"]).returncode, 0)


if __name__ == "__main__":
    unittest.main()
