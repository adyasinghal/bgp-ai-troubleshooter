FROM quay.io/frrouting/frr:8.5.2

# Turn the plain FRR routing container into something that behaves like a real
# switch: reachable over SSH on port 22 as admin/admin, with the BGP daemon
# enabled at boot. Everything is baked into the image ONCE, so you never have
# to add an SSH server to a running container by hand again.

USER root

RUN set -eux; \
    # 1. Install an SSH server (works whether the FRR image is Alpine- or Debian-based)
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
