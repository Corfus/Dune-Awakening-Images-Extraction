#!/bin/sh
# Ubuntu 24.04, root: shared by Dockerfile and GitLab's unprivileged-container job.
set -eu
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends python3 skopeo ca-certificates curl unzip \
  libicu74 libssl3 libstdc++6 zlib1g
depot_version=3.4.0
depot_sha256=a999dec66b4850fc961bd50366696d23c2d0fad7b18790e6a5647b2f19097a53
depot_zip=$(mktemp)
trap 'rm -f "$depot_zip"' EXIT
curl --fail --location --retry 3 \
  "https://github.com/SteamRE/DepotDownloader/releases/download/DepotDownloader_${depot_version}/DepotDownloader-linux-x64.zip" \
  -o "$depot_zip"
echo "$depot_sha256  $depot_zip" | sha256sum -c -
mkdir -p /opt/depotdownloader
unzip -qo "$depot_zip" -d /opt/depotdownloader
chmod +x /opt/depotdownloader/DepotDownloader
ln -sf /opt/depotdownloader/DepotDownloader /usr/local/bin/DepotDownloader
