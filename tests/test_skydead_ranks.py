# -*- coding: utf-8 -*-
"""SkyDead ranks cog: rank parsing and role sync against fake Discord/DB objects.

Run from the repo root: python -m pytest tests/test_skydead_ranks.py
"""

import asyncio
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import asyncpg  # noqa: E402

from cogs import skydead_ranks as sr  # noqa: E402


# ---------- fakes ----------

class FakeRole:
    def __init__(self, rid, name):
        self.id = rid
        self.name = name

    def __repr__(self):
        return f"<Role {self.name}>"


class FakeMember:
    def __init__(self, mid, roles=()):
        self.id = mid
        self.roles = list(roles)
        self.display_name = f"user{mid}"

    async def add_roles(self, *roles, reason=None):
        for r in roles:
            if r not in self.roles:
                self.roles.append(r)

    async def remove_roles(self, *roles, reason=None):
        self.roles = [r for r in self.roles if r not in roles]


class FakeGuild:
    def __init__(self, roles=(), members=()):
        self.roles = list(roles)
        self.members = {m.id: m for m in members}
        self.created = []

    def get_role(self, rid):
        return next((r for r in self.roles if r.id == rid), None)

    def get_member(self, mid):
        return self.members.get(mid)

    async def create_role(self, name, **kw):
        role = FakeRole(9000 + len(self.roles), name)
        self.roles.append(role)
        self.created.append(name)
        return role


class FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, msg, **kw):
        self.sent.append(msg)


class FakeConn:
    def __init__(self, rows, error=None):
        self.rows, self.error = rows, error
        self.queries = []

    def transaction(self, readonly=False):
        assert readonly
        conn = self

        class Tx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *a):
                return False
        return Tx()

    async def fetch(self, query, *args):
        self.queries.append((query, args))
        if self.error:
            raise self.error
        return self.rows


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *a):
                return False
        return Ctx()


class FakeBot:
    def __init__(self, guild, rows, error=None):
        self.guild = guild
        self.channel = FakeChannel()
        self.pg_con = FakePool(FakeConn(rows, error))

    def get_guild(self, gid):
        return self.guild

    def get_channel(self, cid):
        return self.channel


def row(login, kills, discord_id=None):
    return {"login": login, "kills": kills, "discord_id": discord_id}


@pytest.fixture(autouse=True)
def in_tmp(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # STATE_FILE is relative to the working directory
    yield


def make_cog(guild, rows, error=None):
    bot = FakeBot(guild, rows, error)
    cog = sr.SkyDeadRanks(bot)
    cog.ranks = list(sr.DEFAULT_RANKS)
    cog.config_roles = [0] * len(cog.ranks)
    return cog, bot


def run(coro):
    return asyncio.run(coro)


# ---------- ranks ----------

def test_load_ranks_from_bot_guide():
    if not sr.SKYDEAD_YAML.exists():
        pytest.skip("twitch-rpg-game-alterage not next to HorusBot")
    assert sr.load_ranks() == sr.DEFAULT_RANKS


def test_load_ranks_parses_list_and_falls_back(tmp_path):
    y = tmp_path / "skydead.yaml"
    y.write_text("skydead:\n  ranks:\n  - {kills: 2, name: A}\n  - {kills: 1, name: 'B'}\n\n  other: 1\n",
                 encoding="utf-8")
    assert sr.load_ranks(y) == [(1, "B"), (2, "A")]
    assert sr.load_ranks(tmp_path / "missing.yaml") == sr.DEFAULT_RANKS


def test_tier_for():
    ranks = sr.DEFAULT_RANKS
    assert [sr.tier_for(n, ranks) for n in (0, 1, 4, 5, 99, 100, 500)] == [-1, 0, 0, 1, 4, 5, 5]


# ---------- sync ----------

def test_creates_missing_roles_and_gives_highest_only():
    lowest = FakeRole(1, "Zwiadowca Nieba")  # already on the server: found by name, not created
    m1 = FakeMember(111, roles=[lowest])
    m2 = FakeMember(222)
    guild = FakeGuild(roles=[lowest], members=[m1, m2])
    cog, bot = make_cog(guild, [row("hunter", 7, "111"), row("rookie", 1, "222"), row("ghost", 3, None)])
    result = run(cog.sync_roles())
    assert guild.created == [n for _, n in sr.DEFAULT_RANKS[1:]]
    assert [r.name for r in m1.roles] == ["Łowca Skyborne"]  # lower rank removed
    assert [r.name for r in m2.roles] == ["Zwiadowca Nieba"]
    assert result["promoted"] == 2
    text = "\n".join(bot.channel.sent)
    assert "<@111> zdobywa rangę **Łowca Skyborne** za **7 zabójstw**" in text
    assert "<@222> zdobywa rangę **Zwiadowca Nieba** za **1 zabójstwo**" in text
    assert "**ghost** ma już **3 zabójstwa**" in text and "!polacz" in text
    # IDs remembered; a second sync changes and announces nothing
    bot.channel.sent.clear()
    cog2 = sr.SkyDeadRanks(bot)
    cog2.ranks, cog2.config_roles = list(sr.DEFAULT_RANKS), [0] * 6
    run(cog2.sync_roles())
    assert len(guild.roles) == 6 and bot.channel.sent == []


def test_dry_run_changes_nothing():
    m = FakeMember(111)
    guild = FakeGuild(members=[m])
    cog, bot = make_cog(guild, [row("hunter", 30, "111")])
    result = run(cog.sync_roles(dry_run=True))
    assert guild.created == [] and m.roles == [] and bot.channel.sent == []
    assert len(result["created"]) == 6
    assert "Łamacz Skrzydeł" in result["messages"][0]


def test_missing_tables_or_db_error_leave_roles():
    m = FakeMember(111, roles=[FakeRole(5, "Łowca Skyborne")])
    guild = FakeGuild(roles=list(m.roles), members=[m])
    cog, _ = make_cog(guild, [], error=asyncpg.exceptions.UndefinedTableError("skydead_kills"))
    assert run(cog.sync_roles()) is None
    assert guild.created == [] and len(m.roles) == 1
    cog, _ = make_cog(guild, [], error=RuntimeError("down"))
    assert run(cog.sync_roles()) is None


def test_best_of_several_twitch_logins_and_absent_member():
    m = FakeMember(111)
    guild = FakeGuild(members=[m])
    cog, bot = make_cog(guild, [row("alt", 2, "111"), row("main", 16, "111"), row("left", 5, "999")])
    run(cog.sync_roles())
    assert [r.name for r in m.roles] == ["Pogromca Wiatru"]


def test_query_reads_links_kills_and_discord_mapping():
    q = sr.COUNTS_QUERY
    assert "FROM skydead_links" in q and "JOIN skydead_kills k ON k.killer_key = l.character_key" in q
    assert "chatters_information" in q and "discord_id" in q
