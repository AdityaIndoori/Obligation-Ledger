#!/usr/bin/env bash
# Host firewall for the demo. T14d, CORRECTED for this machine's actual network.
#
# WHY THIS FILE EXISTS RATHER THAN THE COMMANDS IN THE MASTER DOC:
#   The doc says `ufw allow from 192.168.0.0/16`. This box is NOT on that
#   subnet -- it sits on 10.10.0.0/24 (enP7s7, host 10.10.0.2) and runs a
#   hotspot on 10.42.0.0/24 (wlP9s9). Running the doc's rules verbatim then
#   enabling ufw would deny BOTH the SSH session and the :8443 UI. That is a
#   lock-yourself-out-of-the-demo-machine bug, not a style difference.
#
# ORDER MATTERS: every allow rule lands BEFORE `ufw enable`.
#
#   sudo ./firewall.sh            apply
#   sudo ./firewall.sh --dry-run  print what would run
set -euo pipefail

LAN_WIRED=${LAN_WIRED:-10.10.0.0/24}     # enP7s7, the SSH path
LAN_WIFI=${LAN_WIFI:-10.42.0.0/24}       # wlP9s9 hotspot, partner's laptop
UI_PORT=${UI_PORT:-8443}

DRY=""
[[ "${1:-}" == "--dry-run" ]] && DRY="echo   would run:"

if [[ -z "$DRY" && $EUID -ne 0 ]]; then
  echo "must run as root (sudo $0)" >&2; exit 1
fi

if ! command -v ufw >/dev/null; then
  echo "! ufw is not installed. On an air-gapped box you cannot apt-get it now." >&2
  echo "  Skipping firewall; note this in the implementation log." >&2
  exit 2
fi

echo "== allow SSH FIRST (both LAN paths) =="
$DRY ufw allow from "$LAN_WIRED" to any port 22 proto tcp
$DRY ufw allow from "$LAN_WIFI"  to any port 22 proto tcp

echo "== allow the approval UI on :$UI_PORT (LAN only) =="
$DRY ufw allow from "$LAN_WIRED" to any port "$UI_PORT" proto tcp
$DRY ufw allow from "$LAN_WIFI"  to any port "$UI_PORT" proto tcp

echo "== default deny inbound, then enable =="
$DRY ufw default deny incoming
$DRY ufw default allow outgoing
$DRY ufw --force enable

if [[ -z "$DRY" ]]; then
  echo
  ufw status verbose
  echo
  echo "Sanity check from your laptop, BEFORE you walk away from this terminal:"
  echo "   ssh dell@10.10.0.2 true   &&   curl -s http://10.10.0.2:$UI_PORT/api/meta"
  echo "If either fails: ufw disable   (you still have this session open)"
fi
