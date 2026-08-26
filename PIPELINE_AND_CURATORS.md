# What the pipeline reads, and what to ask the curators

Verified against the live endpoint and the indexing code on 2026-08-26.

---

## 1. What the pipeline actually reads

**One correction first:** we do **not** read `llm_ontology/*.rdf`, and we do not read their
example files either. **No code opens any file in either curator repo.**

| Input | Where it comes from |
|---|---|
| Ontology labels + definitions (280 classes, 457 properties) | **Endpoint**, live at index time |
| SHACL shapes (12) — cardinality, property names | **Endpoint**, live at index time |
| Prefix map | **Endpoint**, cached to `data/endpoints_metadata.json` |
| `data/elites-suisses-void.ttl` — class/property statistics | **Our repo**, generated from the endpoint by `scripts/enrich_void.py` |
| `data/elites-suisses-examples.md` — 18 examples | **Our repo**, hand-written (some originally pasted from theirs) |
| schema.org description of the dataset | Their homepage — **currently returns nothing**, no JSON-LD there |

Their `.rdf` profiles and `.ttl` shapes matter **only because someone loads them into the
triplestore**. We read the triplestore. The repo is a staging area, not a source we consume.

---

## 2. Your questions, answered

### Did they add new examples? Should we merge, or switch to their file?

**No, and no.** `SPARQL_queries_examples/` has exactly one commit — the July move. Nothing
has been added since.

Ours is strictly ahead: 18 examples vs their 8 queries, prefixes repaired, and every one now
executed against real data. **Keep ours. Nothing to merge.** Do not switch to their file.

### Are our examples still correct?

They are **now**. I ran all 18 today. Five defects found and fixed:

| # | Was broken | Fix |
|---|---|---|
| 5, 6, 8, 10 | `swel:` bound to the retired `swiss-elites.lod4hss.cloud` namespace — returned 0 rows | rebound to `elites-suisses.lod4hss.org` |
| 7, 8 | `sdh-slc:C9` — 0 instances in the data | → `sdh-slc:C3` Social Relationship |
| 8 | `sdh-slc:P20` — 0 occurrences | → `sdh-slc:P15` involves partner |
| 6 | Subject (Ernst Brenner) has no recorded children | → Cécile Forel, who has 4 |
| 16 | `sdh-sls:P19` — 0 occurrences; place of obtention is **not modelled at all** | marked *aspirational* |

**Result: 15/18 return real data.** The 3 empties (11, 12, 16) are explicitly marked
aspirational — data not yet loaded.

**Rule going forward:** execute every example before trusting it. All five defects were
valid SPARQL that happened to match nothing; reading them would never have shown it.

### `llm_documentation/elites-suisses-void.ttl` — tell them to delete?

**Yes.** It is an 11-line stub claiming 150,000 triples; the store holds 735,918. We generate
our own from the live endpoint on every re-index, so it is always current. Nothing reads theirs.

### `llm_documentation/access_to_triplestore.md` — do we have our own?

**We don't have one and don't need one.** It is a human onboarding page, not a machine input.
Our endpoint URL lives in `data/swiss-elites-settings.json` and `src/sparql_llm/agent/prompts.py`.

But it should still be fixed **for humans** — it advertises the dead `/wisski/endpoint/default`
instead of `/wisski/endpoint/default_wisski_distillery_adapter`. It is the first thing a
newcomer copies.

### Is cardinality really only in the SHACL files?

**Confirmed yes.** I searched the entire store:

- `owl:cardinality`, `owl:minCardinality`, `owl:maxCardinality`, `owl:qualifiedCardinality`, `owl:Restriction`, `owl:someValuesFrom`, `owl:allValuesFrom` — **zero occurrences, anywhere**.
- SHACL: **32 `sh:minCount` + 31 `sh:maxCount`** across 12 classes.

So yes — ask them to record cardinality in the OWL profiles or anywhere they will keep current.
It is the one thing that genuinely disappears if SHACL goes.

### "Shape the classes that are actually populated" — what exactly is missing?

12 classes have shapes. 12 classes have data. **Only 6 are in both.**

**Populated but NOT shaped** — these have statistics but no cardinality and no contextual
property names:

| Class | Label | Instances |
|---|---|---|
| `sdh-slc:C5` | **Membership** | 64,290 |
| `sdh-sls:C7` | Obtaining a Study Title | 16,572 |
| `crm:E74` | Group | 3,608 |
| `sdh-sls:C9` | Academic Discipline | 118 |
| `sdh-sls:C8` | Study title | 59 |
| `sdh:C51` | Geographical Place Kind | 2 |

**Shaped but NOT populated** — effort spent on classes with no data: `crm:E69` Death,
`sdh:C17` Construction, `crm:E62` String, `crm-sup:C39` Standard Date-Time, `sdh-slc:C2`,
`sdh-slc:C43`.

**The one-line version: half the shaped classes have no data, and half the populated classes
have no shape — including the largest class in the database.**

---

## 3. On SHACL — the recommendation

They are right that the `.rdf` profiles now carry the labels and definitions. They are wrong
that they carry *everything*: cardinality is not in them.

But the practical answer is **let them drop it**:

- The files are already frozen (last touched 2026-06-12) and were left behind in the old repo during the July migration. This is a description of what already happened, not a proposal.
- The shapes persist inside the triplestore regardless, so nothing breaks today.
- What we lose is cardinality only — useful, not essential.
- And the shapes already miss the six biggest populated classes, so "keep maintaining SHACL" would be the wrong ask anyway.

**Ask for the information, not the format.**

---

## 4. Message to send them

> Thanks — that clears things up. A few points from our side:
>
> **You can stop maintaining the SHACL files.** We confirmed the OWL profiles now carry the
> labels and definitions we need, and we read those from the endpoint, not from the repo.
>
> **One thing is only in SHACL: property cardinality** (min/max counts). It is not in the OWL
> profiles — we checked the whole triplestore and there are no OWL cardinality constructs at
> all. If it is easy to record cardinality in the OntoME profiles, that would be useful for
> generating correct queries. If not, we will manage without it.
>
> **More valuable than either:** the shapes currently cover classes that have no data
> (Death, Construction, String, Standard Date-Time) while the largest populated classes have
> no shape at all — Membership (64,290 instances), Obtaining a Study Title (16,572) and
> Group (3,608). If you do keep describing classes, those are the ones worth describing.
>
> **You can delete `llm_documentation/elites-suisses-void.ttl`.** We generate a full VoID from
> the live endpoint on every re-index (currently 735,918 triples, 155,481 entities, 12 classes),
> so a static file will always be behind.
>
> **Small fix:** `access_to_triplestore.md` still points at
> `/wisski/endpoint/default`; the working endpoint is
> `/wisski/endpoint/default_wisski_distillery_adapter`.
>
> **Two questions:**
> 1. Entity URIs are now minted under `elites-suisses.lod4hss.org/resource/` while the named
>    graph and endpoint stay on `swiss-elites.lod4hss.cloud`. Is that intentional and stable?
> 2. Is a place of obtention for study titles modelled anywhere? `sdh-sls:P19` has no
>    occurrences, and P17/P25/P10 point to Group, Discipline and Title respectively.
>
> **One request that would save us a lot:** when the triplestore is reloaded, could a version
> or timestamp triple be written into the graph? Our schema snapshot was two months stale and
> we had no way to detect it — in that window the database grew 61% and gained the entire
> geographic model.

---

## 5. Changes made to the repo today

- `data/elites-suisses-void.ttl` — regenerated from the live endpoint (9 → 12 classes, +270 lines)
- `data/elites-suisses-examples.md` — 5 defects fixed (see §2)
- `src/sparql_llm/agent/prompts.py` — removed dead `sdh-slc:C9` / `sdh-slc:P20` guidance, added Membership + the `sdh:` core namespace + geo terms, corrected the `swel:` namespace

Not yet committed. Not yet re-indexed on vm7.
