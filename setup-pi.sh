#!/bin/bash
# One-time setup for Spindle on Raspberry Pi OS (or any Debian/Ubuntu).
set -e
cd "$(dirname "$0")"
sudo apt-get update
sudo apt-get install -y python3 python3-mutagen ffmpeg cdparanoia libchromaprint-tools eject
# Let this user read the CD drive without sudo
sudo usermod -aG cdrom "$USER" || true
chmod +x spindle.sh
# Desktop / menu shortcut
mkdir -p ~/.local/share/applications
cat > ~/.local/share/applications/spindle.desktop <<EOF
[Desktop Entry]
Name=Spindle
Comment=Smart CD ripper
Exec=$PWD/spindle.sh
Icon=media-optical
Terminal=false
Type=Application
Categories=AudioVideo;Audio;
EOF
cp ~/.local/share/applications/spindle.desktop ~/Desktop/ 2>/dev/null || true
echo
echo "Spindle is ready. Open it from the menu, the Desktop, or run ./spindle.sh"
echo "(Log out and back in once so the CD drive permission takes effect.)"
