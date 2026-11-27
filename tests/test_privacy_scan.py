"""Rules of the local privacy scanner: loopback is fine, LAN addresses are not.

The fixtures are assembled from parts on purpose: this file has to live in the
repository, so it must not itself contain a literal LAN address, serial-shaped
string or badge that the scanner would (correctly) flag.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import privacy_scan


def hits(text: str) -> list[str]:
    return [name for name, pattern, allow in privacy_scan.RULES
            if (match := pattern.search(text)) and match.group(0) not in allow]


def ip(*octets: int) -> str:
    return ".".join(str(octet) for octet in octets)


class PrivacyScanRuleTestCase(unittest.TestCase):
    def test_loopback_is_not_private_information(self) -> None:
        self.assertEqual(hits(f"curl http://{ip(127, 0, 0, 1)}:8790/v1/status"), [])
        self.assertEqual(hits(f"server binds {ip(127, 0, 0, 1)} only"), [])

    def test_lan_addresses_are_flagged(self) -> None:
        self.assertIn("private-ip", hits(f"host at {ip(192, 168, 1, 14)}"))
        self.assertIn("private-ip", hits(f"host at {ip(10, 0, 0, 7)}"))
        self.assertIn("private-ip", hits(f"host at {ip(172, 20, 3, 4)}"))

    def test_synthetic_serial_passes_but_other_shapes_do_not(self) -> None:
        self.assertEqual(hits("SN SBRC00000000123"), [])
        # Shape only: 11 alphanumerics after SBRC, no real unit's number.
        fake_serial = "SBRC" + "01234567890"
        self.assertIn("serial-number", hits(f"SN {fake_serial}"))
        self.assertEqual(hits("No.123/1000"), [])
        fake_badge = f"No.{999}/1000"
        self.assertIn("edition-badge", hits(fake_badge))


if __name__ == "__main__":
    unittest.main()
