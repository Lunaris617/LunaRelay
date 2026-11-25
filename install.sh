#!/usr/bin/env bash
set -euo pipefail
umask 077
cd "$(dirname "$(realpath "$0")")"

domain="" ip="" admin_port=""
while (( $# )); do
    case "$1" in
        --domain) domain="${2:?Missing domain}"; shift 2 ;;
        --ip) ip="${2:?Missing public IP}"; shift 2 ;;
        --admin-port) admin_port="${2:?Missing administration port}"; shift 2 ;;
        *) printf 'Usage: sudo bash install.sh [--domain HOST | --ip IPv4] [--admin-port PORT]\n'; exit 2 ;;
    esac
done
(( EUID == 0 )) || { printf 'Run this installer with sudo.\n' >&2; exit 1; }
if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
    . /etc/os-release
    [[ "$ID" == ubuntu ]] || { printf 'Install Docker and Compose first.\n' >&2; exit 1; }
    apt-get -o Acquire::Retries=1 update
    DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io docker-compose-v2 ca-certificates curl
    systemctl enable --now docker
fi

if [[ ! -f .env ]]; then
    if [[ -z "$domain" && -z "$ip" && -t 0 ]]; then
        read -rp 'Public domain, or press Enter to use IP: ' domain
        if [[ -z "$domain" ]]; then read -rp 'Public IPv4: ' ip; fi
        if [[ -z "$admin_port" ]]; then read -rp 'Admin port, or press Enter for a random port: ' admin_port; fi
    fi
    public_host="${domain:-$ip}"
    [[ "$public_host" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]] || { printf 'Provide a hostname or IPv4.\n' >&2; exit 2; }
    port_in_use() { ss -H -ltn "sport = :$1" | awk 'END {exit NR == 0}'; }
    for port in 80 443; do
        if port_in_use "$port"; then printf 'Port %s is occupied.\n' "$port" >&2; exit 1; fi
    done
    if [[ -z "$admin_port" ]]; then
        for attempt in {1..100}; do
            candidate=$((20000 + $(od -An -N2 -tu2 /dev/urandom) % 40000))
            if ! port_in_use "$candidate"; then admin_port="$candidate"; break; fi
        done
    fi
    [[ "$admin_port" =~ ^[1-9][0-9]{3,4}$ ]] && (( admin_port <= 65535 )) || { printf 'Invalid administration port.\n' >&2; exit 2; }
    if port_in_use "$admin_port"; then printf 'Administration port is occupied.\n' >&2; exit 1; fi
    issuer=internal options=""
    if [[ -n "$domain" ]]; then issuer=acme options=disable_tlsalpn_challenge; fi
    printf 'PUBLIC_HOST=%s\nADMIN_PORT=%s\nADMIN_PATH=manage-%s\nMIHOMO_SECRET=%s\nTLS_ISSUER=%s\nTLS_OPTIONS=%s\n' \
        "$public_host" "$admin_port" "$(openssl rand -hex 14)" "$(openssl rand -hex 32)" "$issuer" "$options" >.env.tmp
    mv .env.tmp .env
fi

set -a
. ./.env
set +a
install -d -m 0700 state/{relay,mihomo,caddy-data,caddy-config,backups}
if [[ ! -f state/mihomo/config.yaml ]]; then
    printf 'external-controller: 0.0.0.0:9090\nsecret: "%s"\nlog-level: silent\nrules: ["MATCH,REJECT"]\n' \
        "$MIHOMO_SECRET" >state/mihomo/config.yaml
fi
if [[ -f state/relay/relay.sqlite3 && -f state/relay/master.key ]]; then
    backup="state/backups/$(date -u +%Y%m%dT%H%M%S)-$$"
    mkdir -m 0700 "$backup"
    python3 - "$backup" <<'PY'
import pathlib, shutil, sqlite3, sys
target = pathlib.Path(sys.argv[1])
with sqlite3.connect("state/relay/relay.sqlite3") as source, sqlite3.connect(target / "relay.sqlite3") as destination:
    source.backup(destination)
shutil.copy2("state/relay/master.key", target / "master.key")
shutil.copy2(".env", target / "deployment.env")
PY
fi
docker compose config --quiet
docker compose up -d --build
for attempt in {1..25}; do
    if docker compose exec -T relay python -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8080/healthz",timeout=2)' 2>/dev/null; then
        printf 'Customer service: https://%s/\nAdministration: https://%s:%s/%s\n' \
            "$PUBLIC_HOST" "$PUBLIC_HOST" "$ADMIN_PORT" "$ADMIN_PATH"
        if [[ -f state/relay/bootstrap-password ]]; then
            printf 'Initial panel user: root. Read state/relay/bootstrap-password as root.\n'
        fi
        if [[ "$TLS_ISSUER" == internal ]]; then
            printf 'Trust the Caddy CA at state/caddy-data/caddy/pki/authorities/local/root.crt on your client.\n'
        fi
        exit 0
    fi
    sleep 2
done
printf 'The relay did not become healthy. Inspect docker compose ps and logs.\n' >&2
exit 1
