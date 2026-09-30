"""Rules DB: the seed is idempotent, an out-of-date DB rebuilds itself, and
the catalog has what the LLM needs to pick tools."""
import re
import sqlite3

import pytest

from rules_db.rules_db import RulesDB, SCHEMA_PATH, ToolSpec, _schema_version

CHAIN = ["bgp_state_check", "interface_check", "tcp_port_check", "config_check"]
DEVICE_TOOLS = {"bgp_state", "interface", "tcp_port", "config"}


def rule_count(db: RulesDB) -> int:
    return db.conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0]


def version(db: RulesDB) -> int:
    return db.conn.execute("PRAGMA user_version").fetchone()[0]


def test_fresh_db_is_seeded(tmp_path):
    db = RulesDB(tmp_path / "rules.db")
    assert rule_count(db) == 4
    assert {t.tool_id for t in db.list_tools()} == DEVICE_TOOLS | {"ml_classify"}
    assert version(db) == _schema_version(SCHEMA_PATH.read_text())


def test_catalog_describes_every_tool(tmp_path):
    db = RulesDB(tmp_path / "rules.db")
    tools = {t.tool_id: t for t in db.list_tools()}
    for t in tools.values():
        assert t.when_to_use, f"{t.tool_id} has no when_to_use"
        assert isinstance(t.args_schema, dict) and isinstance(t.context_args, dict)
    for tool_id in DEVICE_TOOLS:
        assert tools[tool_id].kind == "device" and tools[tool_id].endpoint.startswith("/tools/")
        assert tools[tool_id].context_args["host"] == "host"
    assert (tools["ml_classify"].kind, tools["ml_classify"].endpoint) == ("internal", None)
    assert tools["tcp_port"].context_args == {"host": "host", "peer_ip": "peer"}
    assert "interface" in tools["interface"].args_schema
    assert all(r.symptoms for r in db.list_rules())


def test_upsert_tool_round_trips_json_columns(tmp_path):
    db = RulesDB(tmp_path / "rules.db")
    spec = ToolSpec("route", "Route", "Route to the peer", "/tools/route", "show ip route",
                    when_to_use="Peer Active and TCP unreachable",
                    args_schema={"prefix": {"type": "string"}}, context_args={"host": "host"})
    db.upsert_tool(spec)
    assert db.get_tool("route") == spec


def test_escalation_chain(tmp_path):
    db = RulesDB(tmp_path / "rules.db")
    intent, seen = CHAIN[0], []
    while intent:
        seen.append(intent)
        intent = db.get_next_intent_on_fail(intent)
    assert seen == CHAIN
    assert [t.tool_id for t in db.get_tools_for_intent("tcp_port_check")] == ["tcp_port"]


def test_reload_seed_does_not_duplicate(tmp_path):
    db = RulesDB(tmp_path / "rules.db")
    db.reload_seed()
    db.reload_seed()
    assert rule_count(db) == 4


def test_old_db_is_rebuilt(tmp_path):
    """A DB from before schema versioning (user_version 0) with duplicated rules."""
    path = tmp_path / "rules.db"
    db = RulesDB(path)
    db.conn.executescript("""
        INSERT INTO rules (intent, tool_id, priority, condition, next_intent_on_fail)
            SELECT intent, tool_id, priority, condition, next_intent_on_fail FROM rules;
        PRAGMA user_version = 0;
    """)
    assert rule_count(db) == 8
    db.conn.close()

    db = RulesDB(path)
    assert rule_count(db) == 4
    assert version(db) > 0


def test_broken_schema_leaves_db_intact(tmp_path, monkeypatch):
    path = tmp_path / "rules.db"
    RulesDB(path).conn.close()
    # A newer schema.sql with a bad statement after the DROPs and the seed.
    schema = SCHEMA_PATH.read_text().replace("PRAGMA user_version", "INSERT INTO nope VALUES (1);\nPRAGMA user_version")
    broken = tmp_path / "schema.sql"
    broken.write_text(re.sub(r"user_version = \d+", "user_version = 99", schema))
    monkeypatch.setattr("rules_db.rules_db.SCHEMA_PATH", broken)

    with pytest.raises(sqlite3.OperationalError, match="nope"):
        RulesDB(path)

    monkeypatch.undo()
    db = RulesDB(path)
    assert rule_count(db) == 4 and version(db) == _schema_version(SCHEMA_PATH.read_text())


def test_runtime_rules_survive_reopen(tmp_path):
    path = tmp_path / "rules.db"
    RulesDB(path).add_rule("extra_check", "config")
    assert "extra_check" in RulesDB(path).list_intents()
