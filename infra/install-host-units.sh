#!/bin/bash
# Install the host-level systemd units this stack needs. Run FROM the repo
# checkout on the server:
#
#     bash /var/www/aibroker/infra/install-host-units.sh
#
# Deliberately NOT part of the deploy: these are host units, not application
# code, and a deploy key must not be able to write systemd units.
#
# Currently one unit: aibroker-vision.slice, whose only job is MemorySwapMax=0
# for the vision container. See the unit file for why docker-compose's own
# memswap_limit is not enough on its own (short version: docker writes the swap
# limit straight into the cgroup, systemd does not know about it, and any
# daemon-reload silently resets it).
set -euo pipefail

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
UNIT=aibroker-vision.slice

[ -f "$SRC_DIR/$UNIT" ] || { echo "missing $SRC_DIR/$UNIT"; exit 1; }

install -m 0644 "$SRC_DIR/$UNIT" "/etc/systemd/system/$UNIT"
systemctl daemon-reload
systemctl start "$UNIT"

echo "installed /etc/systemd/system/$UNIT"
echo -n "MemorySwapMax = "
systemctl show "$UNIT" -p MemorySwapMax --value

# Prove it reached the kernel, not just systemd's idea of it. The slice's own
# cgroup only exists once something is in it, so an empty slice is expected to
# have no directory yet — that is not a failure.
CG=/sys/fs/cgroup/aibroker.slice/$UNIT/memory.swap.max
if [ -r "$CG" ]; then
    echo "cgroup memory.swap.max = $(cat "$CG")"
else
    echo "cgroup not materialised yet (empty slice) — it appears when the"
    echo "container starts under it; verify then with:"
    echo "  cat $CG"
fi

echo
echo "Now recreate the container so it joins the slice:"
echo "  cd /var/www/aibroker && docker compose up -d --force-recreate vision-local"
