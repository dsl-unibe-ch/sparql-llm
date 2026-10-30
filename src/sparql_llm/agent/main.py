"""API to deploy the SPARQL-LLM agent service from LangGraph."""

import asyncio
import contextlib
import html
import json
import logging
import os
import pathlib
import re
import secrets
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from datetime import datetime
from typing import Any
from urllib.parse import quote_plus, urlsplit

import httpx
import openai
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from langchain_core.runnables import RunnableConfig
from langfuse.langchain import CallbackHandler
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from sparql_llm.agent.graph import get_graph, graph
from sparql_llm.agent.request_project import ProjectRefused, allowed_datasets, resolve_request_project
from sparql_llm.agent.roles import (
    ADMIN,
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
from markupsafe import Markup

from sparql_llm.config import env_pins, runtime_settings_path, settings, write_runtime_settings
from sparql_llm.datasets import (
    DatasetError,
    add_dataset,
    check_removable,
    dataset_label,
    dataset_state,
    list_dataset_ids,
    remove_dataset,
    set_published,
)
from sparql_llm.indexing.drift import load_fingerprint
from sparql_llm.indexing.rebuild import recover_after_restart, stop_rebuilds
from sparql_llm.mcp_server import McpDispatcher
from sparql_llm.migration import migrate_configured
from sparql_llm.project import Project, get_projects
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

# One MCP app per dataset, made on first use: /mcp/<id>, and bare /mcp for the first dataset.
mcp_dispatcher = McpDispatcher()

# Login, the users database and the admin pages exist only when auth is enabled.
if settings.auth_enabled:
    from fastapi_users.db import SQLAlchemyUserDatabase
    from fastapi_users.password import PasswordHelper
    from sqlalchemy import select

    from sparql_llm.agent.auth import (
        User,
        UserLLMConfig,
        UserManager,
        async_session_maker,
        auth_backend,
        check_auth_secret,
        cookie_transport,
        create_db_and_tables,
        current_active_user,
        engine,
        fastapi_users,
        get_async_session,
        get_jwt_strategy,
        migrate_conversation_project,
    )
    from sparql_llm.agent.conversations import delete_conversations_of
    from sparql_llm.agent.conversations import router as conversations_router
    from sparql_llm.agent.crypto import decrypt_api_key, encrypt_api_key, mask_key
    from sparql_llm.agent.providers import PROVIDERS, validate_and_list_models
    from sparql_llm.indexing.drift import check_drift
    from sparql_llm.indexing.preflight import run_checks
    from sparql_llm.indexing.rebuild import lock_file, read_job, start_rebuild
    from sparql_llm.project_files import (
        MAX_UPLOAD_BYTES,
        ProjectFileError,
        file_path,
        list_files,
        restore_previous,
        save_upload,
        validate_upload,
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
    migrate_configured()
    # One worker: no rebuild runs yet, so a lock or "running" job on disk was left by a killed process.
    recover_after_restart()
    if settings.auth_enabled:
        check_auth_secret()
        pathlib.Path(settings.auth_db_path).parent.mkdir(parents=True, exist_ok=True)
        await create_db_and_tables()
        # Chats saved before there were several datasets belong to the first one.
        dataset_ids = list_dataset_ids()
        if dataset_ids:
            await migrate_conversation_project(engine, dataset_ids[0])
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
    try:
        async with mcp_dispatcher.running():
            yield
    finally:
        # A rebuild would otherwise be cut off when the process ends.
        await run_in_threadpool(stop_rebuilds)


def _api_description() -> str:
    """The OpenAPI description (F18). It names no dataset: /docs is public, unpublished datasets are not."""
    return f"{settings.app_name}: a natural-language interface to SPARQL endpoints. Pick a dataset with the `project` field of /chat."


app = FastAPI(
    title=settings.app_name,
    description=_api_description(),
    version="1.0.0",
    lifespan=lifespan,
)

app.mount("/mcp", mcp_dispatcher, name="mcp")


#: A local dev page (any port) may call the API, like the public site itself.
LOCAL_ORIGIN = re.compile(r"http://(localhost|127\.0\.0\.1)(:\d+)?")


def trusted_origin(origin: str, host: str) -> bool:
    """Whether a browser page at ``origin`` may act with the login cookie on a request to ``host``.

    Every *.unibe.ch host is the same site for SameSite=Lax: only this site itself, its
    public HTTPS name and a local dev server are trusted, not its siblings.
    """
    parts = urlsplit(origin)
    if not parts.scheme or not parts.netloc:
        return False
    if parts.netloc == host:
        return True
    if settings.app_public_host and origin == f"https://{settings.app_public_host}":
        return True
    return bool(LOCAL_ORIGIN.fullmatch(origin))


@app.middleware("http")
async def refuse_cross_site_cookie_writes(request: Request, call_next: Any) -> Any:
    """A change made with the login cookie must come from a trusted page (CSRF)."""
    if (
        settings.auth_enabled
        and request.method in ("POST", "PUT", "PATCH", "DELETE")
        and cookie_transport.cookie_name in request.cookies
    ):
        origin = request.headers.get("origin")
        if origin is None and request.headers.get("referer"):
            referer = urlsplit(request.headers["referer"])
            origin = f"{referer.scheme}://{referer.netloc}"
        # No Origin and no Referer: not a browser page (curl, scripts), which cookies do not reach unasked.
        if origin is not None and not trusted_origin(origin, request.headers.get("host", "")):
            return JSONResponse({"detail": "This request did not come from this site."}, status_code=403)
    return await call_next(request)


app.add_middleware(
    CORSMiddleware,
    allow_origins=[f"https://{settings.app_public_host}"] if settings.app_public_host else [],
    allow_origin_regex=LOCAL_ORIGIN.pattern,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def intro_html(text: str) -> Markup:
    """The app intro as HTML: escaped text with **bold** and [links](https://…) only."""
    escaped = html.escape(text)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2" target="_blank">\1</a>', escaped)
    return Markup(escaped)


def project_stats(projects: "list[Project] | None" = None) -> list[dict[str, Any]]:
    """Entity counts per type from each project's last fingerprint; empty before the first rebuild."""
    stats = []
    for project in get_projects() if projects is None else projects:
        fingerprint = load_fingerprint(project)
        if not fingerprint or not project.entities:
            continue
        for type_name, entity_type in project.entities.types.items():
            if entity_type.class_iri in fingerprint.classes:
                stats.append({"label": type_name, "count": fingerprint.classes[entity_type.class_iri]})
    return stats


def site_context(request: Request) -> dict[str, Any]:
    """Values every page shows: this instance's name, intro, endpoints and counts.

    On the chat page they are the selected dataset's only (``request.state.dataset``):
    another dataset's intro, endpoint or counts must never appear beside it.
    """
    dataset: Project | None = getattr(request.state, "dataset", None)
    # The chat page with no dataset the user may use shows none: never the served ones.
    chat_page = getattr(request.state, "chat_page", False)
    intro = dataset.intro if dataset is not None else ("" if chat_page else settings.app_intro)
    if dataset is not None:
        endpoints = [{"label": dataset.label, "endpoint_url": dataset.endpoint_url}]
    elif chat_page:
        endpoints = []
    else:
        endpoints = [{"label": e.get("label"), "endpoint_url": e["endpoint_url"]} for e in settings.endpoints]
    return {
        "app_name": settings.app_name,
        "app_intro": intro_html(intro),
        "app_description": re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", intro).replace("**", "")
        or f"Natural-language interface to {settings.app_name}.",
        "app_homepage_url": (dataset.homepage_url if dataset is not None else None) or settings.app_homepage_url,
        "endpoints": endpoints,
        "stats": project_stats([dataset] if dataset is not None else ([] if chat_page else None)),
    }


templates = Jinja2Templates(directory="src/sparql_llm/agent/webapp", context_processors=[site_context])

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

    #: How the admin picker names each dataset state.
    STATE_LABELS = {"published": "published", "ready": "ready, not published", "new": "new"}

    @app.get("/admin", response_class=HTMLResponse, include_in_schema=False)
    async def admin_page(
        request: Request,
        dataset: str = "",
        flash_success: str = "",
        flash_error: str = "",
        user: "User" = Depends(current_active_user),
        session: "AsyncSession" = Depends(get_async_session),
    ) -> HTMLResponse:
        """Admin panel — datasets, their files, index status and rebuild, and users. Admins and curators.

        Every dataset card works on the dataset picked with ``?dataset=`` (the first by
        default). What each row offers (remove, change role) is decided here by the rules
        in ``roles`` rather than in the template, so the page and the endpoints that
        enforce the same rules cannot disagree.
        """
        if not can_manage(user):
            return RedirectResponse("/", status_code=302)
        result = await session.execute(select(User).order_by(User.email))
        users = result.scalars().all()
        dataset_ids = list_dataset_ids()
        selected = dataset if dataset in dataset_ids else (dataset_ids[0] if dataset_ids else "")
        datasets = [
            {"id": pid, "label": dataset_label(pid), "state": dataset_state(pid), "state_label": STATE_LABELS[dataset_state(pid)]}
            for pid in dataset_ids
        ]
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
                "datasets": datasets,
                "dataset": next((d for d in datasets if d["id"] == selected), None),
                "is_admin": role_of(user) == ADMIN,
            },
        )

    @app.post("/admin/datasets", include_in_schema=False)
    async def admin_add_dataset(
        request: Request,
        user: "User" = Depends(current_active_user),
    ) -> JSONResponse:
        """A new, empty dataset (not published), ready for its three files. Admins only."""
        if role_of(user) != ADMIN:
            return JSONResponse({"error": "Only admins can add a dataset."}, status_code=403)
        if request.headers.get("content-type", "").startswith("application/json"):
            body = await request.json()
        else:
            body = dict(await request.form())
        try:
            add_dataset(str(body.get("id", "")).strip(), str(body.get("label", "")))
        except DatasetError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
        return JSONResponse({"ok": True, "dataset": str(body.get("id", "")).strip()})

    async def _set_published(dataset_id: str, published: bool, user: Any) -> JSONResponse:
        if role_of(user) != ADMIN:
            return JSONResponse({"error": "Only admins can publish or unpublish a dataset."}, status_code=403)
        if dataset_id not in list_dataset_ids():
            return JSONResponse({"error": f"There is no dataset '{dataset_id}'."}, status_code=404)
        try:
            set_published(dataset_id, published)
        except DatasetError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        return JSONResponse({"ok": True, "state": dataset_state(dataset_id)})

    @app.post("/admin/datasets/{dataset_id}/remove", include_in_schema=False)
    async def admin_remove_dataset(
        dataset_id: str,
        request: Request,
        user: "User" = Depends(current_active_user),
    ) -> JSONResponse:
        """Remove an unpublished dataset: its index is deleted, its files archived. Admins only.

        The admin types the dataset id to confirm. Runs under the rebuild lock, so no
        rebuild of it can start (or be running) meanwhile.
        """
        from sparql_llm.indexing.rebuild import _acquire_lock, _release_lock, drop_index
        from sparql_llm.project import reload_projects

        if role_of(user) != ADMIN:
            return JSONResponse({"error": "Only admins can remove a dataset."}, status_code=403)
        if dataset_id not in list_dataset_ids():
            return JSONResponse({"error": f"There is no dataset '{dataset_id}'."}, status_code=404)
        body = await request.json()
        if str(body.get("confirm", "")).strip() != dataset_id:
            return JSONResponse({"error": f"Type the dataset id, {dataset_id}, to confirm."}, status_code=422)
        lock_token = _acquire_lock(dataset_id)
        if not lock_token:
            return JSONResponse({"error": "A rebuild is running; try again when it finishes."}, status_code=409)
        try:
            check_removable(dataset_id)
            # The index goes first, while the dataset is still listed: see drop_index.
            await run_in_threadpool(drop_index, dataset_id)
            archived = remove_dataset(dataset_id)
        except DatasetError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        except OSError as exc:
            # Every step can be repeated: a second Remove finishes what this one started.
            logger.exception("Removal of dataset %s cut short", dataset_id)
            return JSONResponse(
                {"error": f"Removal incomplete: its index is gone, but its files could not be archived ({exc}). "
                          "Fix the cause, then press Remove again."},
                status_code=500,
            )
        finally:
            _release_lock(lock_token)
        from sparql_llm.indexing.index_resources import endpoints_metadata

        reload_projects()
        endpoints_metadata.reload()
        logger.info("Dataset %s removed by %s; files archived in %s", dataset_id, getattr(user, "email", ""), archived)
        return JSONResponse({"ok": True, "dataset": list_dataset_ids()[0]})

    @app.post("/admin/datasets/{dataset_id}/publish", include_in_schema=False)
    async def admin_publish_dataset(dataset_id: str, user: "User" = Depends(current_active_user)) -> JSONResponse:
        """Make a rebuilt dataset visible to every user. Admins only."""
        return await _set_published(dataset_id, True, user)

    @app.post("/admin/datasets/{dataset_id}/unpublish", include_in_schema=False)
    async def admin_unpublish_dataset(dataset_id: str, user: "User" = Depends(current_active_user)) -> JSONResponse:
        """Take a dataset out of users' picker; nothing is deleted. Admins only."""
        return await _set_published(dataset_id, False, user)

    def _admin_dataset(dataset: str) -> str | None:
        """The dataset an admin route works on: the one named, or the first; None if there is no such dataset."""
        ids = list_dataset_ids()
        if not dataset:
            return ids[0] if ids else None
        return dataset if dataset in ids else None

    def _no_dataset(dataset: str) -> JSONResponse:
        return JSONResponse({"error": f"There is no dataset '{dataset}'."}, status_code=404)

    @app.get("/admin/index-status", include_in_schema=False)
    async def admin_index_status(
        dataset: str = "",
        user: "User" = Depends(current_active_user),
    ) -> JSONResponse:
        """Is this dataset's index still in step with its endpoint, and is a rebuild running?

        Fetched by the admin page after render rather than during it: the drift check makes
        three SPARQL queries and should never hold up the page.
        """
        if not can_manage(user):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        project_id = _admin_dataset(dataset)
        if project_id is None:
            return _no_dataset(dataset)
        job = read_job(project_id)
        # The page polls every few seconds during a rebuild and shows no drift meanwhile:
        # three endpoint queries per poll would only compete with the rebuild's own.
        drift = None if job.get("state") == "running" else await run_in_threadpool(check_drift, project_id)
        return JSONResponse({"dataset": project_id, "drift": drift, "job": job})

    @app.post("/admin/reindex", include_in_schema=False)
    async def admin_reindex(
        dataset: str = "",
        user: "User" = Depends(current_active_user),
    ) -> JSONResponse:
        """Rebuild one dataset's index from its endpoint. Admins and curators.

        Returns as soon as the job starts. The new index is built into a fresh collection
        and only swapped in when complete, so the assistant keeps answering from the current
        index throughout, and a failed rebuild changes nothing. Other datasets are untouched.
        """
        if not can_manage(user):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        project_id = _admin_dataset(dataset)
        if project_id is None:
            return _no_dataset(dataset)
        result = await run_in_threadpool(start_rebuild, project_id)
        if not result.get("started"):
            running = result.get("dataset") or "another dataset"
            return JSONResponse(
                {"error": f"A rebuild of {running} is already running; try again when it finishes.", **result},
                status_code=409,
            )
        return JSONResponse(result)

    @app.post("/admin/check", include_in_schema=False)
    async def admin_check(
        dataset: str = "",
        user: "User" = Depends(current_active_user),
    ) -> JSONResponse:
        """Run one dataset's preflight checks, without rebuilding anything."""
        if not can_manage(user):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        project_id = _admin_dataset(dataset)
        if project_id is None:
            return _no_dataset(dataset)
        if lock_file().exists():
            return JSONResponse({"error": "A rebuild is running; try again when it finishes."}, status_code=409)
        checks = await run_in_threadpool(run_checks, project_id)
        return JSONResponse({"dataset": project_id, "projects": {project_id: checks}})

    @app.get("/admin/project-files", include_in_schema=False)
    async def admin_project_files(
        dataset: str = "",
        user: "User" = Depends(current_active_user),
    ) -> JSONResponse:
        """The curator files of one dataset (the first by default), for the Project files card."""
        if not can_manage(user):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        project_id = _admin_dataset(dataset)
        if project_id is None:
            return _no_dataset(dataset)
        return JSONResponse({"projects": {project_id: list_files(project_id)}})

    @app.get("/admin/project-files/{project_id}/{name}", include_in_schema=False, response_model=None)
    async def admin_download_project_file(
        project_id: str,
        name: str,
        user: "User" = Depends(current_active_user),
    ) -> PlainTextResponse | JSONResponse:
        """Download the current version of a project file, to edit it."""
        if not can_manage(user):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            path = file_path(project_id, name)
        except ProjectFileError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        return PlainTextResponse(
            path.read_text(encoding="utf-8"),
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    @app.post("/admin/project-files/{project_id}/{name}", include_in_schema=False)
    async def admin_upload_project_file(
        project_id: str,
        name: str,
        file: UploadFile = File(...),
        user: "User" = Depends(current_active_user),
    ) -> JSONResponse:
        """Replace a project file after validating it. Takes effect at the next Rebuild."""
        if not can_manage(user):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        if lock_file().exists():
            return JSONResponse({"error": "A rebuild is running; try again when it finishes."}, status_code=409)
        content = await file.read(MAX_UPLOAD_BYTES + 1)
        errors = validate_upload(project_id, name, content)
        if errors:
            return JSONResponse({"errors": errors}, status_code=422)
        save_upload(project_id, name, content, getattr(user, "email", ""))
        return JSONResponse({"ok": True, "files": list_files(project_id)})

    @app.post("/admin/project-files/{project_id}/{name}/undo", include_in_schema=False)
    async def admin_undo_project_file(
        project_id: str,
        name: str,
        user: "User" = Depends(current_active_user),
    ) -> JSONResponse:
        """Put back the previous version of a project file."""
        if not can_manage(user):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        if lock_file().exists():
            return JSONResponse({"error": "A rebuild is running; try again when it finishes."}, status_code=409)
        try:
            restore_previous(project_id, name)
        except ProjectFileError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        return JSONResponse({"ok": True, "files": list_files(project_id)})

    @app.get("/admin/settings", include_in_schema=False)
    async def admin_get_settings(
        user: "User" = Depends(current_active_user),
    ) -> JSONResponse:
        """Return the mutable runtime settings that admins and curators can change.

        Currently exposes:
        - ``default_number_of_retrieved_docs`` (top-k for RAG retrieval)

        Returns:
            JSON object with the current values.
        """
        if not can_manage(user):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        return JSONResponse({
            "default_number_of_retrieved_docs": settings.default_number_of_retrieved_docs,
            "pinned": {"default_number_of_retrieved_docs": env_pins("default_number_of_retrieved_docs")},
        })

    class _SettingsUpdate(BaseModel):
        """Payload accepted by ``POST /admin/settings``."""
        default_number_of_retrieved_docs: int | None = None

    @app.post("/admin/settings", include_in_schema=False)
    async def admin_update_settings(
        body: _SettingsUpdate,
        user: "User" = Depends(current_active_user),
    ) -> JSONResponse:
        """Update mutable runtime settings and persist them to disk.

        Changes take effect immediately (no restart required) because the
        in-memory ``settings`` singleton is mutated directly. The value is
        also written to ``settings.runtime.json`` in the data dir so it survives restarts.

        Args:
            body: JSON object with the fields to update.

        Returns:
            JSON object echoing the new values.
        """
        if not can_manage(user):
            return JSONResponse({"error": "forbidden"}, status_code=403)

        # Validate and apply
        if body.default_number_of_retrieved_docs is not None:
            value = body.default_number_of_retrieved_docs
            if not 1 <= value <= 100:
                return JSONResponse(
                    {"error": "default_number_of_retrieved_docs must be between 1 and 100."},
                    status_code=422,
                )
            try:
                write_runtime_settings(runtime_settings_path(settings), {"default_number_of_retrieved_docs": value})
            except OSError as exc:
                logger.warning(f"Could not save runtime settings: {exc}")
                return JSONResponse({"error": f"Could not save the setting: {exc}"}, status_code=500)
            if not env_pins("default_number_of_retrieved_docs"):
                settings.default_number_of_retrieved_docs = value

        return JSONResponse({
            "default_number_of_retrieved_docs": settings.default_number_of_retrieved_docs,
            "pinned": {"default_number_of_retrieved_docs": env_pins("default_number_of_retrieved_docs")},
        })

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

    # ── BYOK: per-user LLM provider configuration ─────────────────────────

    class LLMConfigRequest(BaseModel):
        """Payload for ``PUT /llm-config``."""
        provider: str            # "gpustack" | "openai" | "custom"
        api_key: str             # plain text — encrypted before storage
        base_url: str = ""       # empty → use provider default

    class LLMConfigResponse(BaseModel):
        provider: str
        base_url: str
        has_key: bool
        masked_key: str          # "sk-…****1234" or ""

    @app.get("/llm-config", response_model=LLMConfigResponse)
    async def get_llm_config(
        user: "User" = Depends(current_active_user),
        session: "AsyncSession" = Depends(get_async_session),
    ) -> JSONResponse:
        """Return the current user's LLM provider config (key masked)."""
        from sqlalchemy.ext.asyncio import AsyncSession as _AS  # noqa: F811

        cfg = await session.get(UserLLMConfig, user.id)
        if cfg is None:
            return JSONResponse(content={
                "provider": "",
                "base_url": "",
                "has_key": False,
                "masked_key": "",
                "providers": {k: {"label": v.label, "default_base_url": v.default_base_url, "help_text": v.help_text} for k, v in PROVIDERS.items()},
            })
        try:
            plain_key = decrypt_api_key(cfg.encrypted_api_key, settings.auth_secret)
            masked = mask_key(plain_key)
        except Exception:
            masked = "(decryption error)"
        return JSONResponse(content={
            "provider": cfg.provider,
            "base_url": cfg.base_url,
            "has_key": True,
            "masked_key": masked,
            "providers": {k: {"label": v.label, "default_base_url": v.default_base_url, "help_text": v.help_text} for k, v in PROVIDERS.items()},
        })

    @app.put("/llm-config")
    async def set_llm_config(
        body: LLMConfigRequest,
        user: "User" = Depends(current_active_user),
        session: "AsyncSession" = Depends(get_async_session),
    ) -> JSONResponse:
        """Set or update the user's LLM provider credentials.

        Validates the key by calling ``GET /v1/models`` on the provider before
        storing it.  Returns the list of available models on success.
        """
        from fastapi_users_db_sqlalchemy.generics import now_utc as _now_utc
        from sqlalchemy.ext.asyncio import AsyncSession as _AS  # noqa: F811

        if body.provider not in PROVIDERS:
            return JSONResponse(
                status_code=422,
                content={"error": f"Unknown provider '{body.provider}'. Choose from: {', '.join(PROVIDERS)}."},
            )
        if not body.api_key.strip():
            return JSONResponse(status_code=422, content={"error": "API key must not be empty."})

        provider_info = PROVIDERS[body.provider]
        effective_url = body.base_url.strip() or provider_info.default_base_url
        if not effective_url:
            return JSONResponse(
                status_code=422,
                content={"error": "Base URL is required for the 'custom' provider."},
            )

        # Validate the key by fetching models
        try:
            models = await validate_and_list_models(effective_url, body.api_key, body.provider)
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            return JSONResponse(
                status_code=401,
                content={"error": f"Could not validate API key against {effective_url}. {reason}"},
            )

        # Encrypt and store
        encrypted = encrypt_api_key(body.api_key, settings.auth_secret)
        cfg = await session.get(UserLLMConfig, user.id)
        if cfg is None:
            cfg = UserLLMConfig(user_id=user.id)
            session.add(cfg)
        cfg.provider = body.provider
        cfg.encrypted_api_key = encrypted
        cfg.base_url = effective_url
        cfg.updated_at = _now_utc()
        await session.commit()

        return JSONResponse(content={
            "provider": body.provider,
            "base_url": effective_url,
            "has_key": True,
            "masked_key": mask_key(body.api_key),
            "models": models,
        })

    @app.delete("/llm-config", status_code=204)
    async def delete_llm_config(
        user: "User" = Depends(current_active_user),
        session: "AsyncSession" = Depends(get_async_session),
    ) -> JSONResponse:
        """Remove the user's stored LLM credentials."""
        from sqlalchemy.ext.asyncio import AsyncSession as _AS  # noqa: F811

        cfg = await session.get(UserLLMConfig, user.id)
        if cfg is not None:
            await session.delete(cfg)
            await session.commit()
        return JSONResponse(status_code=200, content={"status": "deleted"})

    @app.get("/llm-config/models")
    async def get_llm_models(
        user: "User" = Depends(current_active_user),
        session: "AsyncSession" = Depends(get_async_session),
    ) -> JSONResponse:
        """Fetch the model list from the user's configured provider."""
        from sqlalchemy.ext.asyncio import AsyncSession as _AS  # noqa: F811

        cfg = await session.get(UserLLMConfig, user.id)
        if cfg is None or not cfg.encrypted_api_key:
            return JSONResponse(content={"models": [], "needs_setup": True})
        try:
            plain_key = decrypt_api_key(cfg.encrypted_api_key, settings.auth_secret)
            models = await validate_and_list_models(cfg.base_url, plain_key, cfg.provider)
        except Exception as exc:
            return JSONResponse(
                status_code=401,
                content={"error": f"Failed to fetch models: {type(exc).__name__}: {exc}", "needs_setup": True},
            )
        return JSONResponse(content={"models": models, "provider": cfg.provider, "needs_setup": False})

    @app.get("/api-settings", response_class=HTMLResponse, include_in_schema=False)
    async def api_settings_page(
        request: Request,
        user: "User" = Depends(current_active_user),
    ) -> HTMLResponse:
        """Render the API settings page."""
        return templates.TemplateResponse(
            request,
            "api-settings.html",
            {"current_user": user},
        )

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
# The user database only exists with auth enabled; routes use the session only in that case.
db_session = get_async_session if settings.auth_enabled else _noop


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
    #: The selected dataset's id; empty = the first one the user may ask.
    project: str = ""


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
                {"content": f"⚠️ {chat_error_message(exc)}{hint}", "type": "AIMessageChunk"},
                {"langgraph_node": "call_model"},
            ],
        }
        yield f"data: {json.dumps(error_msg)}\n\n"
        await asyncio.sleep(0)
    yield "data: [DONE]"


def chat_error_message(exc: Exception) -> str:
    """What the user reads when answering failed: plain words, one line (F13)."""
    if isinstance(exc, openai.APIConnectionError | httpx.TimeoutException | TimeoutError):
        # APITimeoutError is an APIConnectionError: the model server did not answer.
        return "The language model did not answer in time. Please try again, or pick another model."
    reason = (str(exc).strip().splitlines() or [type(exc).__name__])[0].rstrip(".")
    return f"Something went wrong while answering: {reason}."


NO_LLM_MESSAGE = (
    "No language model is set up for your account yet. "
    "Add your API key and endpoint in [Settings](/api-settings), then pick a model."
)
NO_MODEL_MESSAGE = "Pick a model first: the list in Settings shows what your provider offers."


def tool_capable_models() -> list[str]:
    """Models usable in MCP tools mode: everything available minus the known-incapable ones."""
    models = settings.available_llm_models or [settings.default_llm_model]
    return [m for m in models if m and m not in settings.tool_incapable_models]


# FastAPI does not support Union in response model (even if it says otherwise in docs)
# so we need to disable response_model for this endpoint
@app.post("/chat", response_model=None)
async def chat(
    request: Request,
    _user: Any = Depends(require_user),
    session: "AsyncSession" = Depends(db_session),
) -> StreamingResponse | JSONResponse:
    """Chat with the assistant main endpoint."""
    chat_request = ChatCompletionRequest(**await request.json())

    # Natural-language mode runs on the MCP tools agent.
    if chat_request.natural_language_only:
        chat_request.use_tools = True

    question: str = chat_request.messages[-1].content if chat_request.messages else ""
    question_logger.info(f"User question: {question}")
    if not question:
        raise ValueError("No question provided")

    # The dataset is the user's selection, checked here; nothing downstream may change it.
    refusal = ""
    try:
        project = resolve_request_project(chat_request.project, _user)
    except ProjectRefused as e:
        refusal = str(e)

    # MCP tools mode needs tool calling, which some GPUStack deployments (e.g. qwen3-vl)
    # reject. Say so up front rather than failing mid-stream.
    if not refusal and chat_request.use_tools and chat_request.model in settings.tool_incapable_models:
        selected_name = chat_request.model.split("/", 1)[-1]
        refusal = (
            f"⚠️ The model **{selected_name}** does not support tool calling, so it can't be used in "
            f"**MCP tools** mode.\n\nEither turn MCP tools off, or pick a different tool-capable model."
        )

    # ── Fetch user's LLM credentials ──
    llm_api_key = ""
    llm_base_url = ""
    llm_provider = "gpustack"

    if settings.auth_enabled and _user:
        user_llm = await session.get(UserLLMConfig, _user.id)
        if user_llm:
            try:
                llm_api_key = decrypt_api_key(user_llm.encrypted_api_key, settings.auth_secret)
                llm_base_url = user_llm.base_url
                llm_provider = user_llm.provider
            except Exception as exc:
                logger.error("Failed to decrypt API key for user %s: %s", _user.id, exc)

    if not refusal:
        if not llm_api_key and not os.getenv("OPENAI_API_KEY"):
            refusal = NO_LLM_MESSAGE
        elif "/" not in chat_request.model:
            refusal = NO_MODEL_MESSAGE

    if refusal:
        msg = refusal

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
            "llm_api_key": llm_api_key,
            "llm_base_url": llm_base_url,
            "llm_provider": llm_provider,
            "temperature": chat_request.temperature,
            "max_tokens": chat_request.max_tokens,
            "validate_output": chat_request.validate_output,
            "enable_sparql_execution": chat_request.enable_sparql_execution,
            "use_tools": chat_request.use_tools,
            "max_try_fix_sparql": max_try,
            "max_tool_iterations": max_tool_iterations,
            "natural_language_only": chat_request.natural_language_only,
            "project": project.id,
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

    try:
        response = await run_graph.ainvoke(inputs, config=config)
    except Exception as exc:
        error_logger.exception("Chat failed (model=%s): %s", chat_request.model, exc)
        return JSONResponse(content={"messages": [{"role": "assistant", "content": chat_error_message(exc)}]})
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
    session: "AsyncSession" = Depends(db_session),
) -> JSONResponse:
    """Return the models offered in the chat UI's picker.

    Queries the user's configured provider for available models. If no key
    is configured, returns `needs_setup: True`.
    """
    if not settings.auth_enabled:
        models = [m for m in (settings.available_llm_models or [settings.default_llm_model]) if m]
        return JSONResponse(content={
            "models": models,
            "default": settings.default_llm_model,
            "tool_capable_models": [m for m in models if m not in settings.tool_incapable_models],
        })

    cfg = await session.get(UserLLMConfig, _user.id)
    if cfg is None or not cfg.encrypted_api_key:
        if os.getenv("OPENAI_API_KEY"):
            # Instance-level key: chat works without a personal key, so offer the instance models.
            models = [m for m in (settings.available_llm_models or [settings.default_llm_model]) if m]
            return JSONResponse(content={
                "models": models,
                "default": settings.default_llm_model,
                "tool_capable_models": [m for m in models if m not in settings.tool_incapable_models],
                "needs_setup": False,
            })
        return JSONResponse(content={"models": [], "needs_setup": True})

    try:
        plain_key = decrypt_api_key(cfg.encrypted_api_key, settings.auth_secret)
        models = await validate_and_list_models(cfg.base_url, plain_key, cfg.provider)
    except Exception as exc:
        logger.error("Failed to fetch models for user %s: %s", _user.id, exc)
        return JSONResponse(
            status_code=401,
            content={"error": "Failed to fetch models. Your API key might be invalid.", "needs_setup": True},
        )

    # All models fetched from a chat model endpoint are considered tool-capable for now,
    # except those explicitly blacklisted in settings.
    tool_capable = [m for m in models if m not in settings.tool_incapable_models]
    
    return JSONResponse(content={
        "models": models,
        "default": models[0] if models else settings.default_llm_model,
        "tool_capable_models": tool_capable,
        "needs_setup": False,
    })


class LogsRequest(BaseModel):
    api_key: str


@app.post("/logs", response_model=list[str])
async def get_user_logs(logs_request: LogsRequest) -> JSONResponse:
    """Get the list of user questions from the logs file. Needs LOGS_API_KEY; closed without one."""
    # Every user's questions, across datasets: never open by default.
    if not settings.logs_api_key:
        return JSONResponse({"detail": "Log access is not configured."}, status_code=403)
    if not secrets.compare_digest(logs_request.api_key.encode(), settings.logs_api_key.encode()):
        return JSONResponse({"detail": "Invalid API key."}, status_code=401)
    questions = set()
    pattern = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) - User question: (.+)")
    with open(settings.logs_filepath, encoding="utf-8") as file:
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


@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    """Liveness: the process answers. No login, no checks."""
    return {"status": "ok"}


@app.get("/readyz", include_in_schema=False)
async def readyz() -> JSONResponse:
    """Readiness for the container healthcheck: local state only, no login, no network."""
    from sparql_llm.agent.health import readiness_checks

    failing = {name: reason for name, reason in (await run_in_threadpool(readiness_checks)).items() if reason}
    if failing:
        return JSONResponse({"status": "not ready", "failing": failing}, status_code=503)
    return JSONResponse({"status": "ready"})


#: Remembers the dataset a browser used last, so "/" opens on it.
LAST_DATASET_COOKIE = "last_dataset"


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def chat_ui(
    request: Request,
    dataset: str = "",
    chat: str = "",
    _user: Any = Depends(require_user),
    session: Any = Depends(db_session),
) -> HTMLResponse:
    """Render the chat UI for one dataset: ``?dataset=``, else a saved chat's, else the last used, else the first.

    A dataset the user may not use is never an error page: the first allowed one is shown.
    """
    allowed = allowed_datasets(_user)
    by_id = {p.id: p for p in allowed}
    wanted = [dataset, request.cookies.get(LAST_DATASET_COOKIE, "")]
    if chat and session is not None and _user is not None:
        # A link to a saved chat opens on that chat's dataset.
        from sparql_llm.agent.auth import Conversation

        conversation = await session.get(Conversation, chat)
        if conversation is not None and conversation.user_id == _user.id:
            wanted.insert(0, conversation.project)
    selected = next((by_id[w] for w in wanted if w in by_id), allowed[0] if allowed else None)
    request.state.dataset = selected
    request.state.chat_page = True
    response = templates.TemplateResponse(
        request,
        "index.html",
        {
            "api_key": settings.chat_api_key,
            "chat_endpoint": "/chat",
            "feedback_endpoint": "/feedback",
            # Saved chats need a logged-in owner; without auth the sidebar stays hidden. The
            # widget builds each chat's URL from this, so the dataset filter is added by the
            # page's fetch wrapper rather than here.
            "history_endpoint": "/conversations" if settings.auth_enabled and selected else "",
            "examples": ",".join(selected.example_questions) if selected else "",
            "auth_enabled": settings.auth_enabled,
            # Shows the header link to /admin — curators need it too.
            "can_manage": can_manage(_user),
            "datasets": [
                {"id": p.id, "label": p.label, "published": dataset_state(p.id) == "published"} for p in allowed
            ],
            "dataset": {"id": selected.id, "label": selected.label} if selected else None,
        },
    )
    if selected is not None:
        response.set_cookie(LAST_DATASET_COOKIE, selected.id, max_age=31536000, samesite="lax")
    return response
