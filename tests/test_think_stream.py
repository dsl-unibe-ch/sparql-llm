"""Offline tests for the streaming <think>-block stripper.

These exercise the pure helper ``strip_think_stream`` the way ``stream_response``
drives it: feed a growing buffer chunk-by-chunk and assemble only the newly
revealed delta. No network or model required.
"""

from sparql_llm.utils import strip_think_stream


def _replay(chunks: list[str]) -> str:
    """Replay token chunks through the stripper, returning the visible text.

    Mirrors the cursor logic in ``main.py:stream_response``.
    """
    buffer = ""
    emitted_len = 0
    out = ""
    for chunk in chunks:
        buffer += chunk
        visible = strip_think_stream(buffer)
        if len(visible) <= emitted_len:
            continue
        out += visible[emitted_len:]
        emitted_len = len(visible)
    return out


def test_opening_tag_glued_to_first_word_is_suppressed():
    # The exact failure mode: "<think>The" arrives in one chunk → must NOT leak.
    chunks = ["<think>The", " user wants a count", "</think>", "The answer is 42."]
    assert _replay(chunks) == "The answer is 42."


def test_tag_split_across_chunks():
    # Tags fragmented character-by-character across chunks.
    chunks = list("<think>reasoning</think>Hello world")
    assert _replay(chunks) == "Hello world"


def test_no_think_block_passes_through():
    chunks = ["Here ", "is ", "a plain answer."]
    assert _replay(chunks) == "Here is a plain answer."


def test_sparql_iri_and_less_than_are_not_held():
    # SPARQL content has lots of "<…>" IRIs and "<" operators — none of these
    # are <think> openers and must survive intact.
    answer = "SELECT * WHERE { ?s a <http://x/C> . FILTER(?n < 3) }"
    chunks = list(answer)  # worst case: one char per chunk
    assert _replay(chunks) == answer


def test_multiple_think_blocks():
    chunks = ["<think>a</think>", "first ", "<think>b</think>", "second"]
    assert _replay(chunks) == "first second"


def test_whole_response_is_reasoning_yields_nothing():
    chunks = ["<think>", "just thinking, no answer yet", "</think>"]
    assert _replay(chunks) == ""


def test_full_buffer_equivalence():
    # Replaying chunk-by-chunk must equal stripping the complete buffer at once.
    full = "<think>The user asks X</think>The result is **5**."
    assert _replay(list(full)) == strip_think_stream(full)
    assert strip_think_stream(full) == "The result is **5**."
