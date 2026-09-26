"""Feature list 3.9: similar questions cluster into one topic.

The assertions that matter:

- **Different wording, same topic, one cluster.** "能不能加急" and "加急打样多久"
  are the case that motivated this - `question_hash` gives them two rows, and
  the whole point of clustering is that an owner sees one demand.
- **Unrelated questions stay apart.** A clusterer that merges everything is
  worse than none: it turns a prioritised queue into a single blob, so the
  negative case is asserted as firmly as the positive one.
- **Deterministic and ordered by demand**, because the output is a work queue
  that someone reads on two different days.
"""

from __future__ import annotations

from platform_core.knowledge.gap_clusters import (
    DEFAULT_SIMILARITY_THRESHOLD,
    cluster_questions,
)


def test_the_same_question_asked_differently_is_one_cluster() -> None:
    """The motivating case: 加急 asked two ways is one demand, not two."""
    clusters = cluster_questions([("能不能加急", 4), ("加急打样多久", 3)])
    assert len(clusters) == 1
    assert clusters[0].size == 2
    assert clusters[0].total_frequency == 7


def test_unrelated_questions_do_not_merge() -> None:
    clusters = cluster_questions([("怎么重置登录密码", 5), ("PCB 打样的交期是多久", 2)])
    assert len(clusters) == 2
    assert all(cluster.size == 1 for cluster in clusters)


def test_a_cluster_is_represented_by_its_most_demanded_question() -> None:
    clusters = cluster_questions([("加急打样多久", 2), ("能不能加急", 9)])
    assert clusters[0].representative == "能不能加急"
    assert clusters[0].total_frequency == 11


def test_clusters_are_ordered_by_total_demand() -> None:
    clusters = cluster_questions([("怎么重置登录密码", 1), ("能不能加急", 8), ("加急打样多久", 6)])
    assert [cluster.total_frequency for cluster in clusters] == [14, 1]


def test_the_shared_terms_are_reported_as_evidence() -> None:
    """A cluster nobody can verify is a cluster nobody will trust."""
    clusters = cluster_questions([("能不能加急", 4), ("加急打样多久", 3)])
    assert clusters[0].shared_terms


def test_clustering_is_deterministic() -> None:
    questions = [("能不能加急", 4), ("加急打样多久", 3), ("怎么重置登录密码", 5)]
    assert cluster_questions(questions) == cluster_questions(questions)


def test_latin_questions_cluster_on_shared_vocabulary() -> None:
    clusters = cluster_questions(
        [("How do I reset my password", 3), ("password reset procedure", 2)]
    )
    assert len(clusters) == 1


def test_empty_input_is_empty_output() -> None:
    assert cluster_questions([]) == []


def test_a_single_question_forms_one_cluster() -> None:
    clusters = cluster_questions([("only one question", 1)])
    assert len(clusters) == 1
    assert clusters[0].size == 1


def test_the_threshold_is_exposed_rather_than_hardcoded() -> None:
    """Operators tune this per tenant; a buried constant cannot be tuned."""
    assert isinstance(DEFAULT_SIMILARITY_THRESHOLD, float)
    questions = [("能不能加急", 4), ("加急打样多久", 3)]
    # An impossible threshold must separate what the default joins.
    assert len(cluster_questions(questions, threshold=0.99)) == 2
