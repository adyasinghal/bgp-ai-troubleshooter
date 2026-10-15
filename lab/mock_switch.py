"""
Mock REST switch — a stand-in device that speaks the same "CLI over REST" shape
as a real switch (Arista eAPI / Cisco NX-API style), so the REST transport can
be demoed without a physical Aruba box or a simulator.

It answers exactly one endpoint:

    POST /command   body: {"command": "show bgp summary", "kind": "vtysh"}
                    reply: {"success": true, "output": "<canned CLI text>"}

The canned output is in the same format FRR's vtysh prints, so the EXISTING
tool parsers (bgp_state, interface, config, tcp_port) read it with zero changes
-- which is the point being demonstrated: swap the transport, keep everything
above it.

Run it (defaults to http on port 9000, matching the demo env vars):

    python3 lab/mock_switch.py
"""

from fastapi import FastAPI
from pydantic import BaseModel
import uvicorn

app = FastAPI(title="Mock REST Switch")

# --- canned device output, in FRR vtysh format -----------------------------

BGP_SUMMARY = """\
IPv4 Unicast Summary (VRF default):
BGP router identifier 172.20.20.2, local AS number 65001 vrf-id 0
BGP table version 0
RIB entries 0, using 0 bytes of memory
Peers 1, using 20 KiB of memory

Neighbor        V         AS   MsgRcvd   MsgSent   TblVer  InQ OutQ  Up/Down State/PfxRcd   PfxSnt Desc
172.20.20.3     4      65002        12        12        0    0    0 00:05:11            0        0 N/A

Total number of neighbors 1
"""

INTERFACE_DETAIL = """\
Interface eth0 is up, line protocol is up
  administratively up
  inet 172.20.20.2/24
Interface eth1 is up, line protocol is up
  administratively up
  inet 10.0.0.1/30
"""

RUNNING_CONFIG = """\
Building configuration...

frr version 8.5.2
frr defaults traditional
!
router bgp 65001
 neighbor 172.20.20.3 remote-as 65002
!
end
"""


class Command(BaseModel):
    command: str
    kind: str = "vtysh"


@app.post("/command")
def run_command(cmd: Command):
    c = cmd.command.lower()

    # transport-layer check (the tcp_port tool sends this as a raw command)
    if cmd.kind == "raw" and "/dev/tcp" in cmd.command:
        return {"success": True, "output": "REACHABLE", "error": None}

    if "show bgp summary" in c:
        return {"success": True, "output": BGP_SUMMARY, "error": None}
    if "show interface" in c:
        return {"success": True, "output": INTERFACE_DETAIL, "error": None}
    if "show running-config" in c:
        return {"success": True, "output": RUNNING_CONFIG, "error": None}

    # unknown command -> succeeds with empty output, like a real device would
    return {"success": True, "output": "", "error": None}


@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=9000)
