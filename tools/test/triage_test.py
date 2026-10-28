"""Unit tests for tools/triage.py."""

import unittest

import triage


class ClassifyTest(unittest.TestCase):
    def test_pass_and_name_specific_categories(self):
        self.assertIs(triage.classify("inline", "NoDefinition"), triage.INLINE_NO_DEFINITION)
        self.assertIs(triage.classify("inline", "TooCostly"), triage.INLINE_TOO_COSTLY)

    def test_pass_categories(self):
        cases = {
            "licm": triage.ALIASING_LOOP,
            "gvn": triage.ALIASING_REDUNDANT_LOAD,
            "loop-vectorize": triage.LOOP_NOT_VECTORIZED,
            "slp-vectorizer": triage.SLP_NOT_VECTORIZED,
            "inline": triage.INLINE_OTHER,
            "loop-unroll": triage.UNROLL,
            "loop-unroll-and-jam": triage.UNROLL,
            "regalloc": triage.REGISTER_PRESSURE,
        }
        for pass_name, category in cases.items():
            with self.subTest(pass_name=pass_name):
                self.assertIs(triage.classify(pass_name, "AnyName"), category)

    def test_unknown_pass_is_other(self):
        self.assertIs(triage.classify("some-future-pass", "X"), triage.OTHER)


class WeightTest(unittest.TestCase):
    def test_category_weight(self):
        self.assertEqual(triage.weight("licm", "LoadWithLoopInvariantAddressInvalidated"), 5.0)
        self.assertEqual(triage.weight("gvn", "LoadClobbered"), 3.0)

    def test_name_overrides(self):
        self.assertEqual(triage.weight("licm", "LoadWithLoopInvariantAddressCondExecuted"), 2.0)
        self.assertEqual(triage.weight("regalloc", "LoopSpillReloadCopies"), 2.5)

    def test_c_callee_is_down_weighted(self):
        self.assertEqual(triage.weight("inline", "NoDefinition", callee="printf"), 0.5)
        self.assertEqual(triage.weight("inline", "NoDefinition", callee="_Z3foov"), 2.5)
        self.assertEqual(triage.weight("inline", "NoDefinition"), 2.5)

    def test_callee_only_matters_for_inline(self):
        self.assertEqual(triage.weight("gvn", "LoadClobbered", callee="printf"), 3.0)


class KnowledgeBaseTest(unittest.TestCase):
    def test_categories_are_documented_and_keyed(self):
        for key, category in triage.CATEGORIES.items():
            with self.subTest(key=key):
                self.assertEqual(category.key, key)
                self.assertTrue(category.title)
                self.assertTrue(category.explanation)
                self.assertTrue(category.remedies)
                self.assertGreater(category.weight, 0)

    def test_every_classified_category_is_registered(self):
        for category in (triage.classify(p, n) for p, n in [("licm", ""), ("inline", "TooCostly"), ("x", "")]):
            self.assertIn(category.key, triage.CATEGORIES)

    def test_ignored_reasons_are_not_explained(self):
        self.assertFalse(triage.IGNORED_VECTORIZE_REASONS & triage.VECTORIZE_REASONS.keys())


if __name__ == "__main__":
    unittest.main()
