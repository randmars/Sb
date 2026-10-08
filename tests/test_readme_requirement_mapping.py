"""The README's requirement mapping must be numbered correctly and point at real evidence.

Defect (Gate 1 correctness audit): the README's "Requirement mapping" table mapped
*ranges* onto the wrong requirements — R04–R06 was described as "identity evidence,
reversible corrections" (that is R13), R07–R08 as "rule preview, per-item keys" (R03 and
R14), R09–R11 as "honest outbound, approval binding, draft-only" (R10; R09 is freshness
and coverage honesty), and R16 as "draft-only launch" (also R10; R16 is the vertical
slice before expansion). A reader checking "does this repo evidence R13?" was pointed at
the wrong code.

These tests read the mapping out of the README and check it against the requirement
register in the PRD extract and against the repository itself:

* every requirement R01–R16 is claimed by exactly one row, and no row claims a range;
* every file path a row names exists, and every ``module.attribute`` it names is real;
* the corrections above are in place, so the mis-numbering cannot come back;
* the README's own test count matches the tests that actually exist.
"""

from __future__ import annotations

import importlib
import re
import unittest
from pathlib import Path

from tests.helpers import REPO_ROOT

README = REPO_ROOT / "README.md"
PRD = REPO_ROOT.parent / "PRD_extracted.md"
MAPPING_HEADER = "### Requirement mapping"
PROOF_HEADER = "## Data model"
REQUIREMENTS = [f"R{n:02d}" for n in range(1, 17)]

PATH_TOKEN = re.compile(r"`([A-Za-z0-9_./-]+\.(?:py|sql|md|json|sh))`")
ATTR_TOKEN = re.compile(r"`([a-z_]+)\.([a-z_]+)`")
TEST_COUNT = re.compile(r"\((\d+) tests\)|#\s*(\d+) tests")


def mapping_rows() -> dict[str, str]:
    """The README mapping table as {requirement id: evidence cell}."""
    text = README.read_text()
    section = text.split(MAPPING_HEADER)[1].split("---")[0]
    rows: dict[str, str] = {}
    for line in section.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 2 or set(cells[0]) <= set("-: "):
            continue
        if cells[0].lower().startswith("prd"):
            continue
        ids = re.findall(r"\bR\d{2}\b", cells[0])
        for requirement in ids:
            rows[requirement] = cells[1]
        if not ids:
            rows.setdefault("__ranges__", "")
            rows["__ranges__"] += cells[0] + " "
    return rows


class TestRequirementMapping(unittest.TestCase):
    def setUp(self) -> None:
        self.rows = mapping_rows()
        self.assertTrue(self.rows, "the README has no requirement mapping")

    def test_every_requirement_is_claimed_by_exactly_one_row(self) -> None:
        missing = [r for r in REQUIREMENTS if r not in self.rows]
        self.assertEqual(missing, [], f"the README does not map {missing}")
        # A range row (R04-R06 ...) claims several requirements in one cell, which is how
        # one wrong description came to look like evidence for four requirements.
        self.assertNotIn("__ranges__", self.rows,
                         "each row must map one requirement, not a range of them")
        self.assertEqual(sorted(r for r in self.rows if r.startswith("R")), REQUIREMENTS)

    def test_the_requirement_ids_exist_in_the_prd_extract(self) -> None:
        if not PRD.exists():
            self.skipTest("PRD extract not available in this checkout")
        prd = PRD.read_text()
        for requirement in REQUIREMENTS:
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, prd)

    def test_every_named_file_and_attribute_exists(self) -> None:
        for requirement, evidence in sorted(self.rows.items()):
            if not requirement.startswith("R"):
                continue
            for token in PATH_TOKEN.findall(evidence):
                with self.subTest(requirement=requirement, token=token):
                    self.assertTrue((REPO_ROOT / token).exists(),
                                    f"{requirement} claims evidence in {token}, which does not exist")
            for module, attribute in ATTR_TOKEN.findall(evidence):
                python_file = REPO_ROOT / "grace" / f"{module}.py"
                if not python_file.exists():
                    continue
                with self.subTest(requirement=requirement, token=f"{module}.{attribute}"):
                    loaded = importlib.import_module(f"grace.{module}")
                    self.assertTrue(hasattr(loaded, attribute),
                                    f"{requirement} claims {module}.{attribute}, which does not exist")
                    if not attribute.startswith("_"):
                        pass

    def test_the_audited_mis_numbering_is_corrected(self) -> None:
        expectations = {
            # Each entry: the requirement, and something its row (or its evidence) must
            # actually be about. These are the five claims the audit found mis-numbered.
            "R03": ("rule",),
            "R09": ("coverage", "freshness"),
            "R10": ("approval", "draft"),
            "R13": ("identity", "revers"),
            "R14": ("rule", "repeat"),
            "R16": ("slice", "gate"),
        }
        for requirement, keywords in expectations.items():
            row = self.rows[requirement].lower()
            with self.subTest(requirement=requirement):
                self.assertTrue(any(keyword in row for keyword in keywords),
                                f"{requirement} row must describe {keywords}, got {row!r}")
        # Identity evidence and reversible corrections belong to R13, not to R04-R06.
        for requirement in ("R04", "R05", "R06"):
            row = self.rows[requirement].lower()
            with self.subTest(requirement=requirement):
                self.assertNotIn("identity", row, f"{requirement} must not claim identity handling")
                self.assertNotIn("reversib", row, f"{requirement} must not claim reversibility")

    def test_identity_and_reversibility_evidence_is_reachable(self) -> None:
        row = self.rows["R13"].lower()
        self.assertIn("identity_link", row + self.rows["R13"])
        self.assertIn("person_merge", row + self.rows["R13"])

    def test_the_documented_test_count_is_true(self) -> None:
        """The README's numbers must have been run, not guessed (team standing correction)."""
        text = README.read_text()
        claimed = {int(a or b) for a, b in TEST_COUNT.findall(text)}
        self.assertTrue(claimed, "the README states no test count")
        discovered = unittest.TestLoader().discover(str(REPO_ROOT / "tests")).countTestCases()
        self.assertEqual(claimed, {discovered},
                         f"the README claims {sorted(claimed)} tests; {discovered} exist. "
                         "Update every test count in the README to the number you ran.")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
