"""The vocabularies both sides of the boundary speak: asserted equal, in one place.

Grace and the Mini worker are separate processes that share one contract. They drifted
once -- the probe emitted ``not_applicable`` while Grace expected ``not_required``/
``unknown``, so a source that needs no macOS grant was read as a problem, and the web
layer filtered on a state no row ever emitted. Every shared vocabulary is asserted here so
the next drift fails a test instead of reaching Randy.

Both modules are stdlib-only and this file imports both; nothing is contacted.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from grace import contracts as C                     # noqa: E402
from grace import ingest as G                        # noqa: E402
from grace import web as W                           # noqa: E402
from switchboard_mini import outcomes as O           # noqa: E402
from switchboard_mini import version as V            # noqa: E402
from switchboard_mini import probe as P              # noqa: E402


class TestPermissionVocabularyIsOne(unittest.TestCase):
    def test_the_four_permission_states_are_identical_and_in_order(self) -> None:
        self.assertEqual(tuple(C.PERMISSION_STATES), tuple(O.PERMISSION_STATES))
        self.assertEqual(set(C.PERMISSION_STATES),
                         {"granted", "denied", "not_determined", "not_applicable"})

    def test_every_spelling_of_a_permission_state_is_the_same_string(self) -> None:
        pairs = (("PERMISSION_GRANTED",), ("PERMISSION_STATE_DENIED",),
                 ("PERMISSION_NOT_DETERMINED",), ("PERMISSION_NOT_APPLICABLE",))
        for (name,) in pairs:
            with self.subTest(name=name):
                self.assertEqual(getattr(C, name), getattr(O, name))

    def test_granted_and_not_applicable_are_the_only_ok_states(self) -> None:
        self.assertEqual(set(C.PERMISSION_OK_STATES),
                         {C.PERMISSION_GRANTED, C.PERMISSION_NOT_APPLICABLE})
        self.assertTrue(set(C.PERMISSION_OK_STATES) <= set(C.PERMISSION_STATES))

    def test_the_web_layer_turns_every_not_ok_permission_into_a_typed_condition(self) -> None:
        for permission in C.PERMISSION_STATES:
            if permission in C.PERMISSION_OK_STATES:
                self.assertNotIn(permission, W.PERMISSION_CONDITIONS)
                continue
            with self.subTest(permission=permission):
                condition = W.PERMISSION_CONDITIONS.get(permission)
                self.assertIsNotNone(condition, "a not-ok permission needs a condition")
                self.assertTrue(W.next_action_for(condition))


class TestAdapterOutcomeVocabularyIsOne(unittest.TestCase):
    def test_the_outcome_codes_are_identical_and_in_order(self) -> None:
        self.assertEqual(tuple(C.ADAPTER_OUTCOMES), tuple(O.ADAPTER_OUTCOMES))

    def test_every_outcome_code_is_the_same_string(self) -> None:
        for name in ("SUCCESS", "PARTIAL", "UNSUPPORTED", "PERMISSION_DENIED", "OFFLINE",
                     "RATE_LIMITED", "RETRYABLE_ERROR", "PERMANENT_ERROR",
                     "OUTCOME_UNKNOWN"):
            with self.subTest(name=name):
                self.assertEqual(getattr(C, name), getattr(O, name))

    def test_the_two_denied_ideas_are_not_the_same_word(self) -> None:
        """An outcome code (``permission_denied``) is not a permission state (``denied``)."""
        self.assertEqual(C.PERMISSION_DENIED, O.PERMISSION_DENIED)
        self.assertNotEqual(C.PERMISSION_DENIED, C.PERMISSION_STATE_DENIED)
        self.assertIn(C.PERMISSION_DENIED, C.ADAPTER_OUTCOMES)


class TestProbeRowVocabularyIsOne(unittest.TestCase):
    def test_unmeasured_and_not_observed_are_the_same_words(self) -> None:
        self.assertEqual(C.PROBE_UNMEASURED, O.PROBE_UNMEASURED)
        self.assertEqual(C.VERSION_NOT_OBSERVED, O.VERSION_NOT_OBSERVED)

    def test_the_worker_only_emits_states_grace_can_store(self) -> None:
        missing = set(O.PROBE_ROW_STATES) - set(C.PROBE_ROW_STATES)
        self.assertEqual(missing, set(),
                         f"the worker can emit states Grace's schema rejects: {missing}")

    def test_grace_adds_exactly_one_state_of_its_own(self) -> None:
        extra = set(C.PROBE_ROW_STATES) - set(O.PROBE_ROW_STATES)
        self.assertEqual(extra, {C.PROBE_HARNESS_ERROR})
        self.assertIn(C.PROBE_UNMEASURED, C.PROBE_ROW_STATES)

    def test_the_origins_are_the_same_three(self) -> None:
        self.assertEqual(set(C.PROBE_ROW_ORIGINS),
                         {C.REAL, "fixture", C.DOCUMENTATION})
        self.assertEqual(set(O.PROBE_ROW_ORIGINS),
                         {O.REAL, O.FIXTURE, O.DOCUMENTATION})
        self.assertEqual(C.DOCUMENTATION, O.DOCUMENTATION)

    def test_both_sides_refuse_a_documentation_row_that_claims_support(self) -> None:
        """The same rows, the same verdict -- the rule the write path enforces."""
        rows = (
            {"origin": C.DOCUMENTATION, "supported": True},
            {"origin": C.DOCUMENTATION, "supported": False},
            {"origin": C.REAL, "supported": True},
            {"origin": "fixture", "supported": True},
        )
        for row in rows:
            with self.subTest(row=row):
                self.assertEqual(C.probe_row_supported_claim_allowed(row),
                                 O.probe_row_supported_claim_allowed(row))

    def test_the_contract_version_is_the_same_on_both_sides(self) -> None:
        self.assertEqual(G.PROBE_CONTRACT_VERSION, V.PROBE_CONTRACT_VERSION)
        self.assertEqual(P.PROBE_CONTRACT_VERSION, V.PROBE_CONTRACT_VERSION)
        self.assertEqual(G.PROBE_CONTRACT_VERSION, P.PROBE_CONTRACT_VERSION)


class TestLabellingVocabulariesAreOne(unittest.TestCase):
    def test_the_documentation_label_prefix_is_one_string(self) -> None:
        self.assertEqual(C.DOCUMENTATION_LABEL_PREFIX, O.DOCUMENTATION_LABEL_PREFIX)

    def test_a_documentation_label_is_built_the_same_way_on_both_sides(self) -> None:
        self.assertEqual(C.documentation_label("hermes", ("O17",)),
                         O.documentation_label("hermes", ("O17",)))
        for builder in (C.documentation_label, O.documentation_label):
            label = builder("hermes", ("O17",))
            self.assertTrue(builder is not None and label.startswith("DOCUMENTATION:hermes"))
            self.assertIn("O17", label)

    def test_both_sides_recognise_a_documentation_label(self) -> None:
        label = O.documentation_label("hermes", ("O17",))
        self.assertTrue(C.is_documentation_label(label))
        self.assertTrue(O.is_documentation_label(label))
        self.assertFalse(O.is_documentation_label("FIXTURE:mail"))
        self.assertFalse(C.is_documentation_label("MOCK:mail"))

    def test_the_fixture_label_prefix_is_one_string(self) -> None:
        self.assertEqual(C.FIXTURE_LABEL_PREFIX, O.FIXTURE_LABEL_PREFIX)
        self.assertTrue(O.is_fixture_label(O.fixture_label("mail")))


class TestConditionVocabularyIsRenderable(unittest.TestCase):
    """Every state the web layer can show comes from ``next_action_for``'s vocabulary."""

    def test_the_condition_vocabulary_is_the_next_action_table(self) -> None:
        self.assertEqual(tuple(W.NEXT_ACTIONS), W.CONDITION_STATES)
        for state in W.CONDITION_STATES:
            with self.subTest(state=state):
                self.assertTrue(W.next_action_for(state))

    def test_every_permission_condition_is_in_the_vocabulary(self) -> None:
        for permission, condition in W.PERMISSION_CONDITIONS.items():
            with self.subTest(permission=permission):
                self.assertIn(condition, W.CONDITION_STATES)

    def test_the_states_the_source_layer_produces_are_all_renderable(self) -> None:
        for state in sorted(set(G.Ingest.TRANSPORT_STATES.values())):
            with self.subTest(state=state):
                self.assertIn(W.condition_state(state), W.CONDITION_STATES)


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
