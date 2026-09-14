"""A SPARQL syntax error must be reported where it is, not where the parser gave up.

rdflib parses with pyparsing, which backs out of a failing nested block and
reports the error at the start of an enclosing one. qwen repeatedly wrote the
query below; the validator told it "Expected SelectQuery, found 'GRAPH'", six
lines before the actual mistake (after ";" the subject is still ?marriage, so
"?rel sdh-short:P9 ?relType" is one term too many).
"""

from rdflib.plugins.sparql import prepareQuery

from sparql_llm.utils import get_prefix_converter
from sparql_llm.validate_sparql import describe_parse_error, validate_sparql_with_void

QUERY = """PREFIX sdh-slc: <https://sdhss.org/ontology/social-life-core/>
PREFIX sdh-short: <https://sdhss.org/ontology/shortcuts/>
PREFIX swel: <https://elites-suisses.lod4hss.org/resource/>

SELECT DISTINCT ?spouse ?spouseName ?relType
WHERE {
  GRAPH <https://swiss-elites.lod4hss.cloud/resource/> {
    ?marriage a sdh-slc:C3 ;
              sdh-slc:P15 swel:p50001 ;
              sdh-slc:P15 ?spouse .
    FILTER(?spouse != swel:p50001)
    OPTIONAL { ?spouse sdh-short:P9 ?spouseName }
    OPTIONAL { ?marriage sdh-slc:P16 ?rel ; ?rel sdh-short:P9 ?relType }
  }
}"""


def _parse_error(query: str) -> Exception:
    try:
        prepareQuery(query)
    except Exception as e:
        return e
    raise AssertionError("query unexpectedly parsed")


def test_error_in_a_nested_block_points_at_the_mistake():
    message = describe_parse_error(QUERY, _parse_error(QUERY))
    assert "line 13" in message
    assert "?relType" in message
    assert "OPTIONAL { ?marriage sdh-slc:P16 ?rel ; ?rel sdh-short:P9 ?relType }" in message
    assert "GRAPH" not in message


def test_error_outside_any_block_keeps_the_parser_message():
    query = "SELCT ?s WHERE { ?s ?p ?o }"
    error = _parse_error(query)
    assert describe_parse_error(query, error) == str(error)


def test_validator_reports_the_located_error():
    # The message the model actually reads, from the execute_sparql_query tool.
    issues = validate_sparql_with_void(QUERY, "https://example.org/sparql", get_prefix_converter({}), {})
    (issue,) = issues
    assert issue.startswith("Error parsing the SPARQL query: ")
    assert "line 13" in issue


def test_error_already_located_keeps_the_parser_message():
    # The parser's own position is right when the mistake is in the outermost
    # block; the scan finds nothing further and must not reword it.
    query = "SELECT ?s WHERE { ?s ?p ?o FILTER(?o = ) }"
    error = _parse_error(query)
    assert describe_parse_error(query, error) == str(error)
