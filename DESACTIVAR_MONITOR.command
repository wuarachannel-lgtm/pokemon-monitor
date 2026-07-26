#!/bin/bash
# Apaga el monitor de Pokémon Center.
cd "$(dirname "$0")"

launchctl bootout "gui/$(id -u)/com.samuel.pokemon-monitor" 2>/dev/null \
  || launchctl unload "$HOME/Library/LaunchAgents/com.samuel.pokemon-monitor.plist" 2>/dev/null
rm -f "$HOME/Library/LaunchAgents/com.samuel.pokemon-monitor.plist"

echo "🛑 Monitor desactivado."
echo "   (El Chrome dedicado sigue abierto; ciérralo con: pkill -f 'remote-debugging-port=9333')"
echo
read -n 1 -s -r -p "Pulsa cualquier tecla para cerrar…"
