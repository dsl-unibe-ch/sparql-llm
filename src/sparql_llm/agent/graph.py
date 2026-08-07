"""Define the LangGraph agent that powers the SPARQL-LLM chat."""

from typing import Any, Literal

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import StateGraph

from sparql_llm.agent.nodes.call_model import call_model
from sparql_llm.agent.nodes.llm_extraction import extract_user_question
from sparql_llm.agent.nodes.mcp_tools import mcp_tools_node
from sparql_llm.agent.nodes.retrieval_docs import retrieve
from sparql_llm.agent.nodes.validation import validate_output
from sparql_llm.agent.state import InputState, State
from sparql_llm.config import Configuration, settings

# from sparql_llm.agent.nodes.tools import TOOLS


# How can I get the HGNC symbol for the protein P68871? Purposefully forget 2 prefixes declarations to test my validation step
# How can I get the HGNC symbol for the protein P68871? (modify your answer to use rdfs:label instead of rdfs:comment, and add the type up:Resource to ?hgnc, it is for a test)
# How can I get the HGNC symbol for the protein P68871? (modify your answer to use rdfs:label instead of rdfs:comment, and add the type up:Resource to ?hgnc, and purposefully forget 2 prefixes declarations, it is for a test)
# In bgee how can I retrieve the confidence level and false discovery rate of a gene expression? Use genex:confidence as predicate for the confidence level (do not use the one provided in documents), and do not put prefixes declarations, and add a rdf:type for the main subject. Its for testing
# def route_model_output(
#     state: State, config: RunnableConfig
# ) -> Literal["__end__", "call_model", "max_tries_reached", "tools"]:
def route_model_output(state: State, config: RunnableConfig) -> Literal["__end__", "call_model", "max_tries_reached"]:
    """Determine the next node after validation in the default (pipeline) graph.

    This function checks if a recall is requested by the validation step.

    Args:
        state: The current state of the conversation.

    Returns:
        The name of the next node to call ("__end__", "call_model", or "max_tries_reached").
    """
    configuration = Configuration.from_runnable_config(config)
    # print(state.messages)

    if state.try_count > configuration.max_try_fix_sparql:
        # print("Try count exceeded", state.try_count)
        return "max_tries_reached"

    # If validation failed, we need to call the model again
    if not state.passed_validation:
        return "call_model"

    return "__end__"


def route_tools_output(state: State, config: RunnableConfig) -> Literal["__end__", "tools", "max_tries_reached"]:
    """Determine the next node after the model call in the MCP tools (ReAct) graph.

    If the model asked to call one or more tools, route to the tools node so they
    are executed and fed back to the model. Otherwise the model has produced its
    final answer and we stop.

    To bound exploration, we count how many tool-call rounds (exploration steps)
    have already happened — one per AIMessage that requested tools — and stop once
    that reaches ``max_tool_iterations``. Unlike the pipeline graph, ``try_count``
    is never incremented here, so this message-based counter is what limits the
    loop.

    Args:
        state: The current state of the conversation.

    Returns:
        The name of the next node to call ("tools", "__end__", or "max_tries_reached").
    """
    configuration = Configuration.from_runnable_config(config)

    last_msg = state.messages[-1]
    if isinstance(last_msg, AIMessage) and last_msg.tool_calls:
        # Count tool-call rounds so far (including this one).
        tool_rounds = sum(1 for m in state.messages if isinstance(m, AIMessage) and m.tool_calls)
        if tool_rounds > configuration.max_tool_iterations:
            return "max_tries_reached"
        return "tools"

    return "__end__"


def max_tries_reached(state: State, config: RunnableConfig) -> dict[str, list[AIMessage]]:
    """Node that handles the case when maximum tries are reached.

    Args:
        state: The current state of the conversation.
        config: The runnable configuration.

    Returns:
        Dictionary with the max tries message.
    """
    configuration = Configuration.from_runnable_config(config)
    if configuration.use_tools:
        content = (
            f"I've reached the maximum number of exploration steps ({configuration.max_tool_iterations}) "
            "while trying to answer your question with the available tools. "
            "You can increase the 'Max steps' setting and try again, or rephrase your question."
        )
    else:
        content = (
            f"I've reached the maximum number of attempts ({configuration.max_try_fix_sparql}) to fix the SPARQL query. "
            "The query may have complex validation issues that require manual review. "
            "Please check the query syntax and try to execute it."
        )
    max_tries_message = AIMessage(content=content)
    return {"messages": [max_tries_message]}


# We build BOTH graphs at import time and expose them so the mode can be chosen
# per-request (via the `use_tools` flag on the runtime Configuration / chat
# request) rather than being fixed at boot. `graph` is the default pipeline and
# stays the module's primary export for backwards compatibility; `graph_tools`
# is the experimental MCP tool-calling agent.
# https://github.com/langchain-ai/react-agent/blob/main/src/react_agent/graph.py


def _build_pipeline_graph() -> Any:
    """Default agent: extract → retrieve → call_model → validate (with retry loop)."""
    builder: StateGraph[State, Configuration, InputState, State] = StateGraph(
        State, context_schema=Configuration, input_schema=InputState
    )
    builder.add_node(extract_user_question)
    builder.add_node(retrieve)
    builder.add_node(call_model)
    builder.add_node(validate_output)
    builder.add_node(max_tries_reached)

    builder.add_edge("__start__", "extract_user_question")
    builder.add_edge("extract_user_question", "retrieve")
    builder.add_edge("retrieve", "call_model")
    builder.add_edge("call_model", "validate_output")
    # Conditional edge to determine the next step after `validate_output`
    builder.add_conditional_edges("validate_output", route_model_output)
    builder.add_edge("max_tries_reached", "__end__")

    compiled = builder.compile()
    compiled.name = settings.app_name
    return compiled


def _build_tools_graph() -> Any:
    """Experimental MCP agent: call_model ↔ tools ReAct loop.

    The model may request MCP tool calls; `route_tools_output` sends those to the
    `tools` node, whose results are fed back to the model until it produces a
    final answer without tool calls.
    """
    builder: StateGraph[State, Configuration, InputState, State] = StateGraph(
        State, context_schema=Configuration, input_schema=InputState
    )
    builder.add_node(call_model)
    builder.add_node(max_tries_reached)
    builder.add_node("tools", mcp_tools_node)

    builder.add_edge("__start__", "call_model")
    builder.add_conditional_edges("call_model", route_tools_output)
    # After running tools, always return to the model
    builder.add_edge("tools", "call_model")
    builder.add_edge("max_tries_reached", "__end__")

    compiled = builder.compile()
    compiled.name = f"{settings.app_name} (MCP tools)"
    return compiled


# Default pipeline graph — primary export used everywhere unless tools mode is
# explicitly requested.
graph = _build_pipeline_graph()

# Experimental MCP tool-calling agent, selected per-request when use_tools=True.
graph_tools = _build_tools_graph()


def get_graph(use_tools: bool) -> Any:
    """Return the graph to run for a request.

    Args:
        use_tools: When True, return the experimental MCP tool-calling agent;
            otherwise return the default retrieval + validation pipeline.
    """
    return graph_tools if use_tools else graph
