"""Loader for SPARQL examples stored as Markdown (natural-language question + ```sparql block).

Stopgap for the Elites Suisses dataset, whose
``elites_suisses_data/llm_documentation/SPARQL_queries_examples/query_examples.md``
follows a plain-Markdown convention rather than the SHACL `.ttl` format that
``SparqlExamplesLoader`` consumes. Once LESSH migrates to SHACL, swap usage for
the upstream loader.
"""

from __future__ import annotations

import re
from pathlib import Path

from langchain_core.document_loaders.base import BaseLoader
from langchain_core.documents import Document
from rdflib.plugins.sparql import prepareQuery

from sparql_llm.utils import logger

# Verified live against https://swiss-elites.lod4hss.cloud/wisski/endpoint/default
# on 2026-05-18. Two known typos in the upstream query_examples.md:
#   1. `sdh-so:` is undefined — should be `sdh-short:`. The endpoint returns
#      `400 MALFORMED QUERY: QName 'sdh-so:P1' uses an undefined prefix`.
#   2. The `sdh-slc:` prefix is declared as `.../social-life/` but the actual
#      namespace is `.../social-life-core/`. Class IRIs like `sdh-slc:C11` would
#      otherwise resolve to a non-existent URI and silently match nothing.
# Both are simple substring replacements (no clash with `social-life-core/`
# since "social-life/" is not a substring of "social-life-core/"). Remove once
# LESSH patches the source.
PREFIX_TYPO_FIXES: dict[str, str] = {
    "sdh-so:": "sdh-short:",
    "https://sdhss.org/ontology/social-life/": "https://sdhss.org/ontology/social-life-core/",
}

_SECTION_PATTERN = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_SPARQL_BLOCK_PATTERN = re.compile(r"```\s*sparql\s*\n(.*?)```", re.DOTALL)
_QUESTION_PATTERN = re.compile(
    r"^(?:Question|Alternative question):\s*(.+?)\s*$", re.MULTILINE
)


class SparqlExamplesMdLoader(BaseLoader):
    """Load SPARQL examples from a Markdown file.

    Expected structure per example::

        ## Example N: <title>

        Question: <natural-language question>
        Alternative question: <optional alternate phrasing>

        ```sparql
        <SPARQL query>
        ```

    Yields one Document per *question phrasing* (so a section with both a
    ``Question:`` and an ``Alternative question:`` line emits two Documents
    sharing the same SPARQL). The Document shape matches the one produced by
    ``SparqlExamplesLoader`` so the indexing pipeline can branch on file
    extension and otherwise stay unchanged.
    """

    def __init__(self, file_path: str | Path, endpoint_url: str):
        self.file_path = Path(file_path)
        self.endpoint_url = endpoint_url

    def load(self) -> list[Document]:
        if not self.file_path.exists():
            logger.warning(f"Examples file not found: {self.file_path}")
            return []

        text = self.file_path.read_text(encoding="utf-8")
        sections = self._split_sections(text)
        docs: list[Document] = []

        for section in sections:
            title_match = _SECTION_PATTERN.match(section)
            title = title_match.group(1).strip() if title_match else ""

            sparql_match = _SPARQL_BLOCK_PATTERN.search(section)
            if not sparql_match:
                continue
            query = self._normalize_query(sparql_match.group(1).strip())

            query_type = None
            try:
                query_type = prepareQuery(query).algebra.name
            except Exception as e:
                logger.warning(
                    f"Could not parse query in section '{title}' of {self.file_path}: {e}"
                )

            questions = [m.group(1).strip() for m in _QUESTION_PATTERN.finditer(section)]
            if not questions:
                questions = [title]

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

    @staticmethod
    def _split_sections(text: str) -> list[str]:
        """Split the document into level-2 (``##``) sections."""
        starts = [m.start() for m in _SECTION_PATTERN.finditer(text)]
        if not starts:
            return []
        starts.append(len(text))
        return [text[starts[i] : starts[i + 1]] for i in range(len(starts) - 1)]

    @staticmethod
    def _normalize_query(query: str) -> str:
        """Apply known prefix-typo fixes before the query reaches the index."""
        for typo, correct in PREFIX_TYPO_FIXES.items():
            query = query.replace(typo, correct)
        return query
