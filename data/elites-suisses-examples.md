# Elites Suisses — SPARQL example queries

Local, curated examples for the RAG corpus. Mix of:
- The two originals from `elites_suisses_data/llm_documentation/SPARQL_queries_examples/query_examples.md` (with the verified prefix fixes already applied — `sdh-so:` → `sdh-short:` and `sdh-slc:` namespace = `social-life-core/`)
- Additional examples covering data that is **populated today** (persons, births, parents, marriages via `sdh-slc:C9`)

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
PREFIX swel: <https://swiss-elites.lod4hss.cloud/resource/>

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

Question: Who are the recorded children of Ernst Brenner (swel:p50001)?
Alternative question: Quels sont les enfants de cette personne ?

```sparql
PREFIX crm: <http://www.cidoc-crm.org/cidoc-crm/>
PREFIX sdh-short: <https://sdhss.org/ontology/shortcuts/>
PREFIX swel: <https://swiss-elites.lod4hss.cloud/resource/>

SELECT DISTINCT ?child ?childName
WHERE {
  GRAPH <https://swiss-elites.lod4hss.cloud/resource/> {
    ?birth crm:P98 ?child .
    { ?birth crm:P96 swel:p50001 } UNION { ?birth crm:P97 swel:p50001 }
    OPTIONAL { ?child sdh-short:P9 ?childName }
  }
}
```

## Example 7: Count marriages

Question: How many marriages are recorded?
Alternative question: Combien de mariages sont enregistrés ?
Comment: Marriages are modelled as instances of the SDHSS class C9 (social relationship event), each linking the spouses via sdh-slc:P20.

```sparql
PREFIX sdh-slc: <https://sdhss.org/ontology/social-life-core/>

SELECT (COUNT(DISTINCT ?marriage) AS ?marriageCount)
WHERE {
  GRAPH <https://swiss-elites.lod4hss.cloud/resource/> {
    ?marriage a sdh-slc:C9 .
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
PREFIX swel: <https://swiss-elites.lod4hss.cloud/resource/>

SELECT DISTINCT ?spouse ?spouseName
WHERE {
  GRAPH <https://swiss-elites.lod4hss.cloud/resource/> {
    ?marriage a sdh-slc:C9 ;
              sdh-slc:P20 swel:p50001 ;
              sdh-slc:P20 ?spouse .
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
PREFIX swel: <https://swiss-elites.lod4hss.cloud/resource/>

SELECT ?birth
WHERE {
  GRAPH <https://swiss-elites.lod4hss.cloud/resource/> {
    ?birth a crm:E67 ;
           crm:P98 swel:p50001 .
  }
}
```

## Example 11 *(aspirational)*: Members of the Federal Council

Question: Who has been a Swiss federal councillor?
Alternative question: What is the list of all the members of the Federal Council?
Comment: Will return 0 rows today — only 2 sdh-slc:C11 group instances exist and none are labelled "Federal Council". Indexed so the system learns the pattern for when LESSH populates the organisations side.

```sparql
PREFIX sdh-slc: <https://sdhss.org/ontology/social-life-core/>
PREFIX sdh-short: <https://sdhss.org/ontology/shortcuts/>

SELECT DISTINCT ?person ?personLabel
WHERE {
  GRAPH <https://swiss-elites.lod4hss.cloud/resource/> {
    ?membership sdh-short:P1 ?person ;
                sdh-short:P2 ?group .
    ?group a sdh-slc:C11 ;
           sdh-short:P9 ?groupLabel .
    FILTER (regex(str(?groupLabel), "federal council|conseil fédéral", "i"))
    ?person sdh-short:P9 ?personLabel .
  }
}
LIMIT 100
```

## Example 12 *(aspirational)*: Federal Council in a given year

Question: Who was in the Federal Council in 2001?
Comment: Aspirational — sdh-short:P3 (start date) and sdh-short:P8 (end date) are not in the graph yet.

```sparql
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
PREFIX sdh-slc: <https://sdhss.org/ontology/social-life-core/>
PREFIX sdh-short: <https://sdhss.org/ontology/shortcuts/>

SELECT DISTINCT ?person ?personLabel
WHERE {
  GRAPH <https://swiss-elites.lod4hss.cloud/resource/> {
    BIND(xsd:date("2001-01-01") AS ?yearStart)
    BIND(xsd:date("2001-12-31") AS ?yearEnd)

    ?membership sdh-short:P1 ?person ;
                sdh-short:P2 ?group ;
                sdh-short:P3 ?startDate .
    OPTIONAL { ?membership sdh-short:P8 ?endDate }
    ?group a sdh-slc:C11 ;
           sdh-short:P9 ?groupLabel .
    FILTER (regex(str(?groupLabel), "federal council|conseil fédéral", "i"))
    FILTER (?startDate <= ?yearEnd && (!BOUND(?endDate) || ?endDate >= ?yearStart))
    ?person sdh-short:P9 ?personLabel .
  }
}
LIMIT 100
```
