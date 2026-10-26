# Norssit — SPARQL example queries

Worked example for docs/ONBOARDING.md. Every query was executed against https://ldf.fi/norssit/sparql on 2026-10-26.

---

## Example 1: Count all persons

Question: How many persons are in the register?

```sparql
PREFIX foaf: <http://xmlns.com/foaf/0.1/>

SELECT (COUNT(DISTINCT ?person) AS ?personCount)
WHERE {
  ?person a foaf:Person .
}
```

## Example 2: Find a person by name

Question: Find persons whose name contains "Waltari", with birth and death dates.

```sparql
PREFIX foaf: <http://xmlns.com/foaf/0.1/>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
PREFIX schema: <http://schema.org/>

SELECT ?person ?name ?birth ?death
WHERE {
  ?person a foaf:Person ;
          skos:prefLabel ?name .
  OPTIONAL { ?person schema:birthDate ?birth }
  OPTIONAL { ?person schema:deathDate ?death }
  FILTER(CONTAINS(LCASE(STR(?name)), "waltari"))
}
LIMIT 20
```

## Example 3: Family relations of a person

Question: Who are the recorded relatives of Mika Waltari (norssit:norssi_2230), and how are they related?

```sparql
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
PREFIX bioc: <http://ldf.fi/schema/bioc/>
PREFIX norssit: <http://ldf.fi/norssit/>

SELECT ?relationType ?relative ?relativeName
WHERE {
  norssit:norssi_2230 bioc:has_family_relation ?relation .
  ?relation a ?relationType ;
            bioc:inheres_in ?relative .
  ?relative skos:prefLabel ?relativeName .
}
```

## Example 4: Career titles of a person

Question: What positions did Mika Waltari hold, at which organisations and when?

```sparql
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
PREFIX bioc: <http://ldf.fi/schema/bioc/>
PREFIX schema: <http://schema.org/>
PREFIX norssit: <http://ldf.fi/norssit/>

SELECT ?title ?orgName ?start ?end
WHERE {
  ?event a bioc:Title ;
         bioc:title_inheres_in norssit:norssi_2230 ;
         skos:prefLabel ?title .
  OPTIONAL { ?event bioc:relates_to ?org . ?org skos:prefLabel ?orgName }
  OPTIONAL { ?event schema:startDate ?start }
  OPTIONAL { ?event schema:endDate ?end }
}
ORDER BY ?start
```

## Example 5: Organisations with the most positions held by alumni

Question: Which organisations employed the most alumni?

```sparql
PREFIX foaf: <http://xmlns.com/foaf/0.1/>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
PREFIX bioc: <http://ldf.fi/schema/bioc/>

SELECT ?org ?orgName (COUNT(?event) AS ?positions)
WHERE {
  ?event a bioc:Title ;
         bioc:relates_to ?org .
  ?org a foaf:Organization ;
       skos:prefLabel ?orgName .
}
GROUP BY ?org ?orgName
ORDER BY DESC(?positions)
LIMIT 10
```

## Example 6: Persons by birth place

Question: What are the most common birth places of the alumni?

```sparql
PREFIX foaf: <http://xmlns.com/foaf/0.1/>
PREFIX schema: <http://schema.org/>

SELECT ?place (COUNT(?person) AS ?persons)
WHERE {
  ?person a foaf:Person ;
          schema:birthPlace ?place .
}
GROUP BY ?place
ORDER BY DESC(?persons)
LIMIT 10
```
