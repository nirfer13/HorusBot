# -*- coding: utf-8 -*-
"""External XP import (the file the RPG bot writes): gains AND deductions.

The RPG bot writes ``{"<discord id>": <xp>}``. A positive amount is XP earned in
a fight; a NEGATIVE amount is a deduction - the victim of an XP theft
(``$kradnijxp``). Until 2026-11-20 every amount <= 0 was skipped, so a theft
created 2500 XP for the thief and took nothing from the victim.

These tests run `_process_external_xp_file` against a real PostgreSQL server,
because the rules live in the SQL (no row is created for a deduction, XP never
goes below zero, gains and deductions of one file are one transaction). They are
skipped when no test server answers. The server is taken from
HORUS_TEST_DATABASE_URL (default: the local throw-away server on port 55432);
a database named ``horus_test_<pid>`` is created there and dropped afterwards.
NEVER point this at a production server.

Run from the repo root: python -m pytest tests/test_external_xp.py
"""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import asyncpg  # noqa: E402

from cogs import discord_xp  # noqa: E402

SERVER = os.environ.get(
    "HORUS_TEST_DATABASE_URL",
    "postgresql://tester@127.0.0.1:55432",
).rstrip("/")
DATABASE = f"horus_test_{os.getpid()}"

THIEF, VICTIM, POOR, STRANGER, NEWCOMER = 111, 222, 333, 444, 555


class FakeBot:
    def __init__(self, pool):
        self.pg_con = pool

    def get_guild(self, guild_id):
        return None          # names fall back to the id; the SQL keeps the stored name


def make_cog(pool):
    """The cog without its __init__ (which reads and may create the real config file)."""
    cog = discord_xp.DiscordXP.__new__(discord_xp.DiscordXP)
    cog.bot = FakeBot(pool)
    cog.config = {"guild_id": 1, "role_rewards_enabled": True}
    cog.role_sync_calls = []
    cog.flush_calls = 0

    async def update_roles(discord_ids, **kwargs):
        cog.role_sync_calls.append(set(discord_ids))

    async def flush_pending_xp():
        cog.flush_calls += 1

    cog._update_roles_for_users = update_roles
    cog.flush_pending_xp = flush_pending_xp
    return cog


async def _with_database(scenario):
    try:
        admin = await asyncpg.connect(f"{SERVER}/postgres", timeout=5)
    except Exception:  # noqa: BLE001 - no test server here
        pytest.skip("PostgreSQL test server is not reachable (see the module docstring).")
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{DATABASE}" WITH (FORCE)')
        await admin.execute(f'CREATE DATABASE "{DATABASE}"')
        pool = await asyncpg.create_pool(f"{SERVER}/{DATABASE}", min_size=1, max_size=2)
        try:
            await pool.execute(
                """
                CREATE TABLE chatters_information_discord (
                    chtr_id BIGINT PRIMARY KEY,
                    chtr_name TEXT,
                    xp BIGINT NOT NULL DEFAULT 0,
                    chatstreak INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            await pool.executemany(
                "INSERT INTO chatters_information_discord (chtr_id, chtr_name, xp, chatstreak) "
                "VALUES ($1, $2, $3, 7)",
                [(THIEF, "zlodziej", 100), (VICTIM, "ofiara", 3000), (POOR, "biedak", 1000)],
            )
            return await scenario(pool)
        finally:
            await pool.close()
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{DATABASE}" WITH (FORCE)')
        await admin.close()


def run(scenario):
    return asyncio.run(_with_database(scenario))


async def xp_table(pool):
    rows = await pool.fetch("SELECT chtr_id, chtr_name, xp, chatstreak FROM chatters_information_discord")
    return {row["chtr_id"]: (row["chtr_name"], row["xp"], row["chatstreak"]) for row in rows}


def write(tmp_path, content):
    path = tmp_path / "expSummary.processing.abc.json"
    path.write_text(json.dumps(content), encoding="utf-8")
    return path


def test_theft_gives_to_the_thief_and_takes_from_the_victim(tmp_path):
    async def scenario(pool):
        cog = make_cog(pool)
        path = write(tmp_path, {str(THIEF): 2500, str(VICTIM): -2500})
        await cog._process_external_xp_file(path)
        assert await xp_table(pool) == {
            THIEF: ("zlodziej", 2600, 7),
            VICTIM: ("ofiara", 500, 7),           # 3000 - 2500, name and streak untouched
            POOR: ("biedak", 1000, 7),
        }
        assert not path.exists()                   # processed: the file is gone
        assert cog.role_sync_calls == [{THIEF, VICTIM}]   # the victim's rank is re-checked too
        assert cog.flush_calls == 1                # unsaved chat XP was saved before the deduction

    run(scenario)


def test_deduction_never_goes_below_zero(tmp_path):
    async def scenario(pool):
        cog = make_cog(pool)
        await cog._process_external_xp_file(write(tmp_path, {str(POOR): -2500}))
        assert (await xp_table(pool))[POOR] == ("biedak", 0, 7)
        assert cog.role_sync_calls == [{POOR}]

    run(scenario)


def test_deduction_creates_no_row_for_an_unknown_user(tmp_path):
    async def scenario(pool):
        cog = make_cog(pool)
        path = write(tmp_path, {str(STRANGER): -2500, str(NEWCOMER): 300})
        await cog._process_external_xp_file(path)
        table = await xp_table(pool)
        assert STRANGER not in table               # nothing to take: no row appears
        assert table[NEWCOMER] == (str(NEWCOMER), 300, 0)   # a gain still creates the row
        assert not path.exists()
        assert cog.role_sync_calls == [{NEWCOMER}]  # nobody's roles are touched without a change

    run(scenario)


def test_gains_work_as_before(tmp_path):
    async def scenario(pool):
        cog = make_cog(pool)
        await cog._process_external_xp_file(write(tmp_path, {str(THIEF): 250, str(VICTIM): "500"}))
        table = await xp_table(pool)
        assert table[THIEF] == ("zlodziej", 350, 7) and table[VICTIM] == ("ofiara", 3500, 7)
        assert cog.role_sync_calls == [{THIEF, VICTIM}]
        assert cog.flush_calls == 0                # gains alone need no flush

    run(scenario)


def test_zero_and_broken_entries_are_ignored(tmp_path):
    async def scenario(pool):
        cog = make_cog(pool)
        before = await xp_table(pool)
        path = write(tmp_path, {str(THIEF): 0, "not-an-id": 50, str(VICTIM): "dużo", "-5": -100, "0": 10})
        await cog._process_external_xp_file(path)
        assert await xp_table(pool) == before and not path.exists()
        assert cog.role_sync_calls == []

        empty = write(tmp_path, {})
        await cog._process_external_xp_file(empty)
        assert not empty.exists() and cog.role_sync_calls == []

    run(scenario)


def test_failure_keeps_the_file_and_changes_nothing(tmp_path):
    """Gains and deductions of one file are one transaction: all of it or none."""
    async def scenario(pool):
        cog = make_cog(pool)
        before = await xp_table(pool)
        # The deduction fails inside the database (a rule of the table refuses it) after
        # the gain of the same file was already written in the transaction.
        await pool.execute(
            "ALTER TABLE chatters_information_discord "
            f"ADD CONSTRAINT untouchable CHECK (chtr_id <> {POOR} OR xp >= 1000)"
        )
        path = write(tmp_path, {str(THIEF): 2500, str(POOR): -100})
        await cog._process_external_xp_file(path)
        assert await xp_table(pool) == before      # the thief's gain was rolled back as well
        assert path.exists()                        # kept for the next try
        assert cog.role_sync_calls == []
        assert cog._imported_external_files() == set()   # not saved: it WILL be tried again

    run(scenario)


def test_roles_are_left_alone_when_role_rewards_are_off(tmp_path):
    async def scenario(pool):
        cog = make_cog(pool)
        cog.config["role_rewards_enabled"] = False
        await cog._process_external_xp_file(write(tmp_path, {str(VICTIM): -100}))
        assert (await xp_table(pool))[VICTIM][1] == 2900 and cog.role_sync_calls == []

    run(scenario)


def test_unsaved_chat_xp_counts_before_the_floor_at_zero(tmp_path):
    """The victim's XP that Horus still holds in memory is part of what can be taken."""
    from collections import defaultdict

    async def scenario(pool):
        cog = make_cog(pool)
        del cog.flush_pending_xp                    # the real one this time
        cog.pending_xp = defaultdict(int, {VICTIM: 400})
        cog.pending_usernames = {VICTIM: "ofiara"}
        cog.pending_reaction_bonuses = {}
        cog.pending_lock = asyncio.Lock()
        await cog._process_external_xp_file(write(tmp_path, {str(VICTIM): -3200}))
        # 3000 saved + 400 unsaved - 3200 = 200. (Without the flush: 0 now, and 400 back later.)
        assert (await xp_table(pool))[VICTIM] == ("ofiara", 200, 7)
        assert not cog.pending_xp

    run(scenario)


def test_absurd_numbers_do_not_block_the_file(tmp_path):
    async def scenario(pool):
        cog = make_cog(pool)
        path = write(tmp_path, {str(VICTIM): -10 ** 30, str(2 ** 63): -5, str(2 ** 70): 100, str(THIEF): 50})
        await cog._process_external_xp_file(path)
        table = await xp_table(pool)
        assert table[VICTIM] == ("ofiara", 0, 7)    # clamped: simply everything
        assert table[THIEF] == ("zlodziej", 150, 7)  # the rest of the file still counts
        assert set(table) == {THIEF, VICTIM, POOR} and not path.exists()

    run(scenario)


def test_file_that_cannot_be_deleted_is_not_imported_twice(tmp_path, monkeypatch):
    """Windows: another program (a backup, an antivirus) has the file open right after the save."""
    async def scenario(pool):
        cog = make_cog(pool)
        cog.config["external_xp_file"] = str(tmp_path / "expSummary.json")
        cog.external_xp_lock = asyncio.Lock()
        (tmp_path / "expSummary.json").write_text(json.dumps({str(THIEF): 2500, str(VICTIM): -2500}),
                                                   encoding="utf-8")
        locked = {"on": True}
        real_unlink = Path.unlink

        def unlink(self, *args, **kwargs):
            if locked["on"] and ".processing." in self.name:
                raise PermissionError(13, "the file is used by another process")
            return real_unlink(self, *args, **kwargs)

        async def no_wait(seconds):
            return None

        monkeypatch.setattr(Path, "unlink", unlink)
        monkeypatch.setattr(discord_xp.asyncio, "sleep", no_wait)

        await cog.import_external_xp()              # saved, but the file cannot be deleted
        leftovers = list(tmp_path.glob("expSummary.processing.*.json"))
        assert len(leftovers) == 1
        expected = {THIEF: ("zlodziej", 2600, 7), VICTIM: ("ofiara", 500, 7), POOR: ("biedak", 1000, 7)}
        assert await xp_table(pool) == expected

        await cog.import_external_xp()              # the next passes do NOT import it again
        await cog.import_external_xp()
        assert await xp_table(pool) == expected and leftovers[0].exists()

        locked["on"] = False                        # the other program is done
        await cog.import_external_xp()
        assert not leftovers[0].exists() and await xp_table(pool) == expected
        assert cog._imported_external_files() == set()

    run(scenario)
