"""Behavior contracts for the read-only ``hermes handoffs`` CLI."""

from __future__ import annotations

import json
import sqlite3
import sys

import hermes_cli.main as main_mod
import hermes_cli.profiles as profiles_mod
import pytest


SAFE_FIELDS = {
    "carrier",
    "kind",
    "id",
    "profile",
    "producer_state",
    "delivery_state",
    "origin_session",
    "target_summary",
    "created_at",
    "updated_at",
    "attempts",
    "stale_reason",
    "payload_available",
    "terminal",
}


def _seed_state_db(home):
    home.mkdir(parents=True, exist_ok=True)
    db_path = home / "state.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE async_delegations (
            delegation_id TEXT PRIMARY KEY,
            origin_session TEXT NOT NULL,
            origin_ui_session_id TEXT NOT NULL DEFAULT '',
            parent_session_id TEXT,
            state TEXT NOT NULL,
            dispatched_at REAL NOT NULL,
            completed_at REAL,
            updated_at REAL NOT NULL,
            event_json TEXT,
            result_json TEXT,
            delivery_state TEXT NOT NULL DEFAULT 'pending',
            delivery_attempts INTEGER NOT NULL DEFAULT 0,
            delivered_at REAL,
            task_json TEXT,
            origin_session_id TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE delivery_obligations (
            obligation_id TEXT PRIMARY KEY,
            session_key TEXT NOT NULL,
            platform TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            thread_id TEXT,
            content TEXT NOT NULL,
            state TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            last_error TEXT,
            adapter_profile TEXT
        );
        """
    )
    conn.executemany(
        """INSERT INTO async_delegations
           (delegation_id, origin_session, parent_session_id, state,
            dispatched_at, completed_at, updated_at, event_json, result_json,
            delivery_state, delivery_attempts, task_json, origin_session_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                "deleg_running",
                "agent:main:telegram:dm:RAW-CHAT-ID",
                "20260827_parent_a1",
                "running",
                10.0,
                None,
                11.0,
                None,
                None,
                "pending",
                0,
                json.dumps({"goal": "DO NOT LEAK PROMPT secret-token"}),
                "",
            ),
            (
                "deleg_completed",
                "raw-api-session",
                "20260827_parent_b2",
                "completed",
                20.0,
                25.0,
                26.0,
                json.dumps({"summary": "DO NOT LEAK RESULT"}),
                json.dumps({"summary": "DO NOT LEAK RESULT"}),
                "pending",
                1,
                json.dumps({"context": "DO NOT LEAK CONTEXT"}),
                "",
            ),
        ],
    )
    conn.executemany(
        """INSERT INTO delivery_obligations
           (obligation_id, session_key, platform, chat_id, thread_id, content,
            state, attempts, created_at, updated_at, last_error, adapter_profile)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                "ob_attempting",
                "agent:main:discord:thread:RAW-CHANNEL:RAW-THREAD",
                "discord",
                "RAW-CHANNEL",
                "RAW-THREAD",
                "DO NOT LEAK FINAL RESPONSE secret-token",
                "attempting",
                1,
                30.0,
                31.0,
                None,
                "default",
            ),
            (
                "ob_delivered",
                "agent:main:slack:dm:RAW-USER",
                "slack",
                "RAW-USER",
                None,
                "DO NOT LEAK DELIVERED CONTENT",
                "delivered",
                1,
                40.0,
                41.0,
                None,
                "default",
            ),
        ],
    )
    conn.commit()
    conn.close()
    return db_path


def _run_main(monkeypatch, capsys, argv):
    monkeypatch.setattr(sys, "argv", ["hermes", *argv])
    main_mod.main()
    return capsys.readouterr()


def test_list_json_preserves_native_states_and_safe_fields(
    tmp_path, monkeypatch, capsys,
):
    home = tmp_path / ".hermes"
    _seed_state_db(home)
    monkeypatch.setenv("HERMES_HOME", str(home))

    captured = _run_main(monkeypatch, capsys, ["handoffs", "list", "--json"])
    payload = json.loads(captured.out)

    assert payload["warnings"] == []
    rows = {f"{row['carrier']}:{row['id']}": row for row in payload["handoffs"]}
    assert set(rows) == {
        "delegation:deleg_running",
        "delegation:deleg_completed",
        "delivery:ob_attempting",
        "delivery:ob_delivered",
    }
    assert rows["delegation:deleg_running"]["producer_state"] == "running"
    assert rows["delegation:deleg_running"]["delivery_state"] == "pending"
    assert rows["delegation:deleg_completed"]["producer_state"] == "completed"
    assert rows["delegation:deleg_completed"]["delivery_state"] == "pending"
    assert rows["delivery:ob_attempting"]["producer_state"] == "unknown"
    assert rows["delivery:ob_attempting"]["delivery_state"] == "attempting"
    assert rows["delivery:ob_delivered"]["delivery_state"] == "delivered"
    assert rows["delivery:ob_delivered"]["terminal"] is True
    assert all(set(row) == SAFE_FIELDS for row in rows.values())

    serialized = captured.out
    for forbidden in (
        "DO NOT LEAK",
        "secret-token",
        "RAW-CHAT-ID",
        "RAW-CHANNEL",
        "RAW-THREAD",
        "RAW-USER",
    ):
        assert forbidden not in serialized


def test_actionable_keeps_dropped_and_abandoned_but_hides_delivered(
    tmp_path, monkeypatch, capsys,
):
    home = tmp_path / ".hermes"
    db_path = _seed_state_db(home)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO async_delegations
           (delegation_id, origin_session, state, dispatched_at, updated_at,
            delivery_state, delivery_attempts)
           VALUES ('deleg_dropped', '', 'completed', 50, 51, 'dropped', 8)"""
    )
    conn.execute(
        """INSERT INTO delivery_obligations
           (obligation_id, session_key, platform, chat_id, content, state,
            attempts, created_at, updated_at)
           VALUES ('ob_abandoned', '', 'telegram', 'RAW-ID', 'secret body',
                   'abandoned', 3, 60, 61)"""
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("HERMES_HOME", str(home))

    captured = _run_main(
        monkeypatch,
        capsys,
        ["handoffs", "list", "--state", "actionable", "--json"],
    )
    rows = {
        f"{row['carrier']}:{row['id']}": row
        for row in json.loads(captured.out)["handoffs"]
    }

    assert "delivery:ob_delivered" not in rows
    assert rows["delegation:deleg_dropped"]["stale_reason"] == "delivery_dropped"
    assert rows["delegation:deleg_dropped"]["terminal"] is True
    assert rows["delivery:ob_abandoned"]["stale_reason"] == "delivery_abandoned"
    assert rows["delivery:ob_abandoned"]["terminal"] is True


def test_list_preserves_native_delegation_error_state(
    tmp_path, monkeypatch, capsys,
):
    home = tmp_path / ".hermes"
    db_path = _seed_state_db(home)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO async_delegations
           (delegation_id, origin_session, state, dispatched_at, updated_at,
            delivery_state, delivery_attempts)
           VALUES ('deleg_error', '', 'error', 50, 51, 'pending', 1)"""
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("HERMES_HOME", str(home))

    captured = _run_main(monkeypatch, capsys, ["handoffs", "list", "--json"])
    row = next(
        item
        for item in json.loads(captured.out)["handoffs"]
        if item["id"] == "deleg_error"
    )

    assert row["producer_state"] == "error"


def test_show_requires_carrier_qualified_id_and_returns_one_safe_row(
    tmp_path, monkeypatch, capsys,
):
    home = tmp_path / ".hermes"
    _seed_state_db(home)
    monkeypatch.setenv("HERMES_HOME", str(home))

    captured = _run_main(
        monkeypatch,
        capsys,
        ["handoffs", "show", "delegation:deleg_completed", "--json"],
    )
    payload = json.loads(captured.out)

    assert payload["warnings"] == []
    assert payload["handoff"]["carrier"] == "delegation"
    assert payload["handoff"]["id"] == "deleg_completed"
    assert set(payload["handoff"]) == SAFE_FIELDS


def test_all_profiles_keeps_healthy_rows_and_opens_state_stores_read_only(
    tmp_path, monkeypatch, capsys,
):
    root = tmp_path / ".hermes"
    _seed_state_db(root)
    worker = root / "profiles" / "worker"
    worker_db = _seed_state_db(worker)
    conn = sqlite3.connect(worker_db)
    conn.execute("UPDATE async_delegations SET delegation_id='worker_' || delegation_id")
    conn.execute("UPDATE delivery_obligations SET obligation_id='worker_' || obligation_id")
    conn.commit()
    conn.close()
    corrupt = root / "profiles" / "corrupt"
    corrupt.mkdir(parents=True)
    (corrupt / "state.db").write_bytes(b"not sqlite")
    (root / "profiles" / "missing").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))

    real_connect = sqlite3.connect
    opened = []

    def recording_connect(database, *args, **kwargs):
        opened.append((str(database), dict(kwargs)))
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", recording_connect)
    monkeypatch.setattr(
        profiles_mod,
        "list_profiles",
        lambda: (_ for _ in ()).throw(AssertionError("rich profile enumeration")),
    )
    captured = _run_main(
        monkeypatch,
        capsys,
        ["handoffs", "list", "--all-profiles", "--json"],
    )
    payload = json.loads(captured.out)

    assert {row["profile"] for row in payload["handoffs"]} == {"default", "worker"}
    assert any(warning.startswith("corrupt:") for warning in payload["warnings"])
    assert "missing: state store is missing" in payload["warnings"]
    state_opens = [entry for entry in opened if "state.db" in entry[0]]
    assert state_opens
    assert all("mode=ro" in database and kwargs.get("uri") is True for database, kwargs in state_opens)
    assert not (worker / "state.db-wal").exists()
    assert not (worker / "state.db-shm").exists()


@pytest.mark.skipif(
    sys.platform == "win32", reason="Symlinks require elevated privileges on Windows"
)
def test_all_profiles_rejects_symlinked_profile_directories(
    tmp_path, monkeypatch, capsys,
):
    root = tmp_path / ".hermes"
    _seed_state_db(root)
    outside = tmp_path / "outside"
    _seed_state_db(outside)
    profiles_root = root / "profiles"
    profiles_root.mkdir()
    (profiles_root / "outside-link").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("HERMES_HOME", str(root))

    captured = _run_main(
        monkeypatch,
        capsys,
        ["handoffs", "list", "--all-profiles", "--json"],
    )
    payload = json.loads(captured.out)

    assert {row["profile"] for row in payload["handoffs"]} == {"default"}
    assert "outside-link: profile directory is a symlink" in payload["warnings"]


@pytest.mark.skipif(
    sys.platform == "win32", reason="Symlinks require elevated privileges on Windows"
)
def test_all_profiles_rejects_symlinked_profiles_root(
    tmp_path, monkeypatch, capsys,
):
    root = tmp_path / ".hermes"
    _seed_state_db(root)
    outside_root = tmp_path / "outside-profiles"
    _seed_state_db(outside_root / "outside")
    (root / "profiles").symlink_to(outside_root, target_is_directory=True)
    monkeypatch.setenv("HERMES_HOME", str(root))

    captured = _run_main(
        monkeypatch,
        capsys,
        ["handoffs", "list", "--all-profiles", "--json"],
    )
    payload = json.loads(captured.out)

    assert {row["profile"] for row in payload["handoffs"]} == {"default"}
    assert "profiles: profile directory is a symlink" in payload["warnings"]


@pytest.mark.skipif(
    sys.platform == "win32", reason="Symlinks require elevated privileges on Windows"
)
def test_all_profiles_rejects_symlinked_state_store(
    tmp_path, monkeypatch, capsys,
):
    root = tmp_path / ".hermes"
    _seed_state_db(root)
    outside = tmp_path / "outside"
    outside_db = _seed_state_db(outside)
    linked = root / "profiles" / "linked"
    linked.mkdir(parents=True)
    (linked / "state.db").symlink_to(outside_db)
    monkeypatch.setenv("HERMES_HOME", str(root))

    captured = _run_main(
        monkeypatch, capsys, ["handoffs", "list", "--all-profiles", "--json"]
    )
    payload = json.loads(captured.out)

    assert {row["profile"] for row in payload["handoffs"]} == {"default"}
    assert "linked: state store is a symlink" in payload["warnings"]


def test_closed_wal_store_is_read_with_normal_sqlite_coordination(
    tmp_path, monkeypatch, capsys,
):
    home = tmp_path / ".hermes"
    db_path = _seed_state_db(home)
    conn = sqlite3.connect(db_path)
    assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    conn.close()
    monkeypatch.setenv("HERMES_HOME", str(home))

    captured = _run_main(monkeypatch, capsys, ["handoffs", "list", "--json"])

    assert len(json.loads(captured.out)["handoffs"]) == 4
    assert json.loads(captured.out)["warnings"] == []


def test_active_wal_store_includes_committed_uncheckpointed_rows(
    tmp_path, monkeypatch, capsys,
):
    home = tmp_path / ".hermes"
    db_path = _seed_state_db(home)
    writer = sqlite3.connect(db_path)
    assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute(
        """INSERT INTO async_delegations
           (delegation_id, origin_session, state, dispatched_at, updated_at,
            delivery_state, delivery_attempts)
           VALUES ('wal_pending', '', 'completed', 90, 91, 'pending', 0)"""
    )
    writer.commit()
    monkeypatch.setenv("HERMES_HOME", str(home))
    try:
        captured = _run_main(monkeypatch, capsys, ["handoffs", "list", "--json"])
    finally:
        writer.close()

    assert any(
        row["id"] == "wal_pending" for row in json.loads(captured.out)["handoffs"]
    )


def test_reader_connections_reject_sql_mutation_and_execute_only_read_queries(
    tmp_path, monkeypatch, capsys,
):
    home = tmp_path / ".hermes"
    _seed_state_db(home)
    monkeypatch.setenv("HERMES_HOME", str(home))
    real_connect = sqlite3.connect
    statements = []
    mutation_checks = []

    class ReadOnlyConnection:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, *args, **kwargs):
            statements.append(sql.strip())
            result = self.connection.execute(sql, *args, **kwargs)
            if sql.strip().upper() == "PRAGMA QUERY_ONLY=ON":
                with pytest.raises(sqlite3.OperationalError, match="readonly"):
                    self.connection.execute("CREATE TABLE forbidden_write (id INTEGER)")
                mutation_checks.append(True)
            return result

        def close(self):
            self.connection.close()

    def guarded_connect(database, *args, **kwargs):
        assert "mode=ro" in str(database)
        return ReadOnlyConnection(real_connect(database, *args, **kwargs))

    monkeypatch.setattr(sqlite3, "connect", guarded_connect)
    captured = _run_main(monkeypatch, capsys, ["handoffs", "list", "--json"])

    assert len(json.loads(captured.out)["handoffs"]) == 4
    assert mutation_checks == [True, True]
    assert all(
        sql.upper().startswith(("PRAGMA QUERY_ONLY", "PRAGMA TABLE_INFO", "SELECT"))
        for sql in statements
    )
    assert not any("CHECKPOINT" in sql.upper() for sql in statements)


def test_reader_open_failure_from_missing_sidecar_permission_warns_precisely(
    tmp_path, monkeypatch, capsys,
):
    home = tmp_path / ".hermes"
    db_path = _seed_state_db(home)
    conn = sqlite3.connect(db_path)
    assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    conn.close()
    monkeypatch.setenv("HERMES_HOME", str(home))

    def denied_connect(database, *args, **kwargs):
        if "mode=ro" in str(database):
            raise sqlite3.OperationalError("unable to create WAL coordination sidecars")
        return sqlite3.connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", denied_connect)
    captured = _run_main(monkeypatch, capsys, ["handoffs", "list", "--json"])
    payload = json.loads(captured.out)

    assert payload["handoffs"] == []
    assert payload["warnings"] == [
        "default: delegation store unavailable (OperationalError)",
        "default: delivery store unavailable (OperationalError)",
    ]


def test_legacy_store_rows_survive_with_unknown_states_and_warning(
    tmp_path, monkeypatch, capsys,
):
    root = tmp_path / ".hermes"
    _seed_state_db(root)
    legacy = root / "profiles" / "legacy"
    legacy.mkdir(parents=True)
    conn = sqlite3.connect(legacy / "state.db")
    conn.executescript(
        """
        CREATE TABLE async_delegations (
            delegation_id TEXT PRIMARY KEY
        );
        INSERT INTO async_delegations VALUES ('legacy_deleg');
        CREATE TABLE delivery_obligations (
            obligation_id TEXT PRIMARY KEY
        );
        INSERT INTO delivery_obligations VALUES ('legacy_delivery');
        """
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("HERMES_HOME", str(root))

    captured = _run_main(
        monkeypatch,
        capsys,
        ["handoffs", "list", "--all-profiles", "--json"],
    )
    payload = json.loads(captured.out)
    rows = {
        f"{row['carrier']}:{row['id']}": row
        for row in payload["handoffs"]
        if row["profile"] == "legacy"
    }

    assert rows["delegation:legacy_deleg"]["producer_state"] == "unknown"
    assert rows["delegation:legacy_deleg"]["delivery_state"] == "unknown"
    assert rows["delivery:legacy_delivery"]["producer_state"] == "unknown"
    assert rows["delivery:legacy_delivery"]["delivery_state"] == "unknown"
    assert any(warning.startswith("legacy:") for warning in payload["warnings"])


def test_malformed_scalar_metadata_degrades_to_safe_unknown_values(
    tmp_path, monkeypatch, capsys,
):
    home = tmp_path / ".hermes"
    db_path = _seed_state_db(home)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """UPDATE async_delegations
           SET state='invented-success', delivery_state='accepted',
               delivery_attempts='not-a-count', updated_at='not-a-timestamp'
           WHERE delegation_id='deleg_running'"""
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("HERMES_HOME", str(home))

    captured = _run_main(monkeypatch, capsys, ["handoffs", "list", "--json"])
    payload = json.loads(captured.out)
    row = next(
        item for item in payload["handoffs"] if item["id"] == "deleg_running"
    )

    assert row["producer_state"] == "unknown"
    assert row["delivery_state"] == "unknown"
    assert row["attempts"] == 0
    assert row["updated_at"] is None


def test_human_list_is_concise_and_shows_only_redacted_target_summary(
    tmp_path, monkeypatch, capsys,
):
    home = tmp_path / ".hermes"
    _seed_state_db(home)
    monkeypatch.setenv("HERMES_HOME", str(home))

    captured = _run_main(monkeypatch, capsys, ["handoffs", "list"])

    assert "Handoffs (4):" in captured.out
    assert "delivery:ob_attempting" in captured.out
    assert "target=discord" in captured.out
    assert "producer=completed delivery=pending" in captured.out
    assert len(captured.out.splitlines()) <= 6
    for forbidden in (
        "DO NOT LEAK",
        "RAW-CHANNEL",
        "RAW-THREAD",
        "RAW-USER",
        "secret-token",
    ):
        assert forbidden not in captured.out
