"""
Rules DB — intent -> tool map, and the tool catalog the LLM chooses from.

This is the component the Orchestrator's Reasoning loop queries on each pass
of the "loops until resolved" cycle: given the current intent, it returns the
ordered list of tools to try, plus what intent to escalate to if a tool's
result doesn't resolve the case (see `next_intent_on_fail` chaining in the
rule book, e.g. bgp_state_check -> interface_check -> tcp_port_check -> config_check).
"""

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).parent / "rules.db"
SCHEMA_PATH = Path(__file__).parent / "schema.sql"

log = logging.getLogger(__name__)

JSON_COLUMNS = ("args_schema", "context_args")


@dataclass
class ToolSpec:
    tool_id: str
    display_name: str
    description: str
    endpoint: Optional[str]        # None for internal tools
    base_command: str
    kind: str = "device"           # or "internal"
    when_to_use: str = ""
    args_schema: dict = field(default_factory=dict)
    context_args: dict = field(default_factory=dict)


@dataclass
class Rule:
    rule_id: int
    intent: str
    tool_id: str
    priority: int
    condition: Optional[str]
    next_intent_on_fail: Optional[str]
    symptoms: str = ""


def _tool(row: sqlite3.Row) -> ToolSpec:
    data = dict(row)
    for col in JSON_COLUMNS:
        data[col] = json.loads(data[col] or "{}")
    return ToolSpec(**data)


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
            # note: drops rules added at runtime with add_rule/upsert_tool
            log.info("Rules DB %s is at schema version %s, schema.sql is %s; rebuilding from seed",
                     self.db_path, current, wanted)
            self._apply(schema)

    def reload_seed(self):
        """Drop and re-apply schema.sql. Useful in dev when the rule book changes."""
        self._apply(SCHEMA_PATH.read_text())

    def _apply(self, schema: str):
        try:
            self.conn.executescript(f"BEGIN;\n{schema}\nCOMMIT;")
        except sqlite3.Error:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise

    # --- lookups used by the Reasoning loop ---

    def get_tools_for_intent(self, intent: str) -> list[ToolSpec]:
        """Return tools to call for a given intent, in priority order."""
        rows = self.conn.execute(
            """
            SELECT t.*
            FROM rules r JOIN tools t ON r.tool_id = t.tool_id
            WHERE r.intent = ?
            ORDER BY r.priority ASC
            """,
            (intent,),
        ).fetchall()
        return [_tool(row) for row in rows]

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
        return _tool(row) if row else None

    def list_intents(self) -> list[str]:
        rows = self.conn.execute("SELECT DISTINCT intent FROM rules").fetchall()
        return [r["intent"] for r in rows]

    def list_tools(self) -> list[ToolSpec]:
        rows = self.conn.execute("SELECT * FROM tools").fetchall()
        return [_tool(row) for row in rows]

    def list_rules(self) -> list[Rule]:
        rows = self.conn.execute("SELECT * FROM rules ORDER BY intent, priority").fetchall()
        return [Rule(**dict(row)) for row in rows]

    # --- admin: add/update rules without editing schema.sql ---

    def upsert_tool(self, spec: ToolSpec):
        self.conn.execute(
            """INSERT OR REPLACE INTO tools (tool_id, display_name, description, endpoint, base_command,
                                             kind, when_to_use, args_schema, context_args)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (spec.tool_id, spec.display_name, spec.description, spec.endpoint, spec.base_command,
             spec.kind, spec.when_to_use, json.dumps(spec.args_schema), json.dumps(spec.context_args)),
        )
        self.conn.commit()

    def add_rule(self, intent: str, tool_id: str, priority: int = 1,
                 condition: str = "", next_intent_on_fail: Optional[str] = None,
                 symptoms: str = ""):
        self.conn.execute(
            """INSERT INTO rules (intent, tool_id, priority, condition, next_intent_on_fail, symptoms)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (intent, tool_id, priority, condition, next_intent_on_fail, symptoms),
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
