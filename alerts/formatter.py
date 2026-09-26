"""Render alerts as plain-language text or a technical terminal banner."""

from __future__ import annotations

import sys
from alerts.models import Alert, AlertSeverity

_COLOURS = {
    AlertSeverity.INFO:     "\033[94m",   # blue
    AlertSeverity.WARNING:  "\033[93m",   # yellow
    AlertSeverity.CRITICAL: "\033[91m",   # red
    AlertSeverity.RECOVERY: "\033[92m",   # green
}
_RESET = "\033[0m"
_BOLD  = "\033[1m"

_WIDTH = 60  # banner width


def _colour_enabled() -> bool:
    """True when stdout supports ANSI escapes."""
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


def _line(content: str = "", pad: str = " ") -> str:
    """Format a single banner line padded to _WIDTH."""
    inner = f" {content} " if content else ""
    return f"║{inner:<{_WIDTH - 2}}║"


def _separator() -> str:
    return "╠" + "═" * (_WIDTH - 2) + "╣"


def _top() -> str:
    return "╔" + "═" * (_WIDTH - 2) + "╗"


def _bottom() -> str:
    return "╚" + "═" * (_WIDTH - 2) + "╝"


def format_plain_english(alert: Alert) -> str:
    """Return a short explanation suitable for operators and faculty."""
    from alerts.models import AlertType, AlertSeverity

    if alert.severity == AlertSeverity.RECOVERY:
        return (
            f"Good news! The BGP session to {alert.neighbor} on {alert.device} "
            f"has recovered and is now ESTABLISHED. "
            f"No further action is needed — but keep watching it for a few minutes to make sure it stays stable."
        )

    if alert.alert_type == AlertType.INTERFACE:
        interfaces = alert.evidence.get("interfaces", {})
        down_ifaces = [
            name for name, info in interfaces.items()
            if info.get("link_state", "up").lower() == "down"
            or info.get("admin_state", "up").lower() == "down"
        ]
        iface_str = ", ".join(down_ifaces) if down_ifaces else "an interface"
        return (
            f"Your interface state is down (interface: {iface_str}). "
            f"That is why your BGP state is stuck at {alert.current_state or 'unknown'}. "
            f"Please bring the interface up to resolve the issue."
        )

    if alert.alert_type == AlertType.TCP:
        port = alert.evidence.get("tcp_port", 179)
        return (
            f"Your BGP peer {alert.neighbor} is not reachable on TCP port {port}. "
            f"That is why your BGP session is stuck at {alert.current_state or 'unknown'}. "
            f"Please check your firewall or routing rules to allow TCP port {port} to the peer."
        )

    if alert.alert_type == AlertType.CONFIG:
        return (
            f"Your router configuration on {alert.device} does not match the saved baseline. "
            f"This may be causing or contributing to the BGP problem. "
            f"Please review and restore the correct BGP settings."
        )

    bgp_state = alert.current_state or "unknown"
    return (
        f"Your BGP session to {alert.neighbor} is stuck at {bgp_state} state. "
        f"The interface and TCP port appear to be OK, so this is likely a configuration mismatch "
        f"(wrong AS number, wrong neighbor IP, or authentication key). "
        f"Please compare the BGP configuration on both routers."
    )


def format_alert(alert: Alert, use_colour: bool | None = None) -> str:
    """Return a detailed evidence-backed terminal banner."""
    colour = use_colour if use_colour is not None else _colour_enabled()

    sev = alert.severity.value
    alert_type = alert.alert_type.value.replace("_", " ")

    heading = f"[{sev}] BGP {alert_type}"

    def _wrap(text: str) -> str:
        if colour:
            c = _COLOURS.get(alert.severity, "")
            return f"{_BOLD}{c}{text}{_RESET}"
        return text

    lines: list[str] = [
        _top(),
        _line(_wrap(f"  {heading}")),
        _separator(),
        _line(f"  Alert ID  : {alert.alert_id}"),
        _line(f"  Timestamp : {alert.timestamp}"),
        _line(f"  Device    : {alert.device}"),
        _line(f"  Neighbor  : {alert.neighbor}"),
    ]

    if alert.current_state:
        lines.append(_line(f"  BGP State : {alert.current_state.upper()}"))
    if alert.previous_state:
        lines.append(_line(f"  Prev State: {alert.previous_state.upper()}"))

    interfaces: dict = alert.evidence.get("interfaces", {})
    for iface_name, iface_info in interfaces.items():
        link = iface_info.get("link_state", "?").upper()
        admin = iface_info.get("admin_state", "?").upper()
        ip = iface_info.get("ip_address") or "no IP"
        lines.append(_line(f"  Interface : {iface_name}  link={link}  admin={admin}  ip={ip}"))

    if "tcp_reachable" in alert.evidence:
        reachable = alert.evidence["tcp_reachable"]
        port = alert.evidence.get("tcp_port", 179)
        status = "REACHABLE" if reachable else "UNREACHABLE"
        lines.append(_line(f"  TCP/179   : {status} (port {port})"))

    if "config_drifted" in alert.evidence:
        drifted = alert.evidence["config_drifted"]
        lines.append(_line(f"  Config    : {'DRIFTED from baseline' if drifted else 'matches baseline'}"))

    if alert.message:
        lines += [
            _separator(),
            _line("  What happened?"),
            *_wrap_text(alert.message, prefix="  "),
        ]

    if alert.cause:
        lines += [
            _separator(),
            _line("  Cause"),
            *_wrap_text(alert.cause, prefix="  "),
        ]

    if alert.recommended_action:
        lines += [
            _separator(),
            _line("  Recommended Action"),
            *_wrap_text(alert.recommended_action, prefix="  "),
        ]

    lines.append(_bottom())
    return "\n".join(lines)


def _wrap_text(text: str, prefix: str = "  ", max_width: int = _WIDTH - 4) -> list[str]:
    """
    Word-wrap text to fit inside the banner and return formatted lines.
    """
    words = text.split()
    result_lines: list[str] = []
    current = prefix
    for word in words:
        if len(current) + len(word) + 1 > max_width:
            result_lines.append(_line(current))
            current = prefix + word
        else:
            current = (current + " " + word).rstrip() if current.strip() else prefix + word
    if current.strip():
        result_lines.append(_line(current))
    return result_lines
