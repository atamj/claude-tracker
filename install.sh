#!/usr/bin/env bash
# Claude Tracker — script d'installation
# Usage : ./install.sh [--non-interactive]

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${CLAUDE_TRACKER_PORT:-8765}"
INTERACTIVE=1
[ "${1:-}" = "--non-interactive" ] && INTERACTIVE=0

c_red()    { printf '\033[31m%s\033[0m\n' "$*"; }
c_green()  { printf '\033[32m%s\033[0m\n' "$*"; }
c_yellow() { printf '\033[33m%s\033[0m\n' "$*"; }
c_blue()   { printf '\033[34m%s\033[0m\n' "$*"; }
c_dim()    { printf '\033[2m%s\033[0m\n' "$*"; }

ask() {
    local prompt="$1" default="${2:-}" answer
    if [ "$INTERACTIVE" = "0" ]; then
        echo "$default"
        return
    fi
    if [ -n "$default" ]; then
        read -r -p "$prompt [$default] : " answer
        echo "${answer:-$default}"
    else
        read -r -p "$prompt : " answer
        echo "$answer"
    fi
}

step() { echo; c_blue "▶ $*"; }

# ============= 1. Prérequis =============
step "1/7 — Vérification des prérequis"

missing=()
for cmd in python3 tmux notify-send claude curl; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        missing+=("$cmd")
    fi
done

if [ ${#missing[@]} -gt 0 ]; then
    c_red "Outils manquants : ${missing[*]}"
    echo "Installe-les avant de relancer :"
    echo "  - Fedora Silverblue : sudo rpm-ostree install --apply-live ${missing[*]}"
    echo "  - Fedora classic    : sudo dnf install ${missing[*]}"
    echo "  - Ubuntu / Debian   : sudo apt install ${missing[*]/notify-send/libnotify-bin}"
    echo "  - Arch              : sudo pacman -S ${missing[*]/notify-send/libnotify}"
    echo "  - claude (CLI)      : voir https://docs.claude.com/claude-code"
    exit 1
fi

if ! python3 -c "import venv" >/dev/null 2>&1; then
    c_red "Module python3 venv manquant. Installe python3-venv (Debian/Ubuntu) ou python3 (autres)."
    exit 1
fi

c_green "  ✓ python3, tmux, notify-send, claude, curl OK"

# ============= 2. venv + dépendances =============
step "2/7 — Environnement Python (venv + dépendances)"
if [ ! -d "$REPO_DIR/venv" ]; then
    python3 -m venv "$REPO_DIR/venv"
    c_green "  ✓ venv créé"
fi
"$REPO_DIR/venv/bin/pip" install --quiet --upgrade pip
"$REPO_DIR/venv/bin/pip" install --quiet -r "$REPO_DIR/server/requirements.txt"
c_green "  ✓ dépendances installées"

# ============= 3. VAPID keys =============
step "3/7 — Clés VAPID pour les Web Push notifications"
if [ -f "$REPO_DIR/server/vapid_keys.json" ] && [ -f "$REPO_DIR/server/vapid_private.pem" ]; then
    c_green "  ✓ clés VAPID déjà présentes"
else
    EMAIL=$(ask "Email pour le subject VAPID (RFC requirement, peut être bidon)" "admin@example.com")
    "$REPO_DIR/venv/bin/python" "$REPO_DIR/server/generate_vapid.py" "$EMAIL"
fi

# ============= 4. Service systemd user =============
step "4/7 — Service systemd user"
USER_SYSTEMD="$HOME/.config/systemd/user"
mkdir -p "$USER_SYSTEMD"

cat > "$USER_SYSTEMD/claude-tracker.service" <<EOF
[Unit]
Description=Claude Tracker - dashboard centralisé des instances Claude Code
After=graphical-session.target
PartOf=graphical-session.target

[Service]
Type=simple
WorkingDirectory=$REPO_DIR/server
ExecStart=$REPO_DIR/venv/bin/uvicorn main:app --host 0.0.0.0 --port $PORT --log-level warning
Restart=on-failure
RestartSec=3
StandardOutput=append:$REPO_DIR/logs/server.log
StandardError=append:$REPO_DIR/logs/server.log

[Install]
WantedBy=default.target
EOF

mkdir -p "$REPO_DIR/logs" "$REPO_DIR/data"

systemctl --user daemon-reload
systemctl --user enable --now claude-tracker.service >/dev/null 2>&1 || true
sleep 1
if systemctl --user is-active claude-tracker.service >/dev/null; then
    c_green "  ✓ service actif sur le port $PORT"
else
    c_red "  service non actif. Vérifie : journalctl --user -u claude-tracker -n 50"
    exit 1
fi

# ============= 5. Wrapper claude dans PATH =============
step "5/7 — Wrapper claude dans le PATH du shell"
chmod +x "$REPO_DIR/bin/claude" "$REPO_DIR/bin/claude-skip-permissions"

USER_SHELL=$(basename "${SHELL:-/bin/bash}")
case "$USER_SHELL" in
    zsh)   RC="$HOME/.zshrc" ;;
    bash)  RC="$HOME/.bashrc" ;;
    *)     RC="$HOME/.profile" ;;
esac

PATH_LINE="export PATH=\"$REPO_DIR/bin:\$PATH\""
if grep -q "claude-tracker/bin" "$RC" 2>/dev/null; then
    c_green "  ✓ PATH déjà configuré dans $RC"
else
    {
        echo ""
        echo "# Claude Tracker — wrapper claude (lance Claude Code dans tmux + tracking)"
        echo "$PATH_LINE"
    } >> "$RC"
    c_green "  ✓ PATH ajouté à $RC"
    c_yellow "  ⚠ Ouvre un nouveau terminal ou fais 'source $RC' pour activer"
fi

# ============= 6. Hooks Claude Code =============
step "6/7 — Hooks Claude Code dans ~/.claude/settings.json"
chmod +x "$REPO_DIR/hooks/send-event.sh"

SETTINGS="$HOME/.claude/settings.json"
mkdir -p "$(dirname "$SETTINGS")"
[ -f "$SETTINGS" ] && cp "$SETTINGS" "$SETTINGS.bak.$(date +%Y%m%d-%H%M%S)" || echo '{}' > "$SETTINGS"

"$REPO_DIR/venv/bin/python" - <<EOF
import json
from pathlib import Path
p = Path("$SETTINGS")
data = json.loads(p.read_text()) if p.read_text().strip() else {}
hooks = data.setdefault("hooks", {})
hook_cmd = "$REPO_DIR/hooks/send-event.sh"
events = ["SessionStart", "UserPromptSubmit", "Notification", "Stop", "SessionEnd"]
for ev in events:
    target_cmd = f"{hook_cmd} {ev}"
    existing = hooks.get(ev, [])
    # Cherche si notre hook est déjà là (par sa commande)
    found = False
    for entry in existing:
        for h in entry.get("hooks", []):
            if h.get("command") == target_cmd:
                found = True
                break
    if not found:
        existing.append({"hooks": [{"type": "command", "command": target_cmd}]})
        hooks[ev] = existing
p.write_text(json.dumps(data, indent=2))
print(f"  ✓ hooks configurés pour : {', '.join(events)}")
EOF

# ============= 7. Récap =============
step "7/7 — Installation terminée"
echo
c_green "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
c_green "  Claude Tracker est installé et tourne sur :"
c_green "    http://127.0.0.1:$PORT"
c_green "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo
echo "Pour utiliser le tracker :"
echo "  1. Ouvre un nouveau terminal (ou 'source $RC')"
echo "  2. Lance Claude Code via 'claude' (le wrapper) — il tournera dans tmux"
echo "  3. Ouvre http://127.0.0.1:$PORT dans ton navigateur"
echo
c_dim "Pour désinstaller : ./uninstall.sh"
c_dim "Logs serveur     : tail -f $REPO_DIR/logs/server.log"
c_dim "Statut service   : systemctl --user status claude-tracker"
echo
c_yellow "Pour les notifs mobile (Android), voir la section Tailscale du README."
