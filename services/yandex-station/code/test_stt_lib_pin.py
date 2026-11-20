"""The pin of the stt-cli core (requirements-stt-lib.txt) is one real, hash-pinned release wheel."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

PIN = Path(__file__).with_name("requirements-stt-lib.txt")
REQUIREMENT = re.compile(
    r"^hyper-stt @ (?P<url>https://\S+/releases/download/v(?P<tag>\d+\.\d+\.\d+)/"
    r"hyper_stt-(?P<wheel>\d+\.\d+\.\d+)-py3-none-any\.whl) --hash=sha256:(?P<sha>[0-9a-f]{64})$"
)


class CorePin(unittest.TestCase):
    def requirements(self) -> list[str]:
        lines = (line.strip() for line in PIN.read_text(encoding="utf-8").splitlines())
        return [line for line in lines if line and not line.startswith("#")]

    def test_it_is_one_requirement_a_wheel_of_the_tagged_release_with_its_sha256(self) -> None:
        requirements = self.requirements()
        self.assertEqual(len(requirements), 1, "the pin file holds exactly one requirement")
        found = REQUIREMENT.match(requirements[0])
        self.assertIsNotNone(found, f"not a hash-pinned release wheel: {requirements[0]!r}")
        assert found is not None
        self.assertEqual(found["tag"], found["wheel"], "the wheel version must be the release tag's")

    def test_it_is_no_placeholder(self) -> None:
        text = PIN.read_text(encoding="utf-8")
        self.assertNotIn("REPLACE", text)
        self.assertNotIn("PLACEHOLDER", text)
        self.assertNotRegex(text, r"sha256:0{64}")


if __name__ == "__main__":
    unittest.main()
