"""Node to call the model to solve the user question given the context previously extracted.

Works with a chat model with tool calling support.
"""

from typing import Any

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableConfig
from langchain_mcp_adapters.client import MultiServerMCPClient

from sparql_llm.agent.prompts import FINAL_TURN_PROMPT
from sparql_llm.agent.state import State, StepOutput
from sparql_llm.agent.utils import count_tool_rounds, fenced, get_msg_text, load_chat_model
from sparql_llm.config import Configuration, settings
from sparql_llm.utils import extract_think_blocks, strip_think_blocks


async def call_model(state: State, config: RunnableConfig) -> dict[str, list[AnyMessage] | bool]:
    """Call the LLM powering our "agent".

    This function prepares the prompt, initializes the model, and processes the response.

    Args:
        state (State): The current state of the conversation.
        config (RunnableConfig): Configuration for the model run.

    Returns:
        dict: A dictionary containing the model's response message.
    """
    configuration = Configuration.from_runnable_config(config)
    tools = None
    # Once the tool-call rounds reach the "Max steps" budget, the model is told to
    # answer from the results it already has. Tools stay bound: without them, models
    # write their calls into the answer as raw markup, while a bound stray call is
    # parsed and route_tools_output stops it.
    final_turn = configuration.use_tools and count_tool_rounds(state.messages) >= configuration.max_tool_iterations

    # Set up MCP client (experimental — enabled per-request via configuration.use_tools)
    if configuration.use_tools:
        mcp_client = MultiServerMCPClient(
            {
                "expasy-mcp": {
                    "url": f"{settings.server_url}/mcp",
                    "transport": "streamable_http",
                }
            }
        )
        try:
            tools = await mcp_client.get_tools()
        except Exception as exc:
            # If the MCP server is unreachable or returns tools we can't load,
            # surface a clear message instead of crashing the whole request.
            raise RuntimeError(
                f"Could not load MCP tools from {settings.server_url}/mcp ({type(exc).__name__}: {exc})"
            ) from exc

    model = load_chat_model(configuration).bind_tools(tools) if tools else load_chat_model(configuration)

    # The budget instruction goes last, where the model weighs it most. It is sent
    # for this call only and never stored in the conversation.
    structured_prompt: dict[str, Any] = {
        "messages": [*state.messages, HumanMessage(content=FINAL_TURN_PROMPT)] if final_turn else state.messages,
    }

    sys_prompt = configuration.system_prompt_tools if configuration.use_tools else configuration.system_prompt
    if configuration.natural_language_only:
        # The user sees only the prose answer: stream_response strips SPARQL blocks
        # from it, and the queries the model ran are shown in the "Thought process"
        # step built below. Do not ask the model to hide its SPARQL in a <think> block:
        # qwen on GPUStack reasons on a separate channel and cannot write into one.
        sys_prompt += (
            "\n\nNATURAL LANGUAGE ONLY MODE:\n"
            "- The user sees only your prose. The queries you ran and their results are shown to them separately, "
            "and any ```sparql codeblock is hidden from them automatically.\n"
            "- Write the final answer in plain natural language, without technical details such as URIs, prefixes "
            "or explanations of the query.\n"
            "- At the very end, offer one short follow-up the user might want (e.g. 'Would you like me to find...'). "
            "Only OFFER it: do NOT call any tool to prepare it, the user decides whether to continue."
        )

    prompt_template = ChatPromptTemplate.from_messages(
        [
            ("system", sys_prompt),
            ("placeholder", "{messages}"),
        ]
    )
    message_value = await prompt_template.ainvoke(structured_prompt, config)
    # ``ainvoke``, not ``invoke``: a blocking call would stall the event loop that
    # streams the response, freezing the chat until the model returns.
    response_msg = await model.ainvoke(message_value, config)

    # Reasoning models (e.g. minimax-m2.7) emit their chain-of-thought inline as
    # <think>…</think>. Show it as a "💭 Thought process" step and strip it from the
    # stored answer, which validation and the non-streaming path read (stream_response
    # cleans the live stream). Without a reasoning block, no step is added.
    reasoning_steps: list[StepOutput] = []
    answer_text = get_msg_text(response_msg)
    reasoning = extract_think_blocks(answer_text)

    # Natural-language mode: list the tool calls and their results from the
    # conversation in the thought process step, so every query stays inspectable.
    if configuration.natural_language_only:
        technical_details = []

        # First pass: build a map from tool_call_id → tool_name from AIMessages
        tool_call_names: dict[str, str] = {}
        for past_msg in state.messages:
            if getattr(past_msg, "tool_calls", None):
                for tc in past_msg.tool_calls:
                    tc_id = tc.get("id", "")
                    tc_name = tc.get("name", "unknown_tool")
                    if tc_id:
                        tool_call_names[tc_id] = tc_name

        # Second pass: collect tool calls and their results in order
        for past_msg in state.messages:
            # Capture outgoing tool calls (AIMessage with tool_calls)
            if getattr(past_msg, "tool_calls", None):
                for tc in past_msg.tool_calls:
                    tool_name = tc.get("name", "unknown_tool")
                    args = tc.get("args", {})
                    # The tool's argument is sparql_query; "query" is a fallback.
                    sparql_query = args.get("sparql_query") or args.get("query")
                    if tool_name == "execute_sparql_query" and sparql_query:
                        technical_details.append(f"**Executed SPARQL query:**\n{fenced(sparql_query, 'sparql')}")
                    elif tool_name == "search_sparql_docs":
                        q = args.get("question", args.get("query", ""))
                        technical_details.append(f"**Searched documentation:** {q}")
                    elif tool_name == "get_classes_schema":
                        technical_details.append(f"**Retrieved class schema:** {args}")
                    elif tool_name == "get_resources_info":
                        technical_details.append(f"**Looked up resource info:** {args}")
                    else:
                        technical_details.append(f"**Called tool `{tool_name}`:** {args}")

            # Capture tool results (ToolMessage)
            msg_type = getattr(past_msg, "type", "")
            if msg_type == "tool":
                # Determine the tool name — either from the message itself or by
                # looking up the tool_call_id in our map.
                tool_name = getattr(past_msg, "name", "") or ""
                if not tool_name:
                    tc_id = getattr(past_msg, "tool_call_id", "")
                    tool_name = tool_call_names.get(tc_id, "unknown_tool")
                tool_content = getattr(past_msg, "content", "")
                if tool_content:
                    # Truncate very long results for readability
                    display = tool_content[:2000] + ("…" if len(tool_content) > 2000 else "")
                    if tool_name == "execute_sparql_query":
                        technical_details.append(f"**Query results:**\n{fenced(display)}")
                    else:
                        technical_details.append(f"**Results from `{tool_name}`:**\n{fenced(display)}")

        if technical_details:
            injected = "\n\n---\n### Technical Details\n\n" + "\n\n".join(technical_details)
            reasoning = (reasoning or "") + injected

    if reasoning:
        response_msg.content = strip_think_blocks(answer_text).lstrip()
        reasoning_steps.append(StepOutput(label="💭 Thought process", details=reasoning))

    # Check if the current response contains tool calls that should be processed
    # Calls with unparseable arguments count too: the tools node answers them with
    # the error so the model can retry.
    has_tool_calls = bool(getattr(response_msg, "tool_calls", None) or getattr(response_msg, "invalid_tool_calls", None))
    if has_tool_calls and not state.is_last_step:
        return {"messages": [response_msg], "passed_validation": False}

    # Handle the case when it's the last step and the model still wants to use a tool
    if state.is_last_step and has_tool_calls:
        return {
            "messages": [
                AIMessage(
                    id=response_msg.id,
                    content="Sorry, I could not find an answer to your question in the specified number of steps.",
                )
            ]
        }
    # Return the model response as a list to be added to existing messages
    return {"messages": [response_msg], "passed_validation": True, "steps": reasoning_steps, "latest_model_output": answer_text}
