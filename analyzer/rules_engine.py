"""The reasoning loop: ask the rulebook, call the tool, decide, escalate.

Escalation order: rule chain -> ML engine -> LLM -> human.

How the rule chain moves (rules mode):
  - bgp_state runs first. Its result picks the next rule: the rule for the
    state it saw (bgp_state_active, bgp_state_idle, ...), or, for a question
    about routes on an Established session, the route rule triage chose.
    Without this jump every case walked bgp_state -> interface -> tcp -> config
    whatever the state was, and the per-state rules were never used.
  - A tool that already ran is not run again (the bgp_state_* rules reuse the
    first bgp_state result), and no intent is visited twice.
  - Some results are not proof on their own (TCP/179 times out, a prefix is
    missing). They are kept as a tentative finding and the chain continues;
    if nothing more specific turns up, the chain ends on that finding instead
    of handing the case to ML/LLM with nothing.
"""
import ipaddress
import json
import logging
import re

from analyzer import llm_escalation, ml_engine
from analyzer.tool_registry import ToolRegistry
from analyzer.triage import route_context, triage
from analyzer.verdict import Verdict

ML_CONFIDENCE_THRESHOLD = 0.7   # below this, the ML guess goes to the LLM as a hint
FLAP_LINK_DOWNS = 3             # link_downs on the peer-facing interface that count as flapping
RECENT_RESET_SECONDS = 300      # an Established session younger than this was reset recently

log = logging.getLogger(__name__)

CONNECT_ERRORS = ("NoValidConnectionsError", "Unable to connect", "timed out", "AuthenticationException")

# bgp_state result -> the rule for that state
STATE_INTENT = {
    "Idle": "bgp_state_idle",
    "Connect": "bgp_state_connect",
    "Active": "bgp_state_active",
    "OpenSent": "bgp_state_opensent",
    "OpenConfirm": "bgp_state_openconfirm",
    "Established": "bgp_state_established",
    "unknown": "bgp_neighbor_detail",   # not in the summary: read the neighbor (likely not configured)
}

# Hints are for the LLM agent; the rule chain ignores them.
STATE_HINTS = {
    "Active": ("Active: the TCP session to the peer is not forming or keeps being closed. "
               "Call bgp_neighbor next: it shows whether TCP ever came up and the last reset reason."),
    "Connect": ("Connect: the TCP session to the peer is not forming. "
                "Call bgp_neighbor next: it shows whether TCP ever came up and the last reset reason."),
    "Idle": ("Idle with no reason: the neighbor is NOT administratively shut down (that shows as Idle (Admin)). "
             "The session keeps failing and restarting, usually because the OPEN exchange is rejected "
             "(remote-as mismatch, router-id conflict) or the peer doesn't have this router configured. "
             "Call bgp_neighbor next: it shows the NOTIFICATION behind it."),
    "OpenSent": ("OpenSent: TCP is up but the OPEN negotiation fails (remote-as, router-id, capabilities). "
                 "Call bgp_neighbor next: it shows the NOTIFICATION behind it."),
    "OpenConfirm": ("OpenConfirm: the OPENs were exchanged but no KEEPALIVE arrived (hold timer, latency, "
                    "or the peer resets after the OPEN). Call bgp_neighbor next."),
    "unknown": ("The peer is not listed in `show bgp summary`, so it is most likely not configured on this router. "
                "Check the config for a `neighbor <peer> remote-as` line."),
}

# best_reason from `show ip bgp <prefix>` -> (path attribute, how the winner compares)
BEST_REASON_ATTR = {
    "Weight": ("weight", "higher weight"),
    "Local Pref": ("local_pref", "higher local-preference"),
    "AS Path": ("as_path_length", "shorter AS-path"),
    "Origin": ("origin", "better origin (i < e < ?)"),
    "MED": ("metric", "lower MED"),
    "Router ID": ("router_id", "lower router-id"),
}
ROUTE_MAP_SETS = {   # path attribute -> words a route-map set clause uses for it
    "weight": ("weight",),
    "local_pref": ("local-preference",),
    "as_path_length": ("as-path",),
    "origin": ("origin",),
    "metric": ("metric",),
}


def evaluate(tool_id: str, result: dict, ctx: dict) -> dict:
    """decision: root_cause | healthy | continue, plus cause/fix/fault_class when decided.

    A "continue" may also carry:
      hint                  - text for the LLM agent
      suggested_next_intent - the rule the chain should go to next (rules mode follows it)
      tentative             - {fault_class, cause, fix}: the answer if nothing better turns up
    """
    if not result.get("success", True):
        error = result.get("error") or ""
        if any(e in error for e in CONNECT_ERRORS):
            return {"decision": "root_cause", "fault_class": "device_unreachable",
                    "cause": f"The diagnostic tools could not reach router {ctx.get('host')}: {error}",
                    "fix": "Check the container is running, its management IP, and the SSH/REST "
                           "credentials the device client uses. Until then nothing on the router can be checked."}
        return {"decision": "continue"}

    parsed = result.get("parsed", {})
    peer = ctx.get("peer")

    if tool_id == "bgp_state":
        return _bgp_state_finding(parsed, ctx)

    if tool_id == "bgp_neighbor":
        return _neighbor_finding(parsed, ctx)

    if tool_id == "interface":
        return _interface_finding(parsed, ctx)

    if tool_id == "route_to_peer":
        return _route_to_peer_finding(parsed, ctx)

    if tool_id == "tcp_port":
        if parsed.get("reachable") is False:
            reason = parsed.get("reason")
            if reason == "refused":
                return {"decision": "root_cause", "fault_class": "peer_not_listening",
                        "cause": f"{peer} refuses TCP connections on port 179: bgpd is not running on the peer, "
                                 f"or a firewall there REJECTs port 179",
                        "fix": "On the peer: check bgpd is running (bgpd=yes in /etc/frr/daemons, then restart "
                               "FRR) and that no iptables REJECT rule covers port 179."}
            if reason == "no_route":
                return {"decision": "root_cause", "fault_class": "no_route_to_peer",
                        "cause": f"This router has no route to {peer}, so TCP/179 can't be opened",
                        "fix": "Fix the peer-facing interface address or add a route to the peer."}
            return {"decision": "continue",
                    "hint": f"TCP/179 to {peer} gets no answer ({reason or 'unreachable'}). With the interface and "
                            f"route fine, packets are being dropped: a firewall/ACL on either router, or link loss.",
                    "tentative": {
                        "fault_class": "tcp_unreachable",
                        "cause": f"TCP/179 to {peer} gets no answer although the interface and the route to it are "
                                 f"fine: a firewall/ACL is dropping port 179 (on either router), or the link is "
                                 f"losing packets",
                        "fix": f"Check `iptables -L -n` on both routers for rules covering port 179, and the link "
                               f"for loss (`ping -c 20 {peer}`)."}}
        if parsed.get("reachable") and ctx.get("_state") not in (None, "Established"):
            # TCP/179 is open, yet the session doesn't come up: the peer takes the connection and drops it
            return {"decision": "continue",
                    "hint": f"TCP/179 to {peer} is reachable but the session is {ctx['_state']}: the peer accepts "
                            f"TCP and then closes it. Check this router's config, then the peer's.",
                    "tentative": {
                        "fault_class": "neighbor_missing",
                        "cause": f"TCP/179 to {peer} is reachable but the session stays {ctx['_state']}: the peer "
                                 f"accepts the TCP connection and then closes it. Most likely the peer has no "
                                 f"`neighbor` statement for this router (or one with the wrong address/remote-as)",
                        "fix": f"On {peer}: `show running-config` and check for `neighbor <this router's address> "
                               f"remote-as <this router's AS>`; add it if missing."}}
        return {"decision": "continue"}

    if tool_id == "config":
        return _config_finding(parsed, result.get("raw_output") or "", ctx)

    if tool_id == "route_table":
        return _route_table_finding(parsed, ctx)

    if tool_id == "route_map":
        return _route_map_finding(parsed, ctx)

    if tool_id == "prefix_list":
        return _prefix_list_finding(parsed, ctx)

    return {"decision": "continue"}


# --- bgp_state -----------------------------------------------------------------

def _bgp_state_finding(parsed: dict, ctx: dict) -> dict:
    peer = ctx.get("peer") or "<peer>"
    state = parsed.get("queried_peer_state")
    reason = parsed.get("queried_peer_state_reason")
    ctx["_state"] = state
    ctx["_state_reason"] = reason
    ctx["_router_id"] = parsed.get("router_id")

    if state == "Established":
        if reason == "Policy" and not ctx.get("route_question"):
            # the session itself is up; for a session question that is the answer
            return {"decision": "healthy", "fault_class": "healthy",
                    "cause": f"BGP peer {peer} is Established, but it exchanges no routes: no route policy for this "
                             f"eBGP peer ((Policy): bgp ebgp-requires-policy)",
                    "fix": "No action needed for the session. If routes from this peer are expected, "
                           "add a route-map in/out for the neighbor."}
        if reason == "Policy":
            return {"decision": "root_cause", "fault_class": "route_filtered",
                    "cause": f"The session with {peer} is up but FRR exchanges no routes with it: it is an eBGP "
                             f"peer with no route policy ((Policy) in show bgp summary; bgp ebgp-requires-policy)",
                    "fix": f"router bgp <local-asn> / address-family ipv4 unicast / neighbor {peer} route-map <MAP> in "
                           f"(and out), or `no bgp ebgp-requires-policy` if no policy is wanted."}
        if reason == "NoNeg":
            # never intended: the config says whether it is capabilities or activation
            return {"decision": "continue", "suggested_next_intent": "config_check",
                    "hint": f"{peer} is Established but no address family was negotiated (NoNeg). Check the config "
                            f"for `no neighbor ... activate`, strict-capability-match or dont-capability-negotiate.",
                    "tentative": {
                        "fault_class": "afi_not_negotiated",
                        "cause": f"The session with {peer} is up but no address family was negotiated (NoNeg), so no "
                                 f"prefixes are exchanged: IPv4 unicast is not activated for the neighbor on one side, "
                                 f"or the peer does not negotiate capabilities (dont-capability-negotiate)",
                        "fix": f"router bgp <local-asn> / address-family ipv4 unicast / neighbor {peer} activate on "
                               f"both routers, and remove dont-capability-negotiate on the peer if it is set."}}
        if ctx.get("route_question"):
            target = ctx.get("route_intent") or "route_received_not_selected"
            return {"decision": "continue", "suggested_next_intent": target,
                    "hint": f"The session with {peer} is up; the question is about routes, so continue with {target}."}
        uptime = (parsed.get("uptimes") or {}).get(peer)
        seconds = _seconds(uptime)
        if seconds is not None and seconds < RECENT_RESET_SECONDS:
            ctx["_recent"] = uptime
            return {"decision": "continue", "suggested_next_intent": "bgp_neighbor_detail",
                    "hint": f"{peer} is Established but only up {uptime}: read the last reset reason.",
                    "tentative": {"fault_class": "healthy",
                    "cause": f"BGP peer {peer} is Established, but it has only been up for {uptime}: the session "
                             f"was reset moments ago (a manual clear, a peer restart, or a flap) and has recovered",
                    "fix": f"No action needed if the reset was intended. If not, check `show bgp neighbors {peer}` "
                           f"(Last reset) and run bgp_state again in a few minutes: an Up/Down that keeps "
                           f"resetting means the session is flapping."}}
        return {"decision": "healthy", "fault_class": "healthy",
                "cause": f"BGP peer {peer} is Established" + (f" (up {uptime})" if uptime else ""),
                "fix": "No action needed — the session is up."}

    if reason == "Admin":
        return {"decision": "root_cause", "fault_class": "neighbor_shutdown",
                "cause": f"Neighbor {peer} is administratively shut down on this router (Idle (Admin))",
                "fix": f"router bgp <local-asn> / no neighbor {peer} shutdown"}
    if reason == "PfxCt":
        return {"decision": "root_cause", "fault_class": "max_prefix_exceeded",
                "cause": f"{peer} sent more prefixes than the maximum-prefix limit allows, so this router tore the "
                         f"session down (Idle (PfxCt))",
                "fix": f"router bgp <local-asn> / address-family ipv4 unicast / neighbor {peer} maximum-prefix "
                       f"<higher limit>, then `clear bgp {peer}`"}

    hint = STATE_HINTS.get(state)
    out = {"decision": "continue", "suggested_next_intent": STATE_INTENT.get(state)}
    if hint:
        out["hint"] = hint
    return out


def _seconds(uptime: str | None) -> int | None:
    """Seconds in an Up/Down value like 00:01:23; None for longer forms (1d02h03m, 02w3d) or none."""
    m = re.fullmatch(r"(\d+):(\d{2}):(\d{2})", uptime or "")
    return int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3]) if m else None


# --- interface / route to peer ---------------------------------------------------

def _addresses(info: dict) -> list[str]:
    return info.get("ip_addresses") or ([info["ip_address"]] if info.get("ip_address") else [])


def peer_facing_interface(interfaces: dict, peer: str | None) -> str | None:
    """The interface with the longest-prefix address whose subnet contains the peer."""
    try:
        peer_ip = ipaddress.ip_address(peer) if peer else None
    except ValueError:
        return None
    if peer_ip is None:
        return None
    best, best_len = None, -1
    for name, info in interfaces.items():
        for addr in _addresses(info):
            try:
                net = ipaddress.ip_interface(addr).network
            except ValueError:
                continue
            if peer_ip.version == net.version and peer_ip in net and net.prefixlen > best_len:
                best, best_len = name, net.prefixlen
    return best


def _interface_finding(parsed: dict, ctx: dict) -> dict:
    interfaces = parsed.get("interfaces", {})
    peer = ctx.get("peer")
    down = [n for n, i in interfaces.items()
            if i.get("link_state") == "down" or i.get("admin_state") == "down"]
    ctx["_down_ifaces"] = down
    ctx["_interfaces"] = interfaces

    if not peer:
        # no peer to relate interfaces to: any down interface is the best lead
        if down:
            return {"decision": "root_cause", "fault_class": "interface_down",
                    "cause": f"Interface(s) down: {', '.join(down)}",
                    "fix": f"Bring it up: conf t / interface {down[0]} / no shutdown"}
        return {"decision": "continue"}

    facing = peer_facing_interface(interfaces, peer)
    if facing is None:
        return {"decision": "continue",
                "hint": f"No interface has an address on {peer}'s subnet: the address is missing or wrong, or the "
                        f"peer is a loopback reached through a route. Check route_to_peer."
                        + (f" Down interfaces: {', '.join(down)}." if down else "")}

    info = interfaces[facing]
    if info.get("admin_state") == "down":
        return {"decision": "root_cause", "fault_class": "interface_down",
                "cause": f"Interface {facing}, the one towards {peer}, is administratively down",
                "fix": f"conf t / interface {facing} / no shutdown (or `ip link set {facing} up`)"}
    if info.get("link_state") == "down":
        return {"decision": "root_cause", "fault_class": "interface_down",
                "cause": f"Interface {facing}, the one towards {peer}, is admin up but has no link "
                         f"(line protocol down): the cable, the link, or the peer's end of it is down",
                "fix": f"Check the peer's interface on this link (`show interface` on {peer}) and the cabling, "
                       f"and bring the far end up."}
    hint = f"Interface {facing} towards {peer} is up/up ({', '.join(_addresses(info))})."
    downs = info.get("link_downs")
    if downs is not None and downs >= FLAP_LINK_DOWNS:
        # a counter since boot, so old outages count too: a hint, not a finding
        hint += (f" It has gone down {downs} times since boot: if that count keeps growing between checks, "
                 f"the link is flapping (see bgp_session_flapping).")
    return {"decision": "continue", "hint": hint}


def _route_to_peer_finding(parsed: dict, ctx: dict) -> dict:
    peer = ctx.get("peer")
    if parsed.get("has_route") is None:
        return {"decision": "continue"}
    if parsed.get("has_route") and not parsed.get("is_default"):
        return {"decision": "continue",
                "hint": f"Route to {peer}: {parsed.get('prefix')} via {parsed.get('protocol')} on "
                        f"{parsed.get('interface') or parsed.get('next_hop')}."}

    down = ctx.get("_down_ifaces") or []
    if down:
        return {"decision": "root_cause", "fault_class": "interface_down",
                "cause": f"This router has no route to {peer} and interface(s) {', '.join(down)} are down; "
                         f"the peer is most likely reached over one of them",
                "fix": f"Bring the interface up: conf t / interface {down[0]} / no shutdown"}
    via_default = (f" except the default route via {parsed.get('interface') or parsed.get('next_hop')}"
                   if parsed.get("is_default") else "")
    return {"decision": "root_cause", "fault_class": "no_route_to_peer",
            "cause": f"This router has no route to {peer}{via_default}: no interface has an address on the "
                     f"peer's subnet (the address is missing or wrong) and no static or IGP route covers it"
                     + _address_suspects(ctx.get("_interfaces") or {}, parsed.get("interface") if parsed.get("is_default") else None, peer),
            "fix": f"Put the right address on the peer-facing interface (`ip addr add <addr>/<len> dev <iface>` "
                   f"or `interface <iface> / ip address <addr>/<len>`), or, for loopback peering, add a route "
                   f"(`ip route {peer}/32 <next-hop>`)."}


def _address_suspects(interfaces: dict, default_iface: str | None, peer: str | None) -> str:
    """Name the interfaces that could be the peer-facing one: up, not loopback/management."""
    candidates = {n: i for n, i in interfaces.items()
                  if n != default_iface and not n.startswith("lo") and i.get("link_state") != "down"}
    bare = [n for n, i in candidates.items() if not _addresses(i)]
    other = [f"{n} has {', '.join(_addresses(i))}" for n, i in candidates.items() if _addresses(i)]
    parts = []
    if bare:
        parts.append(f"{', '.join(bare)} {'is' if len(bare) == 1 else 'are'} up with no IPv4 address")
    if other:
        parts.append(f"{'; '.join(other)}, none of which is on {peer}'s subnet")
    return (". " + ". ".join(parts)) if parts else ""


# --- config ---------------------------------------------------------------------------

ROUTE_MAP_DEF_RE = re.compile(r"^route-map (?P<name>\S+) (?:permit|deny) \d+", re.MULTILINE)


def _config_finding(parsed: dict, running: str, ctx: dict) -> dict:
    peer = ctx.get("peer")

    undefined = _undefined_route_maps(running, peer)
    if undefined:
        name, direction = undefined[0]
        return {"decision": "root_cause", "fault_class": "route_filtered",
                "cause": f"Neighbor {peer} uses route-map {name} ({direction}), but no route-map {name} is "
                         f"defined: FRR then denies every route in that direction",
                "fix": f"Define route-map {name}, or point the neighbor at an existing one: router bgp <local-asn> / "
                       f"address-family ipv4 unicast / neighbor {peer} route-map <existing> {direction}"}

    if peer and re.search(rf"^\s*no neighbor {re.escape(peer)} activate\s*$", running, re.MULTILINE):
        return {"decision": "root_cause", "fault_class": "afi_not_negotiated",
                "cause": f"IPv4 unicast is not activated for neighbor {peer} (`no neighbor {peer} activate` under "
                         f"address-family ipv4 unicast): the neighbor drops out of the IPv4 `show bgp summary` and "
                         f"no IPv4 prefixes are exchanged",
                "fix": f"router bgp <local-asn> / address-family ipv4 unicast / neighbor {peer} activate, then "
                       f"re-apply the neighbor's route-maps in/out (FRR removes them on deactivate)."}

    misdirected = _misdirected_route_maps(running)
    if misdirected:
        neighbor, name, what = misdirected[0]
        return {"decision": "root_cause", "fault_class": "route_filtered",
                "cause": f"Route-map {name} sets {what} but is applied outbound to eBGP neighbor {neighbor}: "
                         f"{what} only takes effect on routes this router receives, so it has no effect here "
                         f"(applied in the wrong direction)",
                "fix": f"router bgp <local-asn> / address-family ipv4 unicast / no neighbor {neighbor} route-map "
                       f"{name} out / neighbor {neighbor} route-map {name} in"}

    lines = [l.strip() for l in running.splitlines() if peer and l.strip().startswith(f"neighbor {peer} ")]
    caps = [l for l in lines if l.endswith((" strict-capability-match", " dont-capability-negotiate"))]
    if caps and (ctx.get("_state") != "Established" or ctx.get("_state_reason") == "NoNeg"):
        return {"decision": "root_cause", "fault_class": "capability_mismatch",
                "cause": f"This router has `{caps[0]}` for {peer}: with a peer that doesn't offer the same "
                         f"capabilities (e.g. it has dont-capability-negotiate), the OPEN fails with Unsupported "
                         f"Capability or the session comes up with no address family negotiated",
                "fix": f"Remove `{caps[0]}` here (and `dont-capability-negotiate` on the peer, if set) so both sides "
                       f"negotiate the same capabilities."}

    filters = re.findall(rf"^\s*neighbor {re.escape(peer or '')} filter-list (\S+) (in|out)\s*$", running, re.MULTILINE)
    if filters and ctx.get("_route_missing"):
        name, direction = filters[0]
        return {"decision": "root_cause", "fault_class": "route_filtered",
                "cause": f"{ctx['_route_missing']} is missing and neighbor {peer} has AS-path filter-list {name} "
                         f"({direction}): routes whose AS-path matches a deny entry of `bgp as-path access-list {name}` "
                         f"are dropped",
                "fix": f"Check `show bgp as-path-access-list {name}`; narrow the deny entry or remove "
                       f"`neighbor {peer} filter-list {name} {direction}`."}

    session_tentative = None
    if any(" password " in f" {l} " for l in lines):
        session_tentative = {
            "fault_class": "auth_mismatch",
            "cause": f"A TCP-MD5 password is configured for {peer}. If the peer has no password or a different "
                     f"one, the kernel silently drops the TCP segments and the session stays Active/Connect",
            "fix": f"Configure the same `neighbor <addr> password <secret>` on both routers (or remove it on both)."}
    elif any(" ttl-security " in f" {l} " for l in lines):
        session_tentative = {
            "fault_class": "config_drift",
            "cause": f"ttl-security (GTSM) is configured for {peer}. If the peer does not send TTL 255, this "
                     f"router drops its packets and the session never forms",
            "fix": f"Configure `neighbor <addr> ttl-security hops <n>` on both routers, or remove it on both."}
    if ctx.get("_state") == "Established":
        session_tentative = None   # these only matter for a session that is down

    if parsed.get("has_baseline") and parsed.get("drifted"):
        severity = parsed.get("severity")
        if severity == "cosmetic":
            if ctx.get("_state") in (None, "Established") and not ctx.get("_tentative"):
                # nothing else is wrong: the drift itself is the answer, and it is harmless
                changes = [l.strip() for l in parsed.get("diff", [])
                           if l[:1] in "+-" and not l.startswith(("+++", "---"))]
                return {"decision": "healthy", "fault_class": "healthy",
                        "cause": "The config differs from the baseline only in descriptions/comments, which can't "
                                 "change BGP behaviour: not a fault" + (f" ({'; '.join(changes[:4])})" if changes else ""),
                        "fix": "No action needed. Save a new baseline if the change is intended."}
            return {"decision": "continue",
                    "hint": "The config differs from the baseline only in descriptions/comments: not a fault."}
        if severity != "none":   # "bgp", or None from an older config tool
            changes = parsed.get("relevant_changes") or [
                l for l in parsed.get("diff", []) if l[:1] in "+-" and not l.startswith(("+++", "---"))]
            shown = "; ".join(l.strip() for l in changes[:5]) + (" ..." if len(changes) > 5 else "")
            # the drift confirms a more specific finding rather than replacing it
            specific = session_tentative or ctx.get("_tentative")
            if specific and specific["fault_class"] not in ("healthy", "tcp_unreachable", "neighbor_missing"):
                return {"decision": "root_cause", **specific,
                        "cause": specific["cause"] + f". Changed since the baseline: {shown}"}
            return {"decision": "root_cause", "fault_class": "config_drift",
                    "cause": f"Running config has drifted from the baseline: {shown}",
                    "fix": "Review the diff and restore the correct BGP settings (lines starting '-' are the "
                           "baseline, '+' the running config)."}

    if peer and running and not any(l.strip().startswith(f"neighbor {peer} ") for l in running.splitlines()):
        return {"decision": "root_cause", "fault_class": "neighbor_missing",
                "cause": f"The running config has no `neighbor {peer}` lines: the neighbor is not configured "
                         f"on this router",
                "fix": f"router bgp <local-asn> / neighbor {peer} remote-as <peer-asn>"}

    out = {"decision": "continue", "hint": _neighbor_facts(running, peer)}
    if session_tentative:
        out["tentative"] = session_tentative
    return out


def _misdirected_route_maps(running: str) -> list[tuple[str, str, str]]:
    """(neighbor, route-map, attribute) for route-maps that set local-preference/weight outbound to eBGP peers."""
    local_as = next((l.split()[2] for l in running.splitlines()
                     if l.startswith("router bgp ") and len(l.split()) > 2), None)
    sets, current = {}, None
    for line in running.splitlines():
        m = ROUTE_MAP_DEF_RE.match(line)
        if m:
            current = m["name"]
        elif current and line.startswith((" ", "\t")):
            words = line.split()
            if words[:2] in (["set", "local-preference"], ["set", "weight"]):
                sets.setdefault(current, words[1])
        else:
            current = None
    remote = dict(re.findall(r"^\s*neighbor (\S+) remote-as (\S+)", running, re.MULTILINE))
    out = []
    for neighbor, name in re.findall(r"^\s*neighbor (\S+) route-map (\S+) out\s*$", running, re.MULTILINE):
        ebgp = remote.get(neighbor) not in (local_as, "internal")
        if name in sets and (ebgp or sets[name] == "weight"):
            out.append((neighbor, name, sets[name]))
    return out


def _undefined_route_maps(running: str, peer: str | None) -> list[tuple[str, str]]:
    if not (peer and running):
        return []
    defined = {m["name"] for m in ROUTE_MAP_DEF_RE.finditer(running)}
    refs = re.findall(rf"^\s*neighbor {re.escape(peer)} route-map (\S+) (in|out)\s*$", running, re.MULTILINE)
    return [(name, direction) for name, direction in refs if name not in defined]


# --- routes ---------------------------------------------------------------------------

def _value(path: dict, attr: str):
    v = path.get(attr)
    return 100 if attr == "local_pref" and v is None else v   # blank LocPrf = default 100


def _best_path_cause(p: dict) -> tuple[str, str | None]:
    """(sentence explaining the winner, deciding attribute or None)."""
    best, others = p["best_path"], p.get("paths_not_selected", [])
    prefix = p.get("prefix") or best.get("network")
    via = f"{best['next_hop']}" + (f" (from {best['from_peer']})" if best.get("from_peer") else "")
    reason = best.get("best_reason")
    attr, how = BEST_REASON_ATTR.get(reason, (None, None))
    if not others:
        return f"{prefix} has one path, via {via}; it is the best path because it is the only one", None
    text = f"The best path for {prefix} is via {via}"
    if reason:
        text += f": it wins on {reason}"
    if attr:
        losers = ", ".join(f"{_value(o, attr)} via {o['next_hop']}" for o in others)
        text += f" ({how}: {_value(best, attr)} vs {losers})"
    invalid = [o for o in others if o.get("valid") is False or o.get("next_hop_reachable") is False]
    multipath = [o for o in others if o.get("multipath")]
    text += f"; the other {len(others)} path(s) lose at that step of the best-path order"
    if multipath:
        text += f" ({len(multipath)} still installed as multipath)"
    if invalid:
        text += f"; {len(invalid)} path(s) are not valid (next hop inaccessible)"
    return text, attr


def _route_table_finding(p: dict, ctx: dict) -> dict:
    prefix = p.get("prefix")
    if p.get("in_table") is False:
        ctx["_route_missing"] = prefix
        return {"decision": "continue",
                "hint": (f"This router has no route for {prefix}: the peer doesn't advertise it, or an inbound "
                         f"route-map/prefix-list filters it. Check route_map: a deny entry with invoked > 0 points "
                         f"at filtering."),
                "tentative": {
                    "fault_class": "route_not_received",
                    "cause": f"This router has no route for {prefix} and no filter on it was found: the peer "
                             f"most likely does not advertise it",
                    "fix": f"On the peer, check `show ip bgp {prefix}` and its `network`/redistribute statement "
                           f"and outbound policy."}}
    best = p.get("best_path")
    paths = p.get("paths") or []
    unusable = _unusable_networks(paths)
    if (prefix and paths and not best) or (not prefix and unusable):
        bad = [x for x in paths if x.get("valid") is False or x.get("next_hop_reachable") is False]
        hops = ", ".join(sorted({x["next_hop"] for x in bad})) or "?"
        what = f"{prefix} is" if prefix else f"{', '.join(unusable)} {'is' if len(unusable) == 1 else 'are'}"
        return {"decision": "root_cause", "fault_class": "route_not_selected",
                "cause": f"{what} received but no path is valid: next hop {hops} is not reachable from this "
                         f"router (typical for iBGP without next-hop-self)",
                "fix": "On the router that passes the route over iBGP: router bgp <asn> / address-family ipv4 "
                       "unicast / neighbor <ibgp-peer> next-hop-self; or make the next hop reachable (IGP/static)."}
    peer = ctx.get("peer")
    if prefix and best and peer and any("from_peer" in x for x in paths) \
            and not any(peer in (x.get("from_peer"), x.get("next_hop")) for x in paths):
        # the question names a peer, and no path comes from it: that path is what's missing
        ctx["_route_missing"] = f"{prefix} from {peer}"
        text, _ = _best_path_cause(p)
        return {"decision": "continue",
                "hint": f"{text}. No path comes from {peer}: it doesn't advertise {prefix} to this router, or an "
                        f"inbound filter drops it. Check route_map, prefix_list and the config.",
                "tentative": {
                    "fault_class": "route_not_received",
                    "cause": f"This router has no path for {prefix} from {peer} ({text}) and no filter on this "
                             f"router explains it: {peer} does not send it (its outbound route-map/prefix-list "
                             f"drops it, or it does not have it), or the AS-path contains this router's own AS "
                             f"and loop prevention drops it",
                    "fix": f"On {peer}: `show ip bgp {prefix}`, `show ip bgp neighbors <this router> "
                           f"advertised-routes` and its outbound route-map/prefix-list; check the AS-path it sends "
                           f"for this router's AS."}}
    if prefix and best:
        text, attr = _best_path_cause(p)
        finding = {"fault_class": "route_not_selected" if p.get("paths_not_selected") else "route_as_intended",
                   "cause": text,
                   "fix": ("If a different path should win, change the deciding attribute (a route-map set clause, "
                           "neighbor weight, or the peer's outbound policy); otherwise this is as intended.")}
        if attr in ROUTE_MAP_SETS:
            # a route-map may have set the deciding attribute: let route_map_check name it
            ctx["_route_finding"] = {**finding, "attr": attr}
            return {"decision": "continue", "hint": text + ". Check route_map for a set clause on it.",
                    "tentative": finding}
        if p.get("paths_not_selected"):
            # decided on a tie-break: a policy that should have decided it may be missing or misapplied
            return {"decision": "continue", "hint": text + ". Check the route-maps and config for a policy "
                                                           "that was meant to decide this.",
                    "tentative": finding}
        return {"decision": "root_cause", **finding}
    return {"decision": "continue", "hint": _route_table_hint(p)}


def _unusable_networks(paths: list[dict]) -> list[str]:
    """Networks in a whole-table listing with paths but no usable (best) one."""
    nets = {}
    for x in paths:
        nets.setdefault(x.get("network"), []).append(x)
    return sorted(n for n, xs in nets.items() if n and not any(x["is_best"] for x in xs)
                  and any(x.get("valid") is False for x in xs))


def _route_map_finding(p: dict, ctx: dict) -> dict:
    maps = p.get("route_maps", [])
    ctx["_rm_prefix_lists"] = sorted({c.split()[-1] for m in maps for c in m.get("match_clauses", [])
                                      if "prefix-list" in c})
    denies = [m for m in maps if m["action"] == "deny" and m.get("invoked")]
    if denies:
        what = ", ".join(f"{m['name']} seq {m['sequence']} ({'; '.join(m['match_clauses']) or 'matches everything'}, "
                         f"{m['invoked']} match(es))" for m in denies)
        return {"decision": "root_cause", "fault_class": "route_filtered",
                "cause": f"Route-map deny entries are filtering routes: {what}",
                "fix": "If these routes should be accepted, narrow the deny entry's match clause, move it after "
                       "the permit, or remove it; if the filtering is intended this is route_as_intended."}

    no_catch_all = sorted({m["name"] for m in maps if m.get("match_clauses")}
                          - {m["name"] for m in maps if m["action"] == "permit" and not m.get("match_clauses")})
    implicit = (f"route-map(s) {', '.join(no_catch_all)} have no catch-all permit entry: routes that match none of "
                f"the entries are dropped by the implicit deny at the end")
    implicit_fix = ("Add a final `route-map <name> permit <seq>` with no match clause if other routes should pass, "
                    "or widen the match clause.")

    found = ctx.get("_route_finding")
    if found:
        words = ROUTE_MAP_SETS.get(found["attr"], ())
        setters = [f"{m['name']} seq {m['sequence']} sets {'; '.join(m['set_clauses'])}"
                   for m in maps if any(w in c for c in m.get("set_clauses", []) for w in words)]
        finding = {k: v for k, v in found.items() if k != "attr"}
        if setters:
            finding["cause"] += ". The deciding value comes from route-map " + ", ".join(setters)
            finding["fix"] = ("Change or remove that set clause (or the route-map on the neighbor) if a "
                              "different path should win; otherwise this is as intended.")
        else:
            finding["cause"] += (". No route-map on this router sets it: look at neighbor weight here, "
                                 "or the peer's outbound policy (e.g. AS-path prepend, MED)")
        if no_catch_all:
            finding["cause"] += f". Note: {implicit}"
        return {"decision": "root_cause", **finding}

    if no_catch_all:
        missing = ctx.get("_route_missing")
        return {"decision": "continue",
                "hint": f"Route-map(s) {', '.join(no_catch_all)} have no catch-all permit: a route matching none of "
                        f"their entries hits the implicit deny.",
                "tentative": {
                    "fault_class": "route_filtered",
                    "cause": (f"{missing} is missing and " if missing else "") + implicit,
                    "fix": implicit_fix}}
    return {"decision": "continue", "hint": _route_map_hint(p)}


def _prefix_list_finding(p: dict, ctx: dict) -> dict:
    prefix = p.get("prefix")
    evaluation = p.get("evaluation") or {}
    referenced = set(ctx.get("_rm_prefix_lists") or [])
    hits = []
    for name, ev in evaluation.items():
        if ev["action"] != "deny":
            continue
        if ev["implicit"] and name not in referenced:
            continue   # an unrelated list that simply doesn't mention the prefix
        hits.append((name, ev))
    if prefix and hits:
        name, ev = hits[0]
        how = (f"entry seq {ev['sequence']} denies it" if not ev["implicit"] else
               "no entry matches it, so the implicit deny drops it (an entry without le/ge only matches "
               "that exact prefix length)")
        return {"decision": "root_cause", "fault_class": "route_filtered",
                "cause": f"prefix-list {name} filters {prefix}: {how}",
                "fix": f"Fix prefix-list {name}: permit {prefix} (e.g. add `le <len>` to the covering entry) or "
                       f"remove the deny entry, then `clear bgp <peer> soft in`."}
    return {"decision": "continue"}


def _route_table_hint(p: dict) -> str:
    if p.get("in_table") is False:
        return (f"This router has no route for {p.get('prefix')}: the peer doesn't advertise it, or an inbound "
                f"route-map/prefix-list filters it. Check route_map: a deny entry with invoked > 0 points at filtering.")
    best = p.get("best_path")
    if not best:
        return "No best path in the table."
    others = p.get("paths_not_selected", [])
    if best.get("best_reason"):
        return (f"Best path via {best['next_hop']} wins on {best['best_reason']}; the other {len(others)} path(s) "
                f"lose at that step of the best-path order. If a route-map set the deciding attribute, check route_map.")
    return f"{p.get('path_count')} path(s). Pass a prefix to see why the best path wins."


def _route_map_hint(p: dict) -> str:
    denies = [f"{m['name']} seq {m['sequence']} ({'; '.join(m['match_clauses']) or 'matches everything'})"
              for m in p.get("route_maps", []) if m["action"] == "deny" and m.get("invoked")]
    sets = [f"{m['name']} seq {m['sequence']} sets {'; '.join(m['set_clauses'])}"
            for m in p.get("route_maps", []) if m["set_clauses"]]
    parts = []
    if denies:
        parts.append("Deny entries that matched routes: " + ", ".join(denies)
                     + ". Routes they match are filtered (route_filtered, or route_as_intended if that's the intended policy).")
    if sets:
        parts.append("Entries that change attributes: " + ", ".join(sets) + ".")
    return " ".join(parts) or "No route-map entry denies routes or changes attributes."


# --- bgp_neighbor ---------------------------------------------------------------------

def _neighbor_finding(p: dict, ctx: dict) -> dict:
    peer = ctx.get("peer")
    if not p.get("configured", True):
        return {"decision": "root_cause", "fault_class": "neighbor_missing",
                "cause": f"Neighbor {peer} is not configured on this router",
                "fix": f"router bgp <local-asn> / neighbor {peer} remote-as <peer-asn>"}
    if p.get("state") == "Established" and ctx.get("_state") == "unknown":
        return {"decision": "root_cause", "fault_class": "afi_not_negotiated",
                "cause": f"Neighbor {peer} is Established but missing from the IPv4 `show bgp summary`: IPv4 "
                         f"unicast is not activated for it, so no IPv4 prefixes are exchanged",
                "fix": f"router bgp <local-asn> / address-family ipv4 unicast / neighbor {peer} activate"}
    if p.get("state") == "Established" and ctx.get("_recent"):
        last = p.get("last_reset") or ""
        if "Hold Timer Expired" in last:
            return {"decision": "root_cause", "fault_class": "hold_timer_expired",
                    "cause": f"The session with {peer} is up again (for {ctx['_recent']}), but it was just torn down by "
                             f"Hold Timer Expired: keepalives are being lost (packet loss or latency on the link, or "
                             f"timers too short for it), so it keeps flapping",
                    "fix": f"Check the link for loss/latency (`ping -c 50 {peer}`); restore default timers "
                           f"(`no neighbor {peer} timers`) or raise them."}
        if "interface down" in last.lower() or "link down" in last.lower():
            return {"decision": "root_cause", "fault_class": "interface_down",
                    "cause": f"The session with {peer} is up again (for {ctx['_recent']}), but it was just reset because "
                             f"the interface towards it went down (Last reset: {last}): the link is unstable/flapping",
                    "fix": "Check the cabling and the far end of the peer-facing link, and whether its link-down "
                           "count (`show interface`) keeps growing."}
        return {"decision": "healthy", "fault_class": "healthy",
                "cause": f"BGP peer {peer} is Established, but it has only been up for {ctx['_recent']}: the session "
                         f"was reset moments ago and has recovered (Last reset: {last or 'unknown'})",
                "fix": "No action needed if the reset was intended (a manual clear or a peer restart). If not, run "
                       "bgp_state again in a few minutes: an Up/Down that keeps resetting means it is flapping."}
    if p.get("state") == "Established":
        return {"decision": "healthy", "fault_class": "healthy",
                "cause": f"BGP peer {peer} is Established",
                "fix": "No action needed — the session is up."}

    local_as, remote_as = p.get("local_as"), p.get("remote_as")
    me = p.get("local_host") or "<this router>"
    on_peer = f"on {p.get('hostname') or peer}"
    if p.get("admin_shutdown"):
        return {"decision": "root_cause", "fault_class": "neighbor_shutdown",
                "cause": f"Neighbor {peer} is administratively shut down on this router",
                "fix": f"router bgp {local_as} / no neighbor {peer} shutdown"}

    note = p.get("notification") or {}
    error, sent = note.get("error", ""), note.get("direction") == "sent"
    if "Bad Peer AS" in error and sent:
        actual = p.get("peer_open_as")
        return {"decision": "root_cause", "fault_class": "remote_as_mismatch",
                "cause": f"This router expects AS {remote_as} for {peer}"
                         + (f", but the peer uses AS {actual}" if actual else ", but the peer's OPEN has another AS"),
                "fix": f"router bgp {local_as} / neighbor {peer} remote-as {actual or '<peer-asn>'}"}
    if "Bad Peer AS" in error:
        return {"decision": "root_cause", "fault_class": "remote_as_mismatch",
                "cause": f"{peer} rejects this router's AS {local_as}: its remote-as for {me} is wrong",
                "fix": f"{on_peer}: router bgp {remote_as} / neighbor {me} remote-as {local_as}"}
    if "Peer De-configured" in error and not sent:
        return {"decision": "root_cause", "fault_class": "neighbor_missing",
                "cause": f"{peer} removed its neighbor config for this router",
                "fix": f"{on_peer}: router bgp {remote_as} / neighbor {me} remote-as {local_as}"}
    if "Administrative Shutdown" in error and not sent:
        return {"decision": "root_cause", "fault_class": "neighbor_shutdown",
                "cause": f"{peer} has shut down its neighbor for this router",
                "fix": f"{on_peer}: router bgp {remote_as} / no neighbor {me} shutdown"}
    if "Bad BGP Identifier" in error:
        rid = p.get("remote_router_id")
        if rid in (None, "0.0.0.0"):   # not learned while the session is down; ours is the duplicate
            rid = ctx.get("_router_id")
        return {"decision": "root_cause", "fault_class": "router_id_conflict",
                "cause": f"The OPEN from {peer} was rejected with Bad BGP Identifier: the two routers use the same "
                         f"BGP router-id" + (f" ({rid})" if rid else ""),
                "fix": "Give one of them a unique router-id: router bgp <asn> / bgp router-id <unique-id>"}
    if "Unsupported Capability" in error:
        return {"decision": "root_cause", "fault_class": "capability_mismatch",
                "cause": f"The OPEN exchange with {peer} fails on capabilities (Unsupported Capability, "
                         f"{'sent by this router' if sent else 'sent by the peer'}): one side requires a capability "
                         f"the other doesn't offer (strict-capability-match / dont-capability-negotiate)",
                "fix": f"Remove `neighbor ... strict-capability-match` or `dont-capability-negotiate` so both "
                       f"sides negotiate the same capabilities."}
    if "Hold Timer Expired" in error:
        return {"decision": "root_cause", "fault_class": "hold_timer_expired",
                "cause": f"The session with {peer} keeps being torn down because no KEEPALIVE arrives within the "
                         f"hold time (Hold Timer Expired): high latency or packet loss on the link, or timers set "
                         f"too short for it",
                "fix": f"Check the link for loss/latency (`ping -c 20 {peer}`); restore default timers "
                       f"(`no neighbor {peer} timers`) or raise them."}
    if "Maximum Number of Prefixes Reached" in error:
        return {"decision": "root_cause", "fault_class": "max_prefix_exceeded",
                "cause": f"{peer} sent more prefixes than the maximum-prefix limit allows, so the session was "
                         f"torn down",
                "fix": f"router bgp {local_as} / address-family ipv4 unicast / neighbor {peer} maximum-prefix "
                       f"<higher limit>, then `clear bgp {peer}`"}

    if p.get("connections_established") == 0:
        return {"decision": "continue",
                "hint": f"State {p.get('state')} and the TCP session has never come up (0 connections), "
                        f"so the fault is below BGP: check the interface to the peer and TCP port 179. "
                        f"'Waiting for peer OPEN' only means no OPEN ever arrived."}
    return {"decision": "continue",
            "hint": f"State {p.get('state')}; the session came up {p.get('connections_established', '?')} "
                    f"time(s) and dropped {p.get('connections_dropped', '?')}. "
                    f"Last reset: {p.get('last_reset') or 'never'}."}


def _neighbor_facts(running: str, peer: str | None) -> str:
    if not peer:
        return "No peer given, so the config's neighbor lines weren't checked."
    lines = [l.strip() for l in running.splitlines() if l.strip().startswith(f"neighbor {peer} ")]
    local_as = next((l.split()[2] for l in running.splitlines()
                     if l.startswith("router bgp ") and len(l.split()) > 2), None)
    if not lines:
        return (f"The running config has no `neighbor {peer}` lines: the neighbor is not configured "
                f"on this router (fault_class neighbor_missing).")
    remote_as = next((l.split()[3] for l in lines if " remote-as " in f" {l} " and len(l.split()) > 3), None)
    shut = any(l == f"neighbor {peer} shutdown" for l in lines)
    facts = [f"This router (local AS {local_as or '?'}) configures neighbor {peer}"
             + (f" with remote-as {remote_as}" if remote_as else " without a remote-as")]
    facts.append("and it IS shut down (`neighbor ... shutdown`)" if shut
                 else "and it is NOT shut down (no `shutdown` line)")
    hint = " ".join(facts) + "."
    extras = [w for w in ("password", "ttl-security", "ebgp-multihop", "update-source", "timers",
                          "maximum-prefix", "local-as") if any(f" {w} " in f" {l} " for l in lines)]
    if extras:
        hint += f" It also sets: {', '.join(extras)} (these must match the peer's side)."
    if shut:
        hint += (f" FRR fix on this router: `router bgp {local_as or '<local-asn>'}` then "
                 f"`no neighbor {peer} shutdown`.")
    elif remote_as:
        hint += (f" A remote-as mismatch shows up as a Bad Peer AS NOTIFICATION in bgp_neighbor; without one, "
                 f"remote-as {remote_as} is not the cause.")
    return hint


# --- the rule chain ---------------------------------------------------------------------

def _args_for(registry: ToolRegistry, tool_id: str, ctx: dict) -> dict:
    """Arguments the rule chain can fill in from the question (the LLM sets them in agent mode)."""
    schema = registry.tools.get(tool_id, {}).get("args_schema", {})
    args = {}
    if "prefix" in schema and ctx.get("prefix"):
        args["prefix"] = ctx["prefix"]
    return args


def diagnose(client, question: str, host: str, peer: str,
             use_ml: bool = True, use_llm: bool = True) -> Verdict:
    route = route_context(question)
    route_question = route["route_question"]
    # a route question still starts from the session: routes mean nothing on a down session
    intent = "bgp_state_check" if route_question else triage(question)
    log.info("Triage: starting intent %s (route question: %s, route intent %s, prefix %s)",
             intent, route_question, route["route_intent"], route["prefix"])
    ctx = {"host": host, "peer": peer, "question": question, **route}
    registry = ToolRegistry.load(client)
    checked, evidence = [], []
    visited, ran = set(), set()
    tentative = None

    while intent and intent not in visited:
        visited.add(intent)
        rule = client.get_rule(intent)
        next_intent = rule.get("next_intent_on_fail")
        log.info("Rule %s: tools %s, next intent on fail %s", intent,
                 [t["tool_id"] for t in rule["tools"]], next_intent)
        for tool in rule["tools"]:
            tool_id = tool["tool_id"]
            args = _args_for(registry, tool_id, ctx)
            key = (tool_id, json.dumps(args, sort_keys=True))
            if key in ran:
                log.info("Tool %s already ran in this diagnosis; not running it again", tool_id)
                continue
            ran.add(key)
            result = registry.execute(client, tool_id, args, ctx, evidence)
            if result.get("success", True):
                log.info("Tool %s succeeded", tool_id)
            else:
                log.warning("Tool %s failed: %s", tool_id, result.get("error"))
            log.debug("Tool %s parsed output: %s", tool_id,
                      json.dumps(result.get("parsed"), sort_keys=True, default=str))
            log.debug("Tool %s raw output:\n%s", tool_id, result.get("raw_output"))
            checked.append(tool_id)
            evidence.append({
                "tool": tool_id,
                "success": result.get("success"),
                "error": result.get("error"),
                "parsed": result.get("parsed"),
                "raw_output": result.get("raw_output"),
            })

            outcome = evaluate(tool_id, result, ctx)
            log.info("Rule decision after %s: %s%s", tool_id, outcome["decision"],
                     f" ({outcome['cause']})" if "cause" in outcome else "")
            if outcome["decision"] in ("root_cause", "healthy"):
                return _rules_verdict(outcome, checked, evidence, peer, use_ml)
            if outcome.get("tentative"):
                tentative = outcome["tentative"]
                ctx["_tentative"] = tentative
                log.info("Tentative finding after %s: %s", tool_id, tentative["fault_class"])
            if outcome.get("suggested_next_intent"):
                next_intent = outcome["suggested_next_intent"]
                log.info("Result of %s points at intent %s", tool_id, next_intent)
        intent = next_intent   # escalate
        if intent:
            log.info("Rule unresolved; escalating to intent %s", intent)

    log.info("Rule chain exhausted without a root cause")
    if tentative:
        log.info("Concluding on the tentative finding %s", tentative["fault_class"])
        verdict = _rules_verdict({"decision": "root_cause", **tentative}, checked, evidence, peer, use_ml)
        verdict.notes.append("No rule was conclusive; this is the most specific finding the chain made")
        return verdict

    # Rule chain exhausted -> ML engine
    ml = ml_engine.predict(evidence, peer) if use_ml else None
    if not ml:
        log.info("ML engine skipped (--no-ml)")
    elif _ml_contradicts(ml.label, ctx):
        log.info("ML engine said %s (%.2f), but the chain saw the session %s%s; not using it",
                 ml.label, ml.confidence, ctx.get("_state"), " on a route question" if route_question else "")
    elif ml.label != "unknown" and ml.confidence >= ML_CONFIDENCE_THRESHOLD:
        log.info("ML engine decided: %s (%.2f >= threshold %.2f)",
                 ml.label, ml.confidence, ML_CONFIDENCE_THRESHOLD)
        cause, fix = ml.describe(peer)
        return Verdict(True, cause, fix, checked, evidence, source="ml", fault_class=ml.label,
                       confidence=f"{ml.confidence:.2f}", ml_prediction=ml.to_dict())
    else:
        log.info("ML engine unsure: %s (%.2f, threshold %.2f)",
                 ml.label, ml.confidence, ML_CONFIDENCE_THRESHOLD)

    # ML unsure -> LLM
    verdict = Verdict(False, "No root cause found by the rule chain.",
                      "Escalate to a human.", checked, evidence,
                      ml_prediction=ml.to_dict() if ml else None)
    if not use_llm:
        log.info("LLM escalation skipped (--no-llm)")
        return verdict
    try:
        llm = llm_escalation.escalate(question, host, peer, checked, evidence,
                                      verdict.ml_prediction)
    except llm_escalation.LLMUnavailable as e:
        log.warning("LLM escalation skipped: %s", e)
        verdict.notes.append(f"LLM escalation skipped: {e}")
        return verdict
    log.info("LLM decided: resolved=%s confidence=%s root cause: %s",
             llm["resolved"], llm["confidence"], llm["root_cause"])

    return Verdict(llm["resolved"], llm["root_cause"], llm["suggested_fix"], checked, evidence,
                   source="llm", confidence=llm["confidence"],
                   ml_prediction=verdict.ml_prediction, next_checks=llm["next_checks"],
                   notes=[f"Diagnosed by {llm['model']}"])


def _ml_contradicts(label: str, ctx: dict) -> bool:
    """A "healthy" guess can't stand once the chain saw a session that is not up, or a route problem."""
    if label != "healthy":
        return False
    return ctx.get("_state") not in (None, "Established") or bool(ctx.get("route_question"))


def _rules_verdict(outcome: dict, checked: list, evidence: list, peer: str, use_ml: bool) -> Verdict:
    verdict = Verdict(True, outcome["cause"], outcome["fix"], checked, evidence,
                      source="rules", fault_class=outcome["fault_class"])
    if use_ml:   # second opinion, shown alongside the rule's answer
        ml = ml_engine.predict(evidence, peer)
        log.info("ML second opinion: %s (%.2f)", ml.label, ml.confidence)
        verdict.ml_prediction = ml.to_dict()
    return verdict
