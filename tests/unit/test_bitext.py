from eval.bitext import groups, split, summarize


def test_similarity_groups_stay_in_same_split():
    rows = [
        {"id": str(i), "en": f"request unique token{i}", "zh": "", "source_label": str(i)}
        for i in range(500)
    ]
    # Numbers normalize to ENTITY, so use genuinely distinct strings.
    for i, row in enumerate(rows):
        row["en"] = "request " + chr(97 + i // 26) + chr(97 + i % 26)
    result = split(rows)
    assert sum(r["split"] == "dev" for r in result) == 200
    dev = {r["group_id"] for r in result if r["split"] == "dev"}
    test = {r["group_id"] for r in result if r["split"] == "test"}
    assert not dev & test


def test_groups_join_exact_duplicates_even_with_conflicting_labels():
    rows = [
        {"en": "Where is my order 123?", "source_label": "a"},
        {"en": "where is my order 456", "source_label": "b"},
    ]
    assert len(groups(rows)) == 1


def test_report_counts_failed_requests_as_errors():
    rows = [
        {"gold": "refund", "prediction": "refund", "review_required": False},
        {"gold": "refund", "prediction": "ERROR", "review_required": True},
    ]
    report = summarize(rows)
    assert report["accuracy"] == 0.5
    assert report["failure_count"] == 1
    assert report["per_class"]["refund"]["precision"] == 1
    assert report["per_class"]["refund"]["recall"] == 0.5
    assert report["human_review_excluded"] == {"n": 1, "accuracy": 1}
