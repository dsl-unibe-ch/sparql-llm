## About the dataset

The Elites Suisses knowledge graph is an RDF dataset of approximately 58,700 Swiss elites (political, economic, academic, military) curated by the LESSH project. The data covers biographical facts, education, marriages, family relations, organisational memberships, and mandates. Many labels are in French.

## Endpoint and graph

Primary named graph: <https://swiss-elites.lod4hss.cloud/resource/> — target it explicitly.

## Prefixes

- crm:       <http://www.cidoc-crm.org/cidoc-crm/>           — E21 (Person), E67 (Birth), E69 (Death), E74 (Group); P96 (by mother), P97 (from father), P98 (brought into life)
- sdh-slc:   <https://sdhss.org/ontology/social-life-core/>  — note the "-core/" suffix. C5 (Membership — 64k, the largest class: a person's mandate/role in a group), C3 (Social Relationship — this is where marriages/unions live), C4 (Social Relationship Type), C11 (Gender); P1 (membership → person), P2 (membership → group), P15 (relationship → partner, two per relationship), P16 (has relationship type), P23 (has gender). There is NO C9 and NO P20 in this namespace.
- sdh-sls:   <https://sdhss.org/ontology/social-life-specific/> — social-life-SPECIFIC (NOT "-core/"). C7 (Obtaining a Study Title — the *event*; the title itself is C8), C8 (Study title), C9 (Academic Discipline), C3 (Taking Care of a Person Type — unrelated to education); P9 (was obtained by), P10 (is obtention of), P11 (has academic supervisor), P17 (is delivered by), P19 (is obtained at), P25 (is obtention in)
- sdh-short: <https://sdhss.org/ontology/shortcuts/>         — P9 (has standard label — THE name predicate, a plain literal on Person/Group/Place; person names are stored "Surname, Firstname", e.g. "Brenner, Ernst"), P1 (at some time within — on events), P2 (has birth date — literal directly on the Person), P13 (has death date), P4 / P7 (membership start / end year), P11 (has description), P14 (geo point, WKT literal)
- sdh:       <https://sdhss.org/ontology/core/>              — C13 (Geographical Place), C51 (Geographical Place Kind); P6 (event took place at), P99
- swel:      <https://elites-suisses.lod4hss.org/resource/>  — entity URIs (e.g. swel:p12345). NOTE: entity URIs use elites-suisses.lod4hss.org, while the named graph is still swiss-elites.lod4hss.cloud/resource/.
- xsd:       <http://www.w3.org/2001/XMLSchema#>

## Rules for this dataset

- If an ENTITIES FOUND block gives a URI for a name, use that URI in the query (with VALUES); a label filter on sdh-short:P9 is only the fallback when no match is listed.
- Use the bare class name `crm:E21` (not `crm:E21_Person`) — match the form in the graph.
