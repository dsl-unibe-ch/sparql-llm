"""Load human-readable class/property definitions and SHACL shapes from the endpoint.

The WissKI endpoint now serves the OntoME application profiles that back the R2RML
mapping as ordinary triples: the OWL ontology lives in the
``https://ontome.net/api/owl-wisski.rdf?namespace=N`` named graphs (~277 classes and
~455 properties, each with ``rdfs:label`` and ``rdfs:comment``) and the SHACL shapes in
``<...>/resource/shacl`` (12 ``sh:NodeShape``). This module queries them once at index
time and exposes the result as two maps:

* ``term_map``  : ``{curie -> {label, comment, notation, kind}}`` for classes *and*
  properties.
* ``shape_map`` : ``{class_curie -> {name, properties:[...]}}`` — human property names
  plus the ``sh:minCount``/``sh:maxCount`` cardinality the VoID statistics don't have.

``SparqlVoidShapesLoader`` consumes both to enrich the schema docs of classes that are
*present in the data*; ``OntologyProfilesLoader`` emits standalone docs for the designed
classes that are not yet populated (so the assistant is ready as the database grows).

Historically these maps were parsed from OWL/SHACL files vendored under ``data/ontology``,
because the endpoint served no labels at all. That is no longer true, and the endpoint's
copy is both broader (277 classes vs the ~34 the 6 vendored profiles covered, and 12
shapes vs 5) and authoritative — it is the same export, kept in sync by the curators.

Keys are the compressed CURIE form (``crm:E21``), which is what the VoID/data use.
"""

import curies
from langchain_core.document_loaders.base import BaseLoader
from langchain_core.documents import Document

from sparql_llm.utils import logger, query_sparql

CLASSES_DOC_TYPE = "SPARQL endpoints classes schema"

_XSD = "http://www.w3.org/2001/XMLSchema#"

# Trim the example-heavy tail of the (long, English) ontology comments to keep the
# embedded text focused; the data is French and the model is multilingual.
MAX_COMMENT_CHARS = 600

# Classes/properties from these namespaces describe the ontology itself (OWL, RDFS,
# SHACL, SKOS) rather than the modelled domain — never useful as schema docs.
_META_NAMESPACES = (
    "http://www.w3.org/2002/07/owl#",
    "http://www.w3.org/2000/01/rdf-schema#",
    "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "http://www.w3.org/ns/shacl#",
    "http://www.w3.org/2004/02/skos/core#",
    _XSD,
)

GET_ONTOLOGY_TERMS_QUERY = """PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT DISTINCT ?term ?kind ?label ?comment WHERE {
    {
        ?term a owl:Class .
        BIND("class" AS ?kind)
    } UNION {
        ?term a ?propType .
        VALUES ?propType { owl:ObjectProperty owl:DatatypeProperty }
        BIND("property" AS ?kind)
    }
    ?term rdfs:label ?label .
    OPTIONAL { ?term rdfs:comment ?comment . }
}"""

GET_SHACL_SHAPES_QUERY = """PREFIX sh: <http://www.w3.org/ns/shacl#>
SELECT DISTINCT ?targetClass ?shapeName ?path ?propName ?datatype ?cls ?min ?max ?order WHERE {
    ?shape a sh:NodeShape ;
        sh:targetClass ?targetClass ;
        sh:property ?prop .
    ?prop sh:path ?path .
    OPTIONAL { ?shape sh:name ?shapeName . }
    OPTIONAL { ?prop sh:name ?propName . }
    OPTIONAL { ?prop sh:datatype ?datatype . }
    OPTIONAL { ?prop sh:class ?cls . }
    OPTIONAL { ?prop sh:minCount ?min . }
    OPTIONAL { ?prop sh:maxCount ?max . }
    OPTIONAL { ?prop sh:order ?order . }
}"""


def _clean(text: str) -> str:
    """Collapse the indentation/newlines in serialized RDF literals into one line."""
    return " ".join(str(text).split())


def _trim_comment(text: str) -> str:
    text = _clean(text)
    if len(text) <= MAX_COMMENT_CHARS:
        return text
    cut = text[:MAX_COMMENT_CHARS]
    # Prefer to end on a sentence boundary when one is reasonably close.
    dot = cut.rfind(". ")
    if dot > MAX_COMMENT_CHARS * 0.6:
        return cut[: dot + 1]
    return cut.rstrip() + "…"


def _curie(converter: curies.Converter, iri: str) -> str:
    return converter.compress(str(iri), passthrough=True)


def _val(row: dict, key: str) -> str | None:
    """Pull a binding's value out of a SPARQL JSON result row."""
    entry = row.get(key)
    return entry["value"] if entry else None


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def fetch_ontology_terms(endpoint_url: str, converter: curies.Converter) -> dict[str, dict]:
    """Query the endpoint's OWL graphs into ``{curie -> {label, comment, notation, kind}}``."""
    term_map: dict[str, dict] = {}
    try:
        res = query_sparql(GET_ONTOLOGY_TERMS_QUERY, endpoint_url, post=True, check_service_desc=True)
    except Exception as e:
        logger.warning(f"Could not retrieve ontology terms from {endpoint_url}: {e}")
        return term_map

    for row in res["results"]["bindings"]:
        iri = _val(row, "term")
        if not iri or iri.startswith(_META_NAMESPACES):
            continue
        key = _curie(converter, iri)
        label = _val(row, "label")
        comment = _val(row, "comment")

        entry = term_map.setdefault(key, {"notation": key, "kind": _val(row, "kind") or "class"})
        # An IRI can be labelled in several languages/graphs; keep the first of each.
        if label and "label" not in entry:
            entry["label"] = _clean(label)
        if comment and "comment" not in entry:
            entry["comment"] = _trim_comment(comment)

    logger.info(f"Fetched {len(term_map)} ontology terms from {endpoint_url}")
    return term_map


def fetch_shacl_shapes(endpoint_url: str, converter: curies.Converter) -> dict[str, dict]:
    """Query the endpoint's SHACL graph into ``{class_curie -> {name, properties:[...]}}``.

    Property nodes are shared between shapes in this endpoint (the generic
    ``sdh-short:P9``/``P10``/``P11``/``P12`` label nodes hang off several of them), so the
    same ``sh:path`` comes back repeatedly per target class. Dedupe on the full property
    signature rather than the path alone — a class legitimately has both a shortcut and a
    fully-modelled version of the same path (e.g. ``P9`` as a literal *and* via ``crm:E62``).
    """
    shape_map: dict[str, dict] = {}
    try:
        res = query_sparql(GET_SHACL_SHAPES_QUERY, endpoint_url, post=True, check_service_desc=True)
    except Exception as e:
        logger.warning(f"Could not retrieve SHACL shapes from {endpoint_url}: {e}")
        return shape_map

    seen: dict[str, set[tuple]] = {}
    for row in res["results"]["bindings"]:
        target_iri = _val(row, "targetClass")
        path_iri = _val(row, "path")
        if not target_iri or not path_iri:
            continue
        key = _curie(converter, target_iri)

        cls = _val(row, "cls")
        datatype = _val(row, "datatype")
        if cls:
            target = _curie(converter, cls)
        elif datatype:
            target = f"xsd:{datatype[len(_XSD):]}" if datatype.startswith(_XSD) else _curie(converter, datatype)
        else:
            target = None

        prop = {
            "path": _curie(converter, path_iri),
            "name": _val(row, "propName"),
            "target": target,
            "min": _int_or_none(_val(row, "min")),
            "max": _int_or_none(_val(row, "max")),
            "order": _int_or_none(_val(row, "order")),
        }
        signature = (prop["path"], prop["name"], prop["target"], prop["min"], prop["max"])
        if signature in seen.setdefault(key, set()):
            continue
        seen[key].add(signature)

        shape = shape_map.setdefault(key, {"name": None, "properties": []})
        if shape["name"] is None:
            shape["name"] = _val(row, "shapeName")
        shape["properties"].append(prop)

    for shape in shape_map.values():
        shape["properties"].sort(key=lambda p: p["order"] if p["order"] is not None else 999)

    logger.info(f"Fetched {len(shape_map)} SHACL node shapes from {endpoint_url}")
    return shape_map


def _cardinality(prop: dict) -> str:
    lo = prop["min"] if prop["min"] is not None else 0
    hi = "*" if prop["max"] is None else prop["max"]
    return f"[{lo}..{hi}]"


def format_shape_properties(shape: dict) -> str:
    """Render a SHACL shape's properties as readable ShEx-style comment lines."""
    lines = []
    for p in shape.get("properties", []):
        name = f' "{p["name"]}"' if p["name"] else ""
        target = f" -> {p['target']}" if p["target"] else ""
        lines.append(f"#   {p['path']}{name}{target} {_cardinality(p)}")
    if not lines:
        return ""
    return "# Designed properties (SHACL profile):\n" + "\n".join(lines)


def build_designed_shape_text(curie: str, term: dict | None, shape: dict | None) -> str:
    """Build the ShEx-like 'answer' text for a designed (not-yet-populated) class."""
    text = f"shape:{curie.replace(':', '_')} {{\n  a [ {curie} ] ;\n"
    if shape:
        props = format_shape_properties(shape)
        if props:
            text += "  " + props.replace("\n", "\n  ") + "\n"
    text = text.rstrip(" ;\n") + "\n}"
    if term and term.get("comment"):
        label = term.get("label", curie)
        text += f"\n# {label} ({curie}): {term['comment']}"
    return text


class OntologyProfilesLoader(BaseLoader):
    """Emit schema docs for the *designed* classes that are not yet present in the data.

    Scoped to the classes the curators gave a SHACL shape. The endpoint serves the full
    CIDOC CRM + SDHSS ontologies (276 classes, 179 of them SDHSS) rather than a
    project-scoped subset, so namespace filtering does not work here — the 12 SHACL shapes
    are the only signal for "what this project actually models". The trade-off: a class the
    curators designed but have not shaped yet gets no doc (currently ``sdh-slc:C5``/``C6``/
    ``C12``, the membership/social-role classes). Those return automatically once they are
    either shaped or loaded into the data.

    Classes already covered by the VoID->ShEx loader are skipped (``skip_iris``) so we never
    produce two competing docs for the same IRI. As the data grows, classes move from here
    to the enriched VoID docs automatically on the next re-index.
    """

    def __init__(
        self,
        endpoint_url: str,
        term_map: dict[str, dict],
        shape_map: dict[str, dict],
        converter: curies.Converter,
        skip_iris: set[str] | None = None,
    ):
        self.endpoint_url = endpoint_url
        self.term_map = term_map or {}
        self.shape_map = shape_map or {}
        self.converter = converter
        self.skip_iris = skip_iris or set()

    def load(self) -> list[Document]:
        docs: list[Document] = []
        for curie in sorted(self.shape_map.keys()):
            if curie in self.skip_iris:
                continue
            term = self.term_map.get(curie)
            shape = self.shape_map.get(curie)
            label = (term or {}).get("label") or (shape or {}).get("name") or curie
            try:
                iri = self.converter.expand(curie) or curie
            except Exception:
                iri = curie

            metadata = {
                "answer": build_designed_shape_text(curie, term, shape),
                "endpoint_url": self.endpoint_url,
                "iri": iri,
                "doc_type": CLASSES_DOC_TYPE,
            }
            docs.append(Document(page_content=label, metadata={"question": label, **metadata}))
            # Separate doc for the definition (mirrors SparqlVoidShapesLoader behaviour).
            if term and term.get("comment"):
                docs.append(
                    Document(
                        page_content=term["comment"],
                        metadata={"question": term["comment"], **metadata},
                    )
                )

        logger.info(f"Extracted {len(docs)} designed-schema docs for {self.endpoint_url}")
        return docs
