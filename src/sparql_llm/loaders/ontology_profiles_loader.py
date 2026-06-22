"""Load human-readable class/property definitions from the OntoME OWL application
profiles and SHACL shapes that back the endpoint's R2RML mapping.

The live WissKI endpoint serves almost no ``rdfs:label``/``rdfs:comment``, so the
VoID->ShEx schema docs are otherwise just opaque IRIs (``crm:E21``, ``sdh-slc:P23``).
This module parses the *designed* schema once at index time and exposes it as two maps:

* ``term_map``  : ``{curie -> {label, comment, notation, kind}}`` from the OWL profiles.
* ``shape_map`` : ``{class_curie -> {name, properties:[...]}}`` from the SHACL shapes
  (human property names + cardinality, which the VoID lacks).

``SparqlVoidShapesLoader`` consumes both to enrich the schema docs of classes that are
*present in the data*; ``OntologyProfilesLoader`` emits standalone docs for the designed
classes that are not yet populated (so the assistant is ready as the database grows).

Keys are the compressed/notation form (``crm:E21``) because the OntoME ``rdf:about`` for
CIDOC classes is ``.../E21_Person`` while the data/VoID use ``.../E21``; ``skos:notation``
is the reliable bridge between the two.
"""

import glob
import os

import curies
from langchain_core.document_loaders.base import BaseLoader
from langchain_core.documents import Document
from rdflib import RDF, RDFS, Graph
from rdflib.namespace import OWL, SH, SKOS

from sparql_llm.utils import logger

CLASSES_DOC_TYPE = "SPARQL endpoints classes schema"

_XSD = "http://www.w3.org/2001/XMLSchema#"

# Trim the example-heavy tail of the (long, English) ontology comments to keep the
# embedded text focused; the data is French and the model is multilingual.
MAX_COMMENT_CHARS = 600

# Manually-verified definitions for IRIs that do NOT resolve against the application
# profiles because of namespace inconsistencies between the live data and the exported
# ontology. Keyed by the compressed/notation IRI used in the VoID. Curated entries win
# over anything parsed from the profiles.
#
# - sdh-slc:C9 — the R2RML mapping classes marriages/unions under
#   ``social-life-core/C9`` (see <#marriage1>: partners via sdh-slc:P20, type via
#   sdh-slc:P16), but the ontology only defines the union concept under the older
#   ``social-life/C9`` module ("Union"). No exact-IRI match exists, so we supply it.
CURATED_TERMS: dict[str, dict[str, str]] = {
    "sdh-slc:C9": {
        "label": "Marriage / Union",
        "comment": (
            "A union between two persons - a marriage or partnership of any social or "
            "legal form, with an optional time span (start/end). In this database the "
            "partners are linked with sdh-slc:P20 and the kind of union with sdh-slc:P16. "
            "Same-sex unions are also encoded with this class."
        ),
        "notation": "sdh-slc:C9",
        "kind": "class",
    },
}


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


def _pick_literal(graph: Graph, subj, pred) -> str | None:
    """Return the English literal for (subj, pred) when available, else any literal."""
    values = list(graph.objects(subj, pred))
    if not values:
        return None
    for v in values:
        if getattr(v, "language", None) == "en":
            return str(v)
    return str(values[0])


def _curie(converter: curies.Converter, iri: str) -> str:
    return converter.compress(str(iri), passthrough=True)


def parse_ontology_profiles(profiles_dir: str, converter: curies.Converter) -> dict[str, dict]:
    """Parse the OWL application profiles into ``{curie -> {label, comment, notation, kind}}``."""
    term_map: dict[str, dict] = {}
    if not profiles_dir or not os.path.isdir(profiles_dir):
        return dict(CURATED_TERMS)

    class_types = {OWL.Class}
    prop_types = {OWL.ObjectProperty, OWL.DatatypeProperty, RDF.Property}

    for path in sorted(glob.glob(os.path.join(profiles_dir, "*.rdf"))):
        g = Graph()
        try:
            g.parse(path, format="xml")
        except Exception as e:
            logger.warning(f"Could not parse ontology profile {path}: {e}")
            continue

        for subj in set(g.subjects(RDF.type, None)):
            types = set(g.objects(subj, RDF.type))
            if types & class_types:
                kind = "class"
            elif types & prop_types:
                kind = "property"
            else:
                continue

            notation = _pick_literal(g, subj, SKOS.notation)
            key = notation or _curie(converter, subj)
            label = _pick_literal(g, subj, RDFS.label)
            comment = _pick_literal(g, subj, RDFS.comment)

            entry = term_map.setdefault(key, {"notation": notation or key, "kind": kind})
            if label and "label" not in entry:
                entry["label"] = _clean(label)
            if comment and "comment" not in entry:
                entry["comment"] = _trim_comment(comment)

    # Curated overrides win over parsed values.
    for key, entry in CURATED_TERMS.items():
        term_map[key] = dict(entry)

    logger.info(f"Parsed {len(term_map)} ontology terms from {profiles_dir}")
    return term_map


def parse_shacl_shapes(shacl_dir: str, converter: curies.Converter) -> dict[str, dict]:
    """Parse the SHACL node shapes into ``{class_curie -> {name, properties:[...]}}``.

    Each property carries the human ``sh:name``, the target ``sh:datatype``/``sh:class``,
    and the ``sh:minCount``/``sh:maxCount`` cardinality the VoID statistics don't have.
    """
    shape_map: dict[str, dict] = {}
    if not shacl_dir or not os.path.isdir(shacl_dir):
        return shape_map

    for path in sorted(glob.glob(os.path.join(shacl_dir, "*.ttl"))):
        g = Graph()
        try:
            g.parse(path, format="turtle")
        except Exception as e:
            logger.warning(f"Could not parse SHACL file {path}: {e}")
            continue

        for shape in g.subjects(RDF.type, SH.NodeShape):
            target = g.value(shape, SH.targetClass)
            if target is None:
                continue
            key = _curie(converter, target)
            name = g.value(shape, SH.name)

            props = []
            for pnode in g.objects(shape, SH.property):
                path_iri = g.value(pnode, SH.path)
                if path_iri is None:
                    continue
                datatype = g.value(pnode, SH.datatype)
                cls = g.value(pnode, SH["class"])
                min_count = g.value(pnode, SH.minCount)
                max_count = g.value(pnode, SH.maxCount)
                order = g.value(pnode, SH.order)
                name = g.value(pnode, SH.name)
                if cls is not None:
                    target = _curie(converter, cls)
                elif datatype is not None:
                    dt = str(datatype)
                    target = f"xsd:{dt[len(_XSD):]}" if dt.startswith(_XSD) else _curie(converter, dt)
                else:
                    target = None
                props.append(
                    {
                        "path": _curie(converter, path_iri),
                        "name": str(name) if name else None,
                        "target": target,
                        "min": int(min_count) if min_count is not None else None,
                        "max": int(max_count) if max_count is not None else None,
                        "order": int(order) if order is not None else None,
                    }
                )
            props.sort(key=lambda p: p["order"] if p["order"] is not None else 999)
            shape_map[key] = {"name": str(name) if name else None, "properties": props}

    logger.info(f"Parsed {len(shape_map)} SHACL node shapes from {shacl_dir}")
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

    Classes already covered by the VoID->ShEx loader are skipped (``skip_iris``) so we
    never produce two competing docs for the same IRI. As the data grows, classes move
    from here to the enriched VoID docs automatically on the next re-index.
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
        # Union of classes known from SHACL shapes and OWL profiles.
        class_curies = set(self.shape_map.keys())
        class_curies |= {k for k, v in self.term_map.items() if v.get("kind") == "class"}

        for curie in sorted(class_curies):
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
