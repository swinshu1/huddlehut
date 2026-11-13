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
from importlib import resources
from urllib.parse import parse_qs, urlparse

from .compact import presence_tag
from .store import IDLE_AFTER, BoardError, Message, Store


def _msg(m: Message, snippets: dict[int, str]) -> dict:
    return {"id": m.id, "author": m.author, "kind": m.kind, "content": m.content, "at": m.created_at,
            "reply_to": m.reply_to, "reply_snippet": snippets.get(m.reply_to) if m.reply_to else None}


class Viewer:
    """JSON for the page. One Store, guarded by a lock (the HTTP server is threaded)."""

    def __init__(self, store: Store):
        self.store = store
        self.lock = threading.Lock()

    def boards(self) -> list[dict]:
        with self.lock:
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

    def board(self, board_id: str, full: bool = False) -> dict:
        from .compact import NOTES_SHOWN

        with self.lock:
            s = self.store
            b = s.get_board(board_id)
            seen = s.presence(b.id)
            statuses, open_items = s.current_statuses(b.id), s.open_items(b.id)
            log = s.log_after(b.id, b.summary_through)
            note_ids = [m.id for m in log if m.kind == "note"]
            hidden = set(note_ids[:-NOTES_SHOWN]) if len(note_ids) > NOTES_SHOWN else set()
            log = [m for m in log if m.id not in hidden]
            transcript = s.all_messages(b.id) if full else []
            msgs = statuses + open_items + log + transcript
            snippets = s.snippets({m.reply_to for m in msgs if m.reply_to})
            agents = {a: {"tag": presence_tag(p).strip(" ()"), "state": p["state"], "seen_at": p["seen_at"],
                          "branch": p.get("branch")} for a, p in seen.items()}
            return {
                "board": {"id": b.id, "name": b.name, "project": b.project, "goal": b.goal, "status": b.status,
                          "created_at": b.created_at, "closed_at": b.closed_at, "outcome": b.outcome,
                          "distill_status": b.distill_status},
                "agents": agents,
                "now": [_msg(m, snippets) for m in statuses],
                "files": [{"agent": c["agent"], "path": c["path"], "locked": bool(c.get("locked"))} for c in s.claims(b.id)],
                "open": [_msg(m, snippets) for m in open_items],
                "summary": b.summary,
                "log": [_msg(m, snippets) for m in log],
                "hidden_notes": len(hidden),
                "message_count": s.board_stats().get(b.id, {}).get("messages", 0),
                "transcript": [_msg(m, snippets) for m in transcript] if full else None,
                "lessons": s.list_memories(board_id=b.id),
            }

    def memory(self, q: str = "", project: str = "") -> dict:
        with self.lock:
            lessons = (self.store.search_memories(q, project or None, limit=100) if q.strip()
                       else self.store.list_memories(project or None))
            names = {b.id: b.name for b in self.store.list_boards(status=None)}
        for m in lessons:
            m["board_name"] = names.get(m["from_board"])
        return {"lessons": lessons, "projects": sorted({m["project"] for m in lessons})}


def make_handler(viewer: Viewer):
    def page() -> bytes:  # read per request: it's small, and edits show up without a restart
        return resources.files("huddlehut").joinpath("viewer.html").read_bytes()

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

        def do_GET(self) -> None:
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                if url.path in ("/", "/index.html"):
                    self._send(200, page(), "text/html; charset=utf-8")
                elif url.path == "/api/boards":
                    self._json(viewer.boards())
                elif url.path.startswith("/api/boards/"):
                    self._json(viewer.board(url.path.rsplit("/", 1)[-1], full=q.get("full") == "1"))
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
