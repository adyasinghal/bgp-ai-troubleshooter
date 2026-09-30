"""
Rules DB — intent -> tool map.

This is the component the Orchestrator's Reasoning loop queries on each pass
of the "loops until resolved" cycle: given the current intent, it returns the
ordered list of tools to try, plus what intent to escalate to if a tool's
result doesn't resolve the case (see `next_intent_on_fail` chaining in the
rule book, e.g. bgp_state_check -> interface_check -> tcp_port_check -> config_check).
"""

import logging
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).parent / "rules.db"
SCHEMA_PATH = Path(__file__).parent / "schema.sql"

log = logging.getLogger(__name__)


@dataclass
class ToolSpec:
    tool_id: str
    display_name: str
    description: str
    endpoint: str
    base_command: str


@dataclass
class Rule:
    rule_id: int
    intent: str
    tool_id: str
    priority: int
    condition: Optional[str]
    next_intent_on_fail: Optional[str]


class RulesDB:
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        schema = SCHEMA_PATH.read_text()
        wanted = _schema_version(schema)
        current = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if current != wanted:
            # A new DB reports version 0. An old one is rebuilt from the seed,
            # which drops rules added at runtime with add_rule/upsert_tool.
            log.info("Rules DB %s is at schema version %s, schema.sql is %s; rebuilding from seed",
                     self.db_path, current, wanted)
            self._apply(schema)

    def reload_seed(self):
        """Drop and re-apply schema.sql. Useful in dev when the rule book changes."""
        self._apply(SCHEMA_PATH.read_text())

    def _apply(self, schema: str):
        self.conn.executescript(schema)
        self.conn.commit()

    # --- lookups used by the Reasoning loop ---

    def get_tools_for_intent(self, intent: str) -> list[ToolSpec]:
        """Return tools to call for a given intent, in priority order."""
        rows = self.conn.execute(
            """
            SELECT t.tool_id, t.display_name, t.description, t.endpoint, t.base_command
            FROM rules r JOIN tools t ON r.tool_id = t.tool_id
            WHERE r.intent = ?
            ORDER BY r.priority ASC
            """,
            (intent,),
        ).fetchall()
        return [ToolSpec(**dict(row)) for row in rows]

    def get_rule(self, intent: str) -> Optional[Rule]:
        """Return the top-priority rule for an intent (what the reasoning loop acts on)."""
        row = self.conn.execute(
            "SELECT * FROM rules WHERE intent = ? ORDER BY priority ASC LIMIT 1",
            (intent,),
        ).fetchone()
        return Rule(**dict(row)) if row else None

    def get_next_intent_on_fail(self, intent: str) -> Optional[str]:
        rule = self.get_rule(intent)
        return rule.next_intent_on_fail if rule else None

    def get_tool(self, tool_id: str) -> Optional[ToolSpec]:
        row = self.conn.execute(
            "SELECT * FROM tools WHERE tool_id = ?", (tool_id,)
        ).fetchone()
        return ToolSpec(**dict(row)) if row else None

    def list_intents(self) -> list[str]:
        rows = self.conn.execute("SELECT DISTINCT intent FROM rules").fetchall()
        return [r["intent"] for r in rows]

    def list_tools(self) -> list[ToolSpec]:
        rows = self.conn.execute("SELECT * FROM tools").fetchall()
        return [ToolSpec(**dict(row)) for row in rows]

    # --- admin: add/update rules without editing schema.sql ---

    def upsert_tool(self, spec: ToolSpec):
        self.conn.execute(
            """INSERT OR REPLACE INTO tools (tool_id, display_name, description, endpoint, base_command)
               VALUES (?, ?, ?, ?, ?)""",
            (spec.tool_id, spec.display_name, spec.description, spec.endpoint, spec.base_command),
        )
        self.conn.commit()

    def add_rule(self, intent: str, tool_id: str, priority: int = 1,
                 condition: str = "", next_intent_on_fail: Optional[str] = None):
        self.conn.execute(
            """INSERT INTO rules (intent, tool_id, priority, condition, next_intent_on_fail)
               VALUES (?, ?, ?, ?, ?)""",
            (intent, tool_id, priority, condition, next_intent_on_fail),
        )
        self.conn.commit()


def _schema_version(schema: str) -> int:
    """The `PRAGMA user_version = N` that schema.sql sets."""
    m = re.search(r"PRAGMA\s+user_version\s*=\s*(\d+)", schema, re.IGNORECASE)
    if not m:
        raise ValueError(f"{SCHEMA_PATH} must set PRAGMA user_version")
    return int(m.group(1))


if __name__ == "__main__":
    # Quick smoke test matching the "BGP peer stuck at Active" example from the notes
    db = RulesDB()
    print("Intents:", db.list_intents())
    print("\nTools for 'bgp_state_check':")
    for t in db.get_tools_for_intent("bgp_state_check"):
        print(f"  {t.tool_id}: {t.base_command} -> {t.endpoint}")
    print("\nEscalation chain from bgp_state_check:")
    intent = "bgp_state_check"
    seen = []
    while intent and intent not in seen:
        seen.append(intent)
        nxt = db.get_next_intent_on_fail(intent)
        print(f"  {intent} --(if unresolved)--> {nxt}")
        intent = nxt
