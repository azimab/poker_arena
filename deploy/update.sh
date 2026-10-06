#!/bin/bash
# Pulls origin/main onto the host and restarts the API, rolling back if the
# install or tests fail. Run as root by poker-arena-update.timer.
set -euo pipefail

ARENA_HOME=/opt/poker-arena
FAILED=/var/lib/poker-arena/failed-deploy

as_arena() { sudo -H -u arena "$@"; }

install_at() {
    as_arena git reset -q --hard "$1" &&
        as_arena .venv/bin/pip install -q -e ".[server,dev]" &&
        docker build -q -t poker-arena-sandbox:latest .
}

# Wrapped in a function so bash has parsed all of it before git rewrites this file.
main() {
    cd "$ARENA_HOME"
    systemctl is-active --quiet poker-arena.service && exit 0

    as_arena git fetch -q origin main
    old=$(as_arena git rev-parse HEAD)
    new=$(as_arena git rev-parse origin/main)
    [ "$old" = "$new" ] && exit 0
    [ "$(cat "$FAILED" 2>/dev/null)" = "$new" ] && exit 0

    echo "deploying $old -> $new"
    if install_at "$new" && as_arena .venv/bin/python -m pytest tests -q; then
        systemctl restart poker-arena-api
        rm -f "$FAILED"
        echo "deployed $new"
    else
        echo "deploy of $new failed, rolling back to $old"
        mkdir -p "$(dirname "$FAILED")"
        echo "$new" >"$FAILED"
        install_at "$old"
        exit 1
    fi
}

main "$@"
exit
