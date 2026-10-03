#!/bin/bash
# Install infra/aibroker-deploy.sh as the restricted deploy entrypoint.
#
# Run it FROM the repo checkout on the server:
#     bash /var/www/aibroker/infra/install-deploy.sh
#
# Deliberately not part of the deploy itself: a deploy must never rewrite the
# entrypoint that is currently executing it. Keeps a timestamped backup so a
# bad entrypoint can be rolled back without a working deploy path.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)/aibroker-deploy.sh"
DST=/usr/local/bin/aibroker-deploy

[ -f "$SRC" ] || { echo "missing $SRC"; exit 1; }
bash -n "$SRC" || { echo "$SRC has a syntax error — refusing to install"; exit 1; }

if [ -f "$DST" ]; then
    BACKUP="$DST.bak.$(date -u +%Y%m%dT%H%M%SZ)"
    cp -p "$DST" "$BACKUP"
    echo "backed up current entrypoint -> $BACKUP"
fi

install -m 0755 "$SRC" "$DST"
echo "installed $DST"
echo "authorized_keys must still pin it:  command=\"$DST\""
grep -c "command=\"$DST\"" /root/.ssh/authorized_keys 2>/dev/null \
    | xargs -I{} echo "authorized_keys entries pointing here: {}"
