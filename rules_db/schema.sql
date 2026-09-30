-- Rules DB: intent -> tool map
-- Consulted by the Orchestrator's Reasoning loop on every iteration of the
-- "loops until resolved" cycle. Given an intent (extracted by the Triage
-- agent LLM) it returns which tool(s) to call next, in priority order.
--
-- This file is the source of truth: applying it resets both tables to the seed
-- below. rules_db.py re-applies it whenever the DB's user_version differs from
-- the one set at the bottom, so bump that number after any change here.

DROP TABLE IF EXISTS rules;
DROP TABLE IF EXISTS tools;

CREATE TABLE tools (
    tool_id      TEXT PRIMARY KEY,     -- e.g. 'bgp_state'
    display_name TEXT NOT NULL,        -- e.g. 'BGP state'
    description  TEXT NOT NULL,        -- what it's for
    endpoint     TEXT NOT NULL,        -- REST path in the tool cohort service
    base_command TEXT NOT NULL         -- canonical CLI command (vendor-neutral form)
);

CREATE TABLE rules (
    rule_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    intent       TEXT NOT NULL,        -- normalized intent label, e.g. 'bgp_state_check'
    tool_id      TEXT NOT NULL,        -- which tool this rule fires
    priority     INTEGER NOT NULL DEFAULT 1,  -- lower = tried first when multiple tools match
    condition    TEXT,                 -- optional free-text condition/note for the reasoning loop
    next_intent_on_fail TEXT,          -- optional chaining: intent to try next if this tool's result doesn't resolve the case
    FOREIGN KEY (tool_id) REFERENCES tools(tool_id)
);

CREATE INDEX idx_rules_intent ON rules(intent);

-- Seed: tools list (matches the Tool cohort in the architecture diagram)
INSERT INTO tools (tool_id, display_name, description, endpoint, base_command) VALUES
    ('bgp_state',  'BGP state',  'Fetch current BGP session state',        '/tools/bgp/state',     'show bgp summary'),
    ('interface',  'Interface',  'Fetch interface detail / link state',    '/tools/interface/detail','show interface'),
    ('tcp_port',   'TCP / port', 'Check TCP reachability on BGP port 179', '/tools/tcp/check',     'check port 179'),
    ('config',     'Config',     'Diff running config against baseline',   '/tools/config/diff',   'show run diff');

-- Seed: rule book (from meeting notes / Untitled 44)
INSERT INTO rules (rule_id, intent, tool_id, priority, condition, next_intent_on_fail) VALUES
    (1, 'bgp_state_check',   'bgp_state',  1, 'Any request for BGP state/peer status', 'interface_check'),
    (2, 'interface_check',   'interface',  1, 'BGP peer stuck (Active/Connect) -> check interface next', 'tcp_port_check'),
    (3, 'tcp_port_check',    'tcp_port',   1, 'Interface up but peer still stuck -> check TCP/179 reachability', 'config_check'),
    (4, 'config_check',      'config',     1, 'TCP reachable but session still down -> diff config for mismatch', NULL);

PRAGMA user_version = 1;
