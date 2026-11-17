# -*- coding: utf-8 -*-
"""Every cog that starts a background loop must start it ONCE, however many times
`on_ready` arrives.

discord.py dispatches `on_ready` again on each re-IDENTIFY, and a dropped internet
connection causes exactly that as soon as the gateway session is too old to RESUME.
A cog that calls `create_task` there without a guard overwrites its own attribute,
so the loop it replaced keeps running with nothing able to reach or cancel it: the
work doubles with every reconnect and only restarting the bot clears it.

`reminders` (three loops) and `messages_analysis` (one that posts the weekly
ranking, clears it and moves the Spamer role) both did this until 2026-11-17. The
test drives `on_ready` several times against fakes and counts the live tasks.

Run from the repo root: python -m pytest tests/test_reconnect_loops.py
"""

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


# ---------- fakes ----------

class FakeMessage:
    async def delete(self):
        return None


class FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, embed=None):
        self.sent.append(content if content is not None else embed)
        return FakeMessage()


class FakeContext:
    def __init__(self):
        self.sent = []
        self.guild = None

    async def send(self, content=None, embed=None):
        self.sent.append(content if content is not None else embed)


class FakeBot:
    """Only what these two cogs touch in on_ready."""

    def __init__(self):
        self.channel = FakeChannel()
        self.ctx = FakeContext()
        self.loop = asyncio.get_event_loop()
        self.created = []

    def get_channel(self, _id):
        return self.channel

    async def get_context(self, _msg):
        return self.ctx

    def create_task(self, coro):
        task = asyncio.ensure_future(coro)
        self.created.append(task)
        return task


async def _idle():
    """Stands in for a `while True` loop: never finishes on its own."""
    await asyncio.Event().wait()


def _live(bot):
    return [task for task in bot.created if not task.done()]


async def _drain(bot):
    for task in bot.created:
        task.cancel()
    await asyncio.gather(*bot.created, return_exceptions=True)


# ---------- reminders: three loops ----------

@pytest.mark.asyncio
async def test_reminders_survives_five_reconnects_with_three_loops(monkeypatch):
    from cogs import reminders as mod

    bot = FakeBot()
    # the real loop bodies would sit on the clock for a week; the point here is how
    # MANY of them exist, not what they do
    monkeypatch.setattr(mod.reminders, "vote_reminder", lambda self, ctx: _idle())
    monkeypatch.setattr(mod.reminders, "command_reminder", lambda self, ctx: _idle())
    monkeypatch.setattr(mod.reminders, "news_reminder", lambda self, ctx: _idle())

    cog = mod.reminders(bot)
    monkeypatch.setattr(bot, "loop", bot)        # bot.loop.create_task -> our counter

    for _ in range(5):                            # the outage, five times over
        await cog.on_ready()

    assert len(_live(bot)) == 3, "one loop each, not three per reconnect"
    # and the context dance (a message posted then deleted) happened only once
    assert bot.channel.sent.count("🔧 Przygotowuję kontekst...") == 1

    cog.cog_unload()
    await _drain(bot)
    assert _live(bot) == []


@pytest.mark.asyncio
async def test_reminders_revives_a_loop_that_died(monkeypatch):
    """A guard that only remembers "started once" would leave a crashed loop dead
    for good. These revive."""
    from cogs import reminders as mod

    bot = FakeBot()
    monkeypatch.setattr(mod.reminders, "vote_reminder", lambda self, ctx: _idle())
    monkeypatch.setattr(mod.reminders, "command_reminder", lambda self, ctx: _idle())
    monkeypatch.setattr(mod.reminders, "news_reminder", lambda self, ctx: _idle())
    cog = mod.reminders(bot)
    monkeypatch.setattr(bot, "loop", bot)

    await cog.on_ready()
    cog.task2.cancel()                            # one loop dies on its own
    await asyncio.gather(cog.task2, return_exceptions=True)

    await cog.on_ready()
    assert len(_live(bot)) == 3

    cog.cog_unload()
    await _drain(bot)


# ---------- messages_analysis: the weekly ranking loop ----------

@pytest.mark.asyncio
async def test_messages_analysis_keeps_one_ranking_loop(monkeypatch):
    """This loop posts the weekly ranking, then clears it and moves the Spamer
    role, so a second copy races the first over the same JSON file and role."""
    from cogs import messages_analysis as mod

    bot = FakeBot()
    monkeypatch.setattr(mod.messages_analysis, "msg1", lambda self, ctx: _idle())

    cog = mod.messages_analysis(bot)
    monkeypatch.setattr(bot, "loop", bot)

    for _ in range(4):
        await cog.on_ready()

    assert len(_live(bot)) == 1
    # the chat announcement is not repeated on every reconnect either
    assert bot.ctx.sent.count("Horus gotowy do szpiegowania.") == 1

    cog.cog_unload()
    await _drain(bot)
    assert _live(bot) == []
