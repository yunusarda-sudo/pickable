#!/usr/bin/env bash
# Origin lockdown for the solveny.dev VPS (Hostinger, Traefik in front).
#
# What it does:
#   1. Fetches the current Cloudflare edge IP ranges.
#   2. Allows 80/443 ONLY from those ranges (plus loopback).
#   3. Keeps SSH (22) reachable only from your admin IP (pass SSH_ALLOW=1.2.3.4).
#   4. Enables UFW and shows the resulting rules.
#
# Run ONLY after Cloudflare proxy (orange cloud) is ON for solveny.dev,
# otherwise you lock yourself out of the website. Keep an SSH session open
# in a second terminal while you run this.
#
# Usage:
#   SSH_ALLOW=<your-ip> sudo bash scripts/cloudflare-origin-lockdown.sh
#   DRY_RUN=1 bash scripts/cloudflare-origin-lockdown.sh   # print only
set -euo pipefail

SSH_ALLOW="${SSH_ALLOW:-}"
DRY_RUN="${DRY_RUN:-0}"
CF_V4_URL="https://www.cloudflare.com/ips-v4"
CF_V6_URL="https://www.cloudflare.com/ips-v6"

run() {
  if [ "$DRY_RUN" = "1" ]; then echo "+ $*"; else eval "$@"; fi
}

need() {
  command -v "$1" >/dev/null 2>&1 || { echo "missing: $1" >&2; exit 1; }
}

need curl
if [ "$DRY_RUN" != "1" ] && [ "$(id -u)" != "0" ]; then
  echo "run as root (sudo)." >&2; exit 1
fi

echo "== fetching Cloudflare ranges =="
CF_V4="$(curl -fsSL --max-time 20 "$CF_V4_URL")"
CF_V6="$(curl -fsSL --max-time 20 "$CF_V6_URL")"
echo "$CF_V4" | head -n 20
echo "$CF_V6" | head -n 8

if [ "$DRY_RUN" != "1" ]; then
  need ufw
  # Reset to a known state (keeps an open SSH session alive; new logins use the new rules).
  run "ufw --force reset"
  run "ufw default deny incoming"
  run "ufw default allow outgoing"
  if [ -n "$SSH_ALLOW" ]; then
    run "ufw allow from $SSH_ALLOW to any port 22 proto tcp comment 'admin ssh'"
  else
    echo "WARNING: SSH_ALLOW empty -> allowing 22 from anywhere (set SSH_ALLOW and re-run)." >&2
    run "ufw allow 22/tcp"
  fi
  while read -r cidr; do
    [ -n "$cidr" ] || continue
    run "ufw allow from $cidr to any port 80 proto tcp comment 'cloudflare http'"
    run "ufw allow from $cidr to any port 443 proto tcp comment 'cloudflare https'"
  done <<< "$CF_V4"
  while read -r cidr; do
    [ -n "$cidr" ] || continue
    run "ufw allow from $cidr to any port 80 proto tcp comment 'cloudflare http v6'"
    run "ufw allow from $cidr to any port 443 proto tcp comment 'cloudflare https v6'"
  done <<< "$CF_V6"
  run "ufw --force enable"
  echo "== result =="
  run "ufw status numbered"
  echo
  echo "Next: set PICKABLE_REQUIRE_CF=1 in .env, redeploy, then verify:"
  echo "  curl -sSI https://solveny.dev/ | grep -i -E 'server|strict|cf-ray'"
  echo "  curl -s -o /dev/null -w '%{http_code}\n' http://72.62.95.96/ -H 'Host: solveny.dev'   # expect 403"
fi
