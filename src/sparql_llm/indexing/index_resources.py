import time

import re

import httpx
from bs4 import BeautifulSoup
from sparql_llm.embeddings import create_embedding_model
from langchain_core.documents import Document
from qdrant_client import QdrantClient, models
from qdrant_client.http.models import Distance, VectorParams
from rdflib import RDF, Dataset, Namespace

from sparql_llm import (
    SparqlExamplesLoader,
    SparqlExamplesMdLoader,
    SparqlInfoLoader,
    SparqlVoidShapesLoader,
)
from sparql_llm.config import SparqlEndpointLinks, settings
from sparql_llm.loaders.ontology_profiles_loader import (
    CLASSES_DOC_TYPE,
    OntologyProfilesLoader,
    fetch_ontology_terms,
    fetch_shacl_shapes,
)
from sparql_llm.project import Project, get_projects
from sparql_llm.loaders.sparql_info_loader import GENERAL_INFO_DOC_TYPE
from sparql_llm.utils import EndpointsMetadataManager, get_prefix_converter

SCHEMA = Namespace("http://schema.org/")
#: A ``## heading`` and the text up to the next one.
_DATASET_SECTION = re.compile(r"^##\s+(.+?)\s*$\n(.*?)(?=^##\s|\Z)", re.MULTILINE | re.DOTALL)


# Global instance, metadata loads lazily on first property access
endpoints_metadata = EndpointsMetadataManager(auto_init=settings.auto_init)

# TODO: Getting `TypeError: cannot pickle '_thread.RLock' object` when doing `QdrantVectorStore.from_existing_collection(client=qdrant_client)`
qdrant_client = (
    QdrantClient(url=settings.vectordb_url, prefer_grpc=True, timeout=600)
    if settings.vectordb_url.startswith(("http", "https"))
    else QdrantClient(path=settings.vectordb_url)
)

embedding_model = create_embedding_model(
    backend=settings.embedding_backend,
    model=settings.embedding_model,
    dimensions=settings.embedding_dimensions,
    api_key=settings.embedding_api_key or None,
    base_url=settings.embedding_base_url or None,
)


def load_schemaorg_description(endpoint: SparqlEndpointLinks) -> list[Document]:
    """Extract datasets descriptions from the schema.org metadata in homepage of the endpoint"""
    docs = []
    homepage_url = endpoint.get("homepage_url")
    try:
        if homepage_url:
            resp = httpx.get(
                homepage_url,
                headers={
                    # "User-Agent": "BgeeBot/1.0",
                    # Adding a user-agent to make it look like we are a google bot to trigger SSR on Bgee
                    "User-Agent": "Mozilla/5.0 (Linux; Android 6.0.1; Nexus 5X Build/MMB29P) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/41.0.2272.96 Mobile Safari/537.36 (compatible; Googlebot/2.1; +http://www.google.com/bot.html) X-Middleton/1",
                },
                timeout=10,
                follow_redirects=True,
            )
            if resp.status_code != 200:
                raise Exception(f"Failed to fetch the webpage: {resp.status_code}")
            # print(resp.text)

            # Parse HTML and find the JSON-LD script tag
            soup = BeautifulSoup(resp.content, "html.parser")
            json_ld_tags = soup.find_all("script", type="application/ld+json")
            if not json_ld_tags:
                raise Exception("No JSON-LD script tags found")

            g = Dataset()
            for json_ld_tag in json_ld_tags:
                json_ld_content = str(json_ld_tag.string)
                # print(json_ld_content, type(json_ld_content))
                if json_ld_content:
                    # TODO: error here now, even if JSON-LD is valid
                    g.parse(data=json_ld_content, format="json-ld")
                    # json_ld_content = json.loads(json_ld_content)
                    question = f"What are the general metadata about {endpoint.get('label')} resource? (description, creators, maintainers, license, dates, version, etc)"
                    docs.append(
                        Document(
                            page_content=question,
                            metadata={
                                "question": question,
                                "answer": json_ld_content,
                                # "answer": f"```json\n{json_ld_content}\n```",
                                "iri": homepage_url,
                                "endpoint_url": endpoint["endpoint_url"],
                                "doc_type": GENERAL_INFO_DOC_TYPE,
                            },
                        )
                    )

            # Concat all schema:description of all classes in the graph
            descs = set()
            for s, _p, _o in g.triples((None, RDF.type, None)):
                for _sd, _pd, desc in g.triples((s, SCHEMA.description, None)):
                    descs.add(str(desc))

            if len(descs) == 0:
                raise Exception("No schema:description found in the JSON-LD script tag")
            question = f"What is the resource {endpoint.get('label')} about?"
            docs.append(
                Document(
                    page_content=question,
                    metadata={
                        "question": question,
                        "answer": "\n".join(descs),
                        "endpoint_url": endpoint["endpoint_url"],
                        "iri": homepage_url,
                        "doc_type": GENERAL_INFO_DOC_TYPE,
                    },
                )
            )
            # print("\n".join(descs))
    except Exception as e:
        print(f"Error while fetching schema.org metadata from {endpoint.get('label')}: {e}")
    return docs


def dataset_description_docs(project: Project) -> list[Document]:
    """One General-information doc per ``##`` section of the project's dataset.md."""
    if not project.dataset_path.is_file():
        return []
    text = project.dataset_path.read_text(encoding="utf-8")
    docs = []
    for match in _DATASET_SECTION.finditer(text):
        heading, body = match.group(1).strip(), match.group(2).strip()
        if not body:
            continue
        docs.append(
            Document(
                page_content=heading,
                metadata={
                    "question": heading,
                    "answer": body,
                    "endpoint_url": project.endpoint_url,
                    "iri": project.homepage_url,
                    "doc_type": GENERAL_INFO_DOC_TYPE,
                },
            )
        )
    return docs


def build_project_docs(project: Project) -> list[Document]:
    """Every retrieval doc for one project: examples, schema, optional ontology, descriptions."""
    endpoint_url = project.endpoint_url
    print(f"\n  🔎 Getting metadata for {project.label} at {endpoint_url}")
    docs: list[Document] = []

    examples_file = project.examples_path
    if examples_file.suffix == ".md":
        docs += SparqlExamplesMdLoader(examples_file, endpoint_url, prefix_repairs=project.prefix_repairs).load()
        # Examples the curators publish in their own repository, refreshed and
        # endpoint-tested by sync_examples at the start of every rebuild. A separate
        # gitignored file so a rebuild never dirties the server's working tree; absent
        # on a first run or when their repository was unreachable, which is not fatal.
        if project.synced_examples_path.exists():
            synced_docs = SparqlExamplesMdLoader(
                project.synced_examples_path, endpoint_url, prefix_repairs=project.prefix_repairs
            ).load()
            print(f"  📥 {len(synced_docs)} example doc(s) synced from the curators' repo")
            docs += synced_docs
    else:
        docs += SparqlExamplesLoader(endpoint_url, examples_file=str(examples_file)).load()

    # The OWL application profiles + SHACL shapes (the designed schema behind an SDHSS
    # mapping), served by the endpoint alongside the data. They supply the human
    # definitions and property cardinalities the VoID statistics don't carry. Optional.
    converter = get_prefix_converter(endpoints_metadata.prefixes_map)
    term_map: dict = {}
    shape_map: dict = {}
    if project.ontology_from_endpoint:
        term_map = fetch_ontology_terms(endpoint_url, converter)
        shape_map = fetch_shacl_shapes(endpoint_url, converter)

    void_docs: list[Document] = []
    if project.void_path.is_file():
        void_docs = SparqlVoidShapesLoader(
            endpoint_url,
            prefix_map=endpoints_metadata.prefixes_map,
            void_file=str(project.void_path),
            term_map=term_map,
            shape_map=shape_map,
            method=project.method,
        ).load()
    else:
        print(f"  ⚠️ No schema file at {project.void_path}; class docs skipped")
    docs += void_docs

    # Emit standalone docs for the designed classes not yet present in the data, so the
    # assistant already knows the schema the full database will instantiate. Skip the
    # classes the VoID already covers to avoid duplicate/competing docs.
    if term_map or shape_map:
        covered = {
            converter.compress(d.metadata["iri"], passthrough=True)
            for d in void_docs
            if d.metadata.get("doc_type") == CLASSES_DOC_TYPE and d.metadata.get("iri")
        }
        docs += OntologyProfilesLoader(
            endpoint_url,
            term_map=term_map,
            shape_map=shape_map,
            converter=converter,
            skip_iris=covered,
        ).load()

    docs += load_schemaorg_description(project.as_endpoint_links())
    docs += dataset_description_docs(project)
    return docs


def general_info_docs() -> list[Document]:
    """The 'which resources does this service cover' doc, linking to the project homepage."""
    projects = get_projects()
    homepage = settings.app_homepage_url or (projects[0].homepage_url if projects else None)
    return SparqlInfoLoader(
        [p.as_endpoint_links() for p in projects],
        source_iri=homepage or None,
        service_label=settings.app_name,
        org_label=f"from the {settings.app_org}" if settings.app_org else "",
    ).load()


def init_vectordb(collection_name: str | None = None) -> int:
    """Build the retrieval index from the SPARQL endpoints.

    Args:
        collection_name: target Qdrant collection. Defaults to ``settings.docs_collection_name``.
            Pass a versioned name to build a replacement index without touching the live one —
            see :func:`rebuild_index_with_alias`.

    Returns:
        The number of documents indexed.
    """
    target = collection_name or settings.docs_collection_name
    docs: list[Document] = []

    for project in get_projects():
        docs += build_project_docs(project)
    docs += general_info_docs()

    print(f"Generating embeddings for {len(docs)} documents")
    start_time = time.time()

    # Initialize the collection
    if qdrant_client.collection_exists(target):
        qdrant_client.delete_collection(target)
    qdrant_client.create_collection(
        collection_name=target,
        vectors_config=VectorParams(size=embedding_model.embedding_size, distance=Distance.COSINE),
    )

    # Generate embeddings with the fastembed `TextEmbedding` instance and upload directly to Qdrant in batches
    # https://qdrant.tech/documentation/fastembed/fastembed-rerankers/
    batch_size = 500
    total_docs = len(docs)
    for batch_start in range(0, total_docs, batch_size):
        batch_end = min(batch_start + batch_size, total_docs)
        batch_docs = docs[batch_start:batch_end]
        # Generate embeddings for this batch
        embeddings = embedding_model.embed([doc.page_content for doc in batch_docs])
        qdrant_client.upsert(
            collection_name=target,
            points=models.Batch(
                ids=list(range(batch_start + 1, batch_end + 1)),
                vectors=[emb.tolist() if hasattr(emb, "tolist") else emb for emb in embeddings],
                payloads=[doc.metadata for doc in batch_docs],
            ),
        )
        print(f"Indexed documents {batch_start + 1}-{batch_end}")

    print(
        f"Done generating and indexing {total_docs} documents into '{target}' in {time.time() - start_time} seconds"
    )
    return total_docs


if __name__ == "__main__":
    init_vectordb()
