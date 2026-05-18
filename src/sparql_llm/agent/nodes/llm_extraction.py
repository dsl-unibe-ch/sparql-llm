"""Node to call the model.

Works with a chat model with tool calling support.
"""

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

    # Try structured output via json_mode first (works with vLLM/GPUStack via
    # response_format=json_object). Function-calling mode requires tool-use
    # support that some local servers don't expose. Fall back to a minimal
    # default if the model returns nothing parseable — better to continue
    # without structured extraction than to kill the whole graph.
    base_model = load_chat_model(configuration)
    model = base_model.with_structured_output(StructuredQuestion, method="json_mode")

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
        raw = await model.ainvoke(
            message_value,
            {**config, "configurable": {**config.get("configurable", {}), "stream": False}},
        )
        if raw is None:
            raise ValueError("model returned None — structured output not supported")
        structured_question = StructuredQuestion.model_validate(raw)
    except Exception as e:
        logger.warning(
            f"Structured extraction failed ({e}); falling back to defaults so retrieval still runs."
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
