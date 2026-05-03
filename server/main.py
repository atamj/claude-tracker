import asyncio
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from db import DB_PATH, get_conn, init_db, purge_old

# --- Web Push (VAPID) ----------------------------------------------------
VAPID_FILE = Path(__file__).resolve().parent / "vapid_keys.json"
VAPID_PEM_FILE = Path(__file__).resolve().parent / "vapid_private.pem"
VAPID_DATA: dict = {}
if VAPID_FILE.exists():
    try:
        VAPID_DATA = json.loads(VAPID_FILE.read_text())
    except Exception:
        VAPID_DATA = {}

try:
    from pywebpush import webpush, WebPushException  # type: ignore
    _push_available = True
except ImportError:
    _push_available = False

DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboard"

app = FastAPI(title="Claude Tracker", docs_url=None, redoc_url=None)


class Broadcaster:
    """In-memory pub/sub for SSE clients."""

    def __init__(self) -> None:
        self.subscribers: set[asyncio.Queue] = set()

    async def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)

    async def publish(self, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False)
        for q in list(self.subscribers):
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                pass


broadcaster = Broadcaster()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def project_name_from_cwd(cwd: str | None) -> str:
    if not cwd:
        return "?"
    return os.path.basename(cwd.rstrip("/")) or cwd


@app.on_event("startup")
async def on_startup() -> None:
    init_db()
    purge_old(days=30)


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "db": str(DB_PATH)}


@app.post("/hook/{event}")
async def receive_hook(event: str, request: Request) -> JSONResponse:
    raw = await request.body()
    try:
        payload: dict[str, Any] = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        payload = {"_raw": raw.decode("utf-8", errors="replace")}

    session_id = payload.get("session_id") or "unknown"
    cwd = payload.get("cwd")
    transcript = payload.get("transcript_path")
    tmux_target = payload.get("tmux_target")  # injecté par le hook depuis l'env
    ts = now_iso()

    new_status: str | None = None
    last_prompt: str | None = None
    last_question: str | None = None
    clear_question = False
    increment_prompt = False
    end_session = False

    e = event.lower()
    if e == "sessionstart":
        new_status = "idle"
    elif e == "userpromptsubmit":
        new_status = "running"
        last_prompt = payload.get("prompt")
        increment_prompt = True
        clear_question = True
        # first_prompt sera défini plus bas si c'est la 1re soumission
    elif e == "notification":
        new_status = "waiting"
        last_question = payload.get("message") or "Claude attend une réponse"
    elif e == "stop":
        new_status = "idle"
        clear_question = True
    elif e == "sessionend":
        new_status = "ended"
        end_session = True
        clear_question = True

    # Sur Notification, capture le terminal pour avoir la vraie question
    terminal_view: str | None = None
    if e == "notification":
        target = tmux_target
        if not target:
            with get_conn() as conn:
                row = conn.execute(
                    "SELECT tmux_target FROM sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if row:
                    target = row["tmux_target"]
        if target:
            terminal_view = await tmux_capture(target, lines=80)

    with get_conn() as conn:
        existing = conn.execute(
            "SELECT session_id, first_prompt, prompt_count FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()

        # Capture le 1er prompt comme "titre" parlant de la session
        first_prompt: str | None = None
        if e == "userpromptsubmit" and last_prompt:
            if existing is None or not existing["first_prompt"]:
                first_prompt = last_prompt

        if existing is None:
            conn.execute(
                """
                INSERT INTO sessions
                  (session_id, cwd, project_name, transcript, started_at,
                   last_activity, status, last_prompt, prompt_count,
                   tmux_target, last_question, terminal_view, first_prompt)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    cwd,
                    project_name_from_cwd(cwd),
                    transcript,
                    ts,
                    ts,
                    new_status or "active",
                    last_prompt,
                    1 if increment_prompt else 0,
                    tmux_target,
                    last_question,
                    terminal_view,
                    first_prompt,
                ),
            )
        else:
            sets = ["last_activity = ?"]
            params: list[Any] = [ts]

            if cwd:
                sets += ["cwd = ?", "project_name = ?"]
                params += [cwd, project_name_from_cwd(cwd)]
            if transcript:
                sets.append("transcript = ?")
                params.append(transcript)
            if tmux_target:
                sets.append("tmux_target = ?")
                params.append(tmux_target)
            if new_status:
                sets.append("status = ?")
                params.append(new_status)
            if last_prompt is not None:
                sets.append("last_prompt = ?")
                params.append(last_prompt)
            if last_question is not None:
                sets.append("last_question = ?")
                params.append(last_question)
            if clear_question:
                sets.append("last_question = NULL")
                sets.append("terminal_view = NULL")
            if terminal_view is not None:
                sets.append("terminal_view = ?")
                params.append(terminal_view)
            if increment_prompt:
                sets.append("prompt_count = prompt_count + 1")
            if first_prompt is not None:
                sets.append("first_prompt = ?")
                params.append(first_prompt)
            if end_session:
                sets.append("ended_at = ?")
                params.append(ts)

            params.append(session_id)
            conn.execute(
                f"UPDATE sessions SET {', '.join(sets)} WHERE session_id = ?",
                params,
            )

        conn.execute(
            "INSERT INTO events (session_id, event_type, payload, created_at) VALUES (?, ?, ?, ?)",
            (session_id, event, json.dumps(payload, ensure_ascii=False), ts),
        )

    await broadcaster.publish({"type": "update", "session_id": session_id, "event": event, "at": ts})

    # Push mobile pour Notification (question) et Stop (terminé)
    if e in ("notification", "stop"):
        with get_conn() as conn:
            r = conn.execute(
                "SELECT project_name, last_question FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        proj = (r["project_name"] if r else None) or "?"
        if e == "notification":
            q = (r["last_question"] if r else None) or "Claude attend une réponse"
            asyncio.create_task(send_push_to_all(
                title=f"🟡 {proj} — Question en attente",
                body=q[:160],
                url=f"/#session={session_id}",
                tag=f"q-{session_id}",
            ))
        elif e == "stop":
            asyncio.create_task(send_push_to_all(
                title=f"✅ {proj} — Réponse terminée",
                body="",
                url=f"/#session={session_id}",
                tag=f"stop-{session_id}",
            ))

    return JSONResponse({"ok": True})


async def _get_alive_tmux_targets() -> set[str] | None:
    """Liste les sessions et panes tmux vivants. None si tmux indisponible."""
    if not shutil.which("tmux"):
        return None  # pas de tmux → on ne reap rien (sécurité)
    alive: set[str] = set()
    for args in (
        ["tmux", "ls", "-F", "#{session_name}"],
        ["tmux", "list-panes", "-a", "-F", "#{pane_id}"],
    ):
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
            # tmux ls renvoie 1 si aucun serveur — c'est OK, on continue
            if proc.returncode in (0, 1):
                for line in out.decode().splitlines():
                    line = line.strip()
                    if line:
                        alive.add(line)
        except (asyncio.TimeoutError, FileNotFoundError):
            return None
    return alive


async def _reap_zombie_sessions() -> int:
    """Marque ended les sessions dont la cible tmux n'existe plus.
    Renvoie le nombre de sessions marquées."""
    alive = await _get_alive_tmux_targets()
    if alive is None:
        return 0  # tmux down → on ne touche à rien
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT session_id, tmux_target FROM sessions "
            "WHERE status != 'ended' AND tmux_target IS NOT NULL AND tmux_target != ''"
        ).fetchall()
        zombies = [r["session_id"] for r in rows if r["tmux_target"] not in alive]
        if not zombies:
            return 0
        ts = now_iso()
        placeholders = ",".join("?" * len(zombies))
        conn.execute(
            f"UPDATE sessions SET status='ended', ended_at=?, last_activity=?, "
            f"last_question=NULL, terminal_view=NULL WHERE session_id IN ({placeholders})",
            [ts, ts] + zombies,
        )
    return len(zombies)


@app.get("/api/sessions")
async def list_sessions(limit: int = 100) -> dict:
    # Détection automatique des sessions zombies (terminal fermé brutalement)
    await _reap_zombie_sessions()
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT session_id, cwd, project_name, started_at, ended_at,
                   last_activity, status, last_prompt, prompt_count,
                   tmux_target, last_question, terminal_view, first_prompt,
                   transcript
            FROM sessions
            ORDER BY
              CASE status WHEN 'waiting' THEN 0 WHEN 'running' THEN 1 WHEN 'idle' THEN 2 ELSE 3 END,
              last_activity DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return {"sessions": [dict(r) for r in rows]}


async def tmux_capture(target: str, lines: int = 100) -> str | None:
    """Capture the last N lines visible in a tmux pane/session."""
    tmux = shutil.which("tmux")
    if not tmux or not target:
        return None
    proc = await asyncio.create_subprocess_exec(
        tmux, "capture-pane", "-t", target, "-p", "-J", "-S", f"-{lines}",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
    except asyncio.TimeoutError:
        proc.kill()
        return None
    if proc.returncode != 0:
        return None
    text = out.decode("utf-8", "replace")
    # Trim trailing empty lines, keep last ~40 non-empty lines
    rows = [r for r in text.splitlines()]
    while rows and not rows[-1].strip():
        rows.pop()
    if len(rows) > 40:
        rows = rows[-40:]
    return "\n".join(rows) if rows else None


async def tmux_send(target: str, text: str, press_enter: bool = True) -> tuple[int, str]:
    """Send literal text (then Enter) to a tmux pane/session."""
    tmux = shutil.which("tmux")
    if not tmux:
        return (127, "tmux not installed on server host")

    # send-keys -l = literal (texte tel quel, pas d'interprétation)
    proc = await asyncio.create_subprocess_exec(
        tmux, "send-keys", "-t", target, "-l", text,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        return (proc.returncode, err.decode("utf-8", "replace"))

    if press_enter:
        proc2 = await asyncio.create_subprocess_exec(
            tmux, "send-keys", "-t", target, "Enter",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, err2 = await proc2.communicate()
        if proc2.returncode != 0:
            return (proc2.returncode, err2.decode("utf-8", "replace"))

    return (0, "")


@app.post("/api/sessions/{session_id}/respond")
async def respond(session_id: str, request: Request) -> JSONResponse:
    body = await request.json()
    text: str = body.get("text", "")
    press_enter: bool = body.get("press_enter", True)

    with get_conn() as conn:
        row = conn.execute(
            "SELECT tmux_target, status FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "session inconnue")
    target = row["tmux_target"]
    if not target:
        raise HTTPException(
            409,
            "session non pilotable (lancée hors du wrapper claude → pas de cible tmux)",
        )

    rc, err = await tmux_send(target, text, press_enter=press_enter)
    if rc != 0:
        raise HTTPException(500, f"tmux send-keys a échoué: {err.strip()}")

    # On marque la session comme running et on efface la question en attente.
    ts = now_iso()
    with get_conn() as conn:
        conn.execute(
            "UPDATE sessions SET status='running', last_question=NULL, terminal_view=NULL, last_activity=? WHERE session_id=?",
            (ts, session_id),
        )
    await broadcaster.publish({"type": "respond", "session_id": session_id, "at": ts})
    return JSONResponse({"ok": True, "sent": text, "press_enter": press_enter})


@app.get("/api/sessions/{session_id}/events")
async def session_events(session_id: str, limit: int = 50) -> dict:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT event_type, payload, created_at FROM events WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
    return {"events": [dict(r) for r in rows]}


def _parse_transcript_msg(line: str) -> dict | None:
    """Parse une ligne du transcript JSONL Claude Code en un message lisible."""
    try:
        d = json.loads(line)
    except Exception:
        return None
    t = d.get("type")
    msg = d.get("message") or {}
    ts = d.get("timestamp")
    uuid = d.get("uuid")
    parent_uuid = d.get("parentUuid")
    if t == "user":
        c = msg.get("content")
        if isinstance(c, list):
            parts = []
            for b in c:
                if isinstance(b, dict):
                    if b.get("type") == "text":
                        parts.append(b.get("text", ""))
                    elif b.get("type") == "tool_result":
                        parts.append(f"[résultat outil]")
            text = "\n".join(parts).strip()
        else:
            text = (c or "").strip()
        if not text:
            return None
        return {"role": "user", "text": text, "ts": ts, "uuid": uuid, "parent_uuid": parent_uuid}
    if t == "assistant":
        c = msg.get("content")
        if isinstance(c, list):
            parts, tools = [], []
            for b in c:
                if isinstance(b, dict):
                    if b.get("type") == "text":
                        parts.append(b.get("text", ""))
                    elif b.get("type") == "tool_use":
                        name = b.get("name", "tool")
                        inp = b.get("input", {})
                        if name == "Bash" and isinstance(inp, dict):
                            cmd = (inp.get("command") or "")[:200]
                            tools.append(f"$ {cmd}")
                        elif name in ("Edit", "Write") and isinstance(inp, dict):
                            fp = inp.get("file_path", "")
                            tools.append(f"[{name}] {fp}")
                        else:
                            tools.append(f"[{name}]")
            text = "\n".join(parts).strip()
            if tools:
                text = (text + "\n\n" if text else "") + "\n".join(tools)
        else:
            text = (c or "").strip()
        if not text:
            return None
        return {"role": "assistant", "text": text, "ts": ts, "uuid": uuid, "parent_uuid": parent_uuid}
    return None


def _annotate_branches(messages: list[dict]) -> list[dict]:
    """Marque chaque message comme 'main' ou 'alt'. Une vraie branche alt apparaît
    quand un même parent_uuid a plusieurs enfants : celui qui n'est pas dans la
    chaîne du dernier message + tous ses descendants sont marqués 'alt'."""
    if not messages:
        return messages

    by_uuid = {m["uuid"]: m for m in messages if m.get("uuid")}
    children: dict[str, list[str]] = {}
    for m in messages:
        p = m.get("parent_uuid")
        u = m.get("uuid")
        if p and u:
            children.setdefault(p, []).append(u)

    # Chaîne main = remontée depuis le dernier message
    main_uuids: set[str] = set()
    current = messages[-1].get("uuid")
    while current and current in by_uuid:
        main_uuids.add(current)
        current = by_uuid[current].get("parent_uuid")

    # Pour chaque parent ayant plusieurs enfants, ceux qui ne sont pas main → alt
    alt_uuids: set[str] = set()
    for p, kids in children.items():
        if len(kids) > 1:
            for k in kids:
                if k not in main_uuids:
                    alt_uuids.add(k)

    # Propage alt à tous les descendants (sauf si déjà main, ce qui ne devrait pas arriver)
    stack = list(alt_uuids)
    while stack:
        u = stack.pop()
        for k in children.get(u, []):
            if k not in alt_uuids and k not in main_uuids:
                alt_uuids.add(k)
                stack.append(k)

    for m in messages:
        m["branch"] = "alt" if m.get("uuid") in alt_uuids else "main"
    return messages


@app.get("/api/sessions/{session_id}/transcript")
async def session_transcript(session_id: str, limit: int = 200) -> dict:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT transcript FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "session inconnue")
    path = row["transcript"]
    if not path:
        return {"messages": [], "note": "pas de fichier transcript pour cette session"}

    p = Path(path)
    if not p.exists():
        return {"messages": [], "note": f"fichier transcript introuvable ({path})"}

    messages: list[dict] = []
    try:
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                m = _parse_transcript_msg(line)
                if m:
                    messages.append(m)
    except Exception as ex:
        raise HTTPException(500, f"impossible de lire le transcript: {ex}")

    # Marque les branches AVANT le tronquage par limite (sinon la chaîne main casse)
    messages = _annotate_branches(messages)
    if limit and len(messages) > limit:
        messages = messages[-limit:]
    n_alt = sum(1 for m in messages if m.get("branch") == "alt")
    return {"messages": messages, "transcript_path": str(p), "alt_count": n_alt}


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str) -> JSONResponse:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT status FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "session inconnue")
        if row["status"] not in ("ended", "idle"):
            raise HTTPException(
                409,
                f"impossible de supprimer une session active (statut: {row['status']})",
            )
        conn.execute("DELETE FROM events WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
    await broadcaster.publish({"type": "delete", "session_id": session_id})
    return JSONResponse({"ok": True})


@app.get("/events")
async def sse(request: Request) -> StreamingResponse:
    async def event_stream():
        q = await broadcaster.subscribe()
        try:
            yield ": connected\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    data = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield f"data: {data}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            broadcaster.unsubscribe(q)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(DASHBOARD_DIR / "index.html")


# ============= PWA assets =============
@app.get("/manifest.json")
async def manifest() -> FileResponse:
    return FileResponse(DASHBOARD_DIR / "manifest.json", media_type="application/manifest+json")


@app.get("/sw.js")
async def service_worker() -> FileResponse:
    # Sert avec headers no-cache pour que les MAJ du SW soient prises en compte
    return FileResponse(
        DASHBOARD_DIR / "sw.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.get("/icon-{size}.png")
async def icon(size: str) -> FileResponse:
    p = DASHBOARD_DIR / f"icon-{size}.png"
    if not p.exists():
        raise HTTPException(404, "icon not found")
    return FileResponse(p, media_type="image/png")


# ============= Web Push =============
@app.get("/api/push/public-key")
async def push_public_key() -> dict:
    if not _push_available or not VAPID_DATA.get("public_b64"):
        return {"available": False}
    return {"available": True, "publicKey": VAPID_DATA["public_b64"]}


@app.post("/api/push/subscribe")
async def push_subscribe(request: Request) -> JSONResponse:
    body = await request.json()
    sub = body.get("subscription") or body
    endpoint = sub.get("endpoint")
    keys = sub.get("keys") or {}
    p256dh = keys.get("p256dh")
    auth = keys.get("auth")
    if not endpoint or not p256dh or not auth:
        raise HTTPException(400, "subscription invalide")

    ua = request.headers.get("user-agent", "")[:200]
    ts = now_iso()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO push_subscriptions (endpoint, p256dh, auth, user_agent, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(endpoint) DO UPDATE SET
                p256dh = excluded.p256dh,
                auth = excluded.auth,
                user_agent = excluded.user_agent
            """,
            (endpoint, p256dh, auth, ua, ts),
        )
    return JSONResponse({"ok": True})


@app.post("/api/push/unsubscribe")
async def push_unsubscribe(request: Request) -> JSONResponse:
    body = await request.json()
    endpoint = body.get("endpoint")
    if not endpoint:
        raise HTTPException(400, "endpoint requis")
    with get_conn() as conn:
        conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
    return JSONResponse({"ok": True})


@app.post("/api/push/test")
async def push_test() -> dict:
    """Envoie une notif de test à toutes les subscriptions enregistrées."""
    sent, failed = await send_push_to_all(
        title="🔔 Test Claude Tracker",
        body="Si tu vois cette notif, les Web Push fonctionnent.",
        url="/",
    )
    return {"sent": sent, "failed": failed}


def _send_push_one(endpoint: str, p256dh: str, auth: str, payload: dict) -> bool:
    """Envoie une push à une subscription. Retire la sub de la DB si elle est expirée (404/410)."""
    if not _push_available or not VAPID_PEM_FILE.exists():
        return False
    try:
        webpush(
            subscription_info={
                "endpoint": endpoint,
                "keys": {"p256dh": p256dh, "auth": auth},
            },
            data=json.dumps(payload, ensure_ascii=False),
            vapid_private_key=str(VAPID_PEM_FILE),  # path du fichier .pem
            vapid_claims={"sub": VAPID_DATA.get("subject", "mailto:admin@example.com")},
            ttl=60,
        )
        return True
    except WebPushException as e:
        # 404/410 = subscription morte côté push service → on la supprime
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status in (404, 410):
            with get_conn() as conn:
                conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
        return False
    except Exception:
        return False


async def send_push_to_all(title: str, body: str, url: str = "/", tag: str | None = None) -> tuple[int, int]:
    """Envoie une push à TOUTES les subscriptions. Retourne (succès, échecs)."""
    if not _push_available:
        return (0, 0)
    with get_conn() as conn:
        rows = conn.execute("SELECT endpoint, p256dh, auth FROM push_subscriptions").fetchall()
    if not rows:
        return (0, 0)

    payload = {"title": title, "body": body, "url": url, "tag": tag or "claude-tracker"}
    loop = asyncio.get_event_loop()
    results = await asyncio.gather(*[
        loop.run_in_executor(None, _send_push_one, r["endpoint"], r["p256dh"], r["auth"], payload)
        for r in rows
    ])
    sent = sum(1 for r in results if r)
    return (sent, len(results) - sent)
