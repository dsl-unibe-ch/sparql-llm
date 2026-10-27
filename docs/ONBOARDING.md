# Onboarding a new SPARQL endpoint

This guide adds a new dataset to the chat assistant. One instance serves several datasets: users pick one in the chat, and every answer comes from that dataset only. You add the dataset on the admin page, edit three files and upload them there. You don't need Python, a terminal or a server login.

The worked example throughout is **Norssit**, the alumni register of the Helsinki Normal Lyceum (`https://ldf.fi/norssit/sparql`). Its finished files are in [`docs/examples/norssit/`](examples/norssit/). Swiss Elites values are shown next to it, for an SDHSS project.

The three files of a project are:

| File | What it holds |
|---|---|
| `project.json` | The endpoint and how its data is shaped: address, graphs, prefixes, which classes are persons and groups. |
| `dataset.md` | What the assistant must know about the data, in your own words: prefixes, classes, how names and dates are stored, rules. |
| `examples.md` | 5–10 example questions with the SPARQL query that answers each. The assistant copies their shape. |

Start from the template files in [`projects/_template/`](../projects/_template/). Keep the file names `project.json`, `dataset.md` and `examples.md`: the admin page manages exactly these three.

## 1. What you need from the dataset

Collect these before you start. Ask the dataset's maintainers, or look at the endpoint's own description.

- **The endpoint address**, and whether it accepts GET or only POST queries.
- **The graphs.** Is the data in named graphs, or in the default graph? If the default graph is the union of all graphs (as on Norssit), you need no graph at all.
- **The prefixes** of the ontologies the data uses. Use the exact namespace: `http://schema.org/` and `https://schema.org/` are different.
- **The classes of the things people ask about by name** (persons, organisations, places), and **the property that holds their name** (`skos:prefLabel`, `rdfs:label`, `sdh-short:P9`, …).
- **Birth and death properties**, if persons have them: they help tell people with the same name apart.
- **5–10 questions** people will ask, with a query that answers each, run on the endpoint and returning rows.
- **Questions with expected answers** for your own evaluation (section 7).

## 2. Adding a dataset on the admin page

An **admin** adds the dataset; no restart, no developer and no server change is needed.

1. On the admin page (`/admin`), open the **Dataset** dropdown at the top of the dataset section and choose its last entry, **＋ Add a dataset…**. Fill in **Id** (lowercase letters, digits and dashes, e.g. `norssit`) and **Name** (shown to users, e.g. `Norssit`), and press **Add dataset**.
2. The dataset is selected and shows 🔴 **new** in the dropdown. Every card of the dataset section (Project files, Search index) works on the dataset selected in that dropdown; the cards under **For all datasets** (Retrieval settings, Users) do not. The dots in the dropdown: 🟢 published, 🟡 ready, not published, 🔴 new.
3. Upload its three files, then Check and Rebuild (section 6). Rebuilding one dataset never touches another dataset's files or index, and a failed rebuild changes nothing.
4. After the first successful rebuild the dataset is **ready, not published**. Admins and curators see it in the chat's dataset list, marked "only visible to curators", and can try it there. Ordinary users don't see it.
5. When it answers well, an admin presses **Publish**. Every user sees it from then on. **Unpublish** takes it out of their list again; nothing is deleted. A rebuild never publishes a dataset on its own.

Datasets never share an endpoint: each `endpoint_url` belongs to one dataset.

**Removing a dataset** (admins): unpublish it, then press **Remove this dataset…** at the bottom of the dataset section and type its id to confirm. Its search index is deleted; its files are moved to `data/removed/` on the server, where a developer can restore them; users' saved chats about it are kept but hidden. The last dataset cannot be removed, nor one while a rebuild runs.

Curators can upload, check and rebuild any dataset. Only admins add, publish and unpublish.

## 3. Filling in `project.json`

Copy `projects/_template/project.json` and fill in each field. Every field except `id`, `label` and `endpoint_url` has a default.

| Field | What it is | Norssit | Swiss Elites |
|---|---|---|---|
| `id` | Short name: lowercase letters, digits and dashes. Must stay the id you gave the dataset. | `norssit` | `swiss-elites` |
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
| `intro` | Shown above the chat when the dataset is picked. `**bold**` and `[links](https://…)` work. | A natural-language interface to **Norssit**… | A natural-language interface to the **Elites Suisses** knowledge graph… |
| `example_questions` | The questions offered as buttons in an empty chat. | `["How many persons are in the register?", …]` | `["How many persons are in the database?", …]` |
| `topics` | A few words on what the dataset is about; they go into the assistant's instructions. | alumni of the Helsinki Normal Lyceum, … | Swiss elites, political figures, … |
| `org` | Who publishes the dataset. | Aalto University, Semantic Computing Research Group (SeCo) | University of Lausanne - Swiss Elites project |

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

On the admin page (`/admin`), pick the dataset in the **Dataset** dropdown, then, in the **Project files** card:

1. For each file: choose it, press **Upload**. A file with a problem is refused with a message. An accepted file **waits**: the card shows it as "waiting for the next Rebuild", and the assistant keeps using the file in service, even after a server restart. **Download** gives you the waiting file if there is one, else the one in service. **Discard upload** drops a waiting file.
2. Press **Check configuration** in the **Search index** card. Each check shows ✓ (fine), ⚠ (a warning; the rebuild still runs) or ✗ (an error; fix it first).
3. Press **Rebuild index**. It checks again, then describes the endpoint's schema, collects the names and builds the search index, all from the waiting files. Only when everything succeeded does it put the files and the new index in service together. If anything fails, nothing changes: the assistant keeps the old files and index, and your uploads keep waiting.

A rebuild takes a few minutes. When it finishes, the status line reads like: *"Norssit: Person 10,137 · Group 2,368 · 330 → 40 classes · 6/6 examples OK."*

Uploaded files take effect only when a Rebuild succeeds.

### What the messages mean

**When you upload a file:**

| Message | What to do |
|---|---|
| `project.json is not valid JSON: … at line N, column M.` | A comma, quote or bracket is wrong at that place. A JSON checker (e.g. jsonlint.com) shows it. |
| `<field>: is required` | Add that field. |
| `method: must be GET or POST` | Write `"GET"` or `"POST"`, in capitals. |
| `id: must use lowercase letters, digits and dashes only…` | Change the id, e.g. `norssit`, not `Norssit Chat`. |
| `id: is 'x', but this is the file of project 'y'…` | Keep the id of the project you upload to. |
| `Project 'x' is not served by this instance.` | There is no dataset with that id; add it first (**＋ Add a dataset…** in the Dataset dropdown). |
| `endpoint_url: is already used by dataset 'x'. Each dataset needs its own endpoint.` | Another dataset already queries that endpoint. Check `endpoint_url`; two datasets never share one. |
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
| `The project file … is missing.` / `The project file … has errors: …` | Upload `project.json` (or fix the field named in the message). |
| `Rebuild failed: …` | Something unexpected. The old index is still serving. Send the message to the developers. |
| `A rebuild of x is already running; try again when it finishes.` | One rebuild runs at a time, whichever dataset. Wait until the other one finishes. |

**In the dataset section and the chat:**

| Message | What to do |
|---|---|
| `The dataset 'x' has not been rebuilt successfully yet.` | Publish works only after a successful Rebuild of that dataset. |
| `'x' is not a valid id…` / `A dataset with the id 'x' already exists.` | Pick another id for the new dataset. |
| `The dataset 'x' is not available.` | In the chat: the dataset is not published (ordinary users), or does not exist. An admin publishes it in the dataset section. |
| `This dataset is not published.` | From an MCP client: the dataset's MCP address answers only once it is published. |

## 7. Testing with your questions

Pick the dataset in the chat's dataset list (top of the left sidebar), ask your evaluation questions and compare the answers with what you expect. Open the steps under an answer to see the query it ran and the examples it used. Before it is published, only admins and curators see the dataset there.

To score a whole list at once, write your questions as a JSON file, like [`docs/examples/norssit/questions.json`](examples/norssit/questions.json):

```json
[{"question": "How many persons are in the register?", "expected": "10137"}]
```

A developer runs it against the instance:

```
python scripts/compare_modes.py run --project norssit --questions docs/examples/norssit/questions.json --chat-url http://127.0.0.1:8000/chat --out norssit.jsonl
python scripts/compare_modes.py report norssit.jsonl
```

The report counts an answer as correct when it contains the numbers, years and names of the expected answer, and writes every answer side by side for you to read.

When an answer is wrong, the fix is usually one of these:
- a missing or unclear point in `dataset.md` (how something is modelled, which property to use);
- a missing example of that question shape in `examples.md`;
- a class left out by `schema_classes`, or too many small classes left in.

## 8. Updating later

- **The data changed** (the endpoint was reloaded): pick the dataset, press **Rebuild index**. The Search index card says when the data no longer matches the index.
- **You improved a file**: Download, edit, Upload, Check, Rebuild.
- **A file broke something**: **Undo** queues the version it replaced; then Rebuild.
- **Files shipped with the code** (`projects/<id>/`) are copied to the server only when the project has no files there yet. After that the files in service are the ones uploaded on the admin page, so a change in the repository reaches the server only by uploading it.
- **MCP clients** (VS Code, Claude Desktop) reach one dataset per address: `/mcp/<id>`, e.g. `/mcp/norssit`. The bare `/mcp` is the first dataset (Swiss Elites). An unpublished dataset's address answers `This dataset is not published.`
