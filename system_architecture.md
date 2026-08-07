# Swiss Elites Chat — System Architecture

This document describes how the service works from end to end, with a focus on
the language model: where its knowledge comes from, how documents are prepared
and embedded for retrieval, and how a question becomes a validated SPARQL query
and a final answer.

The service is a FastAPI application. A browser sends a question, the backend
runs a fixed LangGraph pipeline, and the answer is streamed back token by token.

Key facts about the running configuration:

- **Processing mode:** the steps of the pipeline run in a fixed order
  (`settings.use_tools = False`), rather than letting the model call tools
  freely.
- **Language model provider:** an OpenAI-compatible API (GPUStack). The default
  model at runtime is `minimax-m2.7`; several alternatives are selectable in the
  UI (`gpt-oss-120b`, `qwen3-coder-30b`, `qwen3-vl-30b`).
- **Vector store:** a local, on-disk Qdrant database with a single writer.
- **Embeddings:** the `intfloat/multilingual-e5-large` model (1024-dimensional
  vectors, cosine similarity), run locally through fastembed.

---

## 1. Overview

```
┌────────────────────────────────────────────────────────────────────────────────┐
│                           BROWSER (chat-with-context UI)                       │
│              user types question → POST /chat → SSE token stream               │
└──────────────────────────────────────┬─────────────────────────────────────────┘
                                       │ HTTPS
                                       ▼
┌────────────────────────────────────────────────────────────────────────────────┐
│  FastAPI app (src/sparql_llm/agent/main.py)                                    │
│                                                                                │
│  ┌──────────────────────────────────────────────────────────────────────────┐  │
│  │  LangGraph agent  (src/sparql_llm/agent/graph.py)                        │  │
│  │                                                                          │  │
│  │  __start__ ─► extract_user_question ─► retrieve ─► call_model            │  │
│  │                                                       │                  │  │
│  │                                                       ▼                  │  │
│  │                          ┌───────────────── validate_output              │  │
│  │                          │                          │                    │  │
│  │             (errors OR   │                (passed & │ executed)          │  │
│  │              0 results   │                          ▼                    │  │
│  │              OR HTTP err)│                       __end__                 │  │
│  │                          ▼                                               │  │
│  │                       call_model  (retry, max 3, then max_tries_reached) │  │
│  └──────────────────────────────────────────────────────────────────────────┘  │
└──────────┬─────────────────────────────────┬──────────────────────────────┬────┘
           │ embed & search                  │ chat completions             │ SPARQL GET
           ▼                                 ▼                              ▼
┌───────────────────────┐   ┌────────────────────────────────┐  ┌────────────────────────┐
│  Qdrant (on-disk)     │   │  LLM API (OpenAI-compatible)   │  │  WissKI SPARQL endpoint│
│  vector store         │   │  models:                       │  │  (read-only, GET)      │
│  collection:          │   │   - minimax-m2.7 (default)     │  │  named graph:          │
│    swiss-elites       │   │   - gpt-oss-120b               │  │   <…/resource/>        │
│  vectors: e5-large    │   │   - qwen3-coder-30b, qwen3-vl  │  │                        │
│  (1024-dim, cosine)   │   │                                │  │                        │
└───────────────────────┘   └────────────────────────────────┘  └────────────────────────┘
```

---

## 2. Where the model's knowledge comes from

At answer time, the model draws on several sources of information. Two of them
are fixed text strings (the prompts); the other three are dynamic, meaning they
are produced fresh for each question. The table below lists all of them.

| Source                              | Fixed / Dynamic | File on disk                                   | How it reaches the model            |
| ----------------------------------- | --------------- | ---------------------------------------------- | ----------------------------------- |
| System prompt (`RESOLUTION_PROMPT`) | Fixed           | `src/sparql_llm/agent/prompts.py:32`           | `system` role, on every call        |
| Extraction prompt                   | Fixed           | `src/sparql_llm/agent/prompts.py:13`           | `system` role, on the extraction call |
| SPARQL example patterns             | Dynamic (RAG)   | `data/elites-suisses-examples.md` → Qdrant     | added as an extra `user` message    |
| Schema (VoID + OWL + SHACL)         | Dynamic (RAG)   | `data/elites-suisses-void.ttl`, `data/ontology/{profiles,shacl}/` → Qdrant | added as an extra `user` message |
| Live query results                  | Dynamic (SPARQL)| WissKI endpoint                                | added by `validate_output`          |

The example and schema files are read **only when the search index is built**.
Changing them therefore requires rebuilding the index — either set
`force_index: true` in `data/swiss-elites-settings.json`, or delete
`data/vectordb/collection/swiss-elites/` and restart the service. The collection
name is set by `docs_collection_name = "swiss-elites"` in `config.py`.

---

## 3. Step by step: a single chat turn

```
                                              ┌─────────────────────────────┐
       ┌──────────────────────────┐            │ Config sources at boot:     │
[0]    │ User types a question    │            │  .env → SETTINGS_FILEPATH   │
       │ in the browser and hits  │            │  data/swiss-elites-         │
       │ Send.                    │            │       settings.json         │
       └──────────────┬───────────┘            │  src/sparql_llm/config.py   │
                      │                        └─────────────────────────────┘
                      ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ [1] FastAPI /chat endpoint (main.py)                                         │
│     - Cookie-based JWT auth (auth.py) → validates the user session           │
│     - Loads Configuration (system_prompt=RESOLUTION_PROMPT, model, etc.)     │
│     - Streams via SSE while the LangGraph runs                               │
└──────────────────────────────┬───────────────────────────────────────────────┘
                               ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ [2] Node: extract_user_question  (nodes/llm_extraction.py)                   │
│                                                                              │
│     LLM call #1  ──►  GPUStack /v1/chat/completions                          │
│     system = EXTRACTION_PROMPT                                               │
│     user   = <question>                                                      │
│                                                                              │
│     LLM returns JSON:                                                        │
│       intent            : "access_resources" | "general_information"         │
│       extracted_classes : ["crm:E21", "crm:E67", …]                          │
│       extracted_entities: ["Adolf Ogi", "Bern", …]                           │
│       question_steps    : ["…sub-question 1…", "…sub-question 2…", …]        │
│     Stored in state.structured_question.                                     │
└──────────────────────────────┬───────────────────────────────────────────────┘
                               ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ [3] Node: retrieve  (nodes/retrieval_docs.py)                                │
│                                                                              │
│     Embed each of [question, *question_steps] with                           │
│     intfloat/multilingual-e5-large and search the Qdrant collection          │
│     "swiss-elites". (K = 10 by default; halved when there are >3 queries.)   │
│                                                                              │
│     Round A: filter doc_type == "SPARQL endpoints query examples"            │
│             → returns Q&A pairs from                                          │
│               data/elites-suisses-examples.md                                │
│                                                                              │
│     Round B: filter doc_type != "…query examples"; searches with             │
│             [question, *question_steps, *extracted_classes]                  │
│             → returns ShEx class shapes derived from                         │
│               data/elites-suisses-void.ttl, enriched with                    │
│               data/ontology/profiles/*.rdf and                               │
│               data/ontology/shacl/*.ttl                                       │
│                                                                              │
│     Note: if intent == "general_information", Rounds A and B are skipped      │
│     and a single search runs on doc_type == "General information"            │
│     (the endpoint description) instead.                                       │
│                                                                              │
│     All hits are deduped, sorted by score, formatted, and appended to        │
│     state.messages as a single HumanMessage prefaced with:                   │
│       "The blocks below are REFERENCE EXAMPLES retrieved from a KB…"         │
└──────────────────────────────┬───────────────────────────────────────────────┘
                               ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ [4] Node: call_model  (nodes/call_model.py)                                  │
│                                                                              │
│     LLM call #2 ──► GPUStack /v1/chat/completions                            │
│                                                                              │
│     Prompt = [                                                               │
│       system: RESOLUTION_PROMPT                                              │
│         (endpoint URL, named graph, prefixes list, "one query only" rules)   │
│       user:   <original question>                                            │
│       user:   REFERENCE EXAMPLES bundle (examples + ShEx shapes)             │
│       user:   (on retry) validation errors / execution feedback              │
│     ]                                                                        │
│                                                                              │
│     Model returns markdown containing exactly one ```sparql``` fenced block. │
│     <think>…</think> blocks (minimax reasoning trace) are stripped and       │
│     surfaced as " Thought process" in the UI.                              │
└──────────────────────────────┬───────────────────────────────────────────────┘
                               ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ [5] Node: validate_output  (nodes/validation.py + validate_sparql.py)        │
│                                                                              │
│  5a. Extract SPARQL block(s) from the LLM answer.                            │
│  5b. rdflib parses it.                                                       │
│  5c. Auto-fix missing PREFIX declarations (silent, no retry) if that was     │
│      the only issue.                                                         │
│  5d. Validate each triple against the VoID summary                           │
│      (endpoints_metadata.void_dict, built from elites-suisses-void.ttl):     │
│         - Unknown class?                                                     │
│         - Unknown predicate on subject's class?                              │
│         - Wrong domain/range?                                                │
│      → collect errors                                                        │
│                                                                              │
│  5e. If errors → append recall message ("Fix the SPARQL query …") to        │
│      state.messages, try_count += 1, route back to [4].                     │
│                                                                              │
│  5f. If clean → execute the query directly against the WissKI endpoint:      │
│                                                                              │
│         GET https://swiss-elites.lod4hss.cloud/wisski/endpoint/              │
│                default_wisski_distillery_adapter?query=…                     │
│         Accept: application/sparql-results+json                              │
│                                                                              │
│      Branches on result:                                                     │
│         • 0 bindings   → append FIX_QUERY_PROMPT, retry (loop back to [4])   │
│         • HTTP/SPARQL error → append error + FIX_QUERY_PROMPT, retry         │
│         • ≥1 bindings  → append:                                             │
│              "Executed query on <ep>: <SPARQL> Results: <result JSON>        │
│               Now write the final answer to the user using ONLY these        │
│               results. Do NOT generate another SPARQL query."                │
│              (the result JSON is truncated when the result set is large)      │
│           and route back to [4] for the *synthesis* pass.                    │
└──────────────────────────────┬───────────────────────────────────────────────┘
                               ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ [6] Node: call_model (synthesis pass — LLM call #3, at minimum)              │
│                                                                              │
│     Same prompt template. Now the last user message contains the actual      │
│     query results as JSON, so the model writes natural-language prose        │
│     grounded in real bindings.                                               │
│                                                                              │
│     → validate_output sees no new SPARQL block → passed_validation=True      │
│     → graph exits to __end__.                                                │
└──────────────────────────────┬───────────────────────────────────────────────┘
                               ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ [7] Streaming back to the browser via SSE                                    │
│     - tokens from the final call_model                                       │
│     - stepwise " N documents used", " Refining query…", " SPARQL        │
│       executed" are streamed as StepOutput events for the UI to render as    │
│       collapsible cards.                                                     │
│                                                                              │
│     The full turn is logged to data/logs/user_questions.log.                 │
└──────────────────────────────────────────────────────────────────────────────┘
```

Retry limit: `Configuration.max_try_fix_sparql = 3`
(`src/sparql_llm/config.py:91,247`). Once this limit is exceeded, the
`max_tries_reached` node returns a polite fallback message instead of looping
further.

---

## 4. Indexing pipeline (runs once at startup when the collection is missing)

```
[data/elites-suisses-examples.md]     [data/elites-suisses-void.ttl]
                │                                    │
                ▼                                    ▼
┌────────────────────────────┐         ┌───────────────────────────────────┐
│ SparqlExamplesMdLoader     │         │ SparqlVoidShapesLoader            │
│ (loaders/…md_loader.py)    │         │ enriched with:                    │
│                            │         │  - parse_ontology_profiles()      │
│ For each ## Example block: │         │      data/ontology/profiles/*.rdf │
│   one Document per         │         │  - parse_shacl_shapes()           │
│   "Question:" or "Alt Q:"  │         │      data/ontology/shacl/*.ttl    │
│   page_content = question  │         │                                   │
│   metadata:                │         │ For each class in VoID:           │
│     answer = SPARQL query  │         │   Document(label)                 │
│     endpoint_url, type     │         │      page_content = class label   │
│     doc_type = "SPARQL     │         │   Document(comment) [if present]  │
│       endpoints query      │         │      page_content = definition    │
│       examples"            │         │   metadata:                       │
│                            │         │     answer = full ShEx shape      │
│ ── prefix typo fixes ──    │         │     iri, endpoint_url             │
│  sdh-so:      → sdh-short: │         │     doc_type = "SPARQL endpoints  │
│  social-life/ → …-core/    │         │       classes schema"             │
└─────────────┬──────────────┘         └────────────────┬──────────────────┘
              │                                         │
              └───────────────┬─────────────────────────┘
                              ▼
              ┌───────────────────────────────────┐
              │ SparqlInfoLoader                  │
              │   Document(endpoint summary)      │
              │   page_content = "What resources  │
              │      are available at …?"         │
              │   metadata:                       │
              │     answer = label:endpoint:desc  │
              │     doc_type = "General info…"    │
              └───────────────┬───────────────────┘
                              ▼
              ┌───────────────────────────────────┐
              │ For each Document:                │
              │   fastembed(intfloat/             │
              │     multilingual-e5-large)        │
              │     .embed([doc.page_content])    │
              │   qdrant.upsert(                  │
              │     vectors  = [embedding],       │
              │     payloads = [doc.metadata]     │
              │   )                               │
              └───────────────┬───────────────────┘
                              ▼
              ┌───────────────────────────────────┐
              │ Qdrant collection "swiss-elites"  │
              │ on-disk at data/vectordb/         │
              └───────────────────────────────────┘
```

**Only `page_content` is embedded.** The SPARQL query text, ShEx shapes, IRIs
and endpoint URLs are stored in the `payload` **without being embedded**. They
reach the model only after their document is returned by a similarity search.

---

## 5. How each document type is embedded

This section explains, per source, exactly what text becomes the embedding
vector (`page_content`) and what is carried alongside it unembedded (the
`payload`). Understanding this split is the key to the retrieval behaviour: the
system always matches the user's question against a short, natural-language
`page_content`, then hands the model the richer `answer` payload once a match is
found.

Every document has the same payload shape:

| Field         | Meaning                                                             |
| ------------- | ------------------------------------------------------------------- |
| `question`    | The natural-language text (equal to `page_content`) — the match key. |
| `answer`      | The payload actually shown to the model (a query, a shape, a description). |
| `doc_type`    | One of four values, used to filter searches (see below).            |
| `endpoint_url`| The SPARQL endpoint the document belongs to.                        |
| `iri`         | (schema docs) the class IRI, e.g. `crm:E21`.                         |

The four `doc_type` values and how each is built:

### 5.1 SPARQL query examples — `doc_type = "SPARQL endpoints query examples"`

Source: a Markdown file of worked examples, loaded by `SparqlExamplesMdLoader`.
Each `## Example` block contains a `Question:` line (optionally an
`Alternative question:` line) and one ```` ```sparql ```` block.

- **Embedded (`page_content`):** the natural-language question only. A block with
  both a main and an alternative phrasing produces **two** documents that share
  the same query, so either phrasing can match.
- **Payload (`answer`):** the SPARQL query text. Known prefix typos are repaired
  before indexing (`sdh-so:` → `sdh-short:`, and `…/social-life/` →
  `…/social-life-core/`), and the query is parsed with rdflib to record its
  `query_type` (SELECT, ASK, …).

The effect: the user's question is compared against *past questions*, and on a
hit the model receives a *query that is known to work* for a similar question.

### 5.2 Class schema shapes — `doc_type = "SPARQL endpoints classes schema"`

Source: the VoID description of the endpoint (`SparqlVoidShapesLoader`),
enriched with the local OWL application profiles and SHACL shapes.

For each class the loader builds a **ShEx shape** — a compact description of the
class and the predicates observed on it, e.g.:

```
shape:crm_E21 {
  a [ crm:E21 ] ;
  crm:P98i IRI ;
  sdh-short:P9 xsd:string ;
  ...
}
# Person (crm:E21): <definition text>
# Designed properties (SHACL profile):
#   crm:P98i "was born" -> crm:E67 [1..1]
```

Each class yields up to **two** documents, and it is the *human-readable* text
that gets embedded, not the shape:

- **Document A** — `page_content` = the class **label** (e.g. "Person"), falling
  back to the class IRI if no label exists.
- **Document B** (only if a definition exists) — `page_content` = the class
  **comment/definition** text.
- Both carry the **same** `answer` payload: the full ShEx shape.

Enrichment matters here because the live endpoint serves almost no labels or
comments. The OWL profiles supply the human labels and definitions, and the
SHACL shapes supply the designed property list and cardinalities; these are
merged into the shape so the model sees the intended schema, not just what the
raw data happens to contain.

### 5.3 Designed (not-yet-populated) classes — same `doc_type` as 5.2

Source: `OntologyProfilesLoader`. The database is still being filled, so some
classes described by the ontology are not present in the data yet. To let the
assistant reason about the full intended schema, these classes get their own
schema documents, built the same way as 5.2 (label document + optional
definition document, sharing a ShEx-style `answer`). Classes already covered by
the VoID shapes are skipped to avoid duplicates; as the data grows, a class
moves from here into the VoID-derived docs on the next re-index.

### 5.4 General information — `doc_type = "General information"`

Source: `SparqlInfoLoader`. A single document that summarises what the dataset
covers.

- **Embedded (`page_content`):** a meta-question such as *"Which resources are
  supported by this system?"*
- **Payload (`answer`):** a short prose description of the endpoint and its
  contents.

This document is only searched when the extraction step classifies the question
as `general_information` (see §3, step 3).

### 5.5 Turning documents into vectors

Once all loaders have run, `init_vectordb()` embeds and stores everything:

```
for each Document:
    vector  = e5-large.embed(doc.page_content)   # the question / label / definition
    payload = doc.metadata                        # answer, doc_type, iri, endpoint_url

qdrant.upsert(collection="swiss-elites", vectors=[...], payloads=[...])
```

Documents are embedded and upserted in batches. The collection is created with
1024-dimensional vectors and cosine distance, matching the e5-large model.

---

## 6. Runtime configuration flow at startup

```
Application starts
   │
   ▼
main.py imports config.py
   │
   ├─ load_dotenv()  reads environment (.env)
   │     └─ SETTINGS_FILEPATH → runtime settings JSON
   │     └─ LLM API base URL + API key
   │     └─ auth secret + initial admin credentials
   │
   ├─ Settings.from_file(<settings JSON>)
   │     └─ endpoints[0] = {
   │          endpoint_url    : <WissKI SPARQL endpoint>
   │          examples_file   : data/elites-suisses-examples.md
   │          void_file       : data/elites-suisses-void.ttl
   │          ontology_profiles_dir : data/ontology/profiles
   │          shacl_dir             : data/ontology/shacl
   │        }
   │     └─ default_llm_model = minimax-m2.7
   │     └─ docs_collection_name = "swiss-elites"
   │     └─ vectordb_url = local Qdrant path
   │     └─ auto_init = True, force_index = False
   │
   └─ mcp_server.setup()
         └─ if collection missing or force_index → init_vectordb()  [see §4]
         └─ else → skip and serve from existing vectors
```

Configuration is layered: environment variables (secrets, API endpoints) are
kept out of source control, while non-secret runtime settings (model list,
endpoint metadata, collection name) live in a settings JSON that overrides the
code defaults.

The retriever, the embedding model, the language-model client, and the SPARQL
executor are each created once at startup and reused across all requests.

---

## 7. Prompts sent to the model (per call)

### Extraction call (LLM call #1 per turn)
```
system : EXTRACTION_PROMPT (= INTRODUCTION_PROMPT + "Extract into JSON …")
user   : <original user question>
```

### Answering call (LLM call #2, and each retry / synthesis pass)
```
system : RESOLUTION_PROMPT
         └─ dataset intro
         └─ endpoint URL + primary named graph
         └─ curated prefix table (crm, sdh-slc, sdh-sls, sdh-short, swel, xsd)
         └─ output rules (one SPARQL block, DISTINCT, LIMIT 100, terse answers)

user   : <original user question>
user   : "The blocks below are REFERENCE EXAMPLES retrieved from a KB —
          they show query patterns that worked for similar questions…
          --- REFERENCE EXAMPLES ---
          <Question 1>:
          ```sparql
          #+ endpoint: …
          <example SPARQL 1>
          ```
          ---
          <Question 2>:
          ```sparql …```
          ---
          <Class label / definition>:
          ```shex
          <ShEx shape>
          ```
          …"
user   : (only on retry) "Fix the SPARQL query helping yourself with the
          error message and context …"  OR
         "Executed query on <ep>: <SPARQL> Results: <JSON of ≤50 bindings>
          Now write the final answer to the user using ONLY these results.
          Do NOT generate another SPARQL query."
```



## 8. Where the knowledge lives on disk

Paths below are relative to the project root.

```
├── data/
│   ├── <runtime settings>.json                # non-secret runtime overrides (endpoints, models, force_index)
│   ├── elites-suisses-examples.md             # SPARQL Q/A pairs (RAG source)
│   ├── elites-suisses-void.ttl                # VoID → class/predicate summary
│   ├── ontology/
│   │   ├── profiles/*.rdf                     # OWL application profiles (labels, comments)
│   │   └── shacl/*.ttl                        # SHACL shapes (cardinalities)
│   ├── vectordb/                              # local Qdrant, single-writer
│   │   └── collection/swiss-elites/…          # embedded documents + payloads
│   └── logs/                                  # per-turn question log
└── src/sparql_llm/
    ├── config.py                              # Settings + Configuration dataclasses
    ├── agent/
    │   ├── main.py                            # FastAPI app + SSE streaming
    │   ├── graph.py                           # LangGraph state machine
    │   ├── prompts.py                         # EXTRACTION, RESOLUTION, FIX_QUERY
    │   ├── state.py                           # State, InputState, StepOutput
    │   ├── auth.py                            # cookie JWT + SQLite users
    │   └── nodes/
    │       ├── llm_extraction.py              # step 2
    │       ├── retrieval_docs.py              # step 3
    │       ├── call_model.py                  # steps 4, 6
    │       └── validation.py                  # step 5
    ├── validate_sparql.py                     # rdflib + VoID validation
    ├── loaders/                               # feed the indexer
    │   ├── sparql_examples_md_loader.py       # SPARQL Q/A pairs from the .md file
    │   ├── sparql_examples_loader.py          # SPARQL Q/A pairs from a live endpoint
    │   ├── sparql_void_shapes_loader.py       # ShEx class shapes from VoID
    │   ├── ontology_profiles_loader.py        # OWL profile + SHACL parsing helpers
    │   └── sparql_info_loader.py              # endpoint description document
    ├── indexing/
    │   ├── index_resources.py                 # init_vectordb() — writes Qdrant
    │   └── index_entities.py                  # entity-resolution index (currently disabled)
    └── mcp_server.py                          # startup auto-init check
```
