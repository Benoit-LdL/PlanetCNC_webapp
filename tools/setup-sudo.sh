#!/bin/sh
# Let the dashboard's System window upgrade Raspberry Pi OS packages and reboot or
# shut down the Pi. Run once on the Pi, as the user that runs server.py:
#
#   ~/PlanetCNC_webapp/tools/setup-sudo.sh           allow it
#   ~/PlanetCNC_webapp/tools/setup-sudo.sh remove    take it away again
#
# It writes /etc/sudoers.d/planetcnc-web, which allows exactly these commands as
# root without a password, and nothing else:
#   apt-get update, apt-get upgrade (with fixed options), systemctl reboot, systemctl poweroff
# The server refuses all of them while a program runs. The file is checked with
# visudo before it is installed, so a mistake can't lock you out of sudo.
set -eu
TARGET=/etc/sudoers.d/planetcnc-web

if [ "${1:-}" = "remove" ]; then
    sudo rm -f "$TARGET"
    echo "Removed $TARGET: the dashboard can no longer upgrade packages or reboot the Pi."
    exit 0
fi

USER_NAME="${1:-$(id -un)}"
TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT
# In sudoers, ':' and '=' in arguments must be escaped with a backslash.
cat > "$TMP" <<'EOF'
# Written by PlanetCNC_webapp/tools/setup-sudo.sh: what the web dashboard's System window may run as root.
# Remove with: PlanetCNC_webapp/tools/setup-sudo.sh remove
__USER__ ALL=(root) NOPASSWD: /usr/bin/apt-get update, \
    /usr/bin/apt-get -y -o Dpkg\:\:Options\:\:\=--force-confdef -o Dpkg\:\:Options\:\:\=--force-confold upgrade, \
    /usr/bin/systemctl reboot, /usr/bin/systemctl poweroff
EOF
sed -i "s/__USER__/$USER_NAME/" "$TMP"

sudo visudo -cf "$TMP" >/dev/null
sudo install -m 0440 -o root -g root "$TMP" "$TARGET"
echo "Installed $TARGET for user $USER_NAME."
if sudo -u "$USER_NAME" sudo -n -l /usr/bin/systemctl reboot >/dev/null 2>&1; then
    echo "Check OK. Open the System window in the dashboard and press Check for updates."
else
    echo "Warning: the check failed - is $USER_NAME the user that runs server.py?"
fi
