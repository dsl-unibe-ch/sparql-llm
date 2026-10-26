# My Project — SPARQL example queries

## Example 1: Count all persons

Question: How many persons are in the dataset?

```sparql
PREFIX foaf: <http://xmlns.com/foaf/0.1/>

SELECT (COUNT(DISTINCT ?person) AS ?personCount)
WHERE {
  ?person a foaf:Person .
}
```
