"""API to deploy the SPARQL-LLM agent service from LangGraph."""

import asyncio
import contextlib
import json
import logging
import os
import pathlib
import re
from collections.abc import AsyncGenerator, AsyncIterator
from datetime import datetime
from typing import Any

from fastapi import Depends, FastAPI, Form, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.requests import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from langchain_core.runnables import RunnableConfig
from langfuse.langchain import CallbackHandler
from pydantic import BaseModel

from sparql_llm.agent.graph import get_graph, graph
from sparql_llm.config import settings
from sparql_llm.mcp_server import get_mcp_app
from sparql_llm.utils import logger, strip_think_stream

if settings.sentry_url:
    import sentry_sdk

    sentry_sdk.init(
        dsn=settings.sentry_url,
        # Add data like request headers and IP for users, see https://docs.sentry.io/platforms/python/data-management/data-collected/ for more info
        send_default_pii=True,
        # Set traces_sample_rate to 1.0 to capture 100% of transactions for tracing.
        traces_sample_rate=0.0,
    )


# Initialize Langfuse logs tracing CallbackHandler for Langchain https://langfuse.com/docs/integrations/langchain/example-python-langgraph
langfuse_handler = [CallbackHandler(update_trace=True)] if os.getenv("LANGFUSE_SECRET_KEY") else []

mcp = get_mcp_app()

# Auth imports (only when auth is enabled to avoid import errors if not installed)
if settings.auth_enabled:
    import fastapi_users.exceptions as _fu_exc
    from fastapi_users.authentication import CookieTransport

    from sparql_llm.agent.auth import (
        User,
        auth_backend,
        create_db_and_tables,
        current_active_user,
        fastapi_users,
        get_user_manager,
        optional_current_user,
    )


def is_valid_login_redirect(value: str):
    return (
        value.startswith("/") and
        value.isprintable() and
        not value.startswith("//") and
        not value.startswith("/\\") and
        not value.isspace()
    )


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """FastAPI lifespan that initializes the MCP session manager and auth DB."""
    if settings.auth_enabled:
        # Ensure the data directory exists
        pathlib.Path(settings.auth_db_path).parent.mkdir(parents=True, exist_ok=True)
        await create_db_and_tables()
        # Create initial admin user if credentials are configured
        if settings.admin_email and settings.admin_password:
            from fastapi_users.password import PasswordHelper

            from sparql_llm.agent.auth import User, async_session_maker, get_user_manager
            from sparql_llm.agent.auth import UserManager
            from sparql_llm.agent.auth import get_user_db
            from sparql_llm.agent.auth import SQLAlchemyUserDatabase
            from fastapi_users import schemas

            async with async_session_maker() as session:
                from sparql_llm.agent.auth import User as UserModel
                from fastapi_users.db import SQLAlchemyUserDatabase

                user_db = SQLAlchemyUserDatabase(session, UserModel)
                password_helper = PasswordHelper()
                manager = UserManager(user_db)
                try:
                    from fastapi_users import schemas as fu_schemas
                    existing = await manager.get_by_email(settings.admin_email)
                    logger.info(f"🔐 Admin user already exists: {existing.email}")
                except Exception:
                    hashed = password_helper.hash(settings.admin_password)
                    from sqlalchemy import insert
                    import uuid as _uuid
                    await session.execute(
                        UserModel.__table__.insert().values(
                            id=_uuid.uuid4(),
                            email=settings.admin_email,
                            hashed_password=hashed,
                            is_active=True,
                            is_superuser=True,
                            is_verified=True,
                        )
                    )
                    await session.commit()
                    logger.info(f"🔐 Created initial admin user: {settings.admin_email}")
    async with mcp.session_manager.run():
        yield


app = FastAPI(
    title=settings.app_name,
    description=f"""Natural-language interface to the Elites Suisses knowledge graph
({settings.endpoints[0]['endpoint_url']}). Ask questions in English or French about
~58,700 Swiss elites: biographical data, education, family relations, marriages,
organisational memberships and mandates.""",
    version="1.0.0",
    lifespan=lifespan,
)

app.mount("/mcp", mcp.streamable_http_app(), name="mcp")


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Auth routes ────────────────────────────────────────────────────────────────
if settings.auth_enabled:
    # Mount fastapi-users cookie login/logout router
    app.include_router(
        fastapi_users.get_auth_router(auth_backend),
        prefix="/auth",
        tags=["auth"],
    )

    templates = Jinja2Templates(directory="src/sparql_llm/agent/webapp")

    @app.post("/logout", include_in_schema=False)
    async def logout_redirect(request: Request) -> RedirectResponse:
        """Clear the auth cookie and redirect to /login."""
        response = RedirectResponse(url="/login", status_code=302)
        from sparql_llm.agent.auth import cookie_transport
        response.delete_cookie(key=cookie_transport.cookie_name)
        return response

    @app.get("/change-password", response_class=HTMLResponse, include_in_schema=False)
    async def change_password_page(
        request: Request,
        user: "User" = Depends(current_active_user),
        error: str = "",
        success: str = "",
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            "change-password.html",
            {"request": request, "current_user": user, "error": error, "success": success},
        )

    @app.post("/change-password", response_class=HTMLResponse, include_in_schema=False)
    async def change_password_submit(
        request: Request,
        current_password: str = Form(...),
        new_password: str = Form(...),
        confirm_password: str = Form(...),
        user: "User" = Depends(current_active_user),
    ) -> HTMLResponse:
        """Verify the current password then update to the new one."""
        from fastapi_users.password import PasswordHelper
        from sparql_llm.agent.auth import async_session_maker, User as UserModel
        from fastapi_users.db import SQLAlchemyUserDatabase
        from sparql_llm.agent.auth import UserManager

        def _render(error: str = "", success: str = "") -> HTMLResponse:
            return templates.TemplateResponse(
                "change-password.html",
                {"request": request, "current_user": user, "error": error, "success": success},
            )

        if new_password != confirm_password:
            return _render(error="New passwords do not match.")
        if len(new_password) < 8:
            return _render(error="New password must be at least 8 characters.")

        password_helper = PasswordHelper()
        # Verify current password
        verified, _ = password_helper.verify_and_update(current_password, user.hashed_password)
        if not verified:
            return _render(error="Current password is incorrect.")

        # Save new password
        new_hashed = password_helper.hash(new_password)
        async with async_session_maker() as session:
            db_user = await session.get(UserModel, user.id)
            db_user.hashed_password = new_hashed
            await session.commit()

        return _render(success="Password updated successfully.")

    @app.get("/login", response_class=HTMLResponse, include_in_schema=False)
    async def login_page(request: Request, error: str = "", next: str = "/") -> HTMLResponse:
        return templates.TemplateResponse("login.html", {"request": request, "error": error, "next": next})

    @app.post("/login", response_class=HTMLResponse, include_in_schema=False)
    async def login_form(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
        next: str = Form("/"),
    ) -> HTMLResponse:
        """Handle the HTML login form, set the auth cookie, and redirect."""
        from fastapi_users.authentication import CookieTransport
        from fastapi_users.exceptions import UserInactive, UserNotExists

        from sparql_llm.agent.auth import async_session_maker, get_user_manager
        from sparql_llm.agent.auth import User as UserModel
        from fastapi_users.db import SQLAlchemyUserDatabase

        async with async_session_maker() as session:
            user_db = SQLAlchemyUserDatabase(session, UserModel)
            from sparql_llm.agent.auth import UserManager
            manager = UserManager(user_db)
            try:
                user = await manager.authenticate(
                    credentials=type("Creds", (), {"username": username, "password": password})()
                )
                if user is None or not user.is_active:
                    raise Exception("Invalid credentials")
            except Exception:
                return templates.TemplateResponse(
                    "login.html",
                    {"request": request, "error": "Invalid email or password.", "next": next},
                    status_code=401,
                )

        # Issue JWT token and set cookie
        from sparql_llm.agent.auth import auth_backend, get_jwt_strategy
        strategy = get_jwt_strategy()
        token = await strategy.write_token(user)
        response = RedirectResponse(url=next if is_valid_login_redirect(next) else "/", status_code=302)
        # Set cookie matching the transport config
        from sparql_llm.agent.auth import cookie_transport
        response.set_cookie(
            key=cookie_transport.cookie_name,
            value=token,
            max_age=cookie_transport.cookie_max_age,
            httponly=cookie_transport.cookie_httponly,
            secure=cookie_transport.cookie_secure,
            samesite=cookie_transport.cookie_samesite,
        )
        return response

    @app.get("/admin", response_class=HTMLResponse, include_in_schema=False)
    async def admin_page(
        request: Request,
        flash_success: str = "",
        flash_error: str = "",
        user: "User" = Depends(current_active_user),
    ) -> HTMLResponse:
        """Admin panel — lists all users. Superusers only."""
        if not user.is_superuser:
            return RedirectResponse("/", status_code=302)
        from sparql_llm.agent.auth import async_session_maker, User as UserModel
        from sqlalchemy import select
        async with async_session_maker() as session:
            result = await session.execute(select(UserModel).order_by(UserModel.email))
            users = result.scalars().all()
        return templates.TemplateResponse(
            "admin.html",
            {
                "request": request,
                "current_user": user,
                "users": users,
                "flash_success": flash_success,
                "flash_error": flash_error,
            },
        )

    @app.get("/admin/index-status", include_in_schema=False)
    async def admin_index_status(
        user: "User" = Depends(current_active_user),
    ) -> JSONResponse:
        """Is the retrieval index still in step with the endpoint, and is a rebuild running?

        Fetched by the admin page after render rather than during it: the drift check makes
        three SPARQL queries and should never hold up the page.
        """
        if not user.is_superuser:
            return JSONResponse({"error": "forbidden"}, status_code=403)
        from sparql_llm.indexing.drift import check_drift
        from sparql_llm.indexing.rebuild import read_job

        drift = await run_in_threadpool(check_drift)
        return JSONResponse({"drift": drift, "job": read_job()})

    @app.post("/admin/reindex", include_in_schema=False)
    async def admin_reindex(
        user: "User" = Depends(current_active_user),
    ) -> JSONResponse:
        """Rebuild the retrieval index from the live endpoint. Superusers only.

        Returns as soon as the job starts. The new index is built into a fresh collection
        and only swapped in when complete, so the assistant keeps answering from the current
        index throughout, and a failed rebuild changes nothing.
        """
        if not user.is_superuser:
            return JSONResponse({"error": "forbidden"}, status_code=403)
        from sparql_llm.indexing.rebuild import start_rebuild

        result = await run_in_threadpool(start_rebuild)
        if not result.get("started"):
            return JSONResponse(
                {"error": "A rebuild is already running.", **result}, status_code=409
            )
        return JSONResponse(result)

    @app.post("/admin/add-user", include_in_schema=False)
    async def admin_add_user(
        request: Request,
        email: str = Form(...),
        password: str = Form(...),
        is_superuser: str = Form(""),
        user: "User" = Depends(current_active_user),
    ) -> RedirectResponse:
        """Create a new user account."""
        if not user.is_superuser:
            return RedirectResponse("/", status_code=302)
        from fastapi_users.password import PasswordHelper
        from sparql_llm.agent.auth import async_session_maker, User as UserModel
        import uuid as _uuid
        password_helper = PasswordHelper()
        hashed = password_helper.hash(password)
        try:
            async with async_session_maker() as session:
                await session.execute(
                    UserModel.__table__.insert().values(
                        id=_uuid.uuid4(),
                        email=email,
                        hashed_password=hashed,
                        is_active=True,
                        is_superuser=bool(is_superuser),
                        is_verified=True,
                    )
                )
                await session.commit()
            return RedirectResponse(f"/admin?flash_success=User+{email}+created+successfully", status_code=302)
        except Exception as exc:
            return RedirectResponse(f"/admin?flash_error={exc}", status_code=302)

    @app.post("/admin/delete-user", include_in_schema=False)
    async def admin_delete_user(
        request: Request,
        user_id: str = Form(...),
        user: "User" = Depends(current_active_user),
    ) -> RedirectResponse:
        """Delete a user account."""
        if not user.is_superuser:
            return RedirectResponse("/", status_code=302)
        from sparql_llm.agent.auth import async_session_maker, User as UserModel
        import uuid as _uuid
        try:
            async with async_session_maker() as session:
                uid = _uuid.UUID(user_id)
                db_user = await session.get(UserModel, uid)
                if db_user:
                    await session.delete(db_user)
                    await session.commit()
            return RedirectResponse("/admin?flash_success=User+deleted", status_code=302)
        except Exception as exc:
            return RedirectResponse(f"/admin?flash_error={exc}", status_code=302)

else:
    templates = Jinja2Templates(directory="src/sparql_llm/agent/webapp")

# Redirect unauthenticated browser requests to /login instead of 401 JSON
if settings.auth_enabled:
    from fastapi.exceptions import HTTPException

    @app.exception_handler(401)
    async def unauthorized_handler(request: Request, exc: HTTPException) -> RedirectResponse:
        # API calls (Accept: application/json or non-GET) get a proper 401
        accept = request.headers.get("accept", "")
        if request.method != "GET" or "application/json" in accept:
            return JSONResponse(status_code=401, content={"detail": "Not authenticated"})
        return RedirectResponse(url=f"/login?next={request.url.path}", status_code=302)

# Create logs file if it doesn't exist
question_logger = logging.getLogger("question_logger")
question_logger.setLevel(logging.INFO)
try:
    if not os.path.exists(settings.logs_filepath):
        pathlib.Path(settings.logs_filepath).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(settings.logs_filepath).touch()
    file_handler = logging.FileHandler(settings.logs_filepath)
    file_handler.setFormatter(logging.Formatter("%(asctime)s - %(message)s"))
    question_logger.addHandler(file_handler)
except Exception:
    logger.warning(f"⚠️ Logs filepath {settings.logs_filepath} not writable.")

uvicorn_logger = logging.getLogger("uvicorn")
uvicorn_logger.setLevel(logging.WARNING)

# Error logger — writes full tracebacks from the chat pipeline to a readable file
# (journald requires elevated perms to read). Used by stream_response so failures
# — especially in the experimental MCP tools mode — are diagnosable and surfaced
# to the user with a real reason instead of a generic banner.
error_logger = logging.getLogger("agent_error_logger")
error_logger.setLevel(logging.ERROR)
try:
    _err_path = os.path.join(os.path.dirname(settings.logs_filepath) or ".", "agent_errors.log")
    pathlib.Path(_err_path).parent.mkdir(parents=True, exist_ok=True)
    _err_handler = logging.FileHandler(_err_path)
    _err_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    error_logger.addHandler(_err_handler)
except Exception:
    logger.warning("⚠️ Could not set up agent error log; errors will only go to stderr/journald.")

api_url = "http://localhost:8000"
logger.info(f"""💬 Chat UI at {api_url}
  ⚡️ Streamable HTTP MCP server started on {api_url}/mcp
  🔎 Using similarity search service on {settings.vectordb_url}
""")


# ── Unified auth dependency ────────────────────────────────────────────────────
# When auth is enabled, `require_user` enforces a valid session cookie.
# When auth is disabled it's a no-op so development works without credentials.
if settings.auth_enabled:
    async def _noop():
        return None

    require_user = current_active_user
else:
    async def _noop():  # type: ignore[no-redef]
        return None

    require_user = _noop  # type: ignore[assignment]


class Message(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    messages: list[Message]
    model: str = settings.default_llm_model
    max_tokens: int = settings.default_max_tokens
    temperature: float = settings.default_temperature
    stream: bool = False
    validate_output: bool = True
    enable_sparql_execution: bool = True
    use_tools: bool = settings.use_tools
    natural_language_only: bool = False
    max_try_fix_sparql: int = settings.default_max_try_fix_sparql
    max_tool_iterations: int = settings.default_max_tool_iterations
    headers: dict[str, str] = {}
    session_id: str | None = None


def convert_chunk_to_dict(obj: Any) -> Any:
    """Recursively convert a langgraph chunk object to a dict.

    Required because LangGraph objects are not serializable by default.
    And they use a mix of tuples, dataclasses (State, Configuration) and pydantic BaseModel (BaseMessage).
    """
    # {'retrieve': {'retrieved_docs': [Document(metadata={'endpoint_url':
    # When sending a msg LangGraph sends a tuple with the message and the metadata
    if isinstance(obj, tuple) and len(obj) == 2:
        # Message and metadata
        return [convert_chunk_to_dict(obj[0]), convert_chunk_to_dict(obj[1])]
    elif isinstance(obj, list):
        return [convert_chunk_to_dict(item) for item in obj]
    elif isinstance(obj, dict):
        return {k: convert_chunk_to_dict(v) for k, v in obj.items()}
    elif hasattr(obj, "model_dump"):
        return obj.model_dump()  # type: ignore
    elif hasattr(obj, "dict"):
        return obj.dict()  # type: ignore
    elif hasattr(obj, "__dict__"):
        return obj.__dict__
    # elif hasattr(obj, "__dict__") and not isinstance(obj, type):
    #     # Convert dataclass or other objects to dict, but skip type objects
    #     return {k: convert_chunk_to_dict(v) for k, v in obj.__dict__.items()}
    else:
        return obj


async def stream_response(inputs: Any, config: RunnableConfig, run_graph: Any = graph) -> AsyncGenerator[str, Any]:
    """Stream the response from the assistant.

    Reasoning ("thinking") models served via GPUStack — e.g. minimax-m2.7 —
    interleave their chain-of-thought as ``<think>…</think>`` blocks inside the
    streamed ``content``. We strip those blocks from every node's token stream so
    the chat UI never renders them (the call_model node re-surfaces its own
    reasoning as a populated step). The previous per-chunk substring filter leaked
    the opening ``<think>`` token (and the first word glued to it, e.g.
    ``"<think>The"``) because a tag split across token boundaries was never
    matched. Instead we accumulate the call_model output and re-derive the
    visible (non-reasoning) text on every chunk via ``strip_think_stream``,
    emitting only the newly revealed delta — robust to tags split across any
    number of chunks.
    """
    # Per-message accumulator for the call_model stream, reset at each node
    # boundary (every node emits an "updates" event when it finishes).
    think_buffer = ""
    emitted_len = 0

    try:
        async for event, chunk in run_graph.astream(inputs, stream_mode=["messages", "updates"], config=config):
            if event == "updates":
                # New node starting → reset the think-stripping accumulator.
                think_buffer = ""
                emitted_len = 0
                chunk_dict = convert_chunk_to_dict({"event": event, "data": chunk})
                for node_data in chunk_dict.get("data", {}).values():
                    if node_data and "steps" in node_data:
                        node_data["steps"] = [
                            s for s in node_data["steps"]
                            if s.get("label") or s.get("type") == "fix-message"
                        ]
                yield f"data: {json.dumps(chunk_dict)}\n\n"
                await asyncio.sleep(0)
                continue

            if event == "messages":
                msg, metadata = chunk
                content = getattr(msg, "content", "") if msg else ""
                # A model that is *making a tool call* should not render prose in
                # the chat body — the tool step bubble already shows the action.
                # This matters for gpt-oss-120b, which (unlike other models)
                # duplicates the tool-call arguments JSON into ``content`` (e.g.
                # ``{"question": "...", "sparql_query": "..."}``). Without this
                # guard that raw JSON would be streamed to the UI as garbage text
                # before the real answer. The final, tool-call-free message still
                # streams normally, so the actual answer is unaffected.
                has_tool_calls = bool(
                    getattr(msg, "tool_calls", None) or getattr(msg, "tool_call_chunks", None)
                )
                if has_tool_calls and getattr(msg, "type", "") != "tool":
                    continue
                # Strip inline <think> reasoning from every streamed LLM token (both
                # the extract_user_question and call_model nodes run reasoning models).
                # Tool results are not LLM token streams, so leave them untouched.
                # Holding back incomplete tags also stops a bare "</think>" from ever
                # reaching the UI, where it would create an empty "Thought process"
                # step. The actual reasoning is surfaced as a populated step by the
                # call_model node instead.
                if isinstance(content, str) and content and getattr(msg, "type", "") != "tool":
                    think_buffer += content
                    visible = strip_think_stream(think_buffer)
                    if len(visible) <= emitted_len:
                        # Everything new is reasoning or an incomplete tag — hold back.
                        continue
                    msg.content = visible[emitted_len:]
                    emitted_len = len(visible)

            chunk_dict = convert_chunk_to_dict({"event": event, "data": chunk})
            # Frontend only renders assistant text when type == "AIMessageChunk".
            # When streaming is disabled for a model (gpt-oss uses
            # disable_streaming="tool_calling" because it drops streamed tool
            # calls), the final answer arrives as a single, non-streamed
            # AIMessage whose serialized type is "ai" — which the UI would
            # silently drop, showing a blank reply. Relabel it to
            # "AIMessageChunk" so it renders identically to a streamed answer.
            if event == "messages":
                data = chunk_dict.get("data")
                if isinstance(data, list) and data and isinstance(data[0], dict) and data[0].get("type") == "ai":
                    data[0]["type"] = "AIMessageChunk"
            yield f"data: {json.dumps(chunk_dict)}\n\n"
            await asyncio.sleep(0)
    except Exception as exc:
        # Any failure inside the graph (model rejecting tool calls, MCP transport
        # errors, recursion limit, etc.) would otherwise break the SSE stream and
        # show the UI a generic "contact an admin" banner with no detail. Log the
        # full traceback to the readable error log and surface a short, real reason
        # to the user as an assistant message so the chat stays usable.
        use_tools = bool(config.get("configurable", {}).get("use_tools"))
        error_logger.exception(
            "Chat stream failed (use_tools=%s, model=%s): %s",
            use_tools,
            config.get("configurable", {}).get("model"),
            exc,
        )
        reason = f"{type(exc).__name__}: {exc}".strip()
        hint = ""
        if use_tools:
            hint = (
                "\n\nThis happened in the experimental **MCP tools** mode. The selected "
                "model may not support tool calling, or a tool call failed. Try turning "
                "MCP tools off, or pick a model that supports tools."
            )
        error_msg = {
            "event": "messages",
            "data": [
                {"content": f"⚠️ The request could not be completed.\n\n`{reason}`{hint}", "type": "AIMessageChunk"},
                {"langgraph_node": "call_model"},
            ],
        }
        yield f"data: {json.dumps(error_msg)}\n\n"
        await asyncio.sleep(0)
    yield "data: [DONE]"


# FastAPI does not support Union in response model (even if it says otherwise in docs)
# so we need to disable response_model for this endpoint
@app.post("/chat", response_model=None)
async def chat(
    request: Request,
    _user: Any = Depends(require_user),
) -> StreamingResponse | JSONResponse:
    """Chat with the assistant main endpoint."""
    auth_header = request.headers.get("Authorization", "")
    if settings.chat_api_key and (not auth_header or not auth_header.startswith("Bearer ")):
        raise ValueError("Missing or invalid Authorization header")
    if settings.chat_api_key and auth_header.split(" ")[1] != settings.chat_api_key:
        raise ValueError("Invalid API key")

    chat_request = ChatCompletionRequest(**await request.json())
    
    # If natural language mode is enabled, force the use of MCP tools logic
    if chat_request.natural_language_only:
        chat_request.use_tools = True

    # request.messages = [msg for msg in request.messages if msg.role != "system"]
    # request.messages = [Message(role="system", content=settings.system_prompt), *request.messages]

    question: str = chat_request.messages[-1].content if chat_request.messages else ""
    question_logger.info(f"User question: {question}")
    if not question:
        raise ValueError("No question provided")

    # Guard: MCP tools mode requires a model that supports tool/function calling.
    # Some GPUStack deployments (e.g. the qwen3-vl vision models) are served without
    # the tool-calling flags and will reject any tool request. Rather than let that
    # fail mid-stream, tell the user up front to switch models or turn tools off.
    tool_capable = settings.tool_capable_models or settings.available_llm_models or [settings.default_llm_model]
    if chat_request.use_tools and chat_request.model not in tool_capable:
        capable_names = ", ".join(m.split("/", 1)[-1] for m in tool_capable) or "(none configured)"
        selected_name = chat_request.model.split("/", 1)[-1]
        msg = (
            f"⚠️ The model **{selected_name}** does not support tool calling, so it can't be used in "
            f"**MCP tools** mode.\n\nEither turn MCP tools off, or pick a tool-capable model: {capable_names}."
        )

        async def _reject() -> AsyncGenerator[str, Any]:
            payload = {
                "event": "messages",
                "data": [{"content": msg, "type": "AIMessageChunk"}, {"langgraph_node": "call_model"}],
            }
            yield f"data: {json.dumps(payload)}\n\n"
            yield "data: [DONE]"

        if chat_request.stream:
            return StreamingResponse(_reject(), media_type="text/event-stream")
        return JSONResponse(content={"messages": [{"role": "assistant", "content": msg}]})

    # print(request.model)
    # Pass session_id via metadata for Langfuse to properly group multi-turn conversations
    # https://langfuse.com/docs/integrations/langchain/tracing#trace-attributes
    langfuse_metadata = {}
    if chat_request.session_id:
        langfuse_metadata["langfuse_session_id"] = chat_request.session_id

    # Clamp the user-requested number of self-correction attempts to a sane range
    # so a bad/huge value can't run forever or blow past the graph recursion limit.
    max_try = max(1, min(chat_request.max_try_fix_sparql, 20))
    # MCP tools mode only: how many tool-call rounds (exploration steps) the model
    # may take before it must answer. Clamped to a sane range too.
    max_tool_iterations = max(1, min(chat_request.max_tool_iterations, 30))
    # Each pipeline fix attempt and each tools exploration step costs ~2 graph steps.
    # Scale the LangGraph recursion limit so a higher attempt/step count doesn't trip
    # a GraphRecursionError; never go below the original 25. We size for whichever
    # mode could run so the same config is safe regardless of use_tools.
    recursion_limit = max(25, 2 * max_try + 10, 2 * max_tool_iterations + 10)

    config = RunnableConfig(
        configurable={
            "model": chat_request.model,
            "temperature": chat_request.temperature,
            "max_tokens": chat_request.max_tokens,
            "validate_output": chat_request.validate_output,
            "enable_sparql_execution": chat_request.enable_sparql_execution,
            "use_tools": chat_request.use_tools,
            "max_try_fix_sparql": max_try,
            "max_tool_iterations": max_tool_iterations,
            "natural_language_only": chat_request.natural_language_only,
        },
        metadata=langfuse_metadata,
        recursion_limit=recursion_limit,
        callbacks=langfuse_handler,  # type: ignore
    )
    inputs: Any = {
        "messages": [(msg.role, msg.content) for msg in chat_request.messages[-10:]],
    }

    # Select the graph to run: the experimental MCP tool-calling agent when
    # use_tools is requested, otherwise the default retrieval + validation pipeline.
    run_graph = get_graph(chat_request.use_tools)

    # request.stream = False
    if chat_request.stream:
        return StreamingResponse(
            stream_response(inputs, config, run_graph),
            media_type="text/event-stream",
            # media_type="application/x-ndjson"
        )

    response = await run_graph.ainvoke(inputs, config=config)
    # Convert LangChain message objects to dicts for JSON serialization
    response_dict = convert_chunk_to_dict(response)
    return JSONResponse(content=response_dict)


class LogMessage(Message):
    """Message model for logging purposes."""

    steps: list[Any] | None = None


class FeedbackRequest(BaseModel):
    like: bool
    messages: list[LogMessage]


def log_msg(filename: str, messages: list[LogMessage]) -> None:
    """Log a messages thread to a log file."""
    timestamp = datetime.now().isoformat()
    feedback_data = {
        "timestamp": timestamp,
        "messages": [message.model_dump() for message in messages],
    }
    with open(filename, "a") as f:
        f.write(json.dumps(feedback_data) + "\n")


@app.post("/feedback")
async def post_feedback(
    feedback_request: FeedbackRequest,
    _user: Any = Depends(require_user),
) -> JSONResponse:
    """Save a user feedback in the logs files."""
    filename = (
        f"{settings.logs_folder}/likes.jsonl" if feedback_request.like else f"{settings.logs_folder}/dislikes.jsonl"
    )
    log_msg(filename, feedback_request.messages)
    return JSONResponse(content={"status": "success"})


@app.get("/models")
async def get_models(
    request: Request,
    _user: Any = Depends(require_user),
) -> JSONResponse:
    """Return the list of available LLM models for the chat UI dropdown.

    When ``settings.available_llm_models`` is configured, that explicit list is
    returned as-is — no upstream API call is made, so embedding models, Whisper
    models, and other non-chat models on GPUStack are never exposed.

    When the list is empty, the endpoint falls back to
    ``[settings.default_llm_model]``.
    """
    if settings.chat_api_key:
        auth_header = request.headers.get("Authorization", "")
        token = auth_header.split(" ", 1)[1] if auth_header.startswith("Bearer ") else ""
        if token != settings.chat_api_key:
            return JSONResponse(status_code=401, content={"error": "Unauthorized"})

    models = settings.available_llm_models or [settings.default_llm_model]
    # Expose which models can be used in MCP tools mode so the UI can guide the user.
    # An empty tool_capable_models list means "treat all as capable".
    tool_capable = settings.tool_capable_models or models
    return JSONResponse(
        content={
            "models": models,
            "default": settings.default_llm_model,
            "tool_capable_models": tool_capable,
        }
    )


class LogsRequest(BaseModel):
    api_key: str


@app.post("/logs", response_model=list[str])
async def get_user_logs(logs_request: LogsRequest) -> JSONResponse:
    """Get the list of user questions from the logs file."""
    if settings.logs_api_key and logs_request.api_key != settings.logs_api_key:
        raise ValueError("Invalid API key")
    questions = set()
    pattern = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) - User question: (.+)")
    with open(settings.logs_filepath) as file:
        for line in file:
            match = pattern.search(line)
            if match:
                # date_time = match.group(1)
                question = match.group(2)
                # questions.append({"date": date_time, "question": question})
                questions.add(question)
    return JSONResponse(content=list(questions))


# Serve website built using vitejs
app.mount(
    "/assets",
    StaticFiles(directory="src/sparql_llm/agent/webapp/assets"),
    name="static",
)


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def chat_ui(
    request: Request,
    _user: Any = Depends(require_user),
) -> HTMLResponse:
    """Render the chat UI using jinja2 + HTML."""
    is_superuser = getattr(_user, "is_superuser", False)
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "api_key": settings.chat_api_key,
            "chat_endpoint": "/chat",
            "feedback_endpoint": "/feedback",
            "examples": ",".join(settings.example_questions),
            "auth_enabled": settings.auth_enabled,
            "is_superuser": is_superuser,
        },
    )


# NOTE: experimental AG-UI endpoint
# from ag_ui.core.types import RunAgentInput
# from ag_ui.encoder import EventEncoder
# @app.post("/agent", response_model=list[str])
# async def langgraph_agent_endpoint(request: Request):
#     """Handle LangGraph agent requests with SSE streaming."""
#     # Parse the request body
#     input_data = RunAgentInput(**await request.json())
#     # Get the accept header from the request
#     accept_header = request.headers.get("accept")
#     # Create an event encoder to properly format SSE events
#     encoder = EventEncoder(accept=accept_header)
#     async def event_generator():
#         async for event in graph.run(input_data):
#             yield encoder.encode(event)
#     return StreamingResponse(
#         event_generator(),
#         media_type=encoder.get_content_type()
#     )

# Test it:
# curl -X POST http://localhost:8000/agent -H "Content-Type: application/json" -H "Accept: text/event-stream" -d '{
#  "messages": [
#  	 {"id": "msg_1", "role": "user", "content": "What is the HGNC symbol for the P68871 protein?"}
#  ],
#  "threadId": "t1", "runId": "r1", "tools": [], "context": [], "state": {}, "forwardedProps" : {}
# }'
