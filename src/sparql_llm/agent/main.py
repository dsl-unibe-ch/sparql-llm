"""API to deploy the SPARQL-LLM agent service from LangGraph."""

import asyncio
import contextlib
import json
import logging
import os
import pathlib
import re
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from datetime import datetime
from typing import Any
from urllib.parse import quote_plus

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from langchain_core.runnables import RunnableConfig
from langfuse.langchain import CallbackHandler
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from sparql_llm.agent.graph import get_graph, graph
from sparql_llm.agent.roles import (
    ROLE_LABELS,
    USER,
    assignable_roles,
    can_create,
    can_delete,
    can_manage,
    can_set_role,
    creatable_roles,
    role_flags,
    role_of,
)
from sparql_llm.config import settings
from sparql_llm.mcp_server import get_mcp_app
from sparql_llm.utils import logger, strip_sparql_stream, strip_think_stream

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

# Login, the users database and the admin pages exist only when auth is enabled.
if settings.auth_enabled:
    from fastapi_users.db import SQLAlchemyUserDatabase
    from fastapi_users.password import PasswordHelper
    from sqlalchemy import select

    from sparql_llm.agent.auth import (
        User,
        UserManager,
        async_session_maker,
        auth_backend,
        cookie_transport,
        create_db_and_tables,
        current_active_user,
        fastapi_users,
        get_jwt_strategy,
    )
    from sparql_llm.agent.conversations import delete_conversations_of
    from sparql_llm.agent.conversations import router as conversations_router
    from sparql_llm.indexing.drift import check_drift
    from sparql_llm.indexing.rebuild import read_job, start_rebuild


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
        pathlib.Path(settings.auth_db_path).parent.mkdir(parents=True, exist_ok=True)
        await create_db_and_tables()
        # Create the initial admin account when one is configured and missing.
        if settings.admin_email and settings.admin_password:
            async with async_session_maker() as session:
                manager = UserManager(SQLAlchemyUserDatabase(session, User))
                try:
                    existing = await manager.get_by_email(settings.admin_email)
                    logger.info(f"🔐 Admin user already exists: {existing.email}")
                except Exception:
                    hashed = PasswordHelper().hash(settings.admin_password)
                    await session.execute(
                        User.__table__.insert().values(
                            id=uuid.uuid4(),
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

templates = Jinja2Templates(directory="src/sparql_llm/agent/webapp")

# ── Auth routes ────────────────────────────────────────────────────────────────
if settings.auth_enabled:
    # fastapi-users' cookie login/logout API
    app.include_router(
        fastapi_users.get_auth_router(auth_backend),
        prefix="/auth",
        tags=["auth"],
    )

    # Each user's saved chats, private to them.
    app.include_router(conversations_router)

    @app.post("/logout", include_in_schema=False)
    async def logout_redirect(request: Request) -> RedirectResponse:
        """Clear the auth cookie and redirect to /login."""
        response = RedirectResponse(url="/login", status_code=302)
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
            request,
            "change-password.html",
            {"current_user": user, "error": error, "success": success},
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

        def _render(error: str = "", success: str = "") -> HTMLResponse:
            return templates.TemplateResponse(
                request,
                "change-password.html",
                {"current_user": user, "error": error, "success": success},
            )

        if new_password != confirm_password:
            return _render(error="New passwords do not match.")
        if len(new_password) < 8:
            return _render(error="New password must be at least 8 characters.")

        password_helper = PasswordHelper()
        verified, _ = password_helper.verify_and_update(current_password, user.hashed_password)
        if not verified:
            return _render(error="Current password is incorrect.")

        new_hashed = password_helper.hash(new_password)
        async with async_session_maker() as session:
            db_user = await session.get(User, user.id)
            db_user.hashed_password = new_hashed
            await session.commit()

        return _render(success="Password updated successfully.")

    @app.get("/login", response_class=HTMLResponse, include_in_schema=False)
    async def login_page(request: Request, error: str = "", next: str = "/") -> HTMLResponse:
        return templates.TemplateResponse(request, "login.html", {"error": error, "next": next})

    @app.post("/login", response_class=HTMLResponse, include_in_schema=False)
    async def login_form(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
        next: str = Form("/"),
    ) -> HTMLResponse:
        """Handle the HTML login form, set the auth cookie, and redirect."""
        async with async_session_maker() as session:
            manager = UserManager(SQLAlchemyUserDatabase(session, User))
            try:
                user = await manager.authenticate(
                    credentials=type("Creds", (), {"username": username, "password": password})()
                )
                if user is None or not user.is_active:
                    raise Exception("Invalid credentials")
            except Exception:
                return templates.TemplateResponse(
                    request,
                    "login.html",
                    {"error": "Invalid email or password.", "next": next},
                    status_code=401,
                )

        token = await get_jwt_strategy().write_token(user)
        response = RedirectResponse(url=next if is_valid_login_redirect(next) else "/", status_code=302)
        # The cookie the fastapi-users transport reads back on every request.
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
        """Admin panel — index status and rebuild, and users. Admins and curators.

        What each row offers (remove, change role) is decided here by the rules in
        ``roles`` rather than in the template, so the page and the endpoints that enforce
        the same rules cannot disagree.
        """
        if not can_manage(user):
            return RedirectResponse("/", status_code=302)
        async with async_session_maker() as session:
            result = await session.execute(select(User).order_by(User.email))
            users = result.scalars().all()
        rows = [
            {
                "user": u,
                "role": role_of(u),
                "can_delete": can_delete(user, u),
                "assignable": assignable_roles(user, u),
            }
            for u in users
        ]
        return templates.TemplateResponse(
            request,
            "admin.html",
            {
                "current_user": user,
                "current_role": role_of(user),
                "users": users,
                "rows": rows,
                "creatable_roles": creatable_roles(user),
                "role_labels": ROLE_LABELS,
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
        if not can_manage(user):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        drift = await run_in_threadpool(check_drift)
        return JSONResponse({"drift": drift, "job": read_job()})

    @app.post("/admin/reindex", include_in_schema=False)
    async def admin_reindex(
        user: "User" = Depends(current_active_user),
    ) -> JSONResponse:
        """Rebuild the retrieval index from the live endpoint. Admins and curators.

        Returns as soon as the job starts. The new index is built into a fresh collection
        and only swapped in when complete, so the assistant keeps answering from the current
        index throughout, and a failed rebuild changes nothing.
        """
        if not can_manage(user):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        result = await run_in_threadpool(start_rebuild)
        if not result.get("started"):
            return JSONResponse(
                {"error": "A rebuild is already running.", **result}, status_code=409
            )
        return JSONResponse(result)

    def _flash(kind: str, message: str) -> RedirectResponse:
        """Back to the admin page with a message. Quoted, so any text survives the URL."""
        return RedirectResponse(f"/admin?flash_{kind}={quote_plus(message)}", status_code=302)

    # Every user-management endpoint re-checks the role rules server-side. The admin page
    # only hides buttons a role may not use; hiding is not enforcement.

    @app.post("/admin/add-user", include_in_schema=False)
    async def admin_add_user(
        request: Request,
        email: str = Form(...),
        password: str = Form(...),
        role: str = Form(USER),
        user: "User" = Depends(current_active_user),
    ) -> RedirectResponse:
        """Create a user account. Curators may create plain users only."""
        if not can_manage(user):
            return RedirectResponse("/", status_code=302)
        if not can_create(user, role):
            return _flash("error", f"You cannot create a user with the role '{role}'.")
        hashed = PasswordHelper().hash(password)
        try:
            async with async_session_maker() as session:
                await session.execute(
                    User.__table__.insert().values(
                        id=uuid.uuid4(),
                        email=email,
                        hashed_password=hashed,
                        is_active=True,
                        is_verified=True,
                        **role_flags(role),
                    )
                )
                await session.commit()
            return _flash("success", f"User {email} created as {ROLE_LABELS[role]}.")
        except Exception as exc:
            return _flash("error", str(exc))

    @app.post("/admin/delete-user", include_in_schema=False)
    async def admin_delete_user(
        request: Request,
        user_id: str = Form(...),
        user: "User" = Depends(current_active_user),
    ) -> RedirectResponse:
        """Remove a user account. Curators may remove plain users only; nobody themselves."""
        if not can_manage(user):
            return RedirectResponse("/", status_code=302)
        try:
            async with async_session_maker() as session:
                db_user = await session.get(User, uuid.UUID(user_id))
                if db_user is None:
                    return _flash("error", "No such user.")
                if not can_delete(user, db_user):
                    return _flash("error", f"You cannot remove {db_user.email}.")
                email = db_user.email
                await delete_conversations_of(session, db_user.id)
                await session.delete(db_user)
                await session.commit()
            return _flash("success", f"User {email} removed.")
        except Exception as exc:
            return _flash("error", str(exc))

    @app.post("/admin/set-role", include_in_schema=False)
    async def admin_set_role(
        request: Request,
        user_id: str = Form(...),
        role: str = Form(...),
        user: "User" = Depends(current_active_user),
    ) -> RedirectResponse:
        """Change a user's role. Admins only, and never on their own account."""
        if not can_manage(user):
            return RedirectResponse("/", status_code=302)
        try:
            async with async_session_maker() as session:
                db_user = await session.get(User, uuid.UUID(user_id))
                if db_user is None:
                    return _flash("error", "No such user.")
                if not can_set_role(user, db_user, role):
                    return _flash("error", f"You cannot change the role of {db_user.email}.")
                for column, value in role_flags(role).items():
                    setattr(db_user, column, value)
                email = db_user.email
                await session.commit()
            return _flash("success", f"{email} is now {ROLE_LABELS[role]}.")
        except Exception as exc:
            return _flash("error", str(exc))

    @app.exception_handler(401)
    async def unauthorized_handler(request: Request, exc: HTTPException) -> RedirectResponse | JSONResponse:
        """Send a logged-out browser to /login; API calls (JSON or non-GET) get a plain 401."""
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

# Full tracebacks from the chat pipeline, in a file readable without journald permissions.
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
# With auth enabled, `require_user` enforces a valid session cookie; without it,
# it is a no-op so development works without credentials.
async def _noop() -> None:
    return None


require_user = current_active_user if settings.auth_enabled else _noop


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
    # LangGraph sends a message as a (message, metadata) tuple
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
    else:
        return obj


def hide_sparql_in_answers(response_dict: dict[str, Any]) -> dict[str, Any]:
    """Remove SPARQL codeblocks from the assistant messages of a finished run.

    "Natural language only" mode promises an answer without SPARQL.
    ``stream_response`` enforces that while streaming; the non-streaming path
    returns the finished state instead, so it is enforced here. The queries the
    model executed stay available in the messages' tool calls.
    """
    for msg in response_dict.get("messages", []):
        if isinstance(msg, dict) and msg.get("type") == "ai" and isinstance(msg.get("content"), str):
            msg["content"] = strip_sparql_stream(msg["content"])
    return response_dict


async def stream_response(inputs: Any, config: RunnableConfig, run_graph: Any = graph) -> AsyncGenerator[str, Any]:
    """Stream the graph's run to the chat UI as server-sent events.

    Reasoning models (e.g. minimax-m2.7 on GPUStack) interleave ``<think>…</think>``
    blocks with their answer, and a tag can be split across any number of tokens. So
    the text of each message is accumulated, its visible part re-derived with
    ``strip_think_stream`` on every chunk, and only the newly visible delta is sent.
    The call_model node shows the reasoning itself as a "Thought process" step.
    """
    # Text of the message being streamed, reset at each node boundary (every node
    # emits an "updates" event when it finishes).
    think_buffer = ""
    emitted_len = 0
    # Natural-language mode hides any ```sparql block from the answer. The queries stay
    # visible in the "Thought process" step and the "open in editor" link.
    natural_language_only = bool(config.get("configurable", {}).get("natural_language_only"))

    try:
        async for event, chunk in run_graph.astream(inputs, stream_mode=["messages", "updates"], config=config):
            if event == "updates":
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
                msg, _metadata = chunk
                content = getattr(msg, "content", "") if msg else ""
                # A message making a tool call is shown as its tool step, so its text is
                # not streamed: gpt-oss copies the call's JSON arguments into ``content``,
                # also for calls whose arguments failed to parse (invalid_tool_calls).
                has_tool_calls = bool(
                    getattr(msg, "tool_calls", None)
                    or getattr(msg, "tool_call_chunks", None)
                    or getattr(msg, "invalid_tool_calls", None)
                )
                if has_tool_calls and getattr(msg, "type", "") != "tool":
                    continue
                # Tool results are not model output and pass through untouched. Holding
                # back incomplete tags also keeps a lone "</think>" out of the UI, where
                # it would open an empty "Thought process" step.
                if isinstance(content, str) and content and getattr(msg, "type", "") != "tool":
                    think_buffer += content
                    visible = strip_think_stream(think_buffer)
                    if natural_language_only:
                        visible = strip_sparql_stream(visible)
                    if len(visible) <= emitted_len:
                        # Everything new is reasoning or an incomplete tag — hold back.
                        continue
                    msg.content = visible[emitted_len:]
                    emitted_len = len(visible)

            chunk_dict = convert_chunk_to_dict({"event": event, "data": chunk})
            # The UI renders assistant text only from "AIMessageChunk". A model with
            # streaming disabled (gpt-oss with tools bound) sends its answer as a single
            # "ai" message, so relabel it to render the same way.
            if event == "messages":
                data = chunk_dict.get("data")
                if isinstance(data, list) and data and isinstance(data[0], dict) and data[0].get("type") == "ai":
                    data[0]["type"] = "AIMessageChunk"
            yield f"data: {json.dumps(chunk_dict)}\n\n"
            await asyncio.sleep(0)
    except Exception as exc:
        # Keep the chat usable: log the traceback and answer with the actual reason
        # rather than letting the stream break.
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


def tool_capable_models() -> list[str]:
    """Models usable in MCP tools mode: everything available minus the known-incapable ones."""
    models = settings.available_llm_models or [settings.default_llm_model]
    return [m for m in models if m not in settings.tool_incapable_models]


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

    # Natural-language mode runs on the MCP tools agent.
    if chat_request.natural_language_only:
        chat_request.use_tools = True

    question: str = chat_request.messages[-1].content if chat_request.messages else ""
    question_logger.info(f"User question: {question}")
    if not question:
        raise ValueError("No question provided")

    # MCP tools mode needs tool calling, which some GPUStack deployments (e.g. qwen3-vl)
    # reject. Say so up front rather than failing mid-stream.
    tool_capable = tool_capable_models()
    if chat_request.use_tools and chat_request.model in settings.tool_incapable_models:
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

    # Pass session_id via metadata for Langfuse to properly group multi-turn conversations
    # https://langfuse.com/docs/integrations/langchain/tracing#trace-attributes
    langfuse_metadata = {}
    if chat_request.session_id:
        langfuse_metadata["langfuse_session_id"] = chat_request.session_id

    # Clamp the user's limits so a huge value cannot run away.
    max_try = max(1, min(chat_request.max_try_fix_sparql, 20))
    # MCP tools mode: tool-call rounds allowed before the model must answer.
    max_tool_iterations = max(1, min(chat_request.max_tool_iterations, 30))
    # A fix attempt or a tool round takes ~2 graph steps. Size the recursion limit for
    # whichever mode runs, never below LangGraph's default of 25.
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

    # The MCP tool-calling agent, or the retrieval + validation pipeline.
    run_graph = get_graph(chat_request.use_tools)

    if chat_request.stream:
        return StreamingResponse(
            stream_response(inputs, config, run_graph),
            media_type="text/event-stream",
        )

    response = await run_graph.ainvoke(inputs, config=config)
    # Convert LangChain message objects to dicts for JSON serialization
    response_dict = convert_chunk_to_dict(response)
    if chat_request.natural_language_only:
        hide_sparql_in_answers(response_dict)
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
    with open(filename, "a", encoding="utf-8") as f:
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
    """Return the models offered in the chat UI's picker.

    That is ``settings.available_llm_models``, or ``[settings.default_llm_model]`` when
    it is empty. The provider is never asked, so GPUStack's embedding and other non-chat
    models stay hidden.
    """
    if settings.chat_api_key:
        auth_header = request.headers.get("Authorization", "")
        token = auth_header.split(" ", 1)[1] if auth_header.startswith("Bearer ") else ""
        if token != settings.chat_api_key:
            return JSONResponse(status_code=401, content={"error": "Unauthorized"})

    models = settings.available_llm_models or [settings.default_llm_model]
    # Expose which models can be used in MCP tools mode so the UI can guide the user.
    tool_capable = tool_capable_models()
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
                questions.add(match.group(2))
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
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "api_key": settings.chat_api_key,
            "chat_endpoint": "/chat",
            "feedback_endpoint": "/feedback",
            # Saved chats need a logged-in owner; without auth the sidebar stays hidden.
            "history_endpoint": "/conversations" if settings.auth_enabled else "",
            "examples": ",".join(settings.example_questions),
            "auth_enabled": settings.auth_enabled,
            # Shows the header link to /admin — curators need it too.
            "can_manage": can_manage(_user),
        },
    )
