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

    prompt_template = ChatPromptTemplate.from_messages(
        [
            ("system", configuration.system_prompt_tools if configuration.use_tools else configuration.system_prompt),
            ("placeholder", "{messages}"),
        ]
    )
    message_value = prompt_template.invoke(structured_prompt, config)
    # print(message_value.messages[0].content)
    # print(message_value)
    response_msg = model.invoke(message_value, config)

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
    return {"messages": [response_msg], "passed_validation": True, "steps": reasoning_steps}
