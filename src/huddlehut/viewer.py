"""A small read-only web viewer: all boards, one board in detail, and the lessons in memory.

    huddlehut view            # opens http://127.0.0.1:8787

Standard library only; binds to localhost; reads whichever database the boards use.
"""

from __future__ import annotations

import json
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import re
from importlib import resources
from urllib.parse import parse_qs, urlparse

from .compact import presence_tag
from .service import distill_in_background, sweep_stale_sessions
from .store import IDLE_AFTER, BoardError, Message, Store, short_id, short_model


USER_MENTION = re.compile(r"(?<![\w-])@(?:user|human)\b", re.I)


def _msg(m: Message, snippets: dict[int, str], names: dict[str, str]) -> dict:
    return {"id": m.id, "author": m.author, "name": names.get(m.author) or short_id(m.author),
            "kind": m.kind, "content": m.content, "at": m.created_at,
            "reply_to": m.reply_to, "reply_snippet": snippets.get(m.reply_to) if m.reply_to else None}


class Viewer:
    """JSON for the page. One Store, guarded by a lock (the HTTP server is threaded)."""

    def __init__(self, store: Store):
        self.store = store
        self.lock = threading.Lock()

    def boards(self) -> list[dict]:
        with self.lock:
            sweep_stale_sessions(self.store)  # sessions silent for 3+ days are ended
            stats = self.store.board_stats()
            now, out = time.time(), []
            for b in self.store.list_boards(status=None):
                seen = self.store.presence(b.id)
                active = [a for a, p in seen.items() if p["state"] == "active" and now - p["seen_at"] <= IDLE_AFTER]
                st = stats.get(b.id, {})
                out.append({"id": b.id, "name": b.name, "project": b.project, "goal": b.goal, "status": b.status,
                            "created_at": b.created_at, "closed_at": b.closed_at, "messages": st.get("messages", 0),
                            "last_at": st.get("last_at") or b.created_at, "active_agents": len(active),
                            "agents": len(seen)})
        return sorted(out, key=lambda b: (b["status"] != "open", -b["last_at"]))

    def board(self, board_id: str, limit: int = 500) -> dict:
        """One stream (newest last; the page reverses it) plus what's waiting on the user."""
        with self.lock:
            s = self.store
            b = s.get_board(board_id)
            seen = s.presence(b.id)
            names = s.labels(b.id)
            msgs = s.all_messages(b.id)
            total = len(msgs)
            msgs = msgs[-limit:]
            snippets = s.snippets({m.reply_to for m in msgs if m.reply_to})
            open_ids = {m.id for m in s.open_items(b.id)}
            files: dict[str, list[dict]] = {}
            for c in s.claims(b.id):
                files.setdefault(c["agent"], []).append({"path": c["path"], "locked": bool(c.get("locked"))})
            agents = {a: {"name": names.get(a) or short_id(a), "model": short_model(p.get("model")),
                          "tag": presence_tag(p).strip(" ()"), "state": p["state"], "seen_at": p["seen_at"],
                          "files": files.get(a, [])} for a, p in seen.items()}
            # waiting on the user: sessions whose last reply asked them something, and open @user posts
            waiting = [{"author": a, "name": names.get(a) or short_id(a), "content": p["waiting"],
                        "at": p["waiting_at"], "source": "session"}
                       for a, p in seen.items() if p.get("waiting") and p["state"] == "active"]
            waiting += [dict(_msg(m, snippets, names), source="board")
                        for m in msgs if m.id in open_ids and USER_MENTION.search(m.content)]
            if b.status != "open":
                waiting = []  # a closed board has nothing left to answer
            stream = []
            for m in msgs:
                d = _msg(m, snippets, names)
                d["open"] = m.id in open_ids
                stream.append(d)
            return {
                "board": {"id": b.id, "name": b.name, "project": b.project, "goal": b.goal, "status": b.status,
                          "created_at": b.created_at, "closed_at": b.closed_at, "outcome": b.outcome},
                "agents": agents,
                "waiting": sorted(waiting, key=lambda w: w["at"] or 0, reverse=True),
                "stream": stream,
                "message_count": total,
                "lessons": len(s.list_memories(board_id=b.id)),
            }

    def close_board(self, board_id: str, outcome: str = "") -> dict:
        with self.lock:
            board = self.store.close_board_by_id(board_id, "user", outcome[:1000])
        distill_in_background(self.store.url, board.id)  # lessons, as when an agent closes it
        return {"ok": True}

    def end_session(self, board_id: str, agent: str) -> dict:
        with self.lock:
            if agent not in self.store.presence(board_id) and agent not in self.store.labels(board_id):
                raise BoardError(f"no agent {agent} on this board")
            self.store.end_session(board_id, agent)
        return {"ok": True}

    def memory(self, q: str = "", project: str = "") -> dict:
        with self.lock:
            lessons = (self.store.search_memories(q, project or None, limit=100) if q.strip()
                       else self.store.list_memories(project or None))
            names = {b.id: b.name for b in self.store.list_boards(status=None)}
        for m in lessons:
            m["board_name"] = names.get(m["from_board"])
        return {"lessons": lessons, "projects": sorted({m["project"] for m in lessons})}


def make_handler(viewer: Viewer):
    # Read once at startup so the page and this server always come from the same version. (Reading it
    # per request let a newer page call actions an older running server didn't have.)
    page_bytes = resources.files("huddlehut").joinpath("viewer.html").read_bytes()

    def page() -> bytes:
        return page_bytes

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:  # keep the terminal quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data, code: int = 200) -> None:
            self._send(code, json.dumps(data).encode(), "application/json")

        def do_POST(self) -> None:
            # The page sends a custom header, which other websites can't send to localhost without a CORS
            # preflight we never allow, so a random page can't end your sessions.
            origin = self.headers.get("Origin")
            host = self.headers.get("Host", "")
            if self.headers.get("X-Huddlehut") != "1" or (origin and origin.split("://", 1)[-1] != host):
                self._json({"error": "forbidden"}, 403)
                return
            parts = urlparse(self.path).path.strip("/").split("/")
            try:
                length = min(int(self.headers.get("Content-Length") or 0), 10_000)
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
                if len(parts) == 6 and parts[:2] == ["api", "boards"] and parts[3] == "agents" and parts[5] == "end":
                    self._json(viewer.end_session(parts[2], parts[4]))
                elif len(parts) == 4 and parts[:2] == ["api", "boards"] and parts[3] == "close":
                    self._json(viewer.close_board(parts[2], str(body.get("outcome") or "")))
                else:
                    self._json({"error": "this viewer doesn't know that action; it may be out of date. "
                                         "Restart huddlehut view"}, 404)
            except BoardError as e:
                self._json({"error": str(e)}, 404)
            except ValueError:
                self._json({"error": "bad request"}, 400)

        def do_GET(self) -> None:
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                if url.path in ("/", "/index.html"):
                    self._send(200, page(), "text/html; charset=utf-8")
                elif url.path == "/api/boards":
                    self._json(viewer.boards())
                elif url.path.startswith("/api/boards/"):
                    self._json(viewer.board(url.path.rsplit("/", 1)[-1]))
                elif url.path == "/api/memory":
                    self._json(viewer.memory(q.get("q", ""), q.get("project", "")))
                else:
                    self._json({"error": "not found"}, 404)
            except BoardError as e:
                self._json({"error": str(e)}, 404)

    return Handler


def serve(host: str = "127.0.0.1", port: int = 8787, open_browser: bool = True, store: Store | None = None):
    viewer = Viewer(store or Store())
    httpd = ThreadingHTTPServer((host, port), make_handler(viewer))
    url = f"http://{host}:{httpd.server_address[1]}"
    print(f"Huddle Hut viewer at {url} (Ctrl-C to stop)", flush=True)
    if open_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
