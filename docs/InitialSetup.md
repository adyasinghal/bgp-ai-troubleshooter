# Setup Guide: Running BGP Commands on OrbStack Using Containerlab and FRR

One-time setup. For running the lab and the troubleshooter each session, see `GuideToRun.md`.

FRR: It's an open-source routing suite — the software that actually speaks BGP, OSPF, IS-IS, and PIM. Think of it as the routing brain that vendors like Cisco or Juniper wrap in their own OS; FRR is that brain, standalone and free.

Containerlab: This tool provides a lightweight, container-based approach to orchestrate and spin up complex multi-router network topologies instantly.

OrbStack: Since you are developing on a Mac, OrbStack serves as a fast, resource-efficient container runtime engine that executes these Docker-based labs seamlessly.

macOS (Darwin, arm64)  
├── Ollama (local LLM)  
└── OrbStack VM: Ubuntu arm64  ← real Linux kernel  
    ├── dockerd  
    │   ├── container: router1 (FRR process)  
    │   └── container: router2 (FRR process)  
    └── containerlab (a CLI, not a daemon)  

## Install (macOS side)

\# If you don't have homebrew

/bin/bash -c "\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

```bash
brew install --cask orbstack

orb create ubuntu clab

orb -m clab     # drops you into a shell inside the VM
```

## Install (inside the clab VM)

```bash
sudo apt update && sudo apt install -y curl ca-certificates git python3-pip python3-yaml
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

docker run --rm quay.io/frrouting/frr:8.5.2 uname -m
```

\# Log out and back in so the docker group applies

```bash
exit
orb -m clab
```

\# Python packages (from the repo root; re-run whenever `requirements.txt` changes)

```bash
cd /Users/adyasinghal/HPE-CPP/bgp-ai-troubleshooter
python3 -m pip install -r requirements.txt --break-system-packages
```

---

## Building the router image (FRR + SSH)

The plain FRR image is only the routing brain — it has no SSH server, so the REST tool (which connects over SSH) can't reach it. `lab/containerlab/Dockerfile` builds a custom image that bundles FRR with an SSH server, an `admin`/`admin` login, and the BGP daemon enabled at boot.

```bash
cd lab/containerlab
docker build -t frr-ssh:8.5.2 .
cd ../..
```

Re-run this only if you edit the Dockerfile. A rebuild doesn't reach containers that are already running, so destroy the lab first and the next deploy recreates them:

```bash
sudo containerlab destroy -t lab/containerlab/topology.clab.yml
```

The lab itself is defined in `lab/containerlab/topology.clab.yml`: two routers built from this image, with pinned management IPs (router1 = 172.20.20.2, router2 = 172.20.20.3) and an `eth1` link between them. Deploying it is Step 2 of `GuideToRun.md`.

---

## Ollama (on the Mac)

The troubleshooter's LLM is a free local model served by Ollama. It runs on the Mac, not in the VM, so it can use the Apple GPU. In a normal macOS terminal (not `orb -m clab`):

```bash
brew install ollama
brew services start ollama          # runs the Ollama server now and at login
ollama pull qwen2.5:7b              # the default model
ollama run qwen2.5:7b "say hi"
```

(Or install the Ollama app from ollama.com and open it instead of `brew services start`.)

---

## Accessing the routers

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
