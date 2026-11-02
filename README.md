<div align="center">

# ✨ SPARQL query generation with LLMs 🦜

[![Tests](https://github.com/dsl-unibe-ch/sparql-llm/actions/workflows/test.yml/badge.svg)](https://github.com/dsl-unibe-ch/sparql-llm/actions/workflows/test.yml)
[![Docker image](https://github.com/dsl-unibe-ch/sparql-llm/actions/workflows/docker.yml/badge.svg)](https://github.com/dsl-unibe-ch/sparql-llm/actions/workflows/docker.yml)

</div>

A self-hostable chat assistant that turns natural-language questions into [SPARQL](https://www.w3.org/TR/sparql11-overview/) queries, runs them and explains the results. One instance serves one or more datasets, each backed by a SPARQL endpoint.

This is the [Data Science Lab (University of Bern)](https://www.dsl.unibe.ch/) fork of SIB's [sparql-llm](https://github.com/sib-swiss/sparql-llm). It runs the **Swiss Elites** assistant at **[swiss-elites.dsl.unibe.ch](https://swiss-elites.dsl.unibe.ch)**, over the [Elites suisses](https://elites-suisses.lod4hss.org) knowledge graph. The fork adds what curators need to run their own instance:

- **Datasets managed in the browser.** Curators add a dataset, upload its files, press **Check** and **Rebuild** on the `/admin` page. They never touch the code or the server.
- **Accounts.** Login, invitations and roles. The first administrator comes from `.env`.
- **Bring your own LLM key.** Each user can enter an OpenAI-compatible key and model, or the operator sets one for everyone. The app runs without an LLM and says so in the chat.
- **Docker image.** A single container with an embedded vector store, published to `ghcr.io/dsl-unibe-ch/sparql-llm`.
- **Crash-safe rebuilds.** A rebuild that is stopped or killed halfway changes nothing. One that had already started publishing finishes when the app starts again.
- **Outbound policy.** URLs that users or curators enter can't reach private, loopback or link-local addresses, even through a redirect or a DNS change. The operator can allow specific private addresses with `OUTBOUND_ALLOW`.
- **Health endpoints.** `/healthz` for liveness and `/readyz` for readiness, both without login.

The retrieval-augmented generation (RAG) and query validation come from upstream. The app indexes each endpoint's query examples, its [VoID](https://www.w3.org/TR/void/) schema (as ShEx shapes) and its ontology. It then validates each generated query against the schema and asks the LLM to fix any problems.

## 🐳 Self-hosting with Docker

Requirements: Docker with Compose, about 4 GB of RAM and 5 GB of disk. You don't need Python or Node.js.

```sh
cp .env.example .env      # fill in AUTH_SECRET, ADMIN_EMAIL, ADMIN_PASSWORD
docker compose up -d
```

Then open <http://localhost:8000> and log in as the administrator. The first start downloads an embedding model of about 2 GB, so allow up to 30 minutes.

- The app listens on `127.0.0.1:8000` only. Put a reverse proxy in front of it for public access.
- Data lives in the `sparql-llm-data` volume (`/app/data`) and the model in `sparql-llm-cache` (`/cache`).
- Images are published from `vX.Y.Z` tags. Pin one with `SPARQL_LLM_VERSION` in `.env`, or run `docker compose up -d --build` to build from source.

The guides:

- [`docs/DOCKER.md`](docs/DOCKER.md) is the step-by-step guide for curators. It covers users, LLM keys, what the app may connect to, the reverse proxy, backup and restore, and updating.
- [`docs/ONBOARDING.md`](docs/ONBOARDING.md) explains how to add a SPARQL endpoint: the files a dataset needs, writing examples, and Upload, Check and Rebuild.

## ⚙️ Configuration

Settings are read in this order, and the first one found wins:

1. Environment variables and `.env`. See [`.env.example`](.env.example) for the documented ones.
2. `<data_dir>/settings.runtime.json`, which the admin page writes when you save settings.
3. An install settings file named by `SETTINGS_FILEPATH`. One example is [`data/swiss-elites-settings.json`](data/swiss-elites-settings.json).
4. The defaults in [`src/sparql_llm/config.py`](src/sparql_llm/config.py).

The app refuses to start if a settings file is unreadable. Embeddings are computed locally with fastembed (`intfloat/multilingual-e5-large`). There is no default LLM: set `OPENAI_BASE_URL`, `OPENAI_API_KEY` and `DEFAULT_LLM_MODEL`, or let users bring their own key.

## 🔌 MCP server

Each published dataset is also served as a [Model Context Protocol](https://modelcontextprotocol.io/) server at `/mcp/<dataset-id>`. Bare `/mcp` serves the first dataset. Its tools retrieve relevant query examples and class schemas, and run a SPARQL query on the dataset's endpoint. To run the MCP server on its own during development, see [`CONTRIBUTING.md`](CONTRIBUTING.md).

## 🧑‍💻 Development

```sh
uv sync --extra agent
uv run --extra agent pytest
uv run --extra agent --env-file .env uvicorn src.sparql_llm.agent.main:app --port 8000 --log-config logging.yml --reload
```

The chat UI lives in `chat-with-context/`. Rebuild it with `npm i && npm run build:demo` after changing it. More detail is in [`CONTRIBUTING.md`](CONTRIBUTING.md), and [`system_architecture.md`](system_architecture.md) walks through a question end to end. That file was written in August 2026, before Docker and multi-dataset support, so some details there are out of date.

The agent pipeline is `extract → resolve_entities → retrieve → call_model → validate`. Its nodes are in `src/sparql_llm/agent/nodes/`. Indexing, publishing and crash recovery are in `src/sparql_llm/indexing/`.

## 📦️ Reusable components

These components come from upstream and work on their own. The PyPI package is SIB's release, not this fork.

### Installation

> Requires Python >=3.10

```bash
pip install sparql-llm
```

Or with `uv`:

```sh
uv add sparql-llm
```

### SPARQL query examples loader

Load SPARQL query examples defined using the SHACL ontology from a SPARQL endpoint. See **[github.com/sib-swiss/sparql-examples](https://github.com/sib-swiss/sparql-examples)** for more details on how to define the examples.

```python
from sparql_llm import SparqlExamplesLoader

loader = SparqlExamplesLoader("https://sparql.uniprot.org/sparql/")
docs = loader.load()
print(len(docs))
print(docs[0].metadata)
```

You can provide the examples as a file if it is not integrated in the endpoint, e.g.:

```python
loader = SparqlExamplesLoader("https://sparql.uniprot.org/sparql/", examples_file="uniprot_examples.ttl")
```

> Refer to the [LangChain documentation](https://python.langchain.com/v0.2/docs/) to figure out how to best integrate documents loaders to your system.

> [!NOTE]
>
> You can check the completeness of your examples against the endpoint schema using [this notebook](https://github.com/sib-swiss/sparql-llm/blob/main/notebooks/compare_queries_examples_to_void.ipynb).

### SPARQL endpoint schema loader

Generate a human-readable schema using the ShEx format to describe all classes of a SPARQL endpoint based on the [VoID description](https://www.w3.org/TR/void/) present in the endpoint. Ideally the endpoint should also contain the ontology describing the classes, so the `rdfs:label` and `rdfs:comment` of the classes can be used to generate embeddings and improve semantic matching.

> [!TIP]
>
> Checkout the **[void-generator](https://github.com/JervenBolleman/void-generator)** project to automatically generate VoID description for your endpoint.

```python
from sparql_llm import SparqlVoidShapesLoader

loader = SparqlVoidShapesLoader("https://sparql.uniprot.org/sparql/")
docs = loader.load()
print(len(docs))
print(docs[0].metadata)
```

You can provide the VoID description as a file if it is not integrated in the endpoint, e.g.:

```python
loader = SparqlVoidShapesLoader("https://sparql.uniprot.org/sparql/", void_file="uniprot_void.ttl")
```

> The generated shapes are well-suited for use with a LLM or a human, as they provide clear information about which predicates are available for a class, and the corresponding classes or datatypes those predicates point to. Each object property references a list of classes rather than another shape, making each shape self-contained and interpretable on its own, e.g. for a *Disease Annotation* in UniProt:
>
> ```turtle
> up:Disease_Annotation {
> a [ up:Disease_Annotation ] ;
> up:sequence [ up:Chain_Annotation up:Modified_Sequence ] ;
> rdfs:comment xsd:string ;
> up:disease IRI
> }
> ```

### Generate complete ShEx shapes from VoID description

You can also generate the complete ShEx shapes for a SPARQL endpoint with:

```python
from sparql_llm import get_shex_from_void

shex_str = get_shex_from_void("https://sparql.uniprot.org/sparql/")
print(shex_str)
```

### Validate a SPARQL query based on VoID description

This takes a SPARQL query and validates the predicates/types used are compliant with the VoID description present in the SPARQL endpoint the query is executed on.

This function supports:

* federated queries (VoID description will be automatically retrieved for each SERVICE call in the query),
* path patterns (e.g. `orth:organism/obo:RO_0002162/up:scientificName`)

This function requires that at least one type is defined for each endpoint, but it will be able to infer types of subjects that are connected to the subject for which the type is defined.

It will return a list of issues described in natural language, with hints on how to fix them (by listing the available classes/predicates), which can be passed to an LLM as context to help it figuring out how to fix the query.

```python
from sparql_llm import validate_sparql_with_void

sparql_query = """PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX up: <http://purl.uniprot.org/core/>
PREFIX taxon: <http://purl.uniprot.org/taxonomy/>
PREFIX orth: <http://purl.org/net/orth#>
PREFIX obo: <http://purl.obolibrary.org/obo/>
PREFIX lscr: <http://purl.org/lscr#>
PREFIX genex: <http://purl.org/genex#>
PREFIX sio: <http://semanticscience.org/resource/>
SELECT DISTINCT ?humanProtein ?orthologRatProtein ?orthologRatGene
WHERE {
    ?humanProtein a orth:Protein ;
        lscr:xrefUniprot <http://purl.uniprot.org/uniprot/Q9Y2T1> .
    ?orthologRatProtein a orth:Protein ;
        sio:SIO_010078 ?orthologRatGene ;
        orth:organism/obo:RO_0002162/up:name 'Rattus norvegicus' .
    ?cluster a orth:OrthologsCluster .
    ?cluster orth:hasHomologousMember ?node1 .
    ?cluster orth:hasHomologousMember ?node2 .
    ?node1 orth:hasHomologousMember* ?humanProtein .
    ?node2 orth:hasHomologousMember* ?orthologRatProtein .
    FILTER(?node1 != ?node2)
    SERVICE <https://www.bgee.org/sparql/> {
        ?orthologRatGene a orth:Gene ;
            genex:expressedIn ?anatEntity ;
            orth:organism ?ratOrganism .
        ?anatEntity rdfs:label 'brain' .
        ?ratOrganism obo:RO_0002162 taxon:10116 .
    }
}"""

issues = validate_sparql_with_void(sparql_query, "https://sparql.omabrowser.org/sparql/")
print("\n".join(issues))
```

## 🧑‍🏫 Upstream tutorial and benchmarks

SIB's step-by-step tutorial on building such a chat system is at https://sib-swiss.github.io/sparql-llm. Their benchmark scripts (`tests/benchmark*.py`, [`tests/text2sparql`](tests/text2sparql/README.md)) are kept from upstream. They target the SIB biodata endpoints and still assume upstream's separate Qdrant service.

## 🧑‍💻 Contributing

Checkout the [`CONTRIBUTING.md`](https://github.com/sib-swiss/sparql-llm/blob/main/CONTRIBUTING.md) page.

## 🪶 How to cite this work

If you reuse any part of this work, please cite at least one of our articles below:

- [SPARQL-LLM: Real-Time SPARQL Query Generation from Natural Language Questions](https://arxiv.org/abs/2512.14277)
```bibtex
@misc{smeros2025sparqlllmrealtimesparqlquery,
      title={SPARQL-LLM: Real-Time SPARQL Query Generation from Natural Language Questions},
      author={Panayiotis Smeros and Vincent Emonet and Ruijie Wang and Ana-Claudia Sima and Tarcisio Mendes de Farias},
      year={2025},
      eprint={2512.14277},
      archivePrefix={arXiv},
      primaryClass={cs.IR},
      url={https://arxiv.org/abs/2512.14277},
}
```

- [LLM-based SPARQL Query Generation from Natural Language over Federated Knowledge Graphs](https://ceur-ws.org/Vol-3953/355.pdf)
```bibtex
@conference{emonet2025llm,
    title={LLM-based SPARQL Query Generation from Natural Language over Federated Knowledge Graphs},
    author={Emonet, Vincent and Bolleman, Jerven and Duvaud, Severine and Mendes de Farias, Tarcisio and Sima, Ana Claudia},
    year = 2025,
    note={CEUR-WS.org, online \url{https://ceur-ws.org/Vol-3953/355.pdf}},
    booktitle = {ISWC 2024 Special Session on Harmonising Generative AI and Semantic Web Technologies, November 13, 2024, Baltimore, Maryland},
    volume = 3953,
    series = {CEUR Workshop Proceedings},
}
```

<!-- mcp-name: io.github.sib-swiss/sparql-llm -->
