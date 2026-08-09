#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this script as root." >&2
    exit 1
fi

. /etc/os-release
if [ "${ID:-}" != "ubuntu" ]; then
    echo "This installer supports Ubuntu only." >&2
    exit 1
fi

conflicts="$(
    dpkg-query -W -f='${binary:Package} ${db:Status-Abbrev}\n' \
        docker.io docker-compose docker-compose-v2 docker-doc docker-buildx \
        podman-docker containerd runc 2>/dev/null \
    | awk '$2 ~ /^ii/ {print $1}' \
    || true
)"
if [ -n "$conflicts" ]; then
    echo "Conflicting packages are installed; review them before continuing:" >&2
    echo "$conflicts" >&2
    exit 1
fi

apt-get update
apt-get install -y ca-certificates curl
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc

architecture="$(dpkg --print-architecture)"
codename="${UBUNTU_CODENAME:-$VERSION_CODENAME}"
cat > /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $codename
Components: stable
Architectures: $architecture
Signed-By: /etc/apt/keyrings/docker.asc
EOF

apt-get update
apt-get install -y \
    docker-ce docker-ce-cli containerd.io docker-buildx-plugin \
    docker-compose-plugin

systemctl enable --now docker
docker version --format 'Docker Engine {{.Server.Version}}'
docker compose version
