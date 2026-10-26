## About the dataset

Norssi High School Alumni: the register book of the Helsinki Normal Lyceum (Norssi), with short biographies of its 10,137 students from 1867 to 1992, published as linked open data by Aalto SeCo. It covers each student's dates, birth place, gender, careers (titles held in organisations), family relations to other students and hobbies. Most labels are in Finnish.

## Prefixes

- foaf: <http://xmlns.com/foaf/0.1/> — foaf:Person (the students), foaf:Organization (employers and other organisations)
- schema: <http://schema.org/> — note: **http**, not https. schema:birthDate, schema:deathDate, schema:birthPlace, schema:gender (schema:Male or schema:Female), schema:hobby, schema:givenName, schema:familyName, schema:startDate and schema:endDate (on career titles)
- skos: <http://www.w3.org/2004/02/skos/core#> — skos:prefLabel is the name of persons, organisations, hobbies and title types
- bioc: <http://ldf.fi/schema/bioc/> — careers and family relations (see below)
- reg: <http://ldf.fi/schema/person_registry/> — register facts: reg:enrollmentYear, reg:matriculationYear
- norssit: <http://ldf.fi/norssit/> — the persons (e.g. norssit:norssi_2230 is Mika Waltari), hobbies (norssit:Hobby) and title types

## Graphs

The data sits in eight named graphs, and the default graph is their union: write queries without a GRAPH clause.

## Names and dates

- Person names are in skos:prefLabel as "Surname, Firstnames", e.g. "Waltari, Mika Toimi". To find a person by name, filter case-insensitively on part of the label.
- schema:birthDate is a full xsd:date for most persons; schema:deathDate is often a year only. Use YEAR() on birth dates, and compare death dates as text when only the year matters.
- Birth places (schema:birthPlace) are IRIs under <http://ldf.fi/places/> with **no label**: report the IRI's last part as the place name.

## Careers

A career step is a bioc:Title: `?t a bioc:Title ; bioc:title_inheres_in ?person ; bioc:relates_to ?organisation ; skos:prefLabel ?titleText`, with optional schema:startDate and schema:endDate.
Each title also has a type class under <http://ldf.fi/norssit/titles/>, named and labelled in Finnish, e.g. <http://ldf.fi/norssit/titles/professori> ("professori", professor). To count people with a job, match `?t a <http://ldf.fi/norssit/titles/NAME>` and count DISTINCT persons.

## Family relations

`?person bioc:has_family_relation ?r . ?r a ?relationType ; bioc:inheres_in ?relative .` The relation type names the **relative's** role from the person's point of view: bioc:Father means ?relative is the person's father; also bioc:Brother, bioc:Sister, bioc:Son, bioc:Daughter, bioc:GrandFather, bioc:BrotherInLaw.

## Hobbies

`?person schema:hobby ?h . ?h skos:prefLabel ?hobbyName .` Hobby names are in Finnish, e.g. "musiikki" (music), "kalastus" (fishing).

## Rules for this dataset

- Count persons with COUNT(DISTINCT ?person): a person can have several titles, relations or hobbies.
- Women are `?person schema:gender schema:Female`.
