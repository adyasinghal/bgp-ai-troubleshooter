# Setup Guide: Running BGP Commands on OrbStack Using Containerlab and FRR

FRR: It's an open-source routing suite — the software that actually speaks BGP, OSPF, IS-IS, and PIM. Think of it as the routing brain that vendors like Cisco or Juniper wrap in their own OS; FRR is that brain, standalone and free.

Containerlab: This tool provides a lightweight, container-based approach to orchestrate and spin up complex multi-router network topologies instantly.

OrbStack: Since you are developing on a Mac, OrbStack serves as a fast, resource-efficient container runtime engine that executes these Docker-based labs seamlessly.

macOS (Darwin, arm64)  
└── OrbStack VM: Ubuntu arm64  ← real Linux kernel  
    ├── dockerd  
    │   ├── container: leaf1  (FRR process)  
    │   ├── container: spine1 (FRR process)  
    │   └── ...  
    └── containerlab (a CLI, not a daemon)  

**Commands:**

**Install (macOS side)**

\# If you don't have homebrew

/bin/bash -c "\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

```bash
brew install --cask orbstack

orb create ubuntu clab

orb -m clab           # drops you into a shell inside the VM
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

\# Inside the VM

cd /Users/adyasinghal/HPE-CPP/network-lab

\# Create a YAML file named `topology.clab.yml` to define two connected routers running FRR:

```yaml
# to define two connected routers running FRR

name: bgp-lab
topology:
  nodes:
    router1:
      kind: linux
      image: quay.io/frrouting/frr:8.5.2
    router2:
      kind: linux
      image: quay.io/frrouting/frr:8.5.2
  links:
    - endpoints: ["router1:eth1", "router2:eth1"]

```

\# Deploy the lab

```bash
sudo containerlab deploy -t topology.clab.yml
```

\# Access the FRR CLI

```bash
docker exec -it clab-bgp-lab-router1 vtysh
```

\# Breakdown of command

⚬ docker exec: Tells Docker to execute a command inside a container that is already running.

⚬ -it: Stands for interactive and tty. It allocates a pseudo-TTY and keeps STDIN open so you can type commands and see live output.

⚬ clab-bgp-lab-router1: The specific name of the Docker container created by containerlab for router1 in your lab topology.

⚬ vtysh: The FRRouting integrated shell (zebra/BGP/OSPF shell) that lets you type routing commands like show ip bgp summary just as you would on a physical Cisco or Arista switch.

\# Enable the BGP daemon and restart

```bash
docker exec -it clab-bgp-lab-router1 sed -i 's/bgpd=no/bgpd=yes/' /etc/frr/daemons  

docker restart clab-bgp-lab-router1
```

\# Initialize a BGP router process

```bash
docker exec -it clab-bgp-lab-router1 vtysh
```

\# Insider router1 prompt enter:

```bash
configure terminal      # Your prompt should change to router1(config)#

router bgp 65001        # Your prompt should change to router1(config-router)#

neighbor 192.168.139.63 remote-as 65002     # set remote peer

end         # To go back to main privilege mode

show ip bgp summary

show ip bgp neighbors

show ip bgp statistics

show ip bgp
```
