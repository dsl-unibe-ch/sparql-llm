"""Held-out questions for scripts/compare_modes.py: none of them is worded like, or answered verbatim by, a curated example.

Each has a reference query. `keys` are words a correct answer must contain ("a|b" = either);
`key_col` takes them from the reference results instead (surnames for "Surname, First" names).
"""

P = """PREFIX crm: <http://www.cidoc-crm.org/cidoc-crm/>
PREFIX sdh: <https://sdhss.org/ontology/core/>
PREFIX sdh-slc: <https://sdhss.org/ontology/social-life-core/>
PREFIX sdh-sls: <https://sdhss.org/ontology/social-life-specific/>
PREFIX sdh-short: <https://sdhss.org/ontology/shortcuts/>
"""
G = "GRAPH <https://swiss-elites.lod4hss.cloud/resource/>"
FC = """?m a sdh-slc:C5 ; sdh-slc:P1 ?person ; sdh-slc:P2 ?grp .
    ?grp sdh-short:P9 ?gn . FILTER(LCASE(STR(?gn)) = "conseil fédéral")"""


def name_filter(*parts: str, var: str = "?n") -> str:
    return "FILTER(" + " && ".join(f'CONTAINS(LCASE(STR({var})), "{p}")' for p in parts) + ")"


HELDOUT = [
    {
        "id": "H01",
        "question": "How many people does the database contain?",
        "gold": P + f"SELECT (COUNT(DISTINCT ?p) AS ?n) WHERE {{ {G} {{ ?p a crm:E21 }} }}",
        "key_col": "n",
    },
    {
        "id": "H02",
        "question": "Who were Ernst Brenner's parents?",
        "gold": P + f"""SELECT DISTINCT ?parentName WHERE {{ {G} {{
    ?p a crm:E21 ; sdh-short:P9 ?n . {name_filter("brenner", "ernst")}
    ?b crm:P98 ?p . {{ ?b crm:P96 ?par }} UNION {{ ?b crm:P97 ?par }}
    ?par sdh-short:P9 ?parentName }} }}""",
        "key_col": "parentName",
    },
    {
        "id": "H03",
        "question": "Who was Ernst Brenner married to?",
        "gold": P + f"""SELECT DISTINCT ?spouseName WHERE {{ {G} {{
    ?p a crm:E21 ; sdh-short:P9 ?n . {name_filter("brenner", "ernst")}
    ?rel a sdh-slc:C3 ; sdh-slc:P15 ?p ; sdh-slc:P15 ?sp . FILTER(?sp != ?p)
    ?sp sdh-short:P9 ?spouseName }} }}""",
        "key_col": "spouseName",
    },
    {
        "id": "H04",
        "question": "Who sat on the Federal Council in 1950?",
        "gold": P + f"""SELECT DISTINCT ?personName WHERE {{ {G} {{
    {FC} ?m sdh-short:P4 ?start . OPTIONAL {{ ?m sdh-short:P7 ?end }}
    ?person sdh-short:P9 ?personName .
    FILTER(?start <= 1950 && (!BOUND(?end) || ?end >= 1950)) }} }}""",
        "key_col": "personName",
    },
    {
        "id": "H05",
        "question": "When was Adolf Ogi a member of the Federal Council?",
        "gold": P + f"""SELECT ?start ?end WHERE {{ {G} {{
    {FC} ?person sdh-short:P9 ?n . {name_filter("ogi", "adolf")}
    ?m sdh-short:P4 ?start . OPTIONAL {{ ?m sdh-short:P7 ?end }} }} }}""",
        "keys": ["1988", "2000"],
    },
    {
        "id": "H06",
        "question": "Where was Marcel Pilet-Golaz born?",
        "gold": P + f"""SELECT ?placeName WHERE {{ {G} {{
    ?p a crm:E21 ; sdh-short:P9 ?n . {name_filter("pilet-golaz")}
    ?b a crm:E67 ; crm:P98 ?p ; sdh:P6 ?pl . ?pl sdh-short:P9 ?placeName }} }}""",
        "key_col": "placeName",
    },
    {
        "id": "H07",
        "question": "How many women are recorded in the database?",
        "gold": P + f"""SELECT (COUNT(DISTINCT ?p) AS ?n) WHERE {{ {G} {{
    ?p a crm:E21 ; sdh-slc:P23 ?g . ?g sdh-short:P9 ?gl . FILTER(LCASE(STR(?gl)) = "female") }} }}""",
        "key_col": "n",
    },
    {
        "id": "H08",
        "question": "What did Enrico Celio study, and at which university?",
        "gold": P + f"""SELECT ?disc ?inst ?date WHERE {{
    ?p sdh-short:P9 ?n . {name_filter("celio", "enrico")} FILTER(!CONTAINS(LCASE(STR(?n)), "cattaneo"))
    ?o a sdh-sls:C7 ; sdh-sls:P9 ?p .
    OPTIONAL {{ ?o sdh-sls:P25 ?d . ?d sdh-short:P9 ?disc }}
    OPTIONAL {{ ?o sdh-sls:P17 ?i . ?i sdh-short:P9 ?inst }}
    OPTIONAL {{ ?o sdh-short:P1 ?date }} }}""",
        "keys": ["droit|law|jurisprudence|rechtswissenschaft|legal", "fribourg|freiburg"],
    },
    {
        "id": "H09",
        "question": "Which university awarded the most study titles in the database?",
        "gold": P + """SELECT ?inst (COUNT(DISTINCT ?o) AS ?n) WHERE {
    ?o a sdh-sls:C7 ; sdh-sls:P17 ?i . ?i sdh-short:P9 ?inst }
GROUP BY ?inst ORDER BY DESC(?n) LIMIT 1""",
        "keys": ["Universität Zürich|University of Zurich|Université de Zurich|UZH"],
    },
    {
        "id": "H10",
        "question": "Who were the parents of Anne Louise Naville?",
        "gold": P + f"""SELECT DISTINCT ?parentName WHERE {{ {G} {{
    ?p a crm:E21 ; sdh-short:P9 ?n . {name_filter("naville", "anne louise")}
    ?b crm:P98 ?p . {{ ?b crm:P96 ?par }} UNION {{ ?b crm:P97 ?par }}
    ?par sdh-short:P9 ?parentName }} }}""",
        "keys": ["Catargi", "Jules"],
    },
    {
        "id": "H11",
        "question": "Combien de conseillers fédéraux sont nés à Bâle ?",
        "gold": P + f"""SELECT (COUNT(DISTINCT ?person) AS ?n) WHERE {{ {G} {{
    {FC}
    ?b a crm:E67 ; crm:P98 ?person ; sdh:P6 ?pl . ?pl sdh-short:P9 ?pn .
    FILTER(LCASE(STR(?pn)) IN ("basel", "bâle", "basle")) }} }}""",
        "key_col": "n",
    },
    {
        "id": "H12",
        "question": "Which federal councillors studied at ETH Zurich?",
        "gold": P + f"""SELECT DISTINCT ?name WHERE {{
    {G} {{ {FC} ?person sdh-short:P9 ?name . }}
    ?o a sdh-sls:C7 ; sdh-sls:P9 ?person ; sdh-sls:P17 ?i . ?i sdh-short:P9 ?il .
    FILTER(CONTAINS(LCASE(STR(?il)), "technische hochschule zürich") || CONTAINS(LCASE(STR(?il)), "ethz")) }}""",
        "key_col": "name",
    },
    {
        "id": "H13",
        "question": "What was Adolf Ogi's annual salary as a federal councillor?",
        "gold": None,
        "scoring": "unanswerable",
    },
]
