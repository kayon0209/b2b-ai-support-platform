"""Unit tests: knowledge gap classification and question normalisation.

Two decisions here are load-bearing and easy to get wrong in opposite
directions:

1. Normalisation must fold the *accidental* differences between two asks of
   the same question (case, surrounding whitespace, a leading greeting, a
   trailing "?"). If it folds too little, the same question produces a new
   queue row every time and the frequency signal - which is the entire
   ordering of the queue - is lost.

2. It must NOT fold the *meaningful* differences. "How do I export?" and
   "How do I import?" are different questions; stemming or token-dropping
   would merge them and a reviewer would write documentation for the wrong
   one. This is the failure mode that makes people distrust the queue, so it
   is tested explicitly rather than assumed.
"""

from platform_core.knowledge.gap_models import (
    GAP_REASON_CODES,
    is_knowledge_gap,
    normalize_question,
    question_hash,
)


class TestNormalizationFoldsNoise:
    def test_case_and_whitespace_fold_together(self) -> None:
        assert normalize_question("How do I reset my password?") == normalize_question(
            "  how do i  RESET   my password  "
        )

    def test_trailing_punctuation_folds(self) -> None:
        base = normalize_question("How do I reset my password")
        for variant in (
            "How do I reset my password?",
            "How do I reset my password!",
            "How do I reset my password...",
            "How do I reset my password??",
        ):
            assert normalize_question(variant) == base

    def test_leading_greeting_folds(self) -> None:
        base = normalize_question("How do I reset my password")
        for variant in (
            "Hi, how do I reset my password",
            "Hello how do I reset my password",
            "Hey, how do I reset my password",
            "Hi there, how do I reset my password",
        ):
            assert normalize_question(variant) == base

    def test_greeting_only_mid_sentence_is_preserved(self) -> None:
        # A greeting word that carries meaning must not be stripped. Without
        # this guard the fold rule would eat real words.
        assert "hi" in normalize_question("Can I say hi to my account manager")

    def test_internal_whitespace_collapses(self) -> None:
        assert normalize_question("reset    my\npassword") == "reset my password"


class TestNormalizationPreservesMeaning:
    def test_export_is_not_import(self) -> None:
        # The exact merge a stemmer would make, and the reason we do not stem.
        assert normalize_question("How do I export data?") != normalize_question(
            "How do I import data?"
        )

    def test_negation_is_not_dropped(self) -> None:
        assert normalize_question("How do I enable SSO?") != normalize_question(
            "How do I disable SSO?"
        )

    def test_word_order_is_not_rearranged(self) -> None:
        assert normalize_question("reset password admin") != normalize_question(
            "admin reset password"
        )

    def test_numbers_are_preserved(self) -> None:
        assert normalize_question("What changed in version 2?") != normalize_question(
            "What changed in version 3?"
        )


class TestQuestionHash:
    def test_hash_is_stable_across_noise(self) -> None:
        assert question_hash("How do I reset my password?") == question_hash(
            "hi, HOW do i reset my password"
        )

    def test_hash_differs_for_different_questions(self) -> None:
        assert question_hash("How do I export data?") != question_hash("How do I import data?")

    def test_empty_question_hashes_to_empty(self) -> None:
        # Returning "" rather than a hash of "" matters: `record_gap` treats a
        # falsy hash as "nothing to record", so an empty question cannot
        # create a queue row that no reviewer could ever act on.
        assert question_hash("") == ""
        assert question_hash("   ") == ""
        assert question_hash("!!!") == ""


class TestReasonClassification:
    def test_knowledge_gaps_are_recognized(self) -> None:
        assert GAP_REASON_CODES, "the gap vocabulary must not be empty"
        for code in GAP_REASON_CODES:
            assert is_knowledge_gap(code), f"{code} should be a knowledge gap"

    def test_non_gap_reasons_are_rejected(self) -> None:
        # A restricted request or a policy refusal is not a knowledge gap:
        # documenting it would be the wrong fix, and queueing it would train
        # reviewers to skim.
        for code in (
            "POLICY_DENIED",
            "RESTRICTED_REQUEST",
            "UNAUTHORIZED_ACTION",
            "",
            "no_such_reason",
        ):
            assert not is_knowledge_gap(code), f"{code} must not be a knowledge gap"
