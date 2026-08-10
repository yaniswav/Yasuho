"""What may and may not reach the durable log.

PRIVACY.md makes two claims that no database column can keep on its own:

* "We do not store message content";
* every personal record has an erasure verb (`?mydata deleteprofile`).

A log FILE is storage - durable, greppable, and reachable by no erasure path
the bot has. Whatever is written there outlives the row it describes and
survives the delete the user asked for. So the log lines that had a user id or
a member's own typed text in them are the ones this file pins, by driving the
real code paths and reading what actually came out.

They were found by the same audit and share one lesson: an operator diagnoses a
failure from WHICH command failed and HOW MUCH of it there was, never from the
payload.

The vote webhook appears twice below because it is dispatched twice - both the
webstats cog that receives it and the votes cog that banks it listen for
``on_dbl_vote``, so a payload dump had to be removed from each.
"""

import logging
import types

import pytest

from cogs.community import votes
from cogs.system import arg_completion, webstats

VOTER_ID = "228895251576782858"


# ---------------------------------------------------------------------------
# The top.gg vote webhook (cogs/system/webstats.py)
# ---------------------------------------------------------------------------


def _payload(**over):
    """A top.gg vote payload, in the shape the webhook really delivers."""
    data = {
        "user": VOTER_ID,
        "type": "upvote",
        "bot": "531891873527291930",
        "isWeekend": False,
        "query": "?ref=someone",
    }
    data.update(over)
    return data


async def test_a_vote_never_writes_the_payload_to_the_log(caplog):
    """THE regression. Every vote used to be dumped verbatim.

    The payload carries the voter's id (and whatever query string the vote url
    was called with), so a bot that has been up for a year holds a plaintext
    ledger of who voted and when, in a file `?mydata deleteprofile` cannot
    touch. The vote ROW is erasable; a log line is not.
    """
    cog = object.__new__(webstats.Webstats)
    with caplog.at_level(logging.DEBUG, logger=webstats.__name__):
        await webstats.Webstats.on_dbl_vote(cog, _payload())

    assert caplog.text, "the vote is still observable to an operator"
    assert VOTER_ID not in caplog.text
    assert "?ref=someone" not in caplog.text


async def test_a_test_vote_is_still_announced_without_its_payload(caplog):
    """A test vote is an operator's own webhook check, so it stays at INFO -
    but it carries the same shape of payload and gets the same treatment."""
    cog = object.__new__(webstats.Webstats)
    with caplog.at_level(logging.INFO, logger=webstats.__name__):
        await webstats.Webstats.on_dbl_vote(cog, _payload(type="test"))

    assert "test vote" in caplog.text
    assert VOTER_ID not in caplog.text


# ---------------------------------------------------------------------------
# The SECOND listener on the same event (cogs/community/votes.py)
# ---------------------------------------------------------------------------
#
# discord.py dispatches on_dbl_vote to every cog that listens, so the same
# payload reaches two modules. Fixing one of them is half a fix: the sweep is
# only done when neither writes the payload down.


async def test_the_votes_cog_drops_a_test_payload_without_logging_it(caplog):
    """The other half of the same sweep.

    This listener's test-vote branch dumped the whole dict. It is a tester's own
    id rather than a voter's, which is why it is the smaller finding - and no
    reason at all to keep a durable line nobody can erase.
    """
    cog = object.__new__(votes.Votes)
    with caplog.at_level(logging.DEBUG, logger=votes.__name__):
        await votes.Votes.on_dbl_vote(cog, _payload(type="test"))

    assert "test vote" in caplog.text  # still observable to an operator...
    assert VOTER_ID not in caplog.text  # ... without the payload
    assert "?ref=someone" not in caplog.text


async def test_the_votes_cog_names_the_type_of_an_unknown_payload_only(caplog):
    """The neighbouring branch, which was already right and must stay so: an
    unrecognised payload is worth waking somebody for, and its TYPE is the whole
    diagnostic."""
    cog = object.__new__(votes.Votes)
    with caplog.at_level(logging.DEBUG, logger=votes.__name__):
        await votes.Votes.on_dbl_vote(cog, _payload(type="something-new"))

    assert "something-new" in caplog.text
    assert VOTER_ID not in caplog.text
    assert "?ref=someone" not in caplog.text


# ---------------------------------------------------------------------------
# Interactive argument completion (cogs/system/arg_completion.py)
# ---------------------------------------------------------------------------


class _FakeBot:
    def __init__(self, error):
        self.error = error

    async def get_context(self, message):
        raise self.error

    async def invoke(self, ctx):  # pragma: no cover - never reached
        raise AssertionError("get_context failed first")


def _view(content_typed):
    """The few attributes ``_CompletionView._reinvoke`` actually reads."""
    message = types.SimpleNamespace(id=1234, content="?warn")
    view = object.__new__(arg_completion._CompletionView)
    view.ctx = types.SimpleNamespace(
        message=message, bot=_FakeBot(RuntimeError("converter exploded"))
    )
    view.command = types.SimpleNamespace(qualified_name="warn")
    view.provided = {"member", "reason"}
    view._typed = content_typed
    return view


async def test_a_failed_reinvoke_logs_the_command_and_not_what_was_typed(caplog):
    """THE regression. The rebuilt command line was logged verbatim.

    That line is the member's own words - the reason on a ban, the text of a
    reminder, the body of a tag - which is exactly the thing PRIVACY.md says
    the bot does not store. It is also of no diagnostic use: what failed is the
    command, not the sentence.
    """
    secret = '?warn @Rohan "he keeps posting my address in general"'
    view = _view(secret)

    with caplog.at_level(logging.ERROR, logger=arg_completion.__name__):
        await arg_completion._CompletionView._reinvoke(view, secret)

    assert "re-invoke failed" in caplog.text  # the failure is still visible...
    assert "warn" in caplog.text  # ... and says which command it was...
    assert "2 argument(s)" in caplog.text  # ... and how much was filled in.
    assert "address" not in caplog.text
    assert "Rohan" not in caplog.text


async def test_the_reinvoke_restores_the_shared_message_whatever_happens():
    """Unchanged by the logging fix, and the reason the content is in hand at
    all: the cached Message object is shared, so it is put back verbatim."""
    view = _view("?warn @Rohan late")
    original = view.ctx.message.content

    await arg_completion._CompletionView._reinvoke(view, "?warn @Rohan late")

    assert view.ctx.message.content == original


@pytest.mark.parametrize(
    "module",
    (webstats, arg_completion, votes),
    ids=("webstats", "arg_completion", "votes"),
)
def test_neither_module_formats_a_whole_payload_into_a_log_line(module):
    """A cheap structural backstop over the two behavioural tests above.

    ``log.<level>("...%s", data)`` on a whole dict or a rebuilt command line is
    the shape of both findings; catching the shape is what stops the next one
    being written by hand a year from now.
    """
    import inspect
    import re

    source = inspect.getsource(module)
    for match in re.finditer(r"log\.\w+\(\s*(?:\n\s*)?(.+?)\)\n", source, re.S):
        line = match.group(1)
        assert ", data" not in line, line
        assert ", content" not in line, line
