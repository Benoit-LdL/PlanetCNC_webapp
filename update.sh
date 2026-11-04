#!/bin/sh
# Update the web monitor from GitHub and install the bridge into TNG.
#
#   ./update.sh            get the latest version
#   ./update.sh rollback   go back to the version before the last update
#
# The dashboard's update button runs this same script. Afterwards the server
# must restart (the dashboard does that itself when running as the systemd
# service); the bridge inside TNG reloads itself when its file changes.
#
# PCNC_PROFILE overrides the TNG profile folder (default: ~/PlanetCNC/Profiles/Default).
set -eu
cd "$(dirname "$0")"
PROFILE="${PCNC_PROFILE:-$HOME/PlanetCNC/Profiles/Default}"
PREV_FILE=".git/pcnc_previous"

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "Files in $(pwd) were changed locally - not touching them:"
    git status --short --untracked-files=no
    echo "Undo those changes (git checkout -- <file>) or move them elsewhere, then try again."
    exit 1
fi

case "${1:-update}" in
    update)
        git fetch --quiet origin
        old=$(git rev-parse HEAD)
        git merge --ff-only --quiet '@{u}'
        if [ "$old" = "$(git rev-parse HEAD)" ]; then
            echo "Already up to date: $(git log -1 --format='%h %s')"
        else
            echo "$old" > "$PREV_FILE"
            echo "Updated $(git log -1 --format=%h "$old") -> $(git log -1 --format='%h %s')"
        fi
        ;;
    rollback)
        if [ ! -f "$PREV_FILE" ]; then
            echo "No earlier version recorded - nothing to roll back to."
            exit 1
        fi
        git reset --quiet --hard "$(cat "$PREV_FILE")"
        rm -f "$PREV_FILE"
        echo "Rolled back to $(git log -1 --format='%h %s')"
        ;;
    *)
        echo "usage: $0 [update|rollback]"
        exit 2
        ;;
esac

# Copy the bridge files into TNG's profile. Copy to a temporary name and rename,
# so the running bridge never sees half a file when it checks for a new version.
install_file() {
    mkdir -p "$2"
    cp "$1" "$2/.$(basename "$1").new"
    mv "$2/.$(basename "$1").new" "$2/$(basename "$1")"
}
if [ -d "$PROFILE" ]; then
    install_file bridge/planetcnc_bridge.py "$PROFILE/Scripts"
    install_file bridge/bridge_autostart.py "$PROFILE/Scripts"
    install_file bridge/Expr_WebMonitor.txt "$PROFILE/Expr"
    echo "Bridge installed in $PROFILE"
else
    echo "TNG profile $PROFILE not found - bridge files not installed"
fi
