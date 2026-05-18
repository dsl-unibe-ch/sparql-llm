"""Define the service settings and configurable parameters for the agent."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Annotated, Any, Required, TypeVar

from dotenv import load_dotenv
from langchain_core.runnables import RunnableConfig, ensure_config
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing_extensions import TypedDict

from sparql_llm.agent import prompts

# Load .env into the process environment as well, not just into the Settings
# instance. agent/utils.py reads OPENAI_API_KEY / OPENAI_BASE_URL via os.getenv()
# for the GPUStack client, which would otherwise see empty strings.
load_dotenv()


# Total=False to make all fields optional except those marked as Required
class SparqlEndpointLinks(TypedDict, total=False):
    """A dictionary to store links and filepaths about a SPARQL endpoint."""

    endpoint_url: Required[str]
    void_file: str | None
    examples_file: str | None
    homepage_url: str | None
    label: str | None
    description: str | None
    # ontology_url: Optional[str]


class Settings(BaseSettings):
    """Define the service settings for the agent that can be set using environment variables."""

    # The list of endpoints that will be indexed and supported by the service
    endpoints: list[SparqlEndpointLinks] = [
        {
            "label": "Elites Suisses",
            "endpoint_url": "https://swiss-elites.lod4hss.cloud/wisski/endpoint/default",
            "description": (
                "Swiss elites knowledge graph (~58,700 persons; biographical data, "
                "education, marriages, family relations, organisations, mandates) — "
                "CIDOC CRM + SDHSS (social-life-core, shortcuts) ontologies."
            ),
            "void_file": "data/elites-suisses-void.ttl",
            "examples_file": "data/elites-suisses-examples.md",
            "homepage_url": "https://elites-suisses.lod4hss.org/",
        },
    ]

    # Settings for the vector store and embeddings
    # ⚠️ changing the embedding models require to reindex the data
    # vectordb_url: str = "http://vectordb:6334/"
    vectordb_url: str = "data/vectordb"
    # https://qdrant.github.io/fastembed/examples/Supported_Models/#supported-text-embedding-models
    # embedding_model: str = "BAAI/bge-small-en-v1.5"
    # embedding_model: str = "sentence-transformers/paraphrase-multilingual-mpnet-base-v2"
    embedding_model: str = "intfloat/multilingual-e5-large"

    force_index: bool = False
    # Automatically initialize the vector store client, should be False when deploying in prod with multiple workers
    auto_init: bool = True

    # Sparse embeddings are only used for the entities resolution
    sparse_embedding_model: str = "Qdrant/bm25"
    # sparse_embedding_model: str = "prithivida/Splade_PP_en_v1"
    docs_collection_name: str = "swiss-elites"
    entities_collection_name: str = "entities"

    # Default settings for the agent that can be changed at runtime
    # GPUStack at UniBE: provider "gpustack" is wired in agent/utils.py;
    # set OPENAI_BASE_URL and OPENAI_API_KEY in .env.
    default_llm_model: str = "gpustack/gpt-oss-120b"

    default_number_of_retrieved_docs: int = 10
    default_max_try_fix_sparql: int = 3
    default_temperature: float = 0.0
    default_max_tokens: int = 16384
    default_seed: int = 42

    # List of example questions to display in the chat UI.
    # Chosen to return non-empty results against the current data coverage
    # (persons + births + parent/child are populated; memberships/orgs are not yet).
    example_questions: list[str] = [
        "How many persons are in the database?",
        "List 20 persons with their names",
        "How many birth events are recorded?",
        "Find persons whose name contains 'Ogi'",
        "Who are the recorded parents of person X? (give a swel: URI)",
        "How many persons of each SDHSS class are there?",
    ]

    app_name: str = "Elites Suisses Chat"
    """The name of the application used for display purposes"""

    app_topics: str = "Swiss elites: biographies, education, family relations, marriages, organisations, mandates"
    """The topics of the SPARQL endpoints indexed by this system, used for MCP tool description."""

    app_org: str = "LESSH — Université de Lausanne / Universität Bern"
    """The organization responsible for the application."""

    app_public_host: str = "vm7.dsl.unibe.ch"
    """The public host name where the application is deployed, used for MCP transport security settings."""

    # Public API key used by the frontend to access the chatbot and prevent abuse from bots
    chat_api_key: str = ""
    # Secret API key used by admins to access log file easily from the API
    logs_api_key: str = ""
    # Optional Sentry error report API key
    sentry_url: str = ""

    logs_folder: str = "./data/logs"
    logs_filepath: str = "./data/logs/user_questions.log"

    use_tools: bool = False
    """Experimental: Whether to use tools or not. If set to False, the agent will use the functions sequentially to answer questions."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="allow",
    )

    @property
    def server_url(self) -> str:
        """Computed server URL using the host and port, for accessing locally for /mcp calls.

        Returns:
            A string like 'http://127.0.0.1:8888'.
        """
        # Use 127.0.0.1 for connecting to the service (0.0.0.0 is only for binding)
        # host = "127.0.0.1" if self.server_host == "0.0.0.0" else self.server_host
        # return f"http://{host}:{self.server_port}"
        return "http://127.0.0.1:8000"

    @classmethod
    def from_file(cls, filepath: str) -> Settings:
        """Create a Settings instance from a file.

        Args:
            filepath: The path to the file.
        """
        path = Path(filepath)  # your JSON file path
        if not path.exists():
            return Settings()
        with path.open("r") as f:
            return Settings(**json.load(f))


settings_filepath = os.getenv("SETTINGS_FILEPATH")
settings = Settings.from_file(settings_filepath) if settings_filepath else Settings()
# logger.info(f"📂 Using SETTINGS file: {settings_filepath}")


# Configuration defined at runtime
@dataclass(kw_only=True)
class Configuration:
    """The configuration for the agent that can be changed at runtime when calling the agent."""

    enable_entities_resolution: bool = field(
        default=False,
        metadata={
            "description": "Wherever to enable trying to resolve entities to their URIs in the SPARQL endpoints."
        },
    )

    enable_output_validation: bool = field(
        default=True,
        metadata={"description": "Wherever to validate or not the output of the LLM (e.g. SPARQL queries generated)."},
    )

    enable_sparql_execution: bool = field(
        default=True,
        metadata={
            "description": "Wherever to enable automatically executing a SPARQL query against its endpoint after passing its validation step."
        },
    )

    system_prompt: str = field(
        default=prompts.RESOLUTION_PROMPT,
        metadata={
            "description": "The system prompt to use for the agent's interactions."
            "This prompt sets the context and behavior for the agent."
        },
    )

    model: Annotated[str, {"__template_metadata__": {"kind": "llm"}}] = field(
        default=settings.default_llm_model,
        metadata={
            "description": "The name of the language model to use for the agent's main interactions."
            "Should be in the form: provider/model-name."
        },
    )

    temperature: Annotated[float, {"__template_metadata__": {"kind": "llm"}}] = field(
        default=settings.default_temperature,
        metadata={
            "description": "The temperature of the language model."
            "Should be between 0.0 and 2.0. Higher values make the model more creative but less deterministic."
        },
    )
    max_tokens: Annotated[int, {"__template_metadata__": {"kind": "llm"}}] = field(
        default=settings.default_max_tokens,
        metadata={
            "description": "The maximum number of tokens to generate in the response."
            "Should be between 4000 and 120000 (depends on the model context window)."
        },
    )
    seed: Annotated[int, {"__template_metadata__": {"kind": "llm"}}] = field(
        default=settings.default_seed,
        metadata={"description": "The random seed used for reproducibility."},
    )
    # Number of retrieved docs
    search_kwargs: dict[str, Any] = field(
        default_factory=lambda: {"k": settings.default_number_of_retrieved_docs},
        # default_factory=dict,
        metadata={"description": "Additional keyword arguments to pass to the search function of the retriever."},
    )

    max_try_fix_sparql: int = field(
        default=settings.default_max_try_fix_sparql,
        metadata={"description": "The maximum number of tries when calling the model to fix a SPARQL query."},
    )

    @classmethod
    def from_runnable_config(cls: type[T], config: RunnableConfig | None = None) -> T:
        """Create an IndexConfiguration instance from a RunnableConfig object.

        Adds defaults values to the configurable.

        Args:
            cls (Type[T]): The class itself.
            config (Optional[RunnableConfig]): The configuration object to use.

        Returns:
            T: An instance of IndexConfiguration with the specified configuration.
        """
        config = ensure_config(config)
        configurable = config.get("configurable") or {}
        _fields = {f.name for f in fields(cls) if f.init}
        return cls(**{k: v for k, v in configurable.items() if k in _fields})


T = TypeVar("T", bound=Configuration)
