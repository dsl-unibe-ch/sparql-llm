"""Who may do what on the admin page: admins, curators and plain users.

Three roles, stored as two booleans on the user row — fastapi-users' own
``is_superuser`` for admins plus our ``is_curator`` — so existing admins keep working
and fastapi-users itself needs no changes.

- **Admin** — everything, including assigning roles.
- **Curator** — runs the day-to-day: rebuilds the index when the data or the example
  queries change, and adds and removes *plain* users. Cannot create, remove or re-role
  curators or admins.
- **User** — uses the chat.

Two rules keep at least one admin in existence under any sequence of permitted actions:
nobody can remove themselves or change their own role, and only an admin can act on an
admin — which always leaves that acting admin in place. ``tests/test_roles.py`` checks
this exhaustively rather than trusting the argument.

Pure functions over duck-typed user objects (anything with ``id``, ``is_superuser`` and
optionally ``is_curator``), so the rules are testable without the app, and the admin page
and the endpoints consult the same functions and cannot disagree.
"""

from __future__ import annotations

from typing import Any

ADMIN = "admin"
CURATOR = "curator"
USER = "user"

#: Least to most privileged — also the order the admin page offers them in.
ROLES: tuple[str, ...] = (USER, CURATOR, ADMIN)
ROLE_LABELS: dict[str, str] = {USER: "User", CURATOR: "Curator", ADMIN: "Admin"}


def role_of(user: Any) -> str:
    """The role a user row carries. Admin wins if both flags are somehow set."""
    if getattr(user, "is_superuser", False):
        return ADMIN
    if getattr(user, "is_curator", False):
        return CURATOR
    return USER


def role_flags(role: str) -> dict[str, bool]:
    """The column values that store ``role``."""
    return {"is_superuser": role == ADMIN, "is_curator": role == CURATOR}


def _same(a: Any, b: Any) -> bool:
    if a is b:
        return True
    a_id = getattr(a, "id", None)
    return a_id is not None and a_id == getattr(b, "id", None)


def can_manage(actor: Any) -> bool:
    """Open the admin page, check and rebuild the index, add users."""
    return role_of(actor) in (ADMIN, CURATOR)


def creatable_roles(actor: Any) -> tuple[str, ...]:
    """Roles ``actor`` may give to a new account."""
    role = role_of(actor)
    if role == ADMIN:
        return ROLES
    if role == CURATOR:
        return (USER,)
    return ()


def can_create(actor: Any, role: str) -> bool:
    return role in creatable_roles(actor)


def can_delete(actor: Any, target: Any) -> bool:
    """Admins remove anyone but themselves; curators remove plain users only."""
    if _same(actor, target):
        return False
    role = role_of(actor)
    if role == ADMIN:
        return True
    if role == CURATOR:
        return role_of(target) == USER
    return False


def assignable_roles(actor: Any, target: Any) -> tuple[str, ...]:
    """Roles ``actor`` may move ``target`` to. Admins only, and never on themselves."""
    if role_of(actor) != ADMIN or _same(actor, target):
        return ()
    return ROLES


def can_set_role(actor: Any, target: Any, role: str) -> bool:
    return role in assignable_roles(actor, target)


def ensure_role_column(conn: Any) -> None:
    """Add ``is_curator`` to a user table created before roles existed. Idempotent.

    ``create_all`` creates missing tables but never alters existing ones, so the live
    database on the server needs the column added explicitly; this runs on every
    start-up and does nothing once it is there. Existing users become plain
    non-curators. ``conn`` is anything with SQLAlchemy's ``exec_driver_sql`` — the sync
    connection ``run_sync`` hands over.
    """
    rows = conn.exec_driver_sql('PRAGMA table_info("user")').fetchall()
    columns = {row[1] for row in rows}
    if not columns or "is_curator" in columns:
        return
    conn.exec_driver_sql('ALTER TABLE "user" ADD COLUMN is_curator BOOLEAN NOT NULL DEFAULT 0')
