#!/bin/sh
# P1-5: privilege-drop entrypoint.
#
# Why not a plain `USER hermes` in the Dockerfile? Two deploy targets mount
# /data differently:
#   * Fly.io volumes mount root-owned — the container must start as root just
#     long enough to chown the mount, then drop privileges.
#   * k8s sets runAsUser:1000 (+ fsGroup:1000 on the PVC) — the container
#     starts directly as `hermes`, the chown branch is skipped, and the
#     runAsNonRoot admission check passes.
# Either way the long-running server/loop process NEVER runs as root.
set -e

if [ "$(id -u)" = "0" ]; then
    chown -R hermes:hermes /data 2>/dev/null || true
    # setpriv execs in-place (unlike runuser/su which fork), so the app stays
    # PID 1 and keeps clean signal handling for Fly/k8s graceful shutdown.
    exec setpriv --reuid=1000 --regid=1000 --clear-groups "$@"
fi

exec "$@"
