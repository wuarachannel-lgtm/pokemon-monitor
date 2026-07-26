#!/bin/bash
# Activa el monitor de Pokémon Center para que se ejecute cada 10 minutos,
# también después de reiniciar el Mac.
cd "$(dirname "$0")"

PLIST="$HOME/Library/LaunchAgents/com.samuel.pokemon-monitor.plist"

echo "📦 Instalando la tarea programada…"
mkdir -p "$HOME/Library/LaunchAgents"
cp com.samuel.pokemon-monitor.plist "$PLIST"

launchctl bootout "gui/$(id -u)/com.samuel.pokemon-monitor" 2>/dev/null
launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || launchctl load "$PLIST"

sleep 2
if launchctl list | grep -q com.samuel.pokemon-monitor; then
  echo "✅ Monitor activo. Revisa la categoría cada 10 minutos."
  echo "   Registro:  $(pwd)/monitor.log"
  echo "   Para apagarlo: ejecuta DESACTIVAR_MONITOR.command"
else
  echo "❌ No se pudo activar. Revisa launchd.log"
fi
echo
read -n 1 -s -r -p "Pulsa cualquier tecla para cerrar…"
