"""Loader for SPARQL examples stored as Markdown (natural-language question + ```sparql block).

Curators write example queries in plain Markdown rather than the SHACL `.ttl` format
that ``SparqlExamplesLoader`` consumes; this loader reads that convention. A project's
``prefix_repairs`` fix known prefix typos in those files before indexing.
"""

from __future__ import annotations

from pathlib import Path

from langchain_core.document_loaders.base import BaseLoader
from langchain_core.documents import Document
from rdflib.plugins.sparql import prepareQuery

from sparql_llm.utils import logger


class SparqlExamplesMdLoader(BaseLoader):
    """Load SPARQL examples from a Markdown file.

    Expected structure per example::

        ## Example N: <title>

        Question: <natural-language question>
        Alternative question: <optional alternate phrasing>

        ```sparql
        <SPARQL query>
        ```

    The curators' flat layout (``Question:`` lines and fenced queries, no headings) is read
    too: parsing is ``parse_examples``, the one the upload check and preflight use.

    Yields one Document per *question phrasing* (so a section with both a
    ``Question:`` and an ``Alternative question:`` line emits two Documents
    sharing the same SPARQL). The Document shape matches the one produced by
    ``SparqlExamplesLoader`` so the indexing pipeline can branch on file
    extension and otherwise stay unchanged.
    """

    def __init__(self, file_path: str | Path, endpoint_url: str, prefix_repairs: dict[str, str] | None = None):
        self.file_path = Path(file_path)
        self.endpoint_url = endpoint_url
        self.prefix_repairs = prefix_repairs or {}

    def load(self) -> list[Document]:
        if not self.file_path.exists():
            logger.warning(f"Examples file not found: {self.file_path}")
            return []

        # The same parser as the upload check and preflight: an example they accept is indexed.
        from sparql_llm.indexing.sync_examples import parse_examples

        text = self.file_path.read_text(encoding="utf-8")
        docs: list[Document] = []

        for example in parse_examples(text):
            title = example.title
            query = self._normalize_query(example.query)

            query_type = None
            try:
                query_type = prepareQuery(query).algebra.name
            except Exception as e:
                logger.warning(
                    f"Could not parse query in section '{title}' of {self.file_path}: {e}"
                )

            questions = [q.strip() for q in example.questions] or [title]

            for q in questions:
                docs.append(
                    Document(
                        page_content=q,
                        metadata={
                            "question": q,
                            "answer": query,
                            "endpoint_url": self.endpoint_url,
                            "query_type": query_type,
                            "doc_type": "SPARQL endpoints query examples",
                        },
                    )
                )

        logger.info(f"Loaded {len(docs)} example documents from {self.file_path}")
        return docs

    def _normalize_query(self, query: str) -> str:
        """Apply the project's prefix repairs before the query reaches the index."""
        for typo, correct in self.prefix_repairs.items():
            query = query.replace(typo, correct)
        return query
