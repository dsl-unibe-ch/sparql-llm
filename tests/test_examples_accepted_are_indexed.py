"""Every example an upload accepts is an example the index gets, in either Markdown layout."""

from pathlib import Path

import pytest

from sparql_llm.indexing.sync_examples import parse_examples
from sparql_llm.loaders.sparql_examples_md_loader import SparqlExamplesMdLoader

SECTIONED = """# Examples

## Example 1: Persons

Question: Who are the persons?
Alternative question: List the persons.

```sparql
SELECT ?p WHERE { ?p a <https://e.example/Person> }
```

## Example 2: Count

Question: How many persons?

```sparql
SELECT (COUNT(?p) AS ?n) WHERE { ?p a <https://e.example/Person> }
```
"""

# The curators' layout: no headings, a bare Question line before each fenced query.
FLAT = """Question: Who are the persons?

```sparql
SELECT ?p WHERE { ?p a <https://e.example/Person> }
```

Question: How many persons?

```sparql
SELECT (COUNT(?p) AS ?n) WHERE { ?p a <https://e.example/Person> }
```
"""


@pytest.mark.parametrize("text", [SECTIONED, FLAT], ids=["sectioned", "flat"])
def test_the_index_gets_every_accepted_question(tmp_path: Path, text: str):
    path = tmp_path / "examples.md"
    path.write_text(text, encoding="utf-8")
    accepted = parse_examples(text)
    docs = SparqlExamplesMdLoader(path, "https://e.example/sparql").load()

    assert accepted
    assert sorted(d.page_content for d in docs) == sorted(q for e in accepted for q in e.questions)
    assert {d.metadata["answer"] for d in docs} == {e.query for e in accepted}
