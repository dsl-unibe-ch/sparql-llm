"""Offline tests for the streaming SPARQL-codeblock stripper.

In "natural language only" mode the visible answer must not contain SPARQL —
the query is still surfaced through the "Thought process" step and the
"open in editor" link. Prompting alone does not enforce this reliably (a 27B
model happily emits the codeblock anyway), so ``stream_response`` strips it
from the SSE stream.

These exercise the pure helper ``strip_sparql_stream`` the way
``stream_response`` drives it: feed a growing buffer chunk-by-chunk and
assemble only the newly revealed delta. No network or model required.
"""

from sparql_llm.utils import strip_sparql_stream, strip_think_stream


def _replay(chunks: list[str], strip=strip_sparql_stream) -> str:
    """Replay token chunks through a stripper, returning the visible text.

    Mirrors the cursor logic in ``main.py:stream_response``.
    """
    buffer = ""
    emitted_len = 0
    out = ""
    for chunk in chunks:
        buffer += chunk
        visible = strip(buffer)
        if len(visible) <= emitted_len:
            continue
        out += visible[emitted_len:]
        emitted_len = len(visible)
    return out


def test_sparql_codeblock_is_suppressed():
    # The exact failure mode: the model appends the query to its answer.
    chunks = [
        "Ernst Brenner's spouse was Lina Sturzenegger.\n\n",
        "```sparql\n",
        "SELECT ?spouse WHERE { ?m sdh-slc:P15 ?spouse }\n",
        "```",
    ]
    assert _replay(chunks) == "Ernst Brenner's spouse was Lina Sturzenegger.\n\n"


def test_fence_split_across_chunks():
    # Fence markers fragmented character-by-character across chunks.
    full = "Answer.\n```sparql\nSELECT * {}\n```\nDone."
    assert _replay(list(full)) == "Answer.\nDone."


def test_endpoint_comment_line_inside_block_never_leaks():
    # `#+ endpoint: <URL>` is the first line the prompt asks for — it is the
    # most recognisable piece of technical detail and must not reach the UI.
    full = "Here you go.\n```sparql\n#+ endpoint: https://example.org/sparql\nASK {}\n```"
    assert "endpoint" not in _replay(list(full))


def test_non_sparql_codeblock_passes_through():
    # Only SPARQL is hidden; a plain results table stays visible.
    full = "Results:\n```\nLina Sturzenegger\n```\nThat's it."
    assert _replay(list(full)) == full


def test_json_codeblock_passes_through():
    full = 'Config:\n```json\n{"a": 1}\n```\nend'
    assert _replay(list(full)) == full


def test_inline_backticks_pass_through():
    # NL answers legitimately mention CURIEs in inline code spans.
    full = "The class is `sdh-slc:C3` for marriages."
    assert _replay(list(full)) == full


def test_unclosed_sparql_block_is_held_back():
    # Still streaming: the block has not closed yet, nothing may leak.
    chunks = ["Answer.\n", "```sparql\n", "SELECT ?x WHERE {"]
    assert _replay(chunks) == "Answer.\n"


def test_uppercase_info_string_is_suppressed():
    full = "Answer.\n```SPARQL\nSELECT * {}\n```"
    assert _replay(list(full)) == "Answer.\n"


def test_multiple_sparql_blocks():
    full = "First.\n```sparql\nASK {}\n```\nSecond.\n```sparql\nASK {}\n```\nThird."
    assert _replay(list(full)) == "First.\nSecond.\nThird."


def test_no_codeblock_passes_through():
    chunks = ["Here ", "is ", "a plain answer."]
    assert _replay(chunks) == "Here is a plain answer."


def test_full_buffer_equivalence():
    # Replaying chunk-by-chunk must equal stripping the complete buffer at once.
    full = "Answer.\n```sparql\nSELECT * {}\n```\nFollow-up?"
    assert _replay(list(full)) == strip_sparql_stream(full)
    assert strip_sparql_stream(full) == "Answer.\nFollow-up?"


def test_composes_with_think_stripping():
    # Natural-language mode runs both strippers: reasoning is removed first,
    # then any SPARQL the model put in its visible answer.
    def both(buffer: str) -> str:
        return strip_sparql_stream(strip_think_stream(buffer))

    full = "<think>I should query marriages</think>Lina Sturzenegger.\n```sparql\nASK {}\n```\nMore?"
    assert _replay(list(full), strip=both) == "Lina Sturzenegger.\nMore?"


def test_visible_prefix_is_monotonic():
    # The cursor in stream_response never walks backwards, so the visible text
    # must only ever grow as the buffer grows.
    full = "A.\n```sparql\nASK {}\n```\nB.\n```json\n{}\n```\nC."
    buffer = ""
    previous = ""
    for char in full:
        buffer += char
        visible = strip_sparql_stream(buffer)
        assert visible.startswith(previous), f"visible shrank at {buffer!r}: {previous!r} -> {visible!r}"
        previous = visible
