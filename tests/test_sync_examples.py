"""Offline tests for the curator example sync.

The curators own the example queries in their own repository; a rebuild pulls
them in. Everything here is the pure part of that: parsing what they wrote
(their layout and ours), repairing the two long-standing prefix typos, rendering
into the layout our Markdown loader expects, and splicing the result into our
examples file without disturbing the examples we curate ourselves.

Executing the queries needs the endpoint and is covered by scripts/check_examples.py.
"""

from sparql_llm.indexing.sync_examples import (
    BEGIN_MARKER,
    END_MARKER,
    Example,
    drop_already_curated,
    merge_into,
    normalise_query,
    parse_examples,
    render_examples,
)

OUR_LAYOUT = """# Heading

## Example 1: Count persons

Question: How many persons are there?
Alternative question: Combien de personnes ?

```sparql
SELECT (COUNT(?p) AS ?n) WHERE { ?p a crm:E21 }
```
"""

THEIR_LAYOUT = """Question: What are the dates of the study titles obtained by a person?

``` sparql
SELECT ?d WHERE { ?o sdh-short:P1 ?d }
```

Question: Who are the supervisors?

``` sparql
SELECT ?s WHERE { ?o sdh-slp:P11 ?s }
```
"""


def test_parses_our_layout_keeping_both_phrasings():
    examples = parse_examples(OUR_LAYOUT, source="ours.md")
    assert len(examples) == 1
    assert examples[0].title == "Count persons"
    assert examples[0].questions == ["How many persons are there?", "Combien de personnes ?"]
    assert "COUNT(?p)" in examples[0].query


def test_parses_the_curator_layout_that_has_no_headings():
    # Their file is a flat sequence of "Question:" + fenced block, with a space
    # after the backticks. It must still come through as separate examples.
    examples = parse_examples(THEIR_LAYOUT, source="theirs.md")
    assert len(examples) == 2
    assert examples[0].questions == ["What are the dates of the study titles obtained by a person?"]
    assert "sdh-short:P1" in examples[0].query
    assert examples[1].questions == ["Who are the supervisors?"]


def test_a_title_is_derived_when_the_curators_give_none():
    examples = parse_examples(THEIR_LAYOUT, source="theirs.md")
    assert examples[0].title
    assert "?" not in examples[0].title  # a title, not the raw question


def test_normalise_repairs_the_two_standing_prefix_typos():
    q = "PREFIX sdh-slc: <https://sdhss.org/ontology/social-life/>\nSELECT * { ?m sdh-so:P1 ?p }"
    fixed = normalise_query(q)
    assert "sdh-so:" not in fixed
    assert "social-life-core/" in fixed
    assert "ontology/social-life/" not in fixed


def test_normalise_leaves_a_correct_query_alone():
    q = "PREFIX sdh-slc: <https://sdhss.org/ontology/social-life-core/>\nSELECT * { ?m sdh-slc:P1 ?p }"
    assert normalise_query(q) == q


def test_rendered_output_can_be_parsed_back():
    # Round trip: what we write must be readable by the same parser the loader uses.
    examples = parse_examples(THEIR_LAYOUT, source="theirs.md")
    reparsed = parse_examples(render_examples(examples), source="rendered")
    assert [e.questions for e in reparsed] == [e.questions for e in examples]
    assert [e.query for e in reparsed] == [e.query for e in examples]


def test_merge_appends_a_marked_block_when_none_exists():
    merged = merge_into("# Ours\n\n## Example 1: Mine\n\nQuestion: q\n\n```sparql\nASK {}\n```\n", "SYNCED")
    assert BEGIN_MARKER in merged and END_MARKER in merged
    assert "## Example 1: Mine" in merged
    assert "SYNCED" in merged


def test_merge_replaces_the_previous_block_instead_of_stacking():
    once = merge_into("# Ours\n", "FIRST")
    twice = merge_into(once, "SECOND")
    assert twice.count(BEGIN_MARKER) == 1
    assert "FIRST" not in twice
    assert "SECOND" in twice


def test_merge_never_touches_our_own_examples():
    ours = "# Ours\n\n## Example 1: Mine\n\nQuestion: q\n\n```sparql\nASK {}\n```\n"
    twice = merge_into(merge_into(ours, "A"), "B")
    assert "## Example 1: Mine" in twice
    assert twice.index("## Example 1: Mine") < twice.index(BEGIN_MARKER)


CURATED = """# Ours

## Example 13: Dates of study titles

Question: What are the dates of the study titles obtained by a person?

```sparql
SELECT ?d WHERE { ?o sdh-short:P1 ?d }
```
"""


def _ex(question: str) -> Example:
    return Example(title="t", questions=[question], query="ASK {}", source="theirs.md")


def test_an_example_we_already_curate_is_not_synced_twice():
    kept, skipped = drop_already_curated(
        [_ex("What are the dates of the study titles obtained by a person?")], CURATED
    )
    assert kept == []
    assert len(skipped) == 1


def test_question_matching_tolerates_case_spacing_and_punctuation():
    kept, _ = drop_already_curated(
        [_ex("  what are the DATES of the study titles obtained by a person  ")], CURATED
    )
    assert kept == []


def test_a_question_we_do_not_have_is_kept():
    kept, skipped = drop_already_curated([_ex("Who founded the Rotary Club?")], CURATED)
    assert len(kept) == 1
    assert skipped == []


def test_the_previous_synced_block_does_not_count_as_curated():
    # Otherwise the second sync would drop every example the first one added.
    once = merge_into(CURATED, render_examples([_ex("Who founded the Rotary Club?")]))
    kept, _ = drop_already_curated([_ex("Who founded the Rotary Club?")], once)
    assert len(kept) == 1, "a re-sync must not treat its own previous output as ours"
