"""The rebuild's external steps replaced by stubs that still write the file and the collection a crash test inspects.

Shared by ``test_crash_safety`` and ``crash_child``, which runs in its own process.
"""

from typing import Any, Callable

from qdrant_client import models

from sparql_llm.indexing.drift import Fingerprint
from sparql_llm.indexing.preflight import CheckResult


class FakeMetadata:
    prefixes_map: dict = {}

    def __init__(self, projects=None, auto_init=False):
        pass

    def refresh(self):
        pass

    def reload(self):
        pass


def install_stubs(setattr_fn: Callable[[Any, str, Any], None], rebuild: Any, version_text: str) -> None:
    """Make a rebuild publish ``version_text``: in the live void.ttl and in the one point of a real collection."""

    def generate_void(project):
        project.void_path.write_text(version_text, encoding="utf-8")
        return {"triples": 1, "entities": 1, "classes_total": 1, "classes_kept": 1, "predicates": 1}

    def init_vectordb(collection_name=None, projects=None, **kwargs):
        rebuild.qdrant_client.create_collection(
            collection_name=collection_name,
            vectors_config=models.VectorParams(size=4, distance=models.Distance.COSINE),
        )
        rebuild.qdrant_client.upsert(
            collection_name=collection_name,
            points=models.Batch(ids=[1], vectors=[[0.1, 0.2, 0.3, 0.4]], payloads=[{"version": version_text}]),
        )
        return 1

    setattr_fn(rebuild, "run_preflight", lambda project: [CheckResult("endpoint", "ok", "fine")])
    setattr_fn(
        rebuild, "sync_curator_examples", lambda project: {"skipped": True, "accepted": 0, "rejected": [], "error": ""}
    )
    setattr_fn(rebuild, "generate_void", generate_void)
    setattr_fn(rebuild, "EndpointsMetadataManager", FakeMetadata)
    setattr_fn(rebuild, "endpoints_metadata", FakeMetadata())
    setattr_fn(rebuild, "build_entity_names", lambda project: {"entities": 12, "error": ""})
    setattr_fn(rebuild, "take_fingerprint", lambda project: Fingerprint(triples=1))
    setattr_fn(rebuild, "init_vectordb", init_vectordb)
