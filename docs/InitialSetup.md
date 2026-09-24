# Setup Guide: Running BGP Commands on OrbStack Using Containerlab and FRR

FRR: It's an open-source routing suite — the software that actually speaks BGP, OSPF, IS-IS, and PIM. Think of it as the routing brain that vendors like Cisco or Juniper wrap in their own OS; FRR is that brain, standalone and free.

Containerlab: This tool provides a lightweight, container-based approach to orchestrate and spin up complex multi-router network topologies instantly.

OrbStack: Since you are developing on a Mac, OrbStack serves as a fast, resource-efficient container runtime engine that executes these Docker-based labs seamlessly.

macOS (Darwin, arm64)  
└── OrbStack VM: Ubuntu arm64  ← real Linux kernel  
    ├── dockerd  
    │   ├── container: leaf1  (FRR process)  
    │   ├── container: spine1 (FRR process)  
    │   └── ...  
    └── containerlab (a CLI, not a daemon)  

**Commands:**

**Install (macOS side)**

\# If you don't have homebrew

/bin/bash -c "\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

```bash
brew install --cask orbstack

orb create ubuntu clab

orb -m clab     # drops you into a shell inside the VM
```

**Install (inside the clab VM)**

```bash
sudo apt update && sudo apt install -y curl ca-certificates git python3-yaml
```

\# Docker engine

```bash
curl -fsSL https://get.docker.com | sudo bash

sudo usermod -aG docker $USER
```

\# Containerlab

```bash
sudo bash -c "$(curl -sL https://get.containerlab.dev)"

containerlab version

docker run --rm quay.io/frrouting/frr:10.4.1 uname -m
```

\# To log out of docker

```bash
exit
```

\# Re-enter

```bash
orb -m clab
```
---

### Building the router image (FRR + SSH)

The plain FRR image is only the routing brain — it has no SSH server, so the REST tool (which connects over SSH) can't reach it. We fix this once by building a custom image that bundles FRR with an SSH server, an `admin`/`admin` login, and the BGP daemon enabled at boot. This is baked into the image, so you never have to add SSH to a running container by hand.

\# Inside the VM

cd /Users/adyasinghal/HPE-CPP/bgp-ai-troubleshooter


\# Create a file named `Dockerfile` in this folder:

```dockerfile
FROM quay.io/frrouting/frr:8.5.2

# Turn the plain FRR routing container into something that behaves like a real
# switch: reachable over SSH on port 22 as admin/admin, with the BGP daemon
# enabled at boot. Everything is baked into the image ONCE.

USER root

RUN set -eux; \
    # 1. Install an SSH server (works on Alpine- or Debian-based FRR images)
    if command -v apk >/dev/null 2>&1; then \
        apk add --no-cache openssh shadow; \
    elif command -v apt-get >/dev/null 2>&1; then \
        apt-get update && apt-get install -y --no-install-recommends openssh-server passwd && rm -rf /var/lib/apt/lists/*; \
    fi; \
    # 2. Generate SSH host keys and prepare the runtime directory
    ssh-keygen -A; \
    mkdir -p /var/run/sshd; \
    # 3. Create the 'admin' login the tool connects as, with password 'admin'
    (adduser -D admin 2>/dev/null || useradd -m -s /bin/sh admin); \
    echo "admin:admin" | chpasswd; \
    # 4. Let 'admin' run vtysh (needs membership in the frr + frrvty groups)
    (addgroup admin frr 2>/dev/null || usermod -aG frr admin); \
    (addgroup admin frrvty 2>/dev/null || usermod -aG frrvty admin); \
    # 5. Enable the BGP daemon so it is already running at boot
    sed -i 's/^bgpd=no/bgpd=yes/' /etc/frr/daemons
```

\# Build the image (run once; re-run only if you edit the Dockerfile)

```bash
docker build -t frr-ssh:8.5.2 .
```

---

### Defining and deploying the lab

\# Create a YAML file named `topology.clab.yml` to define two connected routers
built from our custom image. The `exec` line starts the SSH server after each
container boots.

```yaml
name: bgp-lab
topology:
  nodes:
    router1:
      kind: linux
      image: frr-ssh:8.5.2
      exec:
        - /usr/sbin/sshd
    router2:
      kind: linux
      image: frr-ssh:8.5.2
      exec:
        - /usr/sbin/sshd
  links:
    - endpoints: ["router1:eth1", "router2:eth1"]
```

\# Deploy the lab

```bash
sudo containerlab deploy -t topology.clab.yml
```

---

### Accessing the routers

There are two ways to reach a router. SSH is what the REST tool uses; docker exec is a handy manual shortcut.

\# Over SSH (credentials: admin / admin). Quote the whole remote command:

```bash
ssh admin@172.20.20.2 'vtysh -c "show bgp summary"'
```

\# Via docker exec (no SSH needed, for quick manual checks):

```bash
docker exec -it clab-bgp-lab-router1 vtysh
```

\# Breakdown of the docker exec command

⚬ docker exec: Tells Docker to run a command inside a container that is already running.

⚬ -it: interactive + tty, so you can type commands and see live output.

⚬ clab-bgp-lab-router1: the container name Containerlab created for router1.

⚬ vtysh: the FRRouting integrated shell that lets you type routing commands like `show ip bgp summary` just as you would on a physical Cisco or Arista switch.

> Note: the BGP daemon (bgpd) is already enabled in the image

---

### Configuring BGP

Neighbor configuration is not saved across a redeploy, so set it after each deploy. Use the current management IPs (router1 peers with router2 and vice versa). With router1 = 172.20.20.2 and router2 = 172.20.20.3:

```bash
docker exec clab-bgp-lab-router1 vtysh -c "conf t" -c "router bgp 65001" -c "neighbor 172.20.20.3 remote-as 65002" -c "end"

docker exec clab-bgp-lab-router2 vtysh -c "conf t" -c "router bgp 65002" -c "neighbor 172.20.20.2 remote-as 65001" -c "end"
```

\# Verify the session (wait a few seconds; it should reach Established):

```bash
ssh admin@172.20.20.2 'vtysh -c "show bgp summary"'

ssh admin@172.20.20.2 'vtysh -c "show bgp neighbors"'
```

(Here 172.20.20.2 is router1 from your last deploy — use whatever IP the deploy table showed.) Or open a full interactive session and type freely:
```bash
ssh admin@172.20.20.2
vtysh
show ip bgp summary
```

