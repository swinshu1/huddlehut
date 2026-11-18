"""Two separate MCP server processes (as two agent sessions would spawn) talking through one board."""

import os
import re
import sys

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

CLIENT_VARS = ("HUDDLEHUT_SESSION", "CLAUDE_CODE_HOST_SESSION_ID", "CLAUDE_CODE_SESSION_ID")


def _params(db, agent, **extra):
    # drop the session ids of whatever is running the tests: each test server is its own session
    env = {k: v for k, v in os.environ.items() if k not in CLIENT_VARS}
    env.update({"HUDDLEHUT_DATABASE_URL": str(db), "HUDDLEHUT_AGENT": agent, "ANTHROPIC_API_KEY": "", **extra})
    return StdioServerParameters(command=sys.executable, args=["-m", "huddlehut.server"], env=env)


async def _call(session, tool, **args):
    res = await session.call_tool(tool, args)
    return res.content[0].text


def test_claude_and_codex_sessions_share_a_board(db_url):
    db = db_url

    async def run():
        async with stdio_client(_params(db, "claude-code")) as (r1, w1), ClientSession(r1, w1) as claude, \
                   stdio_client(_params(db, "codex")) as (r2, w2), ClientSession(r2, w2) as codex:
            await claude.initialize()
            await codex.initialize()
            tools = {t.name for t in (await codex.list_tools()).tools}
            assert tools == {"create_board", "join_board", "post", "read_messages", "files",
                             "close_board", "reopen_board", "search_memories", "goal"}

            # no board yet -> helpful error, no crash
            assert (await _call(codex, "post", content="hi")).startswith("error: no board.")

            created = await _call(claude, "create_board", name="parser rewrite", project="repo-x", goal="new parser",
                                  nickname="Mochi", model="claude-opus-5-5")
            key = re.search(r"brd_\S+", created).group(0)
            assert "You're mochi here." in created
            # key is remembered for the session: no need to pass it again
            assert await _call(claude, "post", content="parser; own src/parser/*", kind="status") == "ok #1"
            assert await _call(claude, "post", content="spans on AST nodes?", kind="question") == "ok #2"

            joined = await _call(codex, "join_board", key=key, nickname="acorn", model="gpt-5-codex",
                                 status="tests in tests/parser")
            assert "You're acorn here." in joined and "goal: new parser" in joined
            # nicknames everywhere; the model shows in the roster only; "own src/parser/*" went into the inventory
            assert "Now:\n#1 mochi (opus 5.5): parser\n#3 acorn (gpt-5-codex): tests in tests/parser" in joined
            assert "Files:\nmochi: src/parser/*" in joined
            assert "Open:\n#2 mochi/question: spans on AST nodes?" in joined
            assert joined.endswith("last_id=3")

            # codex answers; claude gets it piggybacked on its next post, without a read call
            assert await _call(codex, "post", content="yes", kind="answer", reply_to=2) == "ok #4"
            res = await _call(claude, "post", content="Pratt parser", kind="decision")
            assert res == "ok #5\nNew:\n#3 acorn/status: tests in tests/parser\n#4 acorn/answer re #2 (spans on AST nodes?): yes"
            # and nothing is delivered twice, nor its own posts echoed
            assert (await _call(claude, "read_messages")).startswith("(nothing new)")

            async def reply_later():
                await anyio.sleep(1.5)
                await _call(codex, "post", content="tests ready", kind="result")

            async with anyio.create_task_group() as tg:
                tg.start_soon(reply_later)
                waited = await _call(claude, "read_messages", wait_seconds=10)
            assert "tests ready" in waited and "Pratt" not in waited

            inv = await _call(claude, "files", claim=["src/parser/"])
            assert inv == "Files:\nmochi: src/parser/*"
            clash = await _call(codex, "files", claim=["src/parser/lexer.py", "tests/parser/test_lexer.py"])
            assert "not claimed: src/parser/lexer.py held by mochi" in clash
            assert "acorn: tests/parser/test_lexer.py" in clash
            assert "src/parser/*" in (await _call(codex, "join_board", key=key, nickname="acorn"))  # inventory
            done = await _call(claude, "files", release=["*"])
            assert "mochi" not in done
            locked = await _call(claude, "files", lock=["src/schema.py"])
            assert "mochi: src/schema.py (locked)" in locked
            assert "not locked: src/schema.py held by" in await _call(codex, "files", lock=["src/schema.py"])
            assert "(locked)" not in await _call(claude, "files", unlock=["*"])

            second = await _call(claude, "create_board", name="docs", project="repo-x", nickname="mochi")
            assert "Other open boards repo-x: parser rewrite" in second

            assert "Closed" in await _call(codex, "close_board", outcome="done")
            assert (await _call(codex, "post", content="late")).startswith("error")

    anyio.run(run)
