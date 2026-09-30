"""Rules DB: the seed is idempotent and an out-of-date DB rebuilds itself."""
from rules_db.rules_db import RulesDB, SCHEMA_PATH, _schema_version

CHAIN = ["bgp_state_check", "interface_check", "tcp_port_check", "config_check"]


def rule_count(db: RulesDB) -> int:
    return db.conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0]


def version(db: RulesDB) -> int:
    return db.conn.execute("PRAGMA user_version").fetchone()[0]


def test_fresh_db_is_seeded(tmp_path):
    db = RulesDB(tmp_path / "rules.db")
    assert rule_count(db) == 4
    assert len(db.list_tools()) == 4
    assert version(db) == _schema_version(SCHEMA_PATH.read_text())


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


def test_runtime_rules_survive_reopen(tmp_path):
    path = tmp_path / "rules.db"
    RulesDB(path).add_rule("extra_check", "config")
    assert "extra_check" in RulesDB(path).list_intents()
