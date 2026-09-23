import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "sync-conventions.sh"
CANONICAL = ROOT / "docs" / "conventions.md"
HEADER = "<!-- vendored from platform-edge@"


def run(*args, cwd=ROOT):
    return subprocess.run([str(SCRIPT), *args], cwd=cwd, capture_output=True, text=True)


class SyncConventionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def vendored_copy(self, sha="0123abc"):
        sibling = self.tmp / "sibling"
        (sibling / "docs").mkdir(parents=True)
        copy = sibling / "docs" / "conventions.md"
        copy.write_text(f"{HEADER}{sha} ; do not edit here -->\n" + CANONICAL.read_text())
        return sibling, copy

    def test_check_exits_1_on_drift_or_wrong_direction_and_0_on_fresh_copy(self):
        sibling, copy = self.vendored_copy()
        with self.subTest("fresh copy"):
            result = run("--check", str(sibling))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("ok:", result.stdout)
        with self.subTest("modified line"):
            lines = copy.read_text().splitlines(keepends=True)
            lines[5] = "edited locally\n"
            copy.write_text("".join(lines))
            result = run("--check", str(sibling))
            self.assertEqual(result.returncode, 1)
            self.assertIn("differs:", result.stderr)
        with self.subTest("no header"):
            copy.write_text(CANONICAL.read_text())
            self.assertEqual(run("--check", str(sibling)).returncode, 1)
        with self.subTest("canonical file with a header is refused"):
            # A vendoring header on the canonical file means a sibling copy was pasted back.
            fake_root = self.tmp / "root"
            (fake_root / "scripts").mkdir(parents=True)
            (fake_root / "docs").mkdir()
            shutil.copy(SCRIPT, fake_root / "scripts" / "sync-conventions.sh")
            (fake_root / "docs" / "conventions.md").write_text(
                f"{HEADER}0123abc ; do not edit here -->\n" + CANONICAL.read_text())
            result = subprocess.run([str(fake_root / "scripts" / "sync-conventions.sh"), "--check"],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn("refused:", result.stderr)
        with self.subTest("canonical file without a header passes the self-check"):
            self.assertEqual(run("--check").returncode, 0)
        with self.subTest("vendoring into the canonical checkout is refused"):
            result = run(str(ROOT))
            self.assertEqual(result.returncode, 1)
            self.assertIn("canonical checkout", result.stderr)
            self.assertFalse(CANONICAL.read_text().startswith(HEADER))


if __name__ == "__main__":
    unittest.main()
