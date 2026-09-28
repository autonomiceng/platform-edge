import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ConsoleAssetsTest(unittest.TestCase):
    def test_every_console_reference_is_served_and_exists(self):
        # Caddy serves only the listed console assets; an unlisted file would 404 in production.
        served = re.search(r"@console-assets path (.+)", (ROOT / "Caddyfile").read_text()).group(1).split()
        pages = (ROOT / "docker/console/index.html").read_text() + (ROOT / "docker/console/catalog.js").read_text()
        references = set(re.findall(r'/console/[\w./-]+', pages))
        references |= {f"/console/icons/{icon}" for icon in re.findall(r'icon: "([\w.-]+)"', pages)}
        self.assertIn("/console/platform.css", references)
        for path in sorted(references):
            with self.subTest(path):
                self.assertTrue(any(path == s or (s.endswith("/*") and path.startswith(s[:-1])) for s in served))
                self.assertTrue((ROOT / "docker" / path.removeprefix("/")).is_file())


if __name__ == "__main__":
    unittest.main()
