"""Custom MCP tool node for handling async tool calls."""

import re
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_mcp_adapters.client import MultiServerMCPClient

from sparql_llm.agent.request_project import project_of
from sparql_llm.agent.state import State
from sparql_llm.config import Configuration
from sparql_llm.mcp_server import EMPTY_RESULT_PROMPT, FIXED_QUERY_HEADING, QUERY_RESULTS_HEADING, agent_connection
from sparql_llm.project import Project, allowed_endpoint
from sparql_llm.utils import logger
from sparql_llm.validate_sparql import extract_sparql_queries

# The "tools" node of the MCP tools agent: it runs the tool calls call_model's model made.


def executed_query(tool_call: dict[str, Any], content: str, project: Project) -> dict[str, str] | None:
    """The query an execute_sparql_query call ran, for the chat's "Run or edit the query" link.

    Only a query that reached the endpoint counts (rows or an empty result), not one refused
    by validation or failed. When the tool fixed the prefixes first, the fixed query is the
    one that ran. Its endpoint is the dataset's, as the tool ran it.
    """
    if tool_call["name"] != "execute_sparql_query":
        return None
    if QUERY_RESULTS_HEADING not in content and EMPTY_RESULT_PROMPT not in content:
        return None
    args = tool_call.get("args") or {}
    query = args.get("sparql_query")
    if content.startswith(FIXED_QUERY_HEADING):
        fixed = re.match(re.escape(FIXED_QUERY_HEADING) + r"\n```sparql\n(.*?)\n```", content, re.S)
        if fixed:
            query = fixed.group(1)
    if not query:
        return None
    endpoint_url, _ = allowed_endpoint(args.get("endpoint_url"), project)
    return {"sparql_query": query, "sparql_endpoint_url": endpoint_url}


def _comparable(query: str) -> str:
    """A query without its ``#+ endpoint:`` line and with whitespace collapsed."""
    lines = [line for line in query.splitlines() if not line.strip().startswith("#+ endpoint:")]
    return re.sub(r"\s+", " ", " ".join(lines)).strip()


def log_query_link_mismatch(answer: str, query_link: Any) -> bool | None:
    """Log whether the answer's last ```sparql block is the query behind "Run or edit the query".

    The link carries the last query that ran; the answer's block is the model's own
    re-typing of it, which nothing checks. Returns True when they match, False when
    they differ, None when there is nothing to compare.
    """
    linked = query_link.get("sparql_query") if isinstance(query_link, dict) else None
    printed = extract_sparql_queries(answer or "")
    if not linked and not printed:
        return None
    if not linked or not printed:
        logger.info(
            "Query link check: %s",
            "answer prints a query but none ran (no link)"
            if printed
            else "a query ran (link) but the answer prints none",
        )
        return None
    printed_query = printed[-1]["query"]
    if _comparable(printed_query) == _comparable(linked):
        logger.info("Query link check: the answer's query is the one that ran")
        return True
    logger.warning(
        "Query link check: MISMATCH, the answer prints a query that is not the one that ran.\n"
        "--- printed in the answer:\n%s\n--- ran (Run or edit the query):\n%s",
        printed_query,
        linked,
    )
    return False


async def mcp_tools_node(state: State, config: RunnableConfig) -> dict[str, Any]:
    """Handle MCP tool calls asynchronously.

    Args:
        state: The current state of the conversation.
        config: The runnable configuration.

    Returns:
        Dictionary with tool messages.
    """
    # Get the last message which should contain tool calls
    last_msg = state.messages[-1]

    if not isinstance(last_msg, AIMessage) or not (last_msg.tool_calls or last_msg.invalid_tool_calls):
        # No tool calls to process
        return {"messages": []}

    # A call whose arguments are not valid JSON (gpt-oss sometimes garbles them)
    # is filed under invalid_tool_calls. Answer it with the error so the model can
    # retry, rather than the run ending with its raw JSON shown as the answer.
    tool_messages = [
        ToolMessage(
            content=(
                f"Your call to {call.get('name') or 'the tool'} was not run: its arguments are not valid JSON "
                f"({(call.get('error') or '')[:300]}). Call the tool again with valid JSON arguments."
            ),
            name=call.get("name") or "unknown_tool",
            tool_call_id=call.get("id") or "",
        )
        for call in last_msg.invalid_tool_calls
    ]
    if not last_msg.tool_calls:
        return {"messages": tool_messages}

    # The selected dataset's MCP address, as in call_model: never another dataset's tools.
    project = project_of(Configuration.from_runnable_config(config))
    mcp_client = MultiServerMCPClient({"expasy-mcp": agent_connection(project.id)})
    # The last query that ran, shown under the answer as "Run or edit the query".
    query_link: dict[str, str] | None = None

    async with mcp_client.session("expasy-mcp") as mcp_session:
        # Process each tool call
        for tool_call in last_msg.tool_calls:
            print(tool_call)
            try:
                # Execute the tool via MCP client
                # The langchain-mcp-adapters should handle the tool name mapping
                result = await mcp_session.call_tool(tool_call["name"], tool_call.get("args", {}))  # type: ignore
                print(result)

                # Create tool message with the result
                # The result from MCP client should have content accessible
                content = ""
                if hasattr(result, "content"):
                    if isinstance(result.content, list):
                        # MCP content items have a .text attribute — use it
                        # instead of str() which gives the ugly Python repr.
                        parts = []
                        for item in result.content:
                            if hasattr(item, "text"):
                                parts.append(item.text)
                            else:
                                parts.append(str(item))
                        content = "\n".join(parts)
                    else:
                        content = str(result.content)
                else:
                    content = str(result)

                tool_messages.append(
                    ToolMessage(
                        content=content,
                        name=tool_call["name"],
                        tool_call_id=tool_call["id"],
                    )
                )
                ran = executed_query(tool_call, content, project)
                if ran:
                    logger.info(
                        "Query link: execute_sparql_query ran (link now points to it):\n%s", ran["sparql_query"]
                    )
                query_link = ran or query_link

            except Exception as e:
                # Handle tool execution errors
                print(f"Error executing tool '{tool_call['name']}': {e!s}")
                tool_messages.append(
                    ToolMessage(
                        content=f"Error executing tool '{tool_call['name']}': {e!s}",
                        name=tool_call["name"],
                        tool_call_id=tool_call["id"],
                    )
                )

    if query_link:
        return {"messages": tool_messages, "structured_output": query_link}
    return {"messages": tool_messages}
