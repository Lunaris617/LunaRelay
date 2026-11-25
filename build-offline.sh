#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$(realpath "$0")")"
archive="$(realpath -m "${1:-lunarelay-ubuntu24-amd64.tar.gz}")"
temp="$(mktemp -d)"
trap 'rm -rf -- "$temp"' EXIT
bundle="$temp/LunaRelay"
mkdir -p "$bundle/debs"
cp install.sh compose.yaml Caddyfile README.md LICENSE "$bundle/"

relay_image=lunarelay-relay:offline
caddy_image=caddy:2.10.2-alpine
mihomo_image=metacubex/mihomo:v1.19.34
ubuntu_image=ubuntu:24.04

for image in "$caddy_image" "$mihomo_image" "$ubuntu_image"; do
    docker image inspect "$image" >/dev/null 2>&1 || docker pull --platform linux/amd64 "$image"
done
if [[ "${RELAY_FROM_IMAGE:-}" ]]; then
    docker tag "$RELAY_FROM_IMAGE" "$relay_image"
else
    docker build --platform linux/amd64 -t "$relay_image" .
fi
docker save -o "$bundle/images.tar" "$relay_image" "$caddy_image" "$mihomo_image"

docker run --rm --platform linux/amd64 --pull never \
    -v "$bundle/debs:/packages" -e "UBUNTU_APT_MIRROR=${UBUNTU_APT_MIRROR:-}" \
    "$ubuntu_image" bash -eu -c '
    if [[ -n "$UBUNTU_APT_MIRROR" ]]; then
        sed -E -i "s|https?://((archive|security)\.ubuntu\.com)/ubuntu/?|$UBUNTU_APT_MIRROR|g" /etc/apt/sources.list.d/ubuntu.sources
    fi
    apt-get -o Acquire::Retries=2 update
    mkdir -p /packages/partial
    apt-get -o Dir::Cache::archives=/packages --download-only --no-install-recommends -y install \
        docker.io docker-compose-v2 ca-certificates curl openssl iproute2 python3
    rmdir /packages/partial
    rm -f /packages/lock
    cd /packages
    for file in *.deb; do
        dpkg-deb -f "$file"
        printf "Filename: ./%s\nSize: %s\n" "$file" "$(stat -c %s "$file")"
        printf "SHA256: %s\n\n" "$(sha256sum "$file" | cut -d " " -f 1)"
    done | gzip -n >Packages.gz
'
mkdir -p "$(dirname "$archive")"
tar -C "$temp" -czf "$archive" LunaRelay
(cd "$(dirname "$archive")" && sha256sum "$(basename "$archive")" >"$(basename "$archive").sha256")
printf '%s\n' "$archive"
