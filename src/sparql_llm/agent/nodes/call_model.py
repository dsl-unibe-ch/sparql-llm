"""Node to call the model to solve the user question given the context previosuly extracted.

Works with a chat model with tool calling support.
"""

from typing import Any

from langchain_core.messages import AIMessage, AnyMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableConfig
from langchain_mcp_adapters.client import MultiServerMCPClient

from sparql_llm.agent.state import State, StepOutput
from sparql_llm.agent.utils import get_msg_text, load_chat_model
from sparql_llm.config import Configuration, settings
from sparql_llm.utils import extract_think_blocks, strip_think_blocks

# from sparql_llm.agent.nodes.retrieval_docs import format_docs
# from sparql_llm.agent.nodes.retrieval_entities import format_extracted_entities


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

    structured_prompt: dict[str, Any] = {
        "messages": state.messages,
    }
    # structured_prompt["retrieved_docs"] = format_docs(state.retrieved_docs)
    # if configuration.enable_entities_resolution:
    #     structured_prompt["extracted_entities"] = format_extracted_entities(state.extracted_entities)
    # else:
    #     structured_prompt["extracted_entities"] = ""

    sys_prompt = configuration.system_prompt_tools if configuration.use_tools else configuration.system_prompt
    if configuration.natural_language_only:
        # Override the hardcoded instruction that tells the model to output the SPARQL query in the final answer
        sys_prompt = sys_prompt.replace(
            "Put the SPARQL inside a markdown ```sparql codeblock",
            "Put the SPARQL inside a markdown ```sparql codeblock INSIDE a <think> block"
        )
        sys_prompt = sys_prompt.replace(
            "Include the final working SPARQL query in a ```sparql codeblock",
            "Include the final working SPARQL query in a ```sparql codeblock INSIDE a <think> block"
        )
        
        sys_prompt += (
            "\n\nCRITICAL INSTRUCTION (NATURAL LANGUAGE ONLY MODE):\n"
            "1. If you are generating a SPARQL query or exploring data, you MUST place all reasoning, technical details, and SPARQL queries entirely inside a <think>...</think> block. Do NOT output any natural language outside the <think> block during this phase.\n"
            "2. ONLY when you have the final results and are ready to provide the final answer to the user, output your natural language response outside the <think> block.\n"
            "3. When you provide your final natural language answer, you MUST still include the final working SPARQL query inside your <think> block so the user can inspect it if they expand the thought process.\n"
            "4. Your final visible response outside the <think> block must NOT contain any SPARQL queries or technical details. It must be purely natural language.\n"
            "5. At the very end of your final natural language answer, you MUST ask an engaging follow-up question to keep the conversation interactive (e.g. 'Would you like me to find...', 'Should I explore...')."
        )

    prompt_template = ChatPromptTemplate.from_messages(
        [
            ("system", sys_prompt),
            ("placeholder", "{messages}"),
        ]
    )
    message_value = await prompt_template.ainvoke(structured_prompt, config)
    # print(message_value.messages[0].content)
    # print(message_value)
    # Use the async ``ainvoke`` (not the blocking ``invoke``): this node runs
    # inside the async LangGraph event loop that also drives the SSE response.
    # A synchronous ``invoke`` blocks that loop for the whole model call, so no
    # streamed tokens or heartbeats reach the browser until it finishes — with a
    # reasoning model doing several fix attempts the chat freezes on
    # "🔄 Refining query…" and looks stuck ("keeps loading"). ``ainvoke`` yields
    # control back to the loop so tokens stream live and the UI stays responsive.
    response_msg = await model.ainvoke(message_value, config)

    # print(f"Model response: {response_msg.content}")

    # Reasoning models (e.g. minimax-m2.7) emit their chain-of-thought inline as
    # <think>…</think>. Surface it as a collapsible "💭 Thought process" step and
    # strip it from the stored answer. The live token stream is cleaned
    # separately in main.py:stream_response; this keeps the *persisted* message
    # (used by validation and the non-streaming path) clean too. When there is no
    # reasoning block (e.g. gpt-oss-120b keeps it on a separate channel), no step
    # is added.
    reasoning_steps: list[StepOutput] = []
    answer_text = get_msg_text(response_msg)
    reasoning = extract_think_blocks(answer_text)

    # In natural language mode, programmatically extract the technical details
    # (tool calls, SPARQL queries, tool results) from the conversation history
    # and inject them into the thought process bubble. This way the user sees
    # a clean natural-language answer, but can expand the thought process to
    # inspect every query the model ran behind the scenes.
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
                    if tool_name == "execute_sparql_query" and "query" in args:
                        technical_details.append(
                            f"**Executed SPARQL query:**\n```sparql\n{args['query']}\n```"
                        )
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
                        technical_details.append(
                            f"**Query results:**\n```\n{display}\n```"
                        )
                    else:
                        technical_details.append(
                            f"**Results from `{tool_name}`:**\n```\n{display}\n```"
                        )

        if technical_details:
            injected = "\n\n---\n### Technical Details\n\n" + "\n\n".join(technical_details)
            reasoning = (reasoning or "") + injected

    if reasoning:
        response_msg.content = strip_think_blocks(answer_text).lstrip()
        reasoning_steps.append(StepOutput(label="💭 Thought process", details=reasoning))

    # Check if the current response contains tool calls that should be processed
    has_tool_calls = bool(getattr(response_msg, "tool_calls", None))
    if has_tool_calls and not state.is_last_step:
        return {"messages": [response_msg], "passed_validation": False}

    # TODO: improve the tool use with a supervizor node that check if tool calls are needed or stop
    # last_msg = state.messages[-1]
    # if isinstance(last_msg, (ToolMessage, FunctionMessage)) and last_msg.name in ["access_biomedical_resources", "execute_sparql_query"]:
    #     # If the last message is from one of these tools, we need to check if the response
    #     # might require further tool calls, regardless of whether it explicitly has tool_calls
    #     # This handles cases where output from previous tool calls might trigger a need for more tools
    #     return {"messages": [response_msg], "passed_validation": False}

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
