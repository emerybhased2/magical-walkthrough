"""Offline tests for compare_builds.py name matching (no client builds needed)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import compare_builds as cb
    AVAILABLE = True
except ImportError:  # pragma: no cover - tree-sitter missing
    AVAILABLE = False


@unittest.skipUnless(AVAILABLE, "tree-sitter not installed")
class NameTests(unittest.TestCase):
    def test_norm_and_tokens(self):
        self.assertEqual(cb.norm("The Kraken"), "kraken")
        self.assertEqual(cb.norm("Sea-horse"), "seahorse")
        self.assertEqual(cb.norm("kangaroo_rat"), "kangaroorat")
        self.assertEqual(cb.tokens("boaConstrictor"), ["boa", "constrictor"])
        self.assertEqual(cb.tokens("ostrich/baby-ostrich"), ["baby", "ostrich"])

    def test_renames_need_exactly_one_partner(self):
        removed = [{"name": "Croc", "skin": "croc"}, {"name": "Boa Constrictor", "skin": "boaConstrictor"},
                   {"name": "Baby Ostrich", "skin": "ostrich/baby-ostrich"}, {"name": "Stringray", "skin": None},
                   {"name": "Killer Whale", "skin": "killerwhale"}, {"name": "Land Monster", "skin": "landmonster"}]
        added = [{"name": n} for n in ("crocodile", "boa", "ostrich_baby", "Stingray", "orca", "lava_monster")]
        pairs = {p["old"]: (p["new"], p["rule"]) for p in cb.pair_renames(removed, added)}
        self.assertEqual(pairs["Croc"], ("crocodile", "abbreviation"))
        self.assertEqual(pairs["Boa Constrictor"][0], "boa")
        self.assertEqual(pairs["Baby Ostrich"], ("ostrich_baby", "same words"))
        self.assertEqual(pairs["Stringray"], ("Stingray", "spelling"))
        self.assertNotIn("Killer Whale", pairs)        # no evidence links it to "orca"
        self.assertNotIn("Land Monster", pairs)        # two letters apart from lava_monster: not a rename

    def test_ambiguous_candidates_are_not_paired(self):
        removed = [{"name": "Duck", "skin": None}]
        added = [{"name": "duckling"}, {"name": "duck_boss"}]
        self.assertEqual(cb.pair_renames(removed, added), [])

    def test_diff(self):
        self.assertEqual(cb.diff({1, 2}, {2, 3}), {"added": [3], "removed": [1]})


if __name__ == "__main__":
    unittest.main()
