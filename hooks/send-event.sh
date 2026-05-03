#!/usr/bin/env bash
# Hook script for Claude Code → Claude Tracker.
# - Reads event payload (JSON) from stdin.
# - Injects tmux_target from env (set by the claude wrapper).
# - Forwards enriched payload to the local tracker.
# - Triggers a clickable desktop notification on Notification/Stop.
# Always exits 0 to never block Claude Code.

set -u

EVENT="${1:-unknown}"
SERVER="${CLAUDE_TRACKER_URL:-http://127.0.0.1:8765}"
LOG="${HOME}/claude-tracker/logs/hooks.log"
TMUX_TARGET="${CLAUDE_TRACKER_TMUX_TARGET:-}"

PAYLOAD="$(cat || true)"

mkdir -p "$(dirname "$LOG")" 2>/dev/null || true

# Enrichit le JSON avec tmux_target (s'il est dispo)
ENRICHED="$(printf '%s' "$PAYLOAD" | TMUX_TARGET="$TMUX_TARGET" python3 -c "
import json, os, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
t = os.environ.get('TMUX_TARGET', '')
if t:
    d['tmux_target'] = t
print(json.dumps(d, ensure_ascii=False))
" 2>/dev/null)"
[ -z "$ENRICHED" ] && ENRICHED="$PAYLOAD"

# POST fire-and-forget
(
    curl -sS --max-time 2 -X POST \
        -H "Content-Type: application/json" \
        --data-binary "$ENRICHED" \
        "$SERVER/hook/$EVENT" >/dev/null 2>>"$LOG" || true
) &
disown 2>/dev/null || true

# Helpers d'extraction
extract() {
    local key="$1"
    printf '%s' "$ENRICHED" | python3 -c "
import sys, json, os
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
key = '$key'
if key == 'project':
    cwd = d.get('cwd') or ''
    print(os.path.basename(cwd.rstrip('/')) or (cwd or '?'))
elif key == 'session_id':
    print(d.get('session_id') or '')
elif key == 'message':
    print((d.get('message') or '').strip())
" 2>/dev/null || true
}

SID="$(extract session_id)"
PROJ="$(extract project)"
DASHBOARD_URL="${SERVER}/#session=${SID}"

# Notif desktop avec action cliquable
notify_clickable() {
    local urgency="$1" title="$2" body="$3"
    (
        chosen="$(notify-send --urgency="$urgency" -a "Claude Code" \
            --action="default=Voir dans le dashboard" \
            "$title" "$body" 2>/dev/null || true)"
        if [ "$chosen" = "default" ]; then
            xdg-open "$DASHBOARD_URL" >/dev/null 2>&1 || true
        fi
    ) &
    disown 2>/dev/null || true
}

# Notif desktop notify-send désactivée par défaut depuis qu'on a Web Push (PWA).
# Pour la réactiver : exporter CLAUDE_TRACKER_DESKTOP_NOTIFY=1 dans ~/.zshrc.
if [ "${CLAUDE_TRACKER_DESKTOP_NOTIFY:-0}" = "1" ]; then
case "$EVENT" in
    Notification)
        MSG="$(extract message)"
        [ -z "$MSG" ] && MSG="Claude attend une réponse"
        notify_clickable "critical" "🟡 ${PROJ:-?} — Question en attente" "$MSG"
        ;;
    Stop)
        notify_clickable "normal" "✅ ${PROJ:-?} — Réponse terminée" ""
        ;;
esac
fi

exit 0
