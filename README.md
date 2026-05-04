# Claude Tracker

Tableau de bord centralisé pour piloter plusieurs instances de [Claude Code](https://docs.claude.com/claude-code) en parallèle, avec notifications desktop et mobile.

Conçu pour les développeurs qui jonglent avec plusieurs projets et perdent du temps à chercher quel terminal a une question en attente, ou qui veulent suivre l'avancement depuis leur téléphone sans installer un keylogger.

## Ce que ça fait

- **Vue centralisée** de toutes les sessions Claude Code actives, avec leur statut (en cours, en attente d'une réponse, terminé, inactif).
- **Affiche la question en attente** + les options Claude (1/2/3) + une vue brute du terminal pour le contexte.
- **Réponds depuis le dashboard** : clique "Approuver" / "Refuser" ou tape une réponse libre — la commande arrive dans le bon terminal via `tmux send-keys`. Plus besoin de basculer manuellement.
- **Notifications desktop** quand Claude pose une question ou termine une réponse.
- **PWA installable + Web Push** pour recevoir les notifs sur Android (la PWA marche aussi sur iOS, mais Apple bloque les Web Push hors PWA installée).
- **Modal historique** : clic sur une session → vue de toute la conversation, avec détection des branches alternatives (forks internes de Claude Code).

## Architecture

```
[Claude Code #1] ─┐
[Claude Code #2] ─┼─→ hooks (POST HTTP) ─→ [Serveur FastAPI] ─→ [SQLite]
[Claude Code #N] ─┘                           │
                                              ├─→ Dashboard web (PWA)
                                              ├─→ Web Push (mobile, PC)
                                              └─→ tmux send-keys (réponses)
```

Chaque instance Claude Code est lancée via un wrapper `claude` qui la met dans une session tmux dédiée. Les hooks Claude Code (`SessionStart`, `UserPromptSubmit`, `Notification`, `Stop`, `SessionEnd`) sont configurés pour POST chaque événement à un serveur FastAPI local. Le dashboard web lit cet état en temps réel (Server-Sent Events) et permet de répondre via `tmux send-keys` vers la session tmux correspondante.

## Prérequis

- **Linux** (testé sur Fedora Silverblue + GNOME Wayland ; devrait marcher sur Ubuntu, Arch, etc.)
- `python3` (>= 3.10)
- `tmux`
- `notify-send` (paquet `libnotify` ou `libnotify-bin`)
- `claude` CLI ([installation](https://docs.claude.com/claude-code))
- `curl`

## Installation

```bash
git clone https://github.com/<ton-user>/claude-tracker.git
cd claude-tracker
./install.sh
```

Le script :
1. Vérifie les prérequis
2. Crée un venv Python et installe les dépendances
3. Génère des clés VAPID pour les Web Push (te demande un email pour le subject)
4. Installe et démarre le service systemd user
5. Ajoute le wrapper `claude` à ton PATH (dans `~/.zshrc` ou `~/.bashrc`)
6. Configure les hooks dans `~/.claude/settings.json` (avec backup)

Une fois fini, **ouvre un nouveau terminal** (pour recharger le PATH), lance `claude` dans n'importe quel projet, et ouvre `http://127.0.0.1:8765` dans ton navigateur.

## Mobile (Android) — optionnel

Pour accéder au dashboard depuis ton téléphone et recevoir des notifications push, tu as besoin de **Tailscale** (ou équivalent : Cloudflare Tunnel, ngrok, ZeroTier…).

1. **Installe Tailscale** sur ton ordi : https://tailscale.com/download/linux
2. **Active le daemon au boot** (sinon le tunnel disparaît à chaque redémarrage) :
   ```
   sudo systemctl enable --now tailscaled
   ```
3. **Connecte-toi** : `sudo tailscale up`
4. **Active MagicDNS + HTTPS Certs** dans la console : https://login.tailscale.com/admin/dns
5. **Expose le dashboard en HTTPS** :
   ```
   sudo tailscale serve --bg --https=443 http://localhost:8765
   ```
   La config est persistée — pas besoin de la relancer après un reboot tant que `tailscaled` démarre automatiquement.
6. **Sur ton tel** : installe l'app Tailscale, login avec le même compte, ouvre Chrome sur `https://<nom-de-ta-machine>.<tailnet>.ts.net`
7. **Installe la PWA** : menu Chrome → "Installer l'application"
8. **Active les notifs** : ouvre la PWA, clique sur le bouton 🔕 en haut → autorise → tu reçois une notif de test

Les Web Push exigent **HTTPS** : sans Tailscale (ou autre tunnel TLS), le bouton Activer ne fonctionnera pas.

## Limitations connues

- **Wayland** : impossible de focuser une fenêtre tierce depuis le dashboard (sécurité du compositor). Le clic notif desktop ouvre le dashboard, pas le terminal en question.
- **Forks de conversation** : si tu envoies un texte libre via le dashboard pendant que Claude Code est dans une transition (auto-compaction, par exemple), le message peut atterrir dans une branche alternative de la conversation. Les boutons 1/2/3 (permission) ne sont pas affectés. Le modal détecte et signale les branches alt.
- **Reprise automatique des sessions ended** : pas fiable. Le bouton "Continuer" (✎) ne s'affiche que pour les sessions encore vivantes (status `idle`). Pour reprendre une session terminée, attache-toi manuellement à `claude --resume <session_id>` dans un terminal.
- **iOS** : les Web Push fonctionnent **uniquement** si la PWA est installée sur l'écran d'accueil (limitation Apple). Pas testé.
- **HTTPS requis** pour Web Push : sans tunnel TLS, le bouton 🔕 ne marche pas.

## Désinstallation

```bash
./uninstall.sh
```

Retire le service systemd, les hooks, et la ligne PATH. Garde le code pour que tu puisses réinstaller. Pour tout supprimer : `rm -rf ~/claude-tracker`.

## Architecture des fichiers

```
claude-tracker/
├── bin/
│   ├── claude                    # wrapper qui lance Claude dans tmux
│   └── claude-skip-permissions   # variante avec --dangerously-skip-permissions
├── dashboard/
│   ├── index.html                # SPA + service worker registration
│   ├── manifest.json             # PWA manifest
│   ├── sw.js                     # service worker (Web Push)
│   └── icon-{192,512}.png        # icônes PWA
├── hooks/
│   └── send-event.sh             # hook bash, POST events au serveur
├── server/
│   ├── main.py                   # FastAPI : API, SSE, push, transcripts
│   ├── db.py                     # SQLite + migrations
│   ├── generate_vapid.py         # génère les clés VAPID
│   └── requirements.txt
├── install.sh
├── uninstall.sh
├── LICENSE
└── README.md
```

## Contribuer

PRs bienvenues, surtout pour :
- Tester / corriger pour d'autres distros (Ubuntu, Arch, NixOS…)
- Améliorer la détection des branches dans le transcript
- Support de plusieurs utilisateurs (auth, multi-tenant)
- Tests automatisés

## Licence

MIT — voir [LICENSE](LICENSE).
