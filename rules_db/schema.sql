-- Rules DB: intent -> tool map
-- Consulted by the Orchestrator's Reasoning loop on every iteration of the
-- "loops until resolved" cycle. Given an intent (extracted by the Triage
-- agent LLM) it returns which tool(s) to call next, in priority order.
--
-- Bump user_version at the bottom after any change; the DB is rebuilt from this file.

DROP TABLE IF EXISTS rules;
DROP TABLE IF EXISTS tools;

CREATE TABLE tools (
    tool_id      TEXT PRIMARY KEY,     -- e.g. 'bgp_state'
    display_name TEXT NOT NULL,        -- e.g. 'BGP state'
    description  TEXT NOT NULL,        -- what it's for
    endpoint     TEXT,                 -- REST path in the tool cohort service (NULL for internal tools)
    base_command TEXT NOT NULL,        -- canonical CLI command (vendor-neutral form)
    kind         TEXT NOT NULL DEFAULT 'device',  -- 'device' | 'internal'
    when_to_use  TEXT NOT NULL DEFAULT '',        -- for the LLM
    args_schema  TEXT NOT NULL DEFAULT '{}',      -- JSON: args the LLM may set
    context_args TEXT NOT NULL DEFAULT '{}'       -- JSON: payload field -> run context key (host/peer)
);

CREATE TABLE rules (
    rule_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    intent       TEXT NOT NULL,        -- normalized intent label, e.g. 'bgp_state_check'
    tool_id      TEXT NOT NULL,        -- which tool this rule fires
    priority     INTEGER NOT NULL DEFAULT 1,  -- lower = tried first when multiple tools match
    condition    TEXT,                 -- optional free-text condition/note for the reasoning loop
    next_intent_on_fail TEXT,          -- optional chaining: intent to try next if this tool's result doesn't resolve the case
    symptoms     TEXT NOT NULL DEFAULT '',  -- for the LLM
    likely_causes TEXT NOT NULL DEFAULT '',
    verification TEXT NOT NULL DEFAULT '', -- how to confirm the fix worked
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
     'Idle (Admin) = the neighbor is administratively shut down on this router. ' ||
     'Idle with no reason = NOT shut down: the session keeps failing, usually a rejected OPEN (remote-as mismatch, router-id conflict). ' ||
     'OpenSent/OpenConfirm = TCP is up but the OPEN negotiation fails (remote-as, router-id, capabilities, MD5). ' ||
     'For questions about routes or prefixes, an Established session is only the start: continue with route_table.',
     '{}',
     '{"host": "host", "peer": "peer"}'),
    ('bgp_neighbor', 'BGP neighbor', 'Fetch one peer''s details and last reset reason',
     '/tools/bgp/neighbor', 'show bgp neighbors <peer>', 'device',
     'Configured remote/local AS, state, admin shutdown, and the last reset reason with any NOTIFICATION ' ||
     '(Bad Peer AS, Peer De-configured, Administrative Shutdown...). When this router rejected the peer''s OPEN ' ||
     'with Bad Peer AS, peer_open_as is the AS the peer really uses. Use it once bgp_state shows the peer is not ' ||
     'Established. The last reset is history: it explains the failure only while the session is down.',
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
    ('route_table', 'Route table', 'Fetch BGP table / per-prefix path detail',
     '/tools/route/table', 'show ip bgp [prefix]', 'device',
     'The BGP table on this router: every path for each prefix, which one is best (is_best) and the attributes the best-path ' ||
     'algorithm compares (weight, local_pref, as_path_length, origin, metric). local_pref null means the default, 100. ' ||
     'Pass "prefix" for one prefix in detail: best_reason is FRR''s reason the best path won (e.g. "Local Pref", "AS Path"), and ' ||
     'in_table false means this router has no route for it (the peer doesn''t send it, or an inbound route-map filters it). ' ||
     'Use for questions about routes or prefixes once bgp_state shows the session Established.',
     '{"prefix": {"type": "string", "required": false, "pattern": "^\\d{1,3}(\\.\\d{1,3}){3}(/\\d{1,2})?$", "description": "one prefix, e.g. 192.168.10.0/24"}}',
     '{"host": "host"}'),
    ('route_map', 'Route map', 'Fetch route-map and policy definitions',
     '/tools/route/map', 'show route-map [name]', 'device',
     'Route-maps on this router: each entry''s action (permit/deny), sequence, invoked (how many routes it matched), ' ||
     'match clauses and set clauses. A deny entry with invoked > 0 has filtered routes; set clauses change attributes ' ||
     'such as local-preference or MED. Use when a prefix is missing (in_table false) or a path loses on an attribute a ' ||
     'route-map can set. Pass "name" for one route-map.',
     '{"name": {"type": "string", "required": false, "pattern": "^[A-Za-z0-9_.:-]{1,64}$", "description": "one route-map, e.g. FROM-R2"}}',
     '{"host": "host"}'),
    ('ml_classify', 'ML classifier', 'Classify the evidence gathered so far into a fault class',
     NULL, 'ml classify', 'internal',
     'Runs the scikit-learn fault classifier over the output of every tool called so far and returns the most likely fault class with a confidence. ' ||
     'Trained mostly on synthetic data: treat it as a hint when the evidence is ambiguous, not as proof. Only useful after at least two device tools have run.',
     '{}',
     '{}');

-- Seed: rule book (from meeting notes / Untitled 44, extended with Gaurav's v2 rules from HPE Meeting V1, 24/9/26)
INSERT INTO rules (rule_id, intent, tool_id, priority, condition, next_intent_on_fail, symptoms, likely_causes, verification) VALUES
    (1, 'bgp_state_check', 'bgp_state', 1, 'Any request for BGP state/peer status', 'interface_check',
     'Any BGP question: peer down, stuck, flapping, not coming up, a status request, or a question about routes',
     'See the rule for the state the peer is in (bgp_state_idle, bgp_state_active, ...)',
     'The peer shows Established (a prefix count or (Policy)) in show bgp summary'),
    (2, 'interface_check', 'interface', 1, 'BGP peer stuck (Active/Connect) -> check interface next', 'tcp_port_check',
     'Peer Active/Connect, or the question mentions an interface, link, cable or port being down',
     'Cable/port down; interface admin-shut; wrong VLAN/subinterface; MTU mismatch',
     'Interface shows link up + admin up, and BGP state advances past Active/Connect'),
    (3, 'tcp_port_check',  'tcp_port',  1, 'Interface up but peer still stuck -> check TCP/179 reachability', 'config_check',
     'Peer Active/Connect with interfaces up, or the question mentions TCP, port 179, a firewall or reachability',
     'ACL/firewall blocking TCP/179; peer not listening (BGP process down); asymmetric routing',
     'TCP/179 reachable both directions and BGP state advances to OpenSent'),
    (4, 'config_check',    'config',    1, 'TCP reachable but session still down -> diff config for mismatch', NULL,
     'Peer Idle or OpenSent/OpenConfirm, a suspected remote-as mismatch, a shut down or missing neighbor, or a recent config change',
     'AS number, peer IP, update-source, or authentication mismatch vs. baseline config',
     'Config diff against baseline is empty (or only contains the intended change) and session reaches Established'),
    (5, 'bgp_neighbor_detail', 'bgp_neighbor', 1, 'A state-stuck rule needs the specific NOTIFICATION/error reason', 'config_check',
     'Peer Idle, Active or Connect; why a session keeps failing or resetting; a suspected fault on the peer',
     'See specific NOTIFICATION error code/subcode returned by peer',
     'Error code cleared from show bgp neighbor after fix and session stable for > 60s'),

    -- BGP session states (every state, including intermediate)
    (6, 'bgp_state_idle', 'bgp_state', 1, 'Peer session reported in Idle state', 'interface_check',
     'Neighbor shows Idle in show bgp summary; no connection attempts visible',
     'Neighbor shut down (shows as Idle (Admin)) or not configured; no route to peer; AS/peer-IP misconfigured; manually cleared session',
     'Re-check show bgp summary after config fix; state should progress past Idle within a few seconds'),
    (7, 'bgp_state_connect', 'bgp_state', 1, 'Peer session reported in Connect state', 'tcp_port_check',
     'Router is trying to establish the TCP transport connection to the peer',
     'Peer unreachable at L3; TCP/179 blocked by ACL or firewall; peer not listening',
     'Confirm TCP handshake succeeds (tcp_port_check) and state moves to OpenSent'),
    (8, 'bgp_state_active', 'bgp_state', 1, 'Peer session stuck in Active state', 'interface_check',
     'TCP connection failed or was refused; router is retrying',
     'Interface down; wrong peer IP; route to peer missing; ACL/firewall blocking TCP/179; MTU mismatch',
     'Interface up + TCP/179 reachable + state advances to OpenSent on retry'),
    (9, 'bgp_state_opensent', 'bgp_state', 1, 'Peer session stuck in OpenSent state', 'bgp_neighbor_detail',
     'TCP connected, OPEN message sent, waiting for peer''s OPEN or a NOTIFICATION was received',
     'AS number mismatch; BGP version mismatch; hold-timer mismatch; capability negotiation failure',
     'Check show bgp neighbor for last NOTIFICATION reason; confirmed fix advances state to OpenConfirm'),
    (10, 'bgp_state_openconfirm', 'bgp_state', 1, 'Peer session stuck in OpenConfirm state', 'bgp_neighbor_detail',
     'OPEN messages exchanged, waiting for KEEPALIVE; session may flap here',
     'Authentication (MD5/TCP-AO) mismatch; mismatched hold-timers causing early teardown; policy rejecting session post-open',
     'KEEPALIVE received and state advances to Established without flapping'),
    (11, 'bgp_state_established', 'bgp_state', 1, 'Peer session reports Established but routes look wrong/missing', 'route_received_not_selected',
     'Session up, but expected prefixes not present or not selected as best path',
     'Route not advertised by peer; inbound route-map filtering it; better path chosen from another peer; max-prefix limits',
     'Confirm prefix present in show ip bgp with the expected next-hop and marked as best (>)'),

    -- BGP route selection
    (12, 'route_received_not_selected', 'route_table', 1, 'Prefix is received from a peer but is not the best path / not installed', 'route_map_check',
     'Route visible in show ip bgp with a peer entry but no ">" best-path marker, or absent from the routing table',
     'Lower local-preference; longer AS-path; higher MED from this peer; not the oldest/eBGP-preferred path; inbound route-map denies or devalues it; next-hop unreachable',
     'After adjusting policy/attributes, prefix shows ">" (best) marker and appears in show ip route'),
    (13, 'route_best_path_selection', 'route_table', 1, 'General question about how a path was chosen among multiple candidates', 'route_map_check',
     'Multiple paths to same prefix from different peers/paths',
     'BGP best-path algorithm applied in order: highest weight -> highest local-pref -> locally originated -> shortest AS-path -> lowest origin type -> lowest MED -> eBGP over iBGP -> lowest IGP metric to next-hop -> oldest path -> lowest router-id',
     'The selected path matches the expected outcome of the above order for the observed attribute values'),
    (14, 'route_map_check', 'route_map', 1, 'A route-map or policy is suspected of altering/filtering the path', 'config_check',
     'Route present upstream but missing, devalued, or altered downstream of a route-map application point',
     'Route-map denies the prefix (implicit deny at end); match clause too narrow/wrong; set clause altering an attribute unexpectedly (local-pref, MED, weight)',
     'Prefix passes the route-map as expected and attributes match the intended set clauses'),
    (15, 'route_ignore_case', 'route_table', 2, 'A route is intentionally filtered/ignored and this is expected behavior, not a fault', NULL,
     'Prefix absent from RIB but this matches a deliberate deny policy (e.g. default-route filtering, RFC1918 filtering)',
     'Expected filtering by design (distribute-list, prefix-list deny, route-map deny) -- not a bug',
     'Confirm the filtering policy is the one on record as intended; no further action needed'),
    (16, 'route_leading_case', 'route_table', 1, 'Identify which path is currently the winning/best path for a prefix and why', NULL,
     'Multiple valid paths exist; user wants to know which one is active and the deciding attribute',
     'N/A -- diagnostic/informational rule, not a fault',
     'The ">" marked entry in show ip bgp matches the path with the best value at the first differing best-path step');

PRAGMA user_version = 5;
