"""Two separate MCP server processes (as two agent sessions would spawn) talking through one board."""

import os
import re
import sys

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def _params(db, agent):
    env = {**os.environ, "HUDDLEHUT_DATABASE_URL": str(db), "HUDDLEHUT_AGENT": agent, "ANTHROPIC_API_KEY": ""}
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
            assert tools == {"create_board", "join_board", "post", "read_messages",
                             "close_board", "reopen_board", "search_memories"}

            # no board yet -> helpful error, no crash
            assert (await _call(codex, "post", content="hi")).startswith("error: no board.")

            created = await _call(claude, "create_board", name="parser rewrite", project="repo-x", goal="new parser")
            key = re.search(r"brd_\S+", created).group(0)
            # key is remembered for the session: no need to pass it again
            assert await _call(claude, "post", content="parser; own src/parser/*", kind="status") == "ok #1"
            assert await _call(claude, "post", content="spans on AST nodes?", kind="question") == "ok #2"

            joined = await _call(codex, "join_board", key=key, status="tests in tests/parser")
            assert "goal: new parser" in joined
            assert re.search(r"Now:\n#1 claude-code-\w{4}: parser; own src/parser/\*\n#3 codex-\w{4}: tests", joined)
            assert re.search(r"Open:\n#2 claude-code-\w{4}/question: spans on AST nodes\?", joined)
            assert joined.endswith("last_id=3")

            # codex answers; claude gets it piggybacked on its next post, without a read call
            assert await _call(codex, "post", content="yes", kind="answer", reply_to=2) == "ok #4"
            res = await _call(claude, "post", content="Pratt parser", kind="decision")
            assert re.fullmatch(r"ok #5\nNew:\n#3 codex-\w{4}/status: tests in tests/parser\n#4 codex-\w{4}/answer re #2 \(spans on AST nodes\?\): yes", res)
            # and nothing is delivered twice, nor its own posts echoed
            assert (await _call(claude, "read_messages")).startswith("(nothing new)")

            async def reply_later():
                await anyio.sleep(1.5)
                await _call(codex, "post", content="tests ready", kind="result")

            async with anyio.create_task_group() as tg:
                tg.start_soon(reply_later)
                waited = await _call(claude, "read_messages", wait_seconds=10)
            assert "tests ready" in waited and "Pratt" not in waited

            clash = await _call(codex, "post", content="also fixing src/parser/lexer.py", kind="status")
            assert re.search(r"Overlap: claude-code-\w{4} \(#1\) has src/parser\. Coordinate", clash)

            second = await _call(claude, "create_board", name="docs", project="repo-x")
            assert "Other open boards repo-x: parser rewrite" in second

            assert "Closed" in await _call(codex, "close_board", outcome="done")
            assert (await _call(codex, "post", content="late")).startswith("error")

    anyio.run(run)
