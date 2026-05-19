"""Node to call the model.

Works with a chat model with tool calling support.
"""

import json
import re
from typing import Literal

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, Field

from sparql_llm.agent.prompts import EXTRACTION_PROMPT
from sparql_llm.agent.state import State, StepOutput
from sparql_llm.agent.utils import get_msg_text, load_chat_model
from sparql_llm.config import Configuration
from sparql_llm.utils import logger

# TODO: remove, not used anymore, replaced by tools functions


# https://python.langchain.com/docs/how_to/structured_output
class StructuredQuestion(BaseModel):
    """Extracted."""

    intent: Literal["general_information", "access_resources"] = Field(
        description="Intent extracted from the user question"
    )
    extracted_classes: list[str] = Field(description="List of classes extracted from the user question")
    extracted_entities: list[str] = Field(description="List of entities extracted from the user question")
    question_steps: list[str] = Field(
        default_factory=list,
        description="List of steps extracted from the user question",
    )


async def extract_user_question(
    state: State, config: RunnableConfig
) -> dict[str, StructuredQuestion | list[StepOutput]]:
    """Call the LLM powering our "agent".

    This function prepares the prompt, initializes the model, and processes the response.

    Args:
        state (State): The current state of the conversation.
        config (RunnableConfig): Configuration for the model run.

    Returns:
        dict: A dictionary containing the model's response message.
    """
    configuration = Configuration.from_runnable_config(config)

    # We parse the JSON ourselves rather than using with_structured_output(method="json_mode")
    # because GPUStack-hosted models behave inconsistently:
    # - gpt-oss-120b interleaves reasoning tokens into the JSON (token soup).
    # - qwen3-vl-30b-a3b-instruct produces clean JSON.
    # - minimax-m2.7 (and other reasoning models) wrap the JSON in a <think>…</think>
    #   block — the actual JSON is at the end.
    # langchain's JsonOutputParser blows up on the thinking block. By extracting
    # the JSON manually we tolerate all three patterns. The fallback below
    # catches anything truly unparseable.
    base_model = load_chat_model(configuration)

    prompt_template = ChatPromptTemplate.from_messages(
        [
            ("system", EXTRACTION_PROMPT),
            ("placeholder", "{messages}"),
        ]
    )
    message_value = await prompt_template.ainvoke(
        {
            "messages": state.messages,
        },
        config,
    )

    try:
        response = await base_model.ainvoke(
            message_value,
            {**config, "configurable": {**config.get("configurable", {}), "stream": False}},
        )
        raw_text = get_msg_text(response) if response is not None else ""
        if not raw_text:
            raise ValueError("model returned empty content")
        parsed = _extract_json_object(raw_text)
        structured_question = StructuredQuestion.model_validate(parsed)
    except Exception as e:
        logger.warning(
            f"Structured extraction failed ({type(e).__name__}: {e}); "
            "falling back to defaults so retrieval still runs."
        )
        # Use the latest user message as the single extraction step.
        last_text = ""
        for msg in reversed(state.messages):
            txt = get_msg_text(msg)
            if txt:
                last_text = txt
                break
        structured_question = StructuredQuestion(
            intent="access_resources",
            extracted_classes=[],
            extracted_entities=[],
            question_steps=[last_text] if last_text else [],
        )
    # print(structured_question)
    steps_label = (
        f"{len(structured_question.question_steps)} steps and " if len(structured_question.question_steps) > 0 else ""
    )
    steps_details = (
        f"""
Steps to answer the user question:

{chr(10).join(f"- {step}" for step in structured_question.question_steps)}"""
        if len(structured_question.question_steps) > 0
        else ""
    )

    return {
        "structured_question": structured_question,
        "steps": [
            StepOutput(
                label=f"⚗️ {steps_label}{len(structured_question.extracted_classes)} classes extracted",
                details=f"""Intent: {structured_question.intent.replace("_", " ")}
{steps_details}

Potential classes:

{chr(10).join(f"- {cls}" for cls in structured_question.extracted_classes)}

Potential entities:

{chr(10).join(f"- {entity}" for entity in structured_question.extracted_entities)}""",
            )
        ],
    }


# Reasoning blocks emitted by models like minimax-m2.7, deepseek-r1, qwen-qwen3 reasoning, etc.
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
# Markdown code fences ```json ... ``` or ``` ... ```
_FENCE_OPEN = re.compile(r"^\s*```(?:json)?\s*\n?", re.IGNORECASE | re.MULTILINE)
_FENCE_CLOSE = re.compile(r"\n?\s*```\s*$", re.MULTILINE)


def _extract_json_object(text: str) -> dict:
    """Extract and parse a JSON object from a possibly-noisy LLM response.

    Handles three common patterns we've seen from GPUStack-hosted models:
    1. The whole response is valid JSON.
    2. The response is a `<think>…</think>` reasoning block followed by JSON.
    3. The JSON is wrapped in a ```json … ``` markdown fence.

    Raises ValueError if no JSON object can be located.
    """
    clean = _THINK_BLOCK.sub("", text)
    clean = _FENCE_OPEN.sub("", clean)
    clean = _FENCE_CLOSE.sub("", clean).strip()

    # Fast path: the cleaned response is already a JSON object.
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        pass

    # Slow path: find the outermost {...} substring and try that.
    start = clean.find("{")
    end = clean.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError(f"no JSON object found in response: {clean[:200]!r}")
    return json.loads(clean[start : end + 1])
