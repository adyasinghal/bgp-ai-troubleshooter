-- Rules DB: intent -> tool map, plus the tool catalog the LLM chooses from.
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
    endpoint     TEXT,                 -- REST path in the tool cohort service (NULL for internal tools)
    base_command TEXT NOT NULL,        -- canonical CLI command (vendor-neutral form)
    kind         TEXT NOT NULL DEFAULT 'device',  -- 'device' (REST -> router) | 'internal' (runs in the analyzer)
    when_to_use  TEXT NOT NULL DEFAULT '',        -- guidance for the LLM: what the output proves and doesn't
    args_schema  TEXT NOT NULL DEFAULT '{}',      -- JSON: args the LLM may set, {name: {type, required, pattern, description}}
    context_args TEXT NOT NULL DEFAULT '{}'       -- JSON: payload field -> run context key, e.g. {"peer_ip": "peer"}; never set by the LLM
);

CREATE TABLE rules (
    rule_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    intent       TEXT NOT NULL,        -- normalized intent label, e.g. 'bgp_state_check'
    tool_id      TEXT NOT NULL,        -- which tool this rule fires
    priority     INTEGER NOT NULL DEFAULT 1,  -- lower = tried first when multiple tools match
    condition    TEXT,                 -- optional free-text condition/note for the reasoning loop
    next_intent_on_fail TEXT,          -- optional chaining: intent to try next if this tool's result doesn't resolve the case
    symptoms     TEXT NOT NULL DEFAULT '',  -- for the LLM: which symptoms/questions this rule fits
    FOREIGN KEY (tool_id) REFERENCES tools(tool_id)
);

CREATE INDEX idx_rules_intent ON rules(intent);

-- Seed: tools list (matches the Tool cohort in the architecture diagram)
INSERT INTO tools (tool_id, display_name, description, endpoint, base_command, kind, when_to_use, args_schema, context_args) VALUES
    ('bgp_state', 'BGP state', 'Fetch current BGP session state',
     '/tools/bgp/state', 'show bgp summary', 'device',
     'Start here for almost any BGP question. Shows every peer''s session state and remote AS as this router sees them. ' ||
     'Established = up ("Policy" reason = eBGP with no route policy, so no prefixes are exchanged). ' ||
     'Active/Connect = the TCP session is not forming: check interfaces, routing and port 179. ' ||
     'Idle = not trying; reason "Admin" means the neighbor is administratively shut down, no reason often means repeated OPEN failures such as a remote-as mismatch. ' ||
     'OpenSent/OpenConfirm = TCP is up but the OPEN negotiation fails (remote-as, router-id, capabilities, MD5).',
     '{}',
     '{"host": "host", "peer": "peer"}'),
    ('interface', 'Interface', 'Fetch interface detail / link state',
     '/tools/interface/detail', 'show interface', 'device',
     'Link state, admin state and IPv4 address per interface. Use when the peer is Active/Connect, or the question mentions a link, cable or port. ' ||
     'A down interface only explains the fault if the peer is reached over it. Omit "interface" to list all interfaces.',
     '{"interface": {"type": "string", "required": false, "pattern": "^[A-Za-z0-9_./:-]{1,32}$", "description": "one interface to show, e.g. eth1"}}',
     '{"host": "host"}'),
    ('tcp_port', 'TCP / port', 'Check TCP reachability on BGP port 179',
     '/tools/tcp/check', 'check port 179', 'device',
     'Tries to open a TCP connection from this router to the peer on port 179. Use when the peer is Active/Connect and the interfaces are up. ' ||
     'Unreachable = no route, an ACL/firewall, or bgpd not listening on the peer. Reachable rules out transport problems.',
     '{}',
     '{"host": "host", "peer_ip": "peer"}'),
    ('config', 'Config', 'Diff running config against baseline',
     '/tools/config/diff', 'show run diff', 'device',
     'This router''s running config, plus a diff against a saved known-good baseline. Use to check neighbor settings: remote-as, shutdown, missing neighbor, update-source. ' ||
     'Without a baseline (has_baseline false) the diff shows the whole config as added, which proves nothing; read the running config instead.',
     '{}',
     '{"host": "host"}'),
    ('ml_classify', 'ML classifier', 'Classify the evidence gathered so far into a fault class',
     NULL, 'ml classify', 'internal',
     'Runs the scikit-learn fault classifier over the output of every tool called so far and returns the most likely fault class with a confidence. ' ||
     'Trained mostly on synthetic data: treat it as a hint when the evidence is ambiguous, not as proof. Only useful after at least two device tools have run.',
     '{}',
     '{}');

-- Seed: rule book (from meeting notes / Untitled 44)
INSERT INTO rules (rule_id, intent, tool_id, priority, condition, next_intent_on_fail, symptoms) VALUES
    (1, 'bgp_state_check', 'bgp_state', 1, 'Any request for BGP state/peer status', 'interface_check',
     'Any BGP question: peer down, stuck, flapping, not coming up, or a status request'),
    (2, 'interface_check', 'interface', 1, 'BGP peer stuck (Active/Connect) -> check interface next', 'tcp_port_check',
     'Peer Active/Connect, or the question mentions an interface, link, cable or port being down'),
    (3, 'tcp_port_check',  'tcp_port',  1, 'Interface up but peer still stuck -> check TCP/179 reachability', 'config_check',
     'Peer Active/Connect with interfaces up, or the question mentions TCP, port 179, a firewall or reachability'),
    (4, 'config_check',    'config',    1, 'TCP reachable but session still down -> diff config for mismatch', NULL,
     'Peer Idle or OpenSent/OpenConfirm, a suspected remote-as mismatch, a shut down or missing neighbor, or a recent config change');

PRAGMA user_version = 2;
