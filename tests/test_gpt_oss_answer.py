"""gpt-oss answers that GPUStack files under reasoning_content must reach the chat.

With tools bound and streaming off, GPUStack intermittently returns gpt-oss's
final answer in ``reasoning_content`` with an empty ``content``. ChatOpenAI
ignores that field, so the user got a blank reply in MCP-tools and
natural-language mode. Replayed against GPUStack, the misfiled text was the
finished answer every time it happened.

Requires the optional ``agent`` extra (langchain-openai…), which CI does not
install, so this skips there and runs in a dev environment set up with
``uv sync --extra agent``.
"""

import pytest

pytest.importorskip("langchain_openai", reason="needs the optional 'agent' extra")

from sparql_llm.agent.utils import GptOssChatOpenAI  # noqa: E402

ANSWER = "Ernst Brenner was married to **Lina Sturzenegger**."


def _message(**fields) -> object:
    response = {
        "choices": [{"index": 0, "message": {"role": "assistant", **fields}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    model = GptOssChatOpenAI(model="gpt-oss-120b", api_key="test", base_url="http://localhost")
    return model._create_chat_result(response).generations[0].message


@pytest.mark.parametrize("content", ["", None])
def test_answer_filed_as_reasoning_becomes_the_content(content):
    assert _message(content=content, reasoning_content=ANSWER).content == ANSWER


def test_reply_with_content_is_left_alone():
    # A correctly filed reply: genuine reasoning stays out of the answer.
    message = _message(content=ANSWER, reasoning_content="We have the spouse. Provide the answer.")
    assert message.content == ANSWER


def test_tool_call_turn_is_left_alone():
    # A turn that calls a tool legitimately has no content; its reasoning is
    # the model's plan, not an answer.
    message = _message(
        content="",
        reasoning_content="Need the person's URI first.",
        tool_calls=[{"id": "c1", "type": "function", "function": {"name": "execute_sparql_query", "arguments": "{}"}}],
    )
    assert message.content == ""
    assert message.tool_calls[0]["name"] == "execute_sparql_query"
