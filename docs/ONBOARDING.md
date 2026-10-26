# Onboarding a new SPARQL endpoint

This guide sets up the chat assistant for a new dataset. You edit three files and upload them on the admin page. You don't need Python, a terminal or a server login.

The worked example throughout is **Norssit**, the alumni register of the Helsinki Normal Lyceum (`https://ldf.fi/norssit/sparql`). Its finished files are in [`docs/examples/norssit/`](examples/norssit/). Swiss Elites values are shown next to it, for an SDHSS project.

The three files of a project are:

| File | What it holds |
|---|---|
| `project.json` | The endpoint and how its data is shaped: address, graphs, prefixes, which classes are persons and groups. |
| `dataset.md` | What the assistant must know about the data, in your own words: prefixes, classes, how names and dates are stored, rules. |
| `examples.md` | 5–10 example questions with the SPARQL query that answers each. The assistant copies their shape. |

Start from the template files in [`data/_template/`](../data/_template/). Keep the file names `project.json`, `dataset.md` and `examples.md`: the admin page manages exactly these three.

## 1. What you need from the dataset

Collect these before you start. Ask the dataset's maintainers, or look at the endpoint's own description.

- **The endpoint address**, and whether it accepts GET or only POST queries.
- **The graphs.** Is the data in named graphs, or in the default graph? If the default graph is the union of all graphs (as on Norssit), you need no graph at all.
- **The prefixes** of the ontologies the data uses. Use the exact namespace: `http://schema.org/` and `https://schema.org/` are different.
- **The classes of the things people ask about by name** (persons, organisations, places), and **the property that holds their name** (`skos:prefLabel`, `rdfs:label`, `sdh-short:P9`, …).
- **Birth and death properties**, if persons have them: they help tell people with the same name apart.
- **5–10 questions** people will ask, with a query that answers each, run on the endpoint and returning rows.
- **Questions with expected answers** for your own evaluation (section 7).

## 2. Getting a second instance

Each project runs as its own instance of the assistant: its own address, its own user accounts, its own search index. A new project therefore needs, once:

1. **From IT** (needs administrator rights on the server):
   - a second service unit, a copy of the existing `swiss-elites` one with three different settings:
     - `SETTINGS_FILEPATH`: its own install settings file;
     - its own port, e.g. `8010`;
     - its own data folder, set as `data_dir` in that install settings file (e.g. `data-norssit`), so the two instances never share users, indexes or files;
   - a route on the web proxy from the new address (e.g. `/norssit/`) to that port;
   - **memory**: one instance with the default embedding model uses about 1.8 GB. Either the server gets more RAM, or the new instance uses the smaller model `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` (about 0.6 GB, slightly less accurate).
2. **From the developers**: the install settings file. It lists the project id (`"projects": ["norssit"]`), the chat name and intro shown on the page, the models, and the admin account. You can't upload this file; it holds secrets.

From then on, everything is done from the admin page.

## 3. Filling in `project.json`

Copy `data/_template/project.json` and fill in each field. Every field except `id`, `label` and `endpoint_url` has a default.

| Field | What it is | Norssit | Swiss Elites |
|---|---|---|---|
| `id` | Short name: lowercase letters, digits and dashes. Must match the id in the install settings. | `norssit` | `swiss-elites` |
| `label` | The name shown to users. | `Norssit` | `Swiss Elites` |
| `endpoint_url` | The SPARQL endpoint. | `https://ldf.fi/norssit/sparql` | `https://swiss-elites.lod4hss.cloud/wisski/endpoint/default_wisski_distillery_adapter` |
| `method` | `GET` or `POST`. Use `GET` unless the endpoint only accepts POST. | `GET` | `GET` |
| `description` | One or two sentences on the dataset. | Norssi High School Alumni… | The Swiss Elites knowledge graph… |
| `homepage_url` | The project's web page; the assistant links to it. | `http://www.ldf.fi/dataset/norssit` | `https://elites-suisses.lod4hss.org/` |
| `graphs` | Named graphs to query. Empty list `[]` = the default graph. | `[]` | `["https://swiss-elites.lod4hss.cloud/resource/"]` |
| `prefixes` | Prefix → namespace. These win over anything found on the endpoint. | `schema` → `http://schema.org/`, `bioc`, `skos`, `foaf`, `reg`, `norssit` | `crm`, `sdh-slc`, `sdh-sls`, `sdh-short`, `sdh`, `swel`, … |
| `dataset_description_file` | Leave as `dataset.md`. | | |
| `examples_file` | Leave as `examples.md`. | | |
| `entities.label_property` | The property holding names. | `skos:prefLabel` (full IRI) | `sdh-short:P9` (full IRI) |
| `entities.types` | One entry per kind of named thing: a name you choose, its class IRI, and optionally birth/death properties. | `Person` (foaf:Person, with schema:birthDate/deathDate), `Group` (foaf:Organization) | `Person`, `Group`, `Place`, `Discipline`, `Study title` |
| `schema_classes.min_instances` | Classes with fewer instances are left out of the schema the assistant sees. | `50` | `1` |
| `schema_classes.exclude` | IRI beginnings of classes to leave out. Use it for datasets with hundreds of small classes. | `["http://ldf.fi/norssit/titles/"]` (about 300 job-title classes) | `[]` |
| `curator_examples` | Optional: a public GitHub folder of example files to pull at every rebuild: `{"github_repo", "folder", "branch"}`. | `null` | `lod4hss-projects/elites-suisses`, `llm_documentation/SPARQL_queries_examples`, `main` |
| `prefix_repairs` | Optional: text replacements for known typos in example files. | `{}` | `{"sdh-so:": "sdh-short:", …}` |
| `ontology_from_endpoint` | Optional, SDHSS projects: also read the OntoME class definitions and SHACL shapes from the endpoint. | `false` | `true` |

Write full IRIs in `project.json` (`http://www.w3.org/2004/02/skos/core#prefLabel`, not `skos:prefLabel`).

## 4. Writing `dataset.md`

This is the assistant's cheat sheet. It goes into every prompt, and each `##` section is also searchable on its own. Write it in English, for someone who knows SPARQL but not your data:

- `## About the dataset`: what it holds, which period, which language the labels are in.
- `## Prefixes`: one line per prefix with the classes and properties that matter, and what they mean.
- `## Names and dates`: how names are written ("Surname, Firstnames"), which dates are full dates and which only years, values that have no label.
- One section per tricky part of the model: how careers, family relations or memberships are shaped, with a short triple pattern.
- `## Rules for this dataset`: one line per rule the assistant must follow.

Look at [`docs/examples/norssit/dataset.md`](examples/norssit/dataset.md). Explain what an outsider would get wrong: on Norssit, the family relation type names the *relative's* role, places have no labels, and `schema:` is http.

The endpoint address and graphs are added to the prompt automatically; don't repeat them.

**SDHSS projects:** start from [`docs/examples/sdhss-dataset-snippet.md`](examples/sdhss-dataset-snippet.md). It explains the SDHSS namespaces (`-core/` versus `-specific/`) and the `sdh-short:P9` names.

## 5. Writing examples

`examples.md` holds your example questions. Each example looks like this:

````markdown
## Example 2: Find a person by name

Question: Find persons whose name contains "Waltari", with birth and death dates.

```sparql
PREFIX foaf: <http://xmlns.com/foaf/0.1/>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
SELECT ?person ?name WHERE { ?person a foaf:Person ; skos:prefLabel ?name }
```
````

- Each example needs a `## ` heading, a `Question:` line and a ` ```sparql ` block. You can add `Alternative question:` lines for other phrasings.
- Write the `PREFIX` lines in every query.
- Run each query on the endpoint first. Check runs them again, and a query that returns nothing is reported.
- Cover the question shapes people ask: a count, a lookup by name, the relations of one person, a top-10, a filter by date.

## 6. Upload, Check, Rebuild

On the admin page (`/admin`), in the **Project files** card:

1. For each file: choose it, press **Upload**. A file with a problem is refused with a message, and the old file stays. **Download** gives you the current version; **Undo** puts back the previous one.
2. Press **Check configuration** in the **Search index** card. Each check shows ✓ (fine), ⚠ (a warning; the rebuild still runs) or ✗ (an error; fix it first).
3. Press **Rebuild index**. It checks again, then describes the endpoint's schema, collects the names, builds the search index and switches the assistant to it. The assistant keeps answering from the old index until the new one is ready. If anything fails, the old one stays.

A rebuild takes a few minutes. When it finishes, the status line reads like: *"Norssit: Person 10,137 · Group 2,368 · 330 → 40 classes · 6/6 examples OK."*

Uploaded files take effect only at the next Rebuild.

### What the messages mean

**When you upload a file:**

| Message | What to do |
|---|---|
| `project.json is not valid JSON: … at line N, column M.` | A comma, quote or bracket is wrong at that place. A JSON checker (e.g. jsonlint.com) shows it. |
| `<field>: is required` | Add that field. |
| `method: must be GET or POST` | Write `"GET"` or `"POST"`, in capitals. |
| `id: must use lowercase letters, digits and dashes only…` | Change the id, e.g. `norssit`, not `Norssit Chat`. |
| `id: is 'x', but this is the file of project 'y'…` | Keep the id of the project you upload to. |
| `Project 'x' is not served by this instance.` | The install settings don't list this project; ask the developers. |
| `examples.md has no example: …` | Each example needs a `## ` heading, a `Question:` line and a ` ```sparql ` block. |
| `dataset.md is empty.` | Upload the file with its text. |
| `… is not UTF-8 text.` | Save the file as UTF-8 in your editor and upload again. |
| `… is larger than 1 MB.` | The file is far too large for a project file; check you picked the right one. |
| `A rebuild is running; try again when it finishes.` | Wait for the rebuild to finish, then upload again. |

**When you press Check or Rebuild:**

| Message | What to do |
|---|---|
| `… the endpoint … did not answer a SPARQL query (it did not answer in time).` | The endpoint is down or slow. Try later, or check `endpoint_url`. |
| `… did not answer a SPARQL query (it answered with something other than SPARQL results, e.g. a web page).` | `endpoint_url` is a web page, not the SPARQL endpoint, or `method` is wrong. |
| `… did not answer a SPARQL query (it answered with HTTP error 405).` (or another number) | 405 usually means the endpoint wants the other `method`. 404: the address is wrong. |
| `… the endpoint … answers but holds no data.` | The endpoint is empty, or this is the wrong endpoint. |
| `… the graph <…> listed in the graphs field is empty or does not exist.` | Correct the graph IRI, or use `[]` for the default graph. |
| `… no instance of entities.types.<Name> has a label <…>.` | That class IRI is wrong, or its instances use another name property than `entities.label_property`. |
| `… the dataset description dataset.md is missing.` / `… the examples file examples.md is missing.` | Upload the file. |
| `… the examples file examples.md has no example …` | See above. |
| `… N/M examples return rows. These fail or return nothing: …` (warning) | Run the listed examples on the endpoint and fix them. They are still indexed. |
| `… no entities block, so names in questions will not be looked up.` (warning) | Add the `entities` block, unless the dataset has no named things. |
| `The generated schema for <label> could not be read back: …` | The schema description of the endpoint could not be written. Send the message to the developers. |
| `… the schema could not be generated from …` | The endpoint failed during the schema queries, often a timeout on a large endpoint. Try again later. |
| `Rebuild failed: …` | Something unexpected. The old index is still serving. Send the message to the developers. |

## 7. Testing with your questions

Ask your evaluation questions in the chat and compare the answers with what you expect. Open the steps under an answer to see the query it ran and the examples it used.

To score a whole list at once, write your questions as a JSON file, like [`docs/examples/norssit/questions.json`](examples/norssit/questions.json):

```json
[{"question": "How many persons are in the register?", "expected": "10137"}]
```

A developer runs it against the instance:

```
python scripts/compare_modes.py run --project norssit --questions docs/examples/norssit/questions.json --out norssit.jsonl
python scripts/compare_modes.py report norssit.jsonl
```

The report counts an answer as correct when it contains the numbers, years and names of the expected answer, and writes every answer side by side for you to read.

When an answer is wrong, the fix is usually one of these:
- a missing or unclear point in `dataset.md` (how something is modelled, which property to use);
- a missing example of that question shape in `examples.md`;
- a class left out by `schema_classes`, or too many small classes left in.

## 8. Updating later

- **The data changed** (the endpoint was reloaded): press **Rebuild index**. The Search index card says when the data no longer matches the index.
- **You improved a file**: Download, edit, Upload, Check, Rebuild.
- **A file broke something**: **Undo** puts back the previous version; then Rebuild.
