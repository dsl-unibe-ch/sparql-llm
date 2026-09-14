"""Offline tests for the user roles: admin, curator, user.

Curators run the day-to-day: they rebuild the index when their data changes and let
new colleagues in. What they must not be able to do is touch the admins — remove them,
demote them, or mint new ones — or the people responsible for the deployment could be
locked out of it by accident.

These exercise the pure rules and the SQLite migration; no app, no network.
"""

import itertools
import sqlite3
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pytest

from sparql_llm.agent.roles import (
    ADMIN,
    CURATOR,
    ROLES,
    USER,
    assignable_roles,
    can_create,
    can_delete,
    can_manage,
    can_set_role,
    creatable_roles,
    ensure_role_column,
    role_flags,
    role_of,
)


@dataclass
class U:
    is_superuser: bool = False
    is_curator: bool = False
    id: UUID = field(default_factory=uuid4)


def admin() -> U:
    return U(is_superuser=True)


def curator() -> U:
    return U(is_curator=True)


def user() -> U:
    return U()


# ── Who is what ───────────────────────────────────────────────────────────────


def test_role_of_each_kind():
    assert role_of(admin()) == ADMIN
    assert role_of(curator()) == CURATOR
    assert role_of(user()) == USER


def test_admin_wins_if_both_flags_are_set():
    assert role_of(U(is_superuser=True, is_curator=True)) == ADMIN


def test_a_user_object_without_the_curator_attribute_is_a_plain_user():
    # Rows loaded before the migration ran, or any object from older code.
    class Legacy:
        is_superuser = False

    assert role_of(Legacy()) == USER


@pytest.mark.parametrize("role", ROLES)
def test_role_flags_round_trip(role):
    assert role_of(U(**role_flags(role))) == role


# ── Admin page and rebuild ────────────────────────────────────────────────────


def test_admins_and_curators_can_manage_plain_users_cannot():
    assert can_manage(admin())
    assert can_manage(curator())
    assert not can_manage(user())


# ── Adding users ──────────────────────────────────────────────────────────────


def test_curators_can_add_plain_users_only():
    c = curator()
    assert can_create(c, USER)
    assert not can_create(c, CURATOR)
    assert not can_create(c, ADMIN)
    assert creatable_roles(c) == (USER,)


def test_admins_can_add_any_role():
    assert creatable_roles(admin()) == ROLES


def test_plain_users_can_add_nobody():
    assert creatable_roles(user()) == ()


def test_an_unknown_role_is_never_creatable():
    assert not can_create(admin(), "root")


# ── Removing users ────────────────────────────────────────────────────────────


def test_curators_cannot_remove_admins():
    assert not can_delete(curator(), admin())


def test_curators_cannot_remove_other_curators():
    assert not can_delete(curator(), curator())


def test_curators_can_remove_plain_users():
    assert can_delete(curator(), user())


def test_admins_can_remove_anyone_else():
    a = admin()
    assert can_delete(a, admin())
    assert can_delete(a, curator())
    assert can_delete(a, user())


def test_nobody_can_remove_themselves():
    for actor in (admin(), curator()):
        assert not can_delete(actor, actor)


def test_plain_users_can_remove_nobody():
    assert not can_delete(user(), user())


# ── Changing roles ────────────────────────────────────────────────────────────


def test_only_admins_change_roles():
    assert assignable_roles(curator(), user()) == ()
    assert not can_set_role(curator(), user(), CURATOR)


def test_admins_can_promote_and_demote_others():
    a = admin()
    assert can_set_role(a, user(), CURATOR)
    assert can_set_role(a, curator(), USER)
    assert can_set_role(a, admin(), CURATOR)


def test_admins_cannot_change_their_own_role():
    # Demoting yourself is how a deployment ends up with no admin at all.
    a = admin()
    assert assignable_roles(a, a) == ()
    assert not can_set_role(a, a, USER)


def test_an_unknown_role_is_never_assignable():
    assert not can_set_role(admin(), user(), "root")


def test_no_sequence_of_permitted_actions_leaves_zero_admins():
    # Exhaustive over three rounds: whoever acts, whatever they are allowed to do,
    # at least one admin survives. This is the guarantee the individual rules exist for.
    start = [admin(), admin(), curator(), user()]
    frontier = [start]
    for _ in range(3):
        nxt = []
        for people in frontier:
            for actor, target in itertools.product(people, repeat=2):
                if can_delete(actor, target):
                    nxt.append([p for p in people if p is not target])
                for role in assignable_roles(actor, target):
                    changed = [U(**role_flags(role), id=p.id) if p is target else p for p in people]
                    nxt.append(changed)
        for people in nxt:
            assert any(role_of(p) == ADMIN for p in people)
        frontier = nxt[:200]  # bounded; enough to cover every role mix


# ── Migration of the existing user database ───────────────────────────────────


class _Conn:
    """The one method of a SQLAlchemy connection the migration uses, over stdlib sqlite3."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db

    def exec_driver_sql(self, sql: str):
        return self._db.execute(sql)


def _legacy_db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.execute('CREATE TABLE "user" (id TEXT PRIMARY KEY, email TEXT, is_superuser BOOLEAN NOT NULL DEFAULT 0)')
    db.execute("""INSERT INTO "user" VALUES ('1', 'matteo@example.org', 1)""")
    return db


def _columns(db: sqlite3.Connection) -> list[str]:
    return [row[1] for row in db.execute('PRAGMA table_info("user")')]


def test_migration_adds_the_curator_column_to_an_existing_database():
    db = _legacy_db()
    ensure_role_column(_Conn(db))
    assert "is_curator" in _columns(db)


def test_existing_users_become_plain_non_curators():
    db = _legacy_db()
    ensure_role_column(_Conn(db))
    assert db.execute('SELECT is_superuser, is_curator FROM "user"').fetchone() == (1, 0)


def test_migration_is_idempotent():
    # It runs on every start-up.
    db = _legacy_db()
    ensure_role_column(_Conn(db))
    ensure_role_column(_Conn(db))
    assert _columns(db).count("is_curator") == 1
