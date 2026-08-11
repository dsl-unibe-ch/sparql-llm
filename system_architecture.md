# Swiss Elites Chat — System Architecture

This document explains how the Swiss Elites chatbot works, from the moment a
user types a question to the moment they get an answer.

**Read this first if you're new:** start with the "Plain-English overview" and
the "Glossary" below. The later sections (with diagrams and code) are the
detailed reference for developers.

The chatbot runs as a background service on the server `vm7.dsl.unibe.ch`. It is
started with this command:
`/opt/swiss-elites/uv-env/bin/uv run uvicorn sparql_llm.agent.main:app --host 127.0.0.1 --port 8000`

Key ingredients (explained in the Glossary):
- **The AI model (LLM):** hosted on **GPUStack**, default model `minimax-m2.7`.
- **The search index (Qdrant):** a local database that finds relevant help text
  by *meaning*, not just keywords. Lives in `data/vectordb/`.
- **The data source (SPARQL endpoint):** the actual Swiss Elites knowledge graph
  the chatbot queries to get real answers.

---

## 0. Plain-English overview

Think of the chatbot as a **research assistant who doesn't memorise facts** but
knows how to look them up. When you ask a question, here's what happens behind
the scenes:

1. **Understand the question.** The AI first reads your question and figures out
   what you're really asking — what kind of thing you want (people? birthdates?
   marriages?) and any names you mentioned. It also splits a complex question
   into smaller steps.

2. **Find helpful notes.** The assistant flips through a "cookbook" of past
   example questions and their working database queries, plus a "map" of what
   the data contains. It pulls out the few pages most similar to your question.
   (This is the search index / Qdrant.)

3. **Write a database query.** Using those notes as a guide, the AI writes one
   **SPARQL query** — the special language used to ask questions to the Swiss
   Elites knowledge graph.

4. **Check the query before running it.** The system inspects the query for
   mistakes (wrong names, missing declarations). If something's wrong, it hands
   the error back to the AI and asks it to fix it. It will retry up to 3 times.

5. **Run the query for real.** Once the query looks valid, the system sends it
   to the knowledge graph and gets back raw data.

6. **Explain the results.** Finally, the AI turns that raw data into a plain
   sentence and streams the answer back to your screen, word by word.

The important idea: **the AI never invents the facts.** It only writes a query,
the system runs it against the real database, and the AI explains whatever the
database returns. That's why the answers are grounded in real data.

---

## Glossary — decoding the jargon

| Term | What it means in plain words |
| ---- | ---------------------------- |
| **LLM** | The AI language model that reads and writes text (here, `minimax-m2.7`). |
| **SPARQL** | The query language used to ask questions to the knowledge graph, similar to how SQL queries a normal database. |
| **Knowledge graph / SPARQL endpoint** | The actual Swiss Elites database of people, births, marriages, etc. that answers SPARQL queries. |
| **Embedding** | Turning a piece of text into a list of numbers that captures its *meaning*, so the computer can measure how similar two texts are. |
| **Vector** | The list of numbers produced by an embedding. |
| **Qdrant** | The search database that stores these vectors and finds the closest matches to a question. Also called the "vector store" or "search index". |
| **RAG (Retrieval-Augmented Generation)** | The technique of first *finding* relevant reference material, then giving it to the AI so its answer is grounded in real examples instead of guesses. |
| **Prompt** | The instructions and context given to the AI for one request. |
| **System prompt** | The fixed instructions that set the AI's role and rules (e.g. "you write SPARQL for the Swiss Elites data"). |
| **Node / LangGraph** | The chatbot's workflow is a series of steps ("nodes"). LangGraph is the tool that wires those steps together and lets them loop (e.g. retry a failed query). |
| **Payload / metadata** | Extra information stored *alongside* a vector (like the actual SPARQL query) that is looked up but not used for the meaning-based search. |
| **VoID / ShEx / SHACL / OWL** | Technical formats that describe the *shape* of the data — which classes and properties exist. Used to teach the AI the data's structure. |
| **Indexing** | The one-time job (at startup) that reads the example and schema files and loads them into Qdrant so they can be searched. |
| **Streaming / SSE** | Sending the answer to the browser gradually (word by word) instead of waiting for the whole thing. |

---

## 1. Big picture

```
┌────────────────────────────────────────────────────────────────────────────────┐
│                           BROWSER (chat-with-context UI)                       │
│              user types question → POST /chat → SSE token stream               │
└──────────────────────────────────────┬─────────────────────────────────────────┘
                                       │ HTTPS
                                       ▼
┌────────────────────────────────────────────────────────────────────────────────┐
│  systemd unit: swiss-elites.service                                            │
│  Uvicorn @ 127.0.0.1:8000  ── FastAPI app (src/sparql_llm/agent/main.py)       │
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
│  Qdrant (on-disk)     │   │  GPUStack /v1/chat/completions │  │  WissKI SPARQL endpoint│
│  data/vectordb/       │   │  (OpenAI-compatible)           │  │  swiss-elites.lod4hss  │
│  collection:          │   │  models:                       │  │  ...cloud/wisski/…     │
│    swiss-elites       │   │   - minimax-m2.7 (default)     │  │  named graph:          │
│  vectors: e5-large    │   │   - gpt-oss-120b               │  │   <…/resource/>        │
│  (1024-dim, cosine)   │   │   - qwen3-coder-30b, qwen3-vl  │  │                        │
└───────────────────────┘   └────────────────────────────────┘  └────────────────────────┘
```

---

## 2. Where the LLM's knowledge comes from

The AI pulls from **four** kinds of knowledge when answering. Two are fixed
instructions written into the code; two are looked up fresh for every question.

| Source | Fixed or looked-up? | File on disk | How it reaches the AI |
| ------ | ------------------- | ------------ | --------------------- |
| System prompt (`RESOLUTION_PROMPT`)| Fixed            | `src/sparql_llm/agent/prompts.py:32`           | Always, as the top instruction |
| Extraction prompt                  | Fixed            | `src/sparql_llm/agent/prompts.py:13`           | Always, during the "understand the question" step |
| Example SPARQL queries             | Looked up (search)| `data/elites-suisses-examples.md` → Qdrant     | Added as an extra message |
| Data schema (VoID + OWL + SHACL)   | Looked up (search)| `data/elites-suisses-void.ttl`, `data/ontology/{profiles,shacl}/` → Qdrant | Added as an extra message |
| Live query results                 | Looked up (query) | The knowledge graph itself                     | Added after the query runs |

**Important:** the example and schema files are only read once, when the search
index is built at startup. If you edit them, the AI won't see the changes until
you rebuild the index — either set `force_index: true` in
`data/swiss-elites-settings.json`, or delete the folder
`data/vectordb/collection/swiss-elites/` and restart the service.

---

## 3. Step-by-step: one chat turn

The diagram below traces one question through the whole system. Each numbered
box is one step. Steps [4] and [5] can loop: if the query is wrong or returns
nothing, the system loops back and asks the AI to try again (up to 3 times).

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
│     - JWT auth (auth.py) → validates user session                            │
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
│     For each of [question, *question_steps]:                                 │
│        embed with intfloat/multilingual-e5-large                             │
│        → Qdrant search on collection "swiss-elites"                          │
│                                                                              │
│     Round A (top-K, K=10): filter doc_type == "SPARQL endpoints query        │
│                            examples"                                         │
│                            → returns Q&A pairs from                          │
│                              data/elites-suisses-examples.md                 │
│                                                                              │
│     Round B (top-K, K=10): filter doc_type != "…query examples"              │
│                            queries += extracted_classes                      │
│                            → returns ShEx class shapes derived from          │
│                              data/elites-suisses-void.ttl enriched with      │
│                              data/ontology/profiles/*.rdf and                │
│                              data/ontology/shacl/*.ttl                       │
│                                                                              │
│     If intent == "general_information": one round on doc_type ==             │
│     "General information" (endpoint description).                            │
│                                                                              │
│     All hits are combined, duplicates removed, sorted best-first, and added   │
│     to the conversation as one message that starts with a note telling the    │
│     AI: "these are REFERENCE EXAMPLES to learn from, not new questions to      │
│     answer."                                                                  │
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
│     surfaced as "💭 Thought process" in the UI.                              │
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
│              "Executed query on <ep>: <SPARQL> Results: <JSON of ≤50>        │
│               Now write the final answer to the user using ONLY these        │
│               results. Do NOT generate another SPARQL query."                │
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
│     - stepwise "📚️ N documents used", "🔄 Refining query…", "⚡️ SPARQL        │
│       executed" are streamed as StepOutput events for the UI to render as    │
│       collapsible cards.                                                     │
│                                                                              │
│     The full turn is logged to data/logs/user_questions.log.                 │
└──────────────────────────────────────────────────────────────────────────────┘
```

Retry limit: the AI gets **3 attempts** to produce a working query
(`Configuration.max_try_fix_sparql = 3` in `src/sparql_llm/config.py:91,247`).
After that, the `max_tries_reached` step returns a polite "I couldn't get this
query to work" message instead of looping forever.

**Two kinds of questions, two paths.** In step [2] the AI decides if you're
asking *for data* ("access_resources" — e.g. "How many people are there?") or
*about the dataset itself* ("general_information" — e.g. "What is this database
about?"). For data questions it does the full search for examples + schema and
writes a query. For "about the dataset" questions it skips all that, grabs just
the one description document, and answers in plain words — no query needed.

---

## 4. Indexing pipeline (the one-time setup that fills the search index)

This runs automatically at startup **only when the search index is empty or
missing**. It reads the example and schema files, converts each into small
searchable "documents", turns their text into vectors, and stores them in
Qdrant. After this, the chatbot can search them instantly for every question.

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

**Only the `page_content` (the human-readable question or label) is turned into
a vector.** The SPARQL query text, schema shapes, and URLs are stored as-is
next to the vector (as "payload") and are only pulled out *after* a match is
found — they are never part of the meaning-based search itself.

---

## 5. Runtime configuration flow at boot (what the service reads when it starts)

```
systemd swiss-elites.service
   │ starts uvicorn sparql_llm.agent.main:app
   │
   ▼
main.py imports config.py
   │
   ├─ load_dotenv()  reads /opt/swiss-elites/sparql-llm/.env
   │     └─ SETTINGS_FILEPATH=data/swiss-elites-settings.json
   │     └─ OPENAI_BASE_URL, OPENAI_API_KEY (for GPUStack)
   │     └─ AUTH_SECRET, ADMIN_EMAIL, ADMIN_PASSWORD
   │
   ├─ Settings.from_file("data/swiss-elites-settings.json")
   │     └─ endpoints[0] = {
   │          endpoint_url    : swiss-elites.lod4hss.cloud/…/default_wisski…
   │          examples_file   : data/elites-suisses-examples.md
   │          void_file       : data/elites-suisses-void.ttl
   │          ontology_profiles_dir : data/ontology/profiles
   │          shacl_dir             : data/ontology/shacl
   │        }
   │     └─ default_llm_model = gpustack/minimax-m2.7
   │     └─ docs_collection_name = "swiss-elites"
   │     └─ vectordb_url = "data/vectordb"      (local Qdrant)
   │     └─ auto_init = True, force_index = False
   │
   └─ mcp_server.setup()
         └─ if collection missing or force_index → init_vectordb()  [see §4]
         └─ else → skip and serve from existing vectors
```

The retriever, embedder, LLM client, and SPARQL executor are all initialized
once and shared across requests.

---

## 6. Prompts fed to the LLM (per call)

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

---

## 7. Networking summary

| Direction                            | Protocol            | Endpoint                                                                            |
| ------------------------------------ | ------------------- | ----------------------------------------------------------------------------------- |
| Browser → Uvicorn                    | HTTPS (via reverse proxy on `vm7.dsl.unibe.ch`) → HTTP 127.0.0.1:8000 | `/chat`, `/models`, `/login`, static assets, SSE                    |
| Uvicorn → GPUStack                   | HTTPS (OpenAI API)  | `OPENAI_BASE_URL` in `.env`, path `/v1/chat/completions`, `/v1/models`               |
| Uvicorn → WissKI SPARQL              | HTTPS GET           | `https://swiss-elites.lod4hss.cloud/wisski/endpoint/default_wisski_distillery_adapter` |
| Uvicorn → Qdrant                     | in-process file I/O | `data/vectordb/collection/swiss-elites/storage.sqlite` (single-writer)              |
| Uvicorn → SQLite auth DB             | in-process file I/O | `data/users.db`                                                                     |

---

## 8. Where the knowledge lives on disk

```
/opt/swiss-elites/sparql-llm/
├── .env                                       # SETTINGS_FILEPATH, secrets
├── data/
│   ├── swiss-elites-settings.json             # runtime overrides (endpoints, models, force_index)
│   ├── elites-suisses-examples.md             # SPARQL Q/A pairs (RAG source)
│   ├── elites-suisses-void.ttl                # VoID → class/predicate summary
│   ├── ontology/
│   │   ├── profiles/*.rdf                     # OWL application profiles (labels, comments)
│   │   └── shacl/*.ttl                        # SHACL shapes (cardinalities)
│   ├── vectordb/                              # local Qdrant, single-writer
│   │   ├── meta.json
│   │   └── collection/swiss-elites/…
│   ├── logs/user_questions.log                # per-turn log
│   └── users.db                               # SQLite auth
└── src/sparql_llm/
    ├── config.py                              # Settings + Configuration dataclasses
    ├── agent/
    │   ├── main.py                            # FastAPI app + SSE streaming
    │   ├── graph.py                           # LangGraph state machine
    │   ├── prompts.py                         # EXTRACTION, RESOLUTION, FIX_QUERY
    │   ├── state.py                           # State, InputState, StepOutput
    │   ├── auth.py                            # JWT + SQLite users
    │   └── nodes/
    │       ├── llm_extraction.py              # step 2
    │       ├── retrieval_docs.py              # step 3
    │       ├── call_model.py                  # steps 4, 6
    │       └── validation.py                  # step 5
    ├── validate_sparql.py                     # rdflib + VoID validation
    ├── loaders/                               # feed the indexer
    │   ├── sparql_examples_md_loader.py
    │   ├── sparql_void_shapes_loader.py
    │   ├── ontology_profiles_loader.py
    │   └── sparql_info_loader.py
    ├── indexing/
    │   ├── index_resources.py                 # init_vectordb() — writes Qdrant
    │   └── index_entities.py                  # entity-resolution index (currently disabled)
    └── mcp_server.py                          # boot-time auto-init check
```

---

## 9. Embedding examples — how a file on disk becomes searchable

This section shows, with real examples, how each source file is turned into
searchable entries in Qdrant. For each case you'll see: (a) the raw text on
disk, (b) which part becomes the searchable **vector** (the meaning), and
(c) which part is just stored alongside as **payload** (looked up later, but not
searched).

**The one rule to remember** (from `indexing/index_resources.py:335`): only the
human-readable question or label gets turned into a vector; everything else —
the actual SPARQL, the schema, the URLs — rides along as payload.
```python
embeddings = embedding_model.embed([doc.page_content for doc in batch_docs])
qdrant_client.upsert(
    points=models.Batch(
        vectors=[emb.tolist() for emb in embeddings],
        payloads=[doc.metadata for doc in batch_docs],
    ),
)
```
→ **Only `page_content` is embedded. `metadata` is stored raw.**

### 9.1 Example: a SPARQL example with two phrasings

**On disk** (`data/elites-suisses-examples.md`, Example 2):

```markdown
## Example 2: List persons with their names

Question: List 20 persons with their names.
Alternative question: Donnez-moi 20 personnes avec leur nom.

```sparql
PREFIX crm: <http://www.cidoc-crm.org/cidoc-crm/>
PREFIX sdh-short: <https://sdhss.org/ontology/shortcuts/>

SELECT ?person ?name
WHERE {
  GRAPH <https://swiss-elites.lod4hss.cloud/resource/> {
    ?person a crm:E21 ;
            sdh-short:P9 ?name .
  }
}
LIMIT 20
```
```

**What the loader does** (`loaders/sparql_examples_md_loader.py`): it splits on
`## Example`, extracts the ` ```sparql ` block, and emits **one Document per
phrasing** (both `Question:` and `Alternative question:` share the same
SPARQL). Result: **two Documents** for this one example section.

**Document A (English phrasing):**
```python
Document(
    page_content = "List 20 persons with their names.",     # ← THIS gets embedded
    metadata = {                                             # ← THIS is payload only
        "question": "List 20 persons with their names.",
        "answer": (
            "PREFIX crm: <http://www.cidoc-crm.org/cidoc-crm/>\n"
            "PREFIX sdh-short: <https://sdhss.org/ontology/shortcuts/>\n\n"
            "SELECT ?person ?name\n"
            "WHERE {\n"
            "  GRAPH <https://swiss-elites.lod4hss.cloud/resource/> {\n"
            "    ?person a crm:E21 ;\n"
            "            sdh-short:P9 ?name .\n"
            "  }\n"
            "}\n"
            "LIMIT 20"
        ),
        "endpoint_url": "https://swiss-elites.lod4hss.cloud/wisski/endpoint/default_wisski_distillery_adapter",
        "query_type": "SelectQuery",
        "doc_type": "SPARQL endpoints query examples",
    },
)
```

**Document B (French phrasing — same SPARQL):**
```python
Document(
    page_content = "Donnez-moi 20 personnes avec leur nom.",  # ← different vector
    metadata = { ...same answer, endpoint_url, query_type, doc_type... },
)
```

**Resulting Qdrant points** (schematic; vectors truncated):

| id  | vector (1024-dim, e5-large)                     | payload                                                                     |
|-----|--------------------------------------------------|-----------------------------------------------------------------------------|
| 12  | [0.021, -0.108, 0.043, …, 0.077]  (from "List 20 persons with their names.") | `{question, answer=<full SPARQL>, endpoint_url, query_type, doc_type}` |
| 13  | [0.019, -0.101, 0.052, …, 0.083]  (from "Donnez-moi 20 personnes avec leur nom.") | `{same payload as row 12}`                                        |

So the SPARQL query itself is **duplicated** in Qdrant (once per phrasing) but
**never embedded**. Adding a Spanish or German `Alternative question:` line
would create a third vector pointing at the same query, at essentially no cost.

### 9.2 Example: a schema doc (VoID + OWL profile enrichment)

**On disk** — combined view of the three inputs for the class `crm:E21` (Person):

Snippet from `data/elites-suisses-void.ttl` (auto-derived from the endpoint):
```turtle
[] void:class crm:E21 ;
   void:classPartition [
     void:property sdh-short:P9 ;
     void:distinctSubjects 58732 ;
   ] ;
   void:classPartition [
     void:property crm:P98i_was_born ;
     void:distinctSubjects 41205 ;
   ] .
```

Snippet from `data/ontology/profiles/*.rdf` (human OWL profile):
```xml
<owl:Class rdf:about="http://www.cidoc-crm.org/cidoc-crm/E21">
  <rdfs:label xml:lang="en">Person</rdfs:label>
  <rdfs:comment xml:lang="en">
    This class comprises real persons who live or are assumed to have lived.
  </rdfs:comment>
</owl:Class>
```

Snippet from `data/ontology/shacl/*.ttl` (cardinalities):
```turtle
crm:E21_Shape a sh:NodeShape ;
  sh:targetClass crm:E21 ;
  sh:property [ sh:path sdh-short:P9 ;  sh:minCount 1 ; sh:maxCount 1 ] ;
  sh:property [ sh:path crm:P98i_was_born ; sh:maxCount 1 ] .
```

**What the loader does** (`loaders/sparql_void_shapes_loader.py`, lines 205-234
combined with `parse_ontology_profiles` + `parse_shacl_shapes`): merges the
three sources into one ShEx-style shape per class, then emits **up to two
Documents per class** — one keyed on the label, one keyed on the comment.

**Document C (label-keyed):**
```python
Document(
    page_content = "Person",                                 # ← embedded
    metadata = {
        "question": "Person",
        "answer": (                                          # full ShEx shape
            "crm:E21 {\n"
            "  a [crm:E21] ;\n"
            "  sdh-short:P9 xsd:string  # 1..1 (label)\n"
            "  crm:P98i_was_born @crm:E67 ?  # 0..1 (birth event)\n"
            "  # 58732 distinct instances\n"
            "}"
        ),
        "iri": "http://www.cidoc-crm.org/cidoc-crm/E21",
        "endpoint_url": "https://swiss-elites.lod4hss.cloud/wisski/endpoint/default_wisski_distillery_adapter",
        "doc_type": "SPARQL endpoints classes schema",
    },
)
```

**Document D (comment-keyed — same `answer`):**
```python
Document(
    page_content = "This class comprises real persons who live or are assumed to have lived.",   # ← embedded
    metadata = { ...same answer / iri / endpoint_url / doc_type as Document C... },
)
```

**Resulting Qdrant points:**

| id  | vector source                                                   | payload["answer"]        | payload["doc_type"]                   |
|-----|-----------------------------------------------------------------|--------------------------|---------------------------------------|
| 214 | "Person"                                                        | *full ShEx shape*        | "SPARQL endpoints classes schema"     |
| 215 | "This class comprises real persons who live or are assumed to…" | *same ShEx shape*        | "SPARQL endpoints classes schema"     |

Consequence: a user asking *"Who is Adolf Ogi?"* hits row 214 via "Person"
similarity; a user asking *"real historical individuals"* is more likely to
hit row 215 via the comment. Both paths surface the same ShEx to the LLM.

### 9.3 Example: the endpoint-summary doc

**On disk** — not a file, this one is constructed at index time in
`loaders/sparql_info_loader.py` from `settings.endpoints`:

**Document E:**
```python
Document(
    page_content = (                                         # ← embedded
        "What resources are available at the Swiss Elites Chat "
        "from University of Lausanne - Swiss Elites project?"
    ),
    metadata = {
        "question": "What resources are available at …?",
        "answer": (                                          # concatenation
            "Swiss Elites "
            "(https://swiss-elites.lod4hss.cloud/wisski/endpoint/default_wisski_distillery_adapter): "
            "The Swiss Elites knowledge graph contains prosopographical data "
            "about Swiss political, economic, military, cultural and academic "
            "elites from the 19th and 20th centuries…"
        ),
        "iri": "https://elites-suisses.lod4hss.org/",
        "doc_type": "General information",
    },
)
```

Retrieved only when the extraction step tags the intent as
`general_information` (filter applied in `retrieval_docs.py:41-48`).

### 9.4 Anatomy of a Qdrant point (final storage shape)

Regardless of source, every point looks like:

```jsonc
{
  "id": 12,
  "vector": [0.021, -0.108, 0.043, /* … 1024 floats total, e5-large … */, 0.077],
  "payload": {
    "question": "List 20 persons with their names.",
    "answer":   "PREFIX crm: … SELECT ?person ?name WHERE { … } LIMIT 20",
    "endpoint_url": "https://swiss-elites.lod4hss.cloud/…/default_wisski_distillery_adapter",
    "query_type":   "SelectQuery",
    "doc_type":     "SPARQL endpoints query examples"
    // for schema docs, also: "iri"
  }
}
```

`vector` is what the retriever matches against.
`payload` is what actually ends up in the LLM's prompt.

---

## 10. What the AI actually sees — a full worked example

This section follows one real question all the way through, so you can see the
exact text handed to the AI at each stage. Suppose the user types:

> *"What are the disciplines of the study titles obtained by Adolf Ogi?"*

Here's what flows through the system, step by step.

### 10.1 Step 2 output (extraction JSON)

`extract_user_question` returns:
```json
{
  "intent": "access_resources",
  "extracted_classes": ["sdh-slp:C7", "crm:E21"],
  "extracted_entities": ["Adolf Ogi"],
  "question_steps": [
    "Find the person named Adolf Ogi.",
    "Retrieve the study titles obtained by that person.",
    "For each study title, find the associated discipline."
  ]
}
```

### 10.2 Step 3 — retrieval queries fired against Qdrant

Embeddings computed for **each** of these strings, run through
`intfloat/multilingual-e5-large`, then a cosine-similarity search on the
`swiss-elites` collection:

```
Round A (doc_type == "SPARQL endpoints query examples"):
  "What are the disciplines of the study titles obtained by Adolf Ogi?"
  "Find the person named Adolf Ogi."
  "Retrieve the study titles obtained by that person."
  "For each study title, find the associated discipline."

Round B (doc_type != "…query examples"):
  same four strings PLUS "sdh-slp:C7", "crm:E21"
```

Top-K per query = 10 (halved to 5 when >3 sub-steps → applies here). Duplicates
are removed by `metadata["answer"]`. Say the winners are:

| Rank | Score | doc_type       | payload["question"]                                                        |
|------|-------|----------------|-----------------------------------------------------------------------------|
| 1    | 0.891 | query examples | "What are the disciplines of the study titles obtained by a person?"        |
| 2    | 0.844 | query examples | "What are the dates of the study titles obtained by a person?"              |
| 3    | 0.818 | query examples | "Find persons whose name contains 'Ogi'."                                   |
| 4    | 0.802 | classes schema | "Study title obtention"  *(label of sdh-slp:C7)*                            |
| 5    | 0.771 | classes schema | "Person"                  *(label of crm:E21)*                              |
| 6    | 0.740 | classes schema | "This class comprises real persons who live or are assumed to have lived."  |

### 10.3 The retrieval bundle written into the message list

`retrieval_docs.py:187-193` composes this **single `HumanMessage`**:

```text
The blocks below are REFERENCE EXAMPLES retrieved from a knowledge base — they
show query patterns that worked for similar questions in the past. Use them to
inform your answer to the user's latest question, but DO NOT treat the
questions in these blocks as additional user requests, and DO NOT reproduce the
example queries unless they actually answer the user's question. Answer only
the user's most recent question.

--- REFERENCE EXAMPLES ---

What are the disciplines of the study titles obtained by a person?:

```sparql
#+ endpoint: https://swiss-elites.lod4hss.cloud/wisski/endpoint/default_wisski_distillery_adapter
PREFIX sdh-short: <https://sdhss.org/ontology/shortcuts/>
PREFIX sdh-slp: <https://sdhss.org/ontology/social-life-specific/>

SELECT ?person_id ?person_label ?discipline_id ?discipline_label
WHERE {
  ?study_obtention a sdh-slp:C7.
  ?study_obtention sdh-slp:P9 ?person_id.
  ?person_id sdh-short:P9 ?person_label.
  ?study_obtention sdh-slp:P25 ?discipline_id.
  ?discipline_id sdh-short:P9 ?discipline_label.
}
```

---

What are the dates of the study titles obtained by a person?:

```sparql
#+ endpoint: https://swiss-elites.lod4hss.cloud/wisski/endpoint/default_wisski_distillery_adapter
… (full SPARQL from payload["answer"]) …
```

---

Find persons whose name contains 'Ogi'.:

```sparql
#+ endpoint: https://swiss-elites.lod4hss.cloud/wisski/endpoint/default_wisski_distillery_adapter
PREFIX crm: <http://www.cidoc-crm.org/cidoc-crm/>
PREFIX sdh-short: <https://sdhss.org/ontology/shortcuts/>

SELECT ?person ?name
WHERE {
  GRAPH <https://swiss-elites.lod4hss.cloud/resource/> {
    ?person a crm:E21 ;
            sdh-short:P9 ?name .
    FILTER(CONTAINS(LCASE(STR(?name)), "ogi"))
  }
}
LIMIT 50
```

---

Study title obtention (https://swiss-elites.lod4hss.cloud/wisski/endpoint/default_wisski_distillery_adapter):

```shex
sdh-slp:C7 {
  a [sdh-slp:C7] ;
  sdh-slp:P9  @crm:E21 ;      # person
  sdh-slp:P25 IRI ;           # discipline
  sdh-slp:P10 IRI ? ;         # title
  sdh-slp:P17 IRI ? ;         # institution
  sdh-short:P1 xsd:date ?     # date
}
```

---

Person (https://swiss-elites.lod4hss.cloud/wisski/endpoint/default_wisski_distillery_adapter):

```shex
crm:E21 {
  a [crm:E21] ;
  sdh-short:P9 xsd:string ;
  crm:P98i_was_born @crm:E67 ?
}
```

---

This class comprises real persons who live or are assumed to have lived. (…):

```shex
crm:E21 { … same shape … }
```
```

Formatting is produced by `format_docs()` / `_format_doc()` in
`retrieval_docs.py:207-246`. Each block's opening line is `payload["question"]`
(the natural-language handle) and its fenced body is `payload["answer"]`
(the SPARQL for example docs, the ShEx for schema docs).

### 10.4 Exact message list sent to the LLM (answering call)

`ChatPromptTemplate` in `call_model.py:60-65` emits this list of chat messages
to GPUStack's `/v1/chat/completions`:

```jsonc
[
  {
    "role": "system",
    "content": /* RESOLUTION_PROMPT — dataset intro, endpoint URL, named graph,
                  prefix table, output rules ("one SPARQL block, DISTINCT,
                  LIMIT 100, terse answers"). See prompts.py:32-54. */
  },
  {
    "role": "user",
    "content": "What are the disciplines of the study titles obtained by Adolf Ogi?"
  },
  {
    "role": "user",
    "name": "retrieve_docs",
    "content": /* the entire "REFERENCE EXAMPLES" bundle shown in §10.3 */
  }
  // On a retry loop, one more user message is appended (see §10.5).
]
```

Note: LangChain flattens `("placeholder", "{messages}")` into whichever roles
`state.messages` contains, so the retrieval bundle is a **user** message, not
a system message. This is intentional — mixing it into the system role would
dilute the terse rules in `RESOLUTION_PROMPT`.

### 10.5 What the LLM produces, then the retry / execution loop

The model's response contains prose plus one ```sparql``` fenced block, e.g.:

````text
Here is the query. It looks up study-title obtention events for Adolf Ogi and
returns the associated disciplines.

```sparql
#+ endpoint: https://swiss-elites.lod4hss.cloud/wisski/endpoint/default_wisski_distillery_adapter
PREFIX crm: <http://www.cidoc-crm.org/cidoc-crm/>
PREFIX sdh-short: <https://sdhss.org/ontology/shortcuts/>
PREFIX sdh-slp: <https://sdhss.org/ontology/social-life-specific/>

SELECT DISTINCT ?discipline_label WHERE {
  ?person a crm:E21 ;
          sdh-short:P9 ?person_label .
  FILTER(CONTAINS(LCASE(STR(?person_label)), "ogi"))
  ?obt a sdh-slp:C7 ;
       sdh-slp:P9  ?person ;
       sdh-slp:P25 ?discipline .
  ?discipline sdh-short:P9 ?discipline_label .
}
LIMIT 100
```
````

`validate_output` (§5 above) then either:

- **Appends a "Fix" message** and loops back for LLM call #3:
  ```jsonc
  {
    "role": "user",
    "content": "Fix the SPARQL query helping yourself with the error message and context from previous messages in a way that it is a fully valid query.\n\nSPARQL query: <erroneous query>\n\nError messages:\n- Unknown predicate sdh-slp:P25 on class crm:E21\n- …"
  }
  ```

- **Or (on success) appends an execution result message** and calls the LLM
  once more for the *synthesis pass*:
  ```jsonc
  {
    "role": "user",
    "name": "execute_sparql_query",
    "content": "Executed query on https://swiss-elites.lod4hss.cloud/…/default_wisski_distillery_adapter:\n```sparql\n<the accepted query>\n```\n\nResults:\n```\n{\n  \"head\": {\"vars\": [\"discipline_label\"]},\n  \"results\": {\"bindings\": [\n    {\"discipline_label\": {\"value\": \"Economics\"}},\n    {\"discipline_label\": {\"value\": \"Law\"}}\n  ]}\n}\n```\n\nNow write the final answer to the user using ONLY these results. Do NOT generate another SPARQL query."
  }
  ```

The final LLM call sees the *entire* prior conversation (system prompt +
question + retrieval bundle + generated query + execution result) and produces
the natural-language answer that gets streamed to the browser:

> "Adolf Ogi's study titles are recorded in the disciplines *Economics* and
> *Law*."

### 10.6 One-line summary of the data flow

```
page_content ── embed ──► vector ── search ──► top-K ── format ──► user-role message ── LLM ──► SPARQL ── endpoint ──► JSON ── LLM ──► answer
     ▲                                                    │
   metadata (unembedded) ──────────────────────────────── ┘  (metadata["answer"] is the SPARQL / ShEx body pulled in after the hit)
```
