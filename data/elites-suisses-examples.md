# Elites Suisses — SPARQL example queries

Local, curated examples for the RAG corpus. Mix of:
- The two originals from `elites_suisses_data/llm_documentation/SPARQL_queries_examples/query_examples.md` (with the verified prefix fixes already applied — `sdh-so:` → `sdh-short:` and `sdh-slc:` namespace = `social-life-core/`)
- Additional examples covering data that is **populated today** (persons, births, parents, marriages via `sdh-slc:C3`)

When LESSH migrates to SHACL `.ttl`, port whichever of these are still relevant into the upstream repo.

---

## Example 1: Count all persons

Question: How many persons are in the knowledge graph?
Alternative question: Combien de personnes sont enregistrées dans la base ?

```sparql
PREFIX crm: <http://www.cidoc-crm.org/cidoc-crm/>

SELECT (COUNT(DISTINCT ?person) AS ?personCount)
WHERE {
  GRAPH <https://swiss-elites.lod4hss.cloud/resource/> {
    ?person a crm:E21 .
  }
}
```

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

## Example 3: Find persons by name substring

Question: Find persons whose name contains "Ogi".
Alternative question: Trouvez les personnes dont le nom contient « Ogi ».

```sparql
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

## Example 4: Count birth events

Question: How many recorded birth events are there?
Alternative question: Combien d'événements de naissance sont enregistrés ?

```sparql
PREFIX crm: <http://www.cidoc-crm.org/cidoc-crm/>

SELECT (COUNT(DISTINCT ?birth) AS ?birthCount)
WHERE {
  GRAPH <https://swiss-elites.lod4hss.cloud/resource/> {
    ?birth a crm:E67 .
  }
}
```

## Example 5: List parents of a specific person

Question: Who are the parents of the person with URI <https://swiss-elites.lod4hss.cloud/resource/p50001>?
Alternative question: Find the mother and father of Ernst Brenner.

```sparql
PREFIX crm: <http://www.cidoc-crm.org/cidoc-crm/>
PREFIX sdh-short: <https://sdhss.org/ontology/shortcuts/>
PREFIX swel: <https://elites-suisses.lod4hss.org/resource/>

SELECT ?role ?parent ?parentName
WHERE {
  GRAPH <https://swiss-elites.lod4hss.cloud/resource/> {
    ?birth crm:P98 swel:p50001 .              # birth event that brought the child into life
    {
      ?birth crm:P96 ?parent .                 # by mother
      BIND("mother" AS ?role)
    } UNION {
      ?birth crm:P97 ?parent .                 # from father
      BIND("father" AS ?role)
    }
    OPTIONAL { ?parent sdh-short:P9 ?parentName }
  }
}
```

## Example 6: Children of a person

Question: Who are the recorded children of Cecile Forel (swel:p64067)?
Alternative question: Quels sont les enfants de cette personne ?

```sparql
PREFIX crm: <http://www.cidoc-crm.org/cidoc-crm/>
PREFIX sdh-short: <https://sdhss.org/ontology/shortcuts/>
PREFIX swel: <https://elites-suisses.lod4hss.org/resource/>

SELECT DISTINCT ?child ?childName
WHERE {
  GRAPH <https://swiss-elites.lod4hss.cloud/resource/> {
    ?birth crm:P98 ?child .
    { ?birth crm:P96 swel:p64067 } UNION { ?birth crm:P97 swel:p64067 }
    OPTIONAL { ?child sdh-short:P9 ?childName }
  }
}
```

## Example 7: Count marriages

Question: How many marriages are recorded?
Alternative question: Combien de mariages sont enregistrés ?
Comment: Marriages are modelled as instances of sdh-slc:C3 (Social Relationship), each linking its two spouses via sdh-slc:P15. The relationship's type is reached with sdh-slc:P16; the single type instance carries sdh-short:P9 "Marriage".

```sparql
PREFIX sdh-slc: <https://sdhss.org/ontology/social-life-core/>

SELECT (COUNT(DISTINCT ?marriage) AS ?marriageCount)
WHERE {
  GRAPH <https://swiss-elites.lod4hss.cloud/resource/> {
    ?marriage a sdh-slc:C3 .
  }
}
```

## Example 8: Spouses of a specific person

Question: Who has been a spouse of person swel:p50001?
Alternative question: Quels étaient les conjoints de cette personne ?

```sparql
PREFIX crm: <http://www.cidoc-crm.org/cidoc-crm/>
PREFIX sdh-slc: <https://sdhss.org/ontology/social-life-core/>
PREFIX sdh-short: <https://sdhss.org/ontology/shortcuts/>
PREFIX swel: <https://elites-suisses.lod4hss.org/resource/>

SELECT DISTINCT ?spouse ?spouseName
WHERE {
  GRAPH <https://swiss-elites.lod4hss.cloud/resource/> {
    ?marriage a sdh-slc:C3 ;
              sdh-slc:P15 swel:p50001 ;
              sdh-slc:P15 ?spouse .
    FILTER(?spouse != swel:p50001)
    OPTIONAL { ?spouse sdh-short:P9 ?spouseName }
  }
}
```

## Example 9: Distribution of persons per class

Question: How many entities are there per class in the graph?
Alternative question: Quelles classes sont présentes et combien d'instances ont-elles ?

```sparql
SELECT ?class (COUNT(*) AS ?n)
WHERE {
  GRAPH <https://swiss-elites.lod4hss.cloud/resource/> {
    ?s a ?class .
  }
}
GROUP BY ?class
ORDER BY DESC(?n)
```

## Example 10: Find a person's birth event

Question: What is the birth event linked to person swel:p50001?
Alternative question: Trouver l'événement de naissance d'une personne.

```sparql
PREFIX crm: <http://www.cidoc-crm.org/cidoc-crm/>
PREFIX swel: <https://elites-suisses.lod4hss.org/resource/>

SELECT ?birth
WHERE {
  GRAPH <https://swiss-elites.lod4hss.cloud/resource/> {
    ?birth a crm:E67 ;
           crm:P98 swel:p50001 .
  }
}
```

## Example 11: Members of the Federal Council

Question: Who has been a Swiss federal councillor?
Alternative question: What is the list of all the members of the Federal Council?
Comment: Verified live 2026-09-07 — returns 100+ persons. Memberships link with `sdh-slc:P1`/`sdh-slc:P2` (NOT `sdh-short:`), and the group is a `crm:E74` matched by its French label; `sdh-slc:C11` is Gender, not Group.

```sparql
PREFIX sdh-slc: <https://sdhss.org/ontology/social-life-core/>
PREFIX sdh-short: <https://sdhss.org/ontology/shortcuts/>

SELECT DISTINCT ?person ?personName
WHERE {
  GRAPH <https://swiss-elites.lod4hss.cloud/resource/> {
    ?membership a sdh-slc:C5 ;
                sdh-slc:P1 ?person ;
                sdh-slc:P2 ?group .
    ?group sdh-short:P9 ?groupName .
    FILTER (LCASE(STR(?groupName)) = "conseil fédéral")
    ?person sdh-short:P9 ?personName .
  }
}
ORDER BY ?personName
LIMIT 100
```

## Example 12: Federal Council in a given year

Question: Who was in the Federal Council in 2001?
Alternative question: Qui siégeait au Conseil fédéral en 2001 ?
Comment: Verified live 2026-09-07 — returns the 7 councillors of 2001. Membership start/end are `sdh-short:P4`/`sdh-short:P7` and are **plain `xsd:integer` years**, so compare them as numbers; do not build `xsd:date` values from them. The end date is optional (a sitting mandate has none).

```sparql
PREFIX sdh-slc: <https://sdhss.org/ontology/social-life-core/>
PREFIX sdh-short: <https://sdhss.org/ontology/shortcuts/>

SELECT DISTINCT ?person ?personName ?start ?end
WHERE {
  GRAPH <https://swiss-elites.lod4hss.cloud/resource/> {
    ?membership a sdh-slc:C5 ;
                sdh-slc:P1 ?person ;
                sdh-slc:P2 ?group ;
                sdh-short:P4 ?start .
    OPTIONAL { ?membership sdh-short:P7 ?end }
    ?group sdh-short:P9 ?groupName .
    FILTER (LCASE(STR(?groupName)) = "conseil fédéral")
    ?person sdh-short:P9 ?personName .
    FILTER (?start <= 2001 && (!BOUND(?end) || ?end >= 2001))
  }
}
ORDER BY ?personName
LIMIT 100
```

## Example 13: Dates of study titles obtained by a person

Question: What are the dates of the study titles obtained by a person?

```sparql
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
PREFIX crm: <http://www.cidoc-crm.org/cidoc-crm/>
PREFIX sdh: <https://sdhss.org/ontology/core/>
PREFIX crm-sup: <https://sdhss.org/ontology/crm-supplement/>
PREFIX sdh-slc: <https://sdhss.org/ontology/social-life-core/>
PREFIX sdh-short: <https://sdhss.org/ontology/shortcuts/>
PREFIX sdh-info: <https://sdhss.org/ontology/sources-information-metadata/>
PREFIX sdh-sls: <https://sdhss.org/ontology/social-life-specific/>

SELECT ?person_id ?person_label ?obtention_date
WHERE {

?study_obtention a sdh-sls:C7.
?study_obtention sdh-sls:P9 ?person_id.
?person_id sdh-short:P9 ?person_label.
?study_obtention sdh-short:P1 ?obtention_date.

}
```

## Example 14: Disciplines of study titles obtained by a person

Question: What are the disciplines of the study titles obtained by a person?

```sparql
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
PREFIX crm: <http://www.cidoc-crm.org/cidoc-crm/>
PREFIX sdh: <https://sdhss.org/ontology/core/>
PREFIX crm-sup: <https://sdhss.org/ontology/crm-supplement/>
PREFIX sdh-slc: <https://sdhss.org/ontology/social-life-core/>
PREFIX sdh-short: <https://sdhss.org/ontology/shortcuts/>
PREFIX sdh-info: <https://sdhss.org/ontology/sources-information-metadata/>
PREFIX sdh-sls: <https://sdhss.org/ontology/social-life-specific/>

SELECT ?person_id ?person_label ?discipline_id ?discipline_label
WHERE {

?study_obtention a sdh-sls:C7.
?study_obtention sdh-sls:P9 ?person_id.
?person_id sdh-short:P9 ?person_label.
?study_obtention sdh-sls:P25 ?discipline_id.
?discipline_id sdh-short:P9 ?discipline_label.

}
```

## Example 15: Institutions that delivered study titles obtained by a person

Question: What are the institutions that delivered the study titles obtained by a person?

```sparql
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
PREFIX crm: <http://www.cidoc-crm.org/cidoc-crm/>
PREFIX sdh: <https://sdhss.org/ontology/core/>
PREFIX crm-sup: <https://sdhss.org/ontology/crm-supplement/>
PREFIX sdh-slc: <https://sdhss.org/ontology/social-life-core/>
PREFIX sdh-short: <https://sdhss.org/ontology/shortcuts/>
PREFIX sdh-info: <https://sdhss.org/ontology/sources-information-metadata/>
PREFIX sdh-sls: <https://sdhss.org/ontology/social-life-specific/>

SELECT ?person_id ?person_label ?institution_id ?institution_label
WHERE {

?study_obtention a sdh-sls:C7.
?study_obtention sdh-sls:P9 ?person_id.
?person_id sdh-short:P9 ?person_label.
?study_obtention sdh-sls:P17 ?institution_id.
?institution_id sdh-short:P9 ?institution_label.

}
```

## Example 16 *(aspirational)*: Places where study titles were obtained by a person

Question: What are the places where the study titles were obtained by a person?

Comment: NOT ANSWERABLE against the current data. Re-verified 2026-09-07: an `sdh-sls:C7` obtention carries exactly six predicates — `sdh-short:P1` (date), `sdh-sls:P9` (person), `P17` (institution), `P25` (discipline), `P10` (title), `P11` (supervisor). `sdh-sls:P19` has zero occurrences. Going one hop through the institution is a dead end too: its only other predicate, `sdh:P99`, is the same constant (`g-ty6`) on all 16,570 — a type marker, not a location. Kept as a target for when the curators model it; the assistant should answer "not in the database" until then.

```sparql
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
PREFIX crm: <http://www.cidoc-crm.org/cidoc-crm/>
PREFIX sdh: <https://sdhss.org/ontology/core/>
PREFIX crm-sup: <https://sdhss.org/ontology/crm-supplement/>
PREFIX sdh-slc: <https://sdhss.org/ontology/social-life-core/>
PREFIX sdh-short: <https://sdhss.org/ontology/shortcuts/>
PREFIX sdh-info: <https://sdhss.org/ontology/sources-information-metadata/>
PREFIX sdh-sls: <https://sdhss.org/ontology/social-life-specific/>

SELECT ?person_id ?person_label ?place_id ?place_label
WHERE {

?study_obtention a sdh-sls:C7.
?study_obtention sdh-sls:P9 ?person_id.
?person_id sdh-short:P9 ?person_label.
?study_obtention sdh-sls:P19 ?place_id.
?place_id sdh-short:P9 ?place_label.

}
```

## Example 17: Titles (type of diploma) of study titles obtained by a person

Question: What are the titles (type of diploma) of the study titles obtained by a person?

```sparql
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
PREFIX crm: <http://www.cidoc-crm.org/cidoc-crm/>
PREFIX sdh: <https://sdhss.org/ontology/core/>
PREFIX crm-sup: <https://sdhss.org/ontology/crm-supplement/>
PREFIX sdh-slc: <https://sdhss.org/ontology/social-life-core/>
PREFIX sdh-short: <https://sdhss.org/ontology/shortcuts/>
PREFIX sdh-info: <https://sdhss.org/ontology/sources-information-metadata/>
PREFIX sdh-sls: <https://sdhss.org/ontology/social-life-specific/>

SELECT ?person_id ?person_label ?title_id ?title_label
WHERE {

?study_obtention a sdh-sls:C7.
?study_obtention sdh-sls:P9 ?person_id.
?person_id sdh-short:P9 ?person_label.
?study_obtention sdh-sls:P10 ?title_id.
?title_id sdh-short:P9 ?title_label.

}
```

## Example 18: Supervisors of study titles obtained by a person

Question: Who are the supervisors of the study titles obtained by a person?

```sparql
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
PREFIX crm: <http://www.cidoc-crm.org/cidoc-crm/>
PREFIX sdh: <https://sdhss.org/ontology/core/>
PREFIX crm-sup: <https://sdhss.org/ontology/crm-supplement/>
PREFIX sdh-slc: <https://sdhss.org/ontology/social-life-core/>
PREFIX sdh-short: <https://sdhss.org/ontology/shortcuts/>
PREFIX sdh-info: <https://sdhss.org/ontology/sources-information-metadata/>
PREFIX sdh-sls: <https://sdhss.org/ontology/social-life-specific/>

SELECT ?person_id ?person_label ?supervisor_id ?supervisor_label
WHERE {

?study_obtention a sdh-sls:C7.
?study_obtention sdh-sls:P9 ?person_id.
?person_id sdh-short:P9 ?person_label.
?study_obtention sdh-sls:P11 ?supervisor_id.
?supervisor_id sdh-short:P9 ?supervisor_label.

}
```
