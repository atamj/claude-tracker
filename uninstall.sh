#!/usr/bin/env bash
# Claude Tracker — script de désinstallation
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

c_red()    { printf '\033[31m%s\033[0m\n' "$*"; }
c_green()  { printf '\033[32m%s\033[0m\n' "$*"; }
c_yellow() { printf '\033[33m%s\033[0m\n' "$*"; }

echo
c_yellow "Cette commande va :"
echo "  • Arrêter et désactiver le service systemd claude-tracker"
echo "  • Retirer les hooks Claude Code dans ~/.claude/settings.json"
echo "  • Retirer la ligne PATH ajoutée à ton .zshrc/.bashrc"
echo
echo "Elle ne touchera PAS :"
echo "  • Le code (ce dossier)"
echo "  • La DB (data/) ni les logs (logs/)"
echo "  • tmux ni les sessions Claude en cours"
echo

read -r -p "Continuer ? [y/N] : " ans
[[ "$ans" =~ ^[yY] ]] || { echo "Annulé."; exit 0; }

# 1. Service systemd
if systemctl --user list-unit-files claude-tracker.service >/dev/null 2>&1; then
    systemctl --user disable --now claude-tracker.service 2>/dev/null || true
    rm -f "$HOME/.config/systemd/user/claude-tracker.service"
    systemctl --user daemon-reload
    c_green "  ✓ service systemd retiré"
fi

# 2. Hooks Claude Code
SETTINGS="$HOME/.claude/settings.json"
if [ -f "$SETTINGS" ]; then
    cp "$SETTINGS" "$SETTINGS.bak.$(date +%Y%m%d-%H%M%S)"
    python3 - <<EOF
import json
from pathlib import Path
p = Path("$SETTINGS")
data = json.loads(p.read_text())
hook_cmd_prefix = "$REPO_DIR/hooks/send-event.sh"
hooks = data.get("hooks", {})
for ev, entries in list(hooks.items()):
    new_entries = []
    for entry in entries:
        new_hooks = [h for h in entry.get("hooks", []) if hook_cmd_prefix not in h.get("command", "")]
        if new_hooks:
            entry["hooks"] = new_hooks
            new_entries.append(entry)
    if new_entries:
        hooks[ev] = new_entries
    else:
        del hooks[ev]
if not hooks:
    data.pop("hooks", None)
p.write_text(json.dumps(data, indent=2))
EOF
    c_green "  ✓ hooks retirés de settings.json (backup créé)"
fi

# 3. PATH dans rc shell
for RC in "$HOME/.zshrc" "$HOME/.bashrc" "$HOME/.profile"; do
    if [ -f "$RC" ] && grep -q "claude-tracker/bin" "$RC"; then
        cp "$RC" "$RC.bak.$(date +%Y%m%d-%H%M%S)"
        # Retire la ligne export PATH + le commentaire qui la précède
        sed -i '/# Claude Tracker/,+1d' "$RC"
        # Au cas où le commentaire est absent
        sed -i '\|claude-tracker/bin|d' "$RC"
        c_green "  ✓ PATH retiré de $RC (backup créé)"
    fi
done

echo
c_green "Désinstallation terminée."
echo "Le code est toujours dans $REPO_DIR — supprime-le manuellement si tu le veux :"
echo "  rm -rf $REPO_DIR"
