"""Define the service settings and configurable parameters for the agent."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Annotated, Any, Required, TypeVar

from dotenv import load_dotenv
from langchain_core.runnables import RunnableConfig, ensure_config
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing_extensions import TypedDict


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


class Settings(BaseSettings):
    """Define the service settings for the agent that can be set using environment variables."""

    # Folder holding this instance's data: one sub-folder per project, the vector store,
    # users, logs and job files. A second instance on the same host uses its own folder.
    data_dir: str = "data"

    # Ids of the projects this instance serves; each is described by <data_dir>/<id>/project.json
    projects: list[str] = []

    # Settings for the vector store and embeddings
    # ⚠️ changing the embedding model or backend requires reindexing the data
    # Left empty, it defaults to <data_dir>/vectordb
    vectordb_url: str = ""

    # Embedding backend: "local" (fastembed/ONNX in-process, the default, no network dependency)
    # or "openai" (opt-in: a remote OpenAI-compatible server, e.g. GPUStack, which offloads
    # inference and frees ~1.5 GB RAM).
    embedding_backend: str = "local"

    # Model identifier — meaning depends on the backend:
    #   openai: the model name served by the API (e.g. "qwen3-embedding-0.6b")
    #   local:  a HuggingFace/fastembed model id (e.g. "intfloat/multilingual-e5-large")
    embedding_model: str = "intfloat/multilingual-e5-large"

    # Vector dimension. Must match the model output and the Qdrant collection.
    # qwen3-embedding-0.6b and intfloat/multilingual-e5-large both produce 1024-d vectors.
    # The openai backend uses this value; the local backend reads it from the model.
    embedding_dimensions: int = 1024

    # Optional overrides for the embedding API endpoint.  When empty, falls back
    # to OPENAI_BASE_URL / OPENAI_API_KEY (same as the LLM client).
    embedding_base_url: str = ""
    embedding_api_key: str = ""

    force_index: bool = False
    # Automatically initialize the vector store client, should be False when deploying in prod with multiple workers
    auto_init: bool = False

    # Sparse embeddings are only used for the entities resolution
    sparse_embedding_model: str = "Qdrant/bm25"
    docs_collection_name: str = "docs"
    entities_collection_name: str = "entities"

    # Default settings for the agent that can be changed at runtime
    # No default LLM: users bring their own (provider/model-name) via the settings file,
    # the environment or the admin UI. Provider "gpustack" is wired in agent/utils.py;
    # set OPENAI_BASE_URL and OPENAI_API_KEY in .env.
    default_llm_model: str = ""

    # Model IDs (provider/model-name) offered in the chat UI's model picker, in order.
    available_llm_models: list[str] = []

    # Models kept out of the MCP tools mode because GPUStack serves them without
    # --enable-auto-tool-choice, so they reject tool requests (e.g. the qwen3-vl vision
    # models). A deny-list, so a newly added model is never locked out of tools mode.
    # To check a model, POST to $OPENAI_BASE_URL/chat/completions with a "tools" array
    # and look for "tool_calls" in the reply.
    tool_incapable_models: list[str] = []

    default_number_of_retrieved_docs: int = 10
    default_max_try_fix_sparql: int = 3
    default_max_tool_iterations: int = 10
    default_temperature: float = 0.0
    default_max_tokens: int = 16384
    default_seed: int = 42

    # Example questions shown in the chat UI, chosen to return results on the current data.
    example_questions: list[str] = []

    app_name: str = "SPARQL Chat"
    """The name of the application used for display purposes"""

    app_topics: str = ""
    """The topics of the SPARQL endpoints indexed by this system, used for MCP tool description."""

    app_org: str = ""
    """The organization responsible for the application."""

    app_intro: str = ""
    """Short intro paragraph shown on the chat page (plain text or simple Markdown)."""

    app_homepage_url: str = ""
    """Homepage of the project behind this instance."""

    app_public_host: str = ""
    """The public host name where the application is deployed (served over HTTPS): the MCP transport
    security settings, the browser origin trusted with the login cookie, and Secure cookies."""

    # Public API key used by the frontend to access the chatbot and prevent abuse from bots
    chat_api_key: str = ""
    # Secret API key used by admins to access log file easily from the API
    logs_api_key: str = ""
    # Optional Sentry error report API key
    sentry_url: str = ""

    # Left empty, these default to <data_dir>/logs and <data_dir>/logs/user_questions.log
    logs_folder: str = ""
    logs_filepath: str = ""

    # Auth settings
    auth_enabled: bool = True
    """Set to False to disable login (useful for local dev without auth)."""
    auth_secret: str = "CHANGE_ME_IN_PRODUCTION_USE_A_LONG_RANDOM_STRING"
    """JWT signing secret — override via AUTH_SECRET env var. With auth on, the app refuses to
    start on this placeholder or anything shorter than 32 characters."""
    cookie_secure: bool | None = None
    """Send the login cookie over HTTPS only. Left unset, on whenever ``app_public_host`` is set."""
    auth_db_path: str = ""
    """Path to the SQLite database file for user accounts. Left empty, it defaults to <data_dir>/users.db."""
    admin_email: str = ""
    """Email for the initial admin account created on first run (set via ADMIN_EMAIL env var)."""
    admin_password: str = ""
    """Password for the initial admin account (set via ADMIN_PASSWORD env var)."""

    server_port: int = 8000
    """The port this instance listens on; the tools agent fetches its MCP tools from it."""

    use_tools: bool = False
    """Experimental: Whether to use tools or not. If set to False, the agent will use the functions sequentially to answer questions."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="allow",
    )

    @model_validator(mode="after")
    def _paths_under_data_dir(self) -> Settings:
        data_dir = self.data_dir.rstrip("/\\") or "."
        self.vectordb_url = self.vectordb_url or f"{data_dir}/vectordb"
        self.auth_db_path = self.auth_db_path or f"{data_dir}/users.db"
        self.logs_folder = self.logs_folder or f"{data_dir}/logs"
        self.logs_filepath = self.logs_filepath or f"{self.logs_folder}/user_questions.log"
        return self

    @property
    def endpoints(self) -> list[SparqlEndpointLinks]:
        """The endpoints served by this instance, one per project file."""
        from sparql_llm.project import get_projects

        return [p.as_endpoint_links() for p in get_projects()]

    @property
    def server_url(self) -> str:
        """Computed server URL using the host and port, for accessing locally for /mcp calls.

        Returns:
            A string like 'http://127.0.0.1:8888'.
        """
        # Use 127.0.0.1 for connecting to the service (0.0.0.0 is only for binding)
        return f"http://127.0.0.1:{self.server_port}"

    @classmethod
    def from_file(cls, filepath: str) -> Settings:
        """Create a Settings instance from a file.

        Args:
            filepath: The path to the file.
        """
        path = Path(filepath)
        if not path.is_file():
            raise SettingsError(f"The settings file {path} (SETTINGS_FILEPATH) does not exist.")
        try:
            content = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise SettingsError(f"The settings file {path} (SETTINGS_FILEPATH) is not valid JSON: {e}") from e
        return Settings(**content)


class SettingsError(Exception):
    """The install settings file is missing or unreadable; the app refuses to start."""


settings_filepath = os.getenv("SETTINGS_FILEPATH")
settings = Settings.from_file(settings_filepath) if settings_filepath else Settings()


# Configuration defined at runtime
@dataclass(kw_only=True)
class Configuration:
    """The configuration for the agent that can be changed at runtime when calling the agent."""

    enable_entities_resolution: bool = field(
        default=True,
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

    use_tools: bool = field(
        default=settings.use_tools,
        metadata={
            "description": "Whether to answer using the experimental MCP tool-calling agent (True) or the default "
            "retrieval + validation pipeline (False). Toggled per-request so the UI can switch modes at runtime."
        },
    )

    project: str = field(
        default="",
        metadata={
            "description": "The id of the dataset this request is answered from, checked by the chat endpoint. "
            "Empty = the first dataset."
        },
    )

    natural_language_only: bool = field(
        default=False,
        metadata={
            "description": "If True, instructs the model to enclose all reasoning and SPARQL queries inside a <think> block so they are hidden from the main UI chat body, exposing only natural language answers."
        },
    )

    system_prompt: str = field(
        default="",
        metadata={
            "description": "The system prompt to use for the agent's interactions."
            "This prompt sets the context and behavior for the agent. Empty = assembled from the projects."
        },
    )

    system_prompt_tools: str = field(
        default="",
        metadata={
            "description": "System prompt used in MCP tools mode (use_tools=True). Unlike system_prompt it "
            "instructs the model to actually execute queries and iterate with the tools rather than "
            "producing a single unexecuted query."
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

    max_tool_iterations: int = field(
        default=settings.default_max_tool_iterations,
        metadata={
            "description": "MCP tools mode only: the maximum number of tool-call rounds (exploration steps) "
            "the model may take before it must produce a final answer."
        },
    )

    # ── Per-user LLM credentials (injected at request time) ──────────────
    llm_api_key: str = field(
        default="",
        metadata={"description": "Per-user LLM API key, injected at request time. Never persisted in state."},
    )
    llm_base_url: str = field(
        default="",
        metadata={"description": "Per-user LLM base URL, injected at request time."},
    )
    llm_provider: str = field(
        default="gpustack",
        metadata={"description": "LLM provider identifier (gpustack, openai, custom)."},
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
