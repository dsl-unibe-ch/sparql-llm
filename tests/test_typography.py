"""Typographic hyphens and no-break spaces from the model become plain characters.

gpt-oss writes "Perret-Gentil" with U+2011 (non-breaking hyphen) instead of "-". It looks
right, but a FILTER or a follow-up question built from it matches nothing in the data.
The agent-level tests are in test_typography_agent.py.
"""

from sparql_llm.utils import normalize_typography, normalize_typography_deep

NB_HYPHEN = "\u2011"


def test_hyphens_and_no_break_spaces_become_plain():
    text = f"Perret{NB_HYPHEN}Gentil, Charles\u00a0: 1835\u2010 01\u202f"
    assert normalize_typography(text) == "Perret-Gentil, Charles : 1835- 01 "


def test_length_is_preserved_for_stream_offsets():
    text = f"Jean{NB_HYPHEN}Louis\u00a0Andrié"
    assert len(normalize_typography(text)) == len(text)


def test_dashes_and_accents_are_left_alone():
    text = "Andrié \u2013 Pourtalès \u2014 1753\u20131795"
    assert normalize_typography(text) == text


def test_deep_normalizes_nested_strings_only():
    args = {"sparql_query": f'FILTER(CONTAINS(?n, "perret{NB_HYPHEN}gentil"))', "limit": 5, "tags": [f"a{NB_HYPHEN}b"]}
    assert normalize_typography_deep(args) == {
        "sparql_query": 'FILTER(CONTAINS(?n, "perret-gentil"))',
        "limit": 5,
        "tags": ["a-b"],
    }
