<!--
SDHSS snippet for dataset.md. For a project modelled with CIDOC CRM and the SDHSS ontologies
(as WissKI / OntoME projects usually are), paste these sections into your dataset.md and
adjust them: keep only the classes and properties your data uses, and add your own
entity namespace (the prefix of your entity URIs) to the list.
-->

## Prefixes

- crm:       <http://www.cidoc-crm.org/cidoc-crm/>           — E21 (Person), E67 (Birth), E69 (Death), E74 (Group); P96 (by mother), P97 (from father), P98 (brought into life)
- sdh-slc:   <https://sdhss.org/ontology/social-life-core/>  — note the "-core/" suffix. C5 (Membership: a person's role in a group), C3 (Social Relationship, e.g. marriages), C4 (Social Relationship Type), C11 (Gender); P1 (membership → person), P2 (membership → group), P15 (relationship → partner), P16 (has relationship type), P23 (has gender)
- sdh-sls:   <https://sdhss.org/ontology/social-life-specific/> — social-life-SPECIFIC (not "-core/"). C7 (Obtaining a Study Title — the event), C8 (Study title), C9 (Academic Discipline); P9 (was obtained by), P10 (is obtention of), P11 (has academic supervisor), P17 (is delivered by)
- sdh-short: <https://sdhss.org/ontology/shortcuts/>         — P9 (has standard label — the name of persons, groups and places), P1 (at some time within — dates on events), P2 (birth date, directly on the person), P13 (death date), P4 / P7 (membership start / end year)
- sdh:       <https://sdhss.org/ontology/core/>              — C13 (Geographical Place), C51 (Geographical Place Kind); P6 (event took place at)
- xsd:       <http://www.w3.org/2001/XMLSchema#>

## Rules for this dataset

- Names are in sdh-short:P9 (a plain literal). Say here how person names are written, e.g. "Surname, Firstname".
- Use the bare class names as they are in the graph, e.g. `crm:E21`, not `crm:E21_Person`.
- The "-core/" and "-specific/" namespaces are different: sdh-slc:C9 and sdh-sls:C9 are unrelated classes.

<!--
In project.json, an SDHSS project usually also sets:
  "entities": {"label_property": "https://sdhss.org/ontology/shortcuts/P9", ...}
  "ontology_from_endpoint": true    (fetches the OntoME class definitions and SHACL shapes)
-->
