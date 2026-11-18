"""Every agent stays in the loop: huddlers on join, @you flags, unknown-mention feedback, hooks-off nudge."""

import os
import re
import sys

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def _params(db, agent):
    env = {**os.environ, "HUDDLEHUT_DATABASE_URL": str(db), "HUDDLEHUT_AGENT": agent, "ANTHROPIC_API_KEY": "",
           "HUDDLEHUT_HOOK_GRACE": "0"}
    return StdioServerParameters(command=sys.executable, args=["-m", "huddlehut.server"], env=env)


async def _call(session, tool, **args):
    return (await session.call_tool(tool, args)).content[0].text


def test_agents_stay_in_the_loop(db_url):
    async def run():
        async with stdio_client(_params(db_url, "claude-code")) as (r1, w1), ClientSession(r1, w1) as claude, \
                   stdio_client(_params(db_url, "codex")) as (r2, w2), ClientSession(r2, w2) as codex:
            await claude.initialize()
            await codex.initialize()
            created = await _call(claude, "create_board", name="b", project="p", nickname="mochi")
            key = re.search(r"brd_\S+", created).group(0)
            joined = await _call(codex, "join_board", key=key, nickname="acorn", model="gpt-5-codex")
            assert re.search(r"Huddlers: acorn \(gpt-5-codex\), mochi", joined)          # who you can tag

            res = await _call(claude, "post", content="@acorn rerun the check pls; cc @pebble", kind="question")
            assert "@pebble isn't on this board, so they won't see it. Huddlers here: acorn, mochi." in res

            # codex: the mention is flagged; hooks never ran in this test, so it's told once to poll
            got = await _call(codex, "read_messages")
            assert re.search(r"^@you #\d+ mochi/question: @acorn rerun", got, re.M)
            assert "no hook delivery here" in got
            again = await _call(codex, "post", content="on it", kind="answer", reply_to=2)
            assert "no hook delivery" not in again                                        # once per board

    anyio.run(run)
