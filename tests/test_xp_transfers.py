# -*- coding: utf-8 -*-
"""XP transfers (an XP theft in the RPG bot) settled by Horus, protocol version 1.

The RPG bot writes ``xp_transfers/<id>.json`` (``{"version": 1, "id", "from", "to",
"amount", "created"}``). Horus moves ``min(amount, the victim's XP)`` from the victim to
the thief in ONE transaction, records the id in ``discord_xp_transfers`` (its PRIMARY KEY
makes it exactly once), writes ``xp_transfer_results/<id>.json`` and only then deletes
the request. Until 2026-11-22 a theft went through ``expSummary.json`` as two separate
amounts: the thief always got the full amount, so XP was created whenever the victim had
less.

Like tests/test_external_xp.py these run against a real PostgreSQL server, because the
rules live in the SQL (row locks, the PRIMARY KEY, one transaction). They are skipped
when no test server answers. The server is taken from HORUS_TEST_DATABASE_URL (default:
the local throw-away server on port 55432); a database named
``horus_transfer_test_<pid>`` is created there and dropped afterwards.
NEVER point this at a production server.

Run from the repo root: python -m pytest tests/test_xp_transfers.py
"""

import asyncio
import datetime as dt
import json
import logging
import os
import sys
import uuid
from collections import defaultdict
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
DATABASE = f"horus_transfer_test_{os.getpid()}"

THIEF, VICTIM, POOR, BROKE, STRANGER, NEWCOMER = 111, 222, 333, 666, 444, 555
MAX_BIGINT = 2 ** 63 - 1

STARTING_ROWS = {
    THIEF: ("zlodziej", 100, 7),
    VICTIM: ("ofiara", 30000, 7),
    POOR: ("biedak", 4000, 7),
    BROKE: ("golec", 0, 7),
}

RESULT_KEYS = {"version", "id", "from", "to", "requested", "moved", "applied"}
LOGGER_NAME = discord_xp.logger.name


class Crash(BaseException):
    """Horus stopping dead at this point (killed, power cut): nothing after it runs."""


class FakeMember:
    def __init__(self, name):
        self.name = name


class FakeGuild:
    def __init__(self, members):
        self.members = members

    def get_member(self, discord_id):
        return self.members.get(discord_id)


class FakeBot:
    def __init__(self, pool, members=None):
        self.pg_con = pool
        self.members = members

    def get_guild(self, guild_id):
        if self.members is None:
            return None      # names fall back to the id; the SQL keeps a stored name
        return FakeGuild(self.members)


def make_cog(pool, data_folder, members=None):
    """The cog without its __init__ (which reads and may create the real config file).

    A new cog is also how a RESTART is simulated: nothing kept in memory survives.
    """
    cog = discord_xp.DiscordXP.__new__(discord_xp.DiscordXP)
    cog.bot = FakeBot(pool, members)
    cog.config = {
        "guild_id": 1,
        "role_rewards_enabled": True,
        "external_xp_enabled": True,
        "external_xp_file": str(data_folder / "expSummary.json"),
        "external_xp_transfers_dir": None,
        "external_xp_transfer_results_dir": None,
    }
    cog.external_xp_lock = asyncio.Lock()
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
        pool = await asyncpg.create_pool(f"{SERVER}/{DATABASE}", min_size=1, max_size=4)
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
                "VALUES ($1, $2, $3, $4)",
                [(key, *value) for key, value in STARTING_ROWS.items()],
            )
            return await scenario(pool)
        finally:
            await pool.close()
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{DATABASE}" WITH (FORCE)')
        await admin.close()


def run(scenario):
    return asyncio.run(_with_database(scenario))


# ---------- helpers ----------

def requests_folder(data_folder):
    return data_folder / "xp_transfers"


def results_folder(data_folder):
    return data_folder / "xp_transfer_results"


def write_request(data_folder, from_id, to_id, amount, request_id=None, **overrides):
    """Write a request as the RPG bot does: temp file, then os.replace."""
    request_id = request_id or uuid.uuid4().hex
    content = {
        "version": 1,
        "id": request_id,
        "from": str(from_id),
        "to": str(to_id),
        "amount": amount,
        "created": "2026-11-22T10:00:00+00:00",
    }
    content.update(overrides)
    folder = requests_folder(data_folder)
    folder.mkdir(parents=True, exist_ok=True)
    temporary = folder / f"{request_id}.json.tmp"
    temporary.write_text(json.dumps(content), encoding="utf-8")
    os.replace(temporary, folder / f"{request_id}.json")
    return request_id


def request_path(data_folder, request_id):
    return requests_folder(data_folder) / f"{request_id}.json"


def result_path(data_folder, request_id):
    return results_folder(data_folder) / f"{request_id}.json"


def read_result(data_folder, request_id):
    return json.loads(result_path(data_folder, request_id).read_text(encoding="utf-8"))


async def xp_table(pool):
    rows = await pool.fetch("SELECT chtr_id, chtr_name, xp, chatstreak FROM chatters_information_discord")
    return {row["chtr_id"]: (row["chtr_name"], row["xp"], row["chatstreak"]) for row in rows}


async def total_xp(pool):
    return int(await pool.fetchval("SELECT COALESCE(SUM(xp), 0) FROM chatters_information_discord"))


async def transfer_rows(pool):
    exists = await pool.fetchval("SELECT to_regclass('discord_xp_transfers') IS NOT NULL")
    if not exists:
        return {}
    rows = await pool.fetch("SELECT * FROM discord_xp_transfers")
    return {row["transfer_id"]: dict(row) for row in rows}


def leftover_files(folder):
    return sorted(path.name for path in folder.rglob("*")) if folder.exists() else []


def transfer_logs(caplog, level=None):
    return [
        record for record in caplog.records
        if record.name == LOGGER_NAME and (level is None or record.levelno == level)
    ]


# ---------- the happy path: min(amount, what the victim has) ----------

@pytest.mark.parametrize(
    "victim, amount, moved",
    [
        (VICTIM, 10000, 10000),   # richer than the amount
        (POOR, 10000, 4000),      # poorer: the thief gets only what was there
        (POOR, 4000, 4000),       # exactly the amount
    ],
)
def test_the_thief_gets_exactly_what_the_victim_loses(tmp_path, caplog, victim, amount, moved):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    async def scenario(pool):
        cog = make_cog(pool, tmp_path)
        total_before = await total_xp(pool)
        request_id = write_request(tmp_path, victim, THIEF, amount)

        await cog.settle_xp_transfers()

        table = await xp_table(pool)
        victim_name, victim_xp, _ = STARTING_ROWS[victim]
        assert table[victim] == (victim_name, victim_xp - moved, 7)
        assert table[THIEF] == ("zlodziej", 100 + moved, 7)
        assert await total_xp(pool) == total_before          # no XP out of thin air
        assert not request_path(tmp_path, request_id).exists()
        result = read_result(tmp_path, request_id)
        assert result["requested"] == amount and result["moved"] == moved
        assert cog.role_sync_calls == [{THIEF, victim}]
        assert cog.flush_calls == 1                            # unsaved chat XP saved first
        row = (await transfer_rows(pool))[request_id]
        assert (row["from_id"], row["to_id"], row["requested"], row["moved"]) == (victim, THIEF, amount, moved)
        assert row["reported_at"] is not None

        infos = [r.getMessage() for r in transfer_logs(caplog, logging.INFO) if "Settled XP transfer" in r.getMessage()]
        assert infos == [
            f"Settled XP transfer {request_id}: from {victim} to {THIEF}, requested {amount}, moved {moved}."
        ]
        assert not transfer_logs(caplog, logging.WARNING) and not transfer_logs(caplog, logging.ERROR)

    run(scenario)


@pytest.mark.parametrize("victim", [BROKE, STRANGER], ids=["zero-xp", "no-row"])
def test_nothing_to_take_moves_zero_and_creates_no_row(tmp_path, victim):
    async def scenario(pool):
        for thief in (THIEF, NEWCOMER):            # a thief with and without a row
            cog = make_cog(pool, tmp_path)
            before = await xp_table(pool)
            request_id = write_request(tmp_path, victim, thief, 10000)

            await cog.settle_xp_transfers()

            assert await xp_table(pool) == before  # no row for the victim nor the thief
            assert read_result(tmp_path, request_id)["moved"] == 0
            assert not request_path(tmp_path, request_id).exists()
            assert (await transfer_rows(pool))[request_id]["moved"] == 0
            assert cog.role_sync_calls == []       # nobody's XP changed

    run(scenario)


def test_a_thief_without_a_row_gets_one(tmp_path):
    async def scenario(pool):
        cog = make_cog(pool, tmp_path)
        first = write_request(tmp_path, VICTIM, NEWCOMER, 10000)
        await cog.settle_xp_transfers()
        table = await xp_table(pool)
        assert table[NEWCOMER] == (str(NEWCOMER), 10000, 0)     # like the external XP gain
        assert table[VICTIM] == ("ofiara", 20000, 7)
        assert read_result(tmp_path, first)["moved"] == 10000

        # A known member: his name is stored, and a stored name is not replaced by the id.
        named = make_cog(pool, tmp_path, members={STRANGER: FakeMember("nowy")})
        write_request(tmp_path, POOR, STRANGER, 1000)
        await named.settle_xp_transfers()
        assert (await xp_table(pool))[STRANGER] == ("nowy", 1000, 0)

    run(scenario)


def test_the_result_file_has_exactly_the_fields_of_the_spec(tmp_path):
    async def scenario(pool):
        cog = make_cog(pool, tmp_path)
        request_id = write_request(tmp_path, POOR, THIEF, 10000)
        await cog.settle_xp_transfers()

        result = read_result(tmp_path, request_id)
        assert set(result) == RESULT_KEYS
        assert result["version"] == 1 and result["id"] == request_id
        assert result["from"] == str(POOR) and result["to"] == str(THIEF)   # strings, like the request
        assert result["requested"] == 10000 and result["moved"] == 4000
        applied = dt.datetime.fromisoformat(result["applied"])
        assert applied.utcoffset() == dt.timedelta(0)                        # UTC
        stored = (await transfer_rows(pool))[request_id]["applied_at"]
        assert abs((applied - stored).total_seconds()) < 1
        # No temporary file is left next to it.
        assert leftover_files(results_folder(tmp_path)) == [f"{request_id}.json"]

    run(scenario)


def test_the_request_is_deleted_only_after_the_result_exists(tmp_path, monkeypatch):
    async def scenario(pool):
        cog = make_cog(pool, tmp_path)
        request_id = write_request(tmp_path, VICTIM, THIEF, 10000)
        seen = []
        real_unlink = Path.unlink

        def unlink(self, *args, **kwargs):
            if self == request_path(tmp_path, request_id):
                seen.append(result_path(tmp_path, request_id).exists())
            return real_unlink(self, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", unlink)
        await cog.settle_xp_transfers()
        assert seen == [True]
        assert not request_path(tmp_path, request_id).exists()

    run(scenario)


def test_two_thefts_from_one_victim_are_settled_oldest_first(tmp_path):
    async def scenario(pool):
        cog = make_cog(pool, tmp_path)
        newer = write_request(tmp_path, POOR, THIEF, 3000, request_id="f" * 32)
        older = write_request(tmp_path, POOR, NEWCOMER, 3000, request_id="0" * 32)
        os.utime(request_path(tmp_path, newer), (2_000_000_000, 2_000_000_000))
        os.utime(request_path(tmp_path, older), (1_000_000_000, 1_000_000_000))
        total_before = await total_xp(pool)

        await cog.settle_xp_transfers()

        assert read_result(tmp_path, older)["moved"] == 3000
        assert read_result(tmp_path, newer)["moved"] == 1000      # the rest
        table = await xp_table(pool)
        assert table[POOR][1] == 0 and table[NEWCOMER][1] == 3000 and table[THIEF][1] == 1100
        assert await total_xp(pool) == total_before
        assert cog.role_sync_calls == [{POOR, THIEF, NEWCOMER}]   # one role sync per pass

    run(scenario)


# ---------- invalid requests ----------

INVALID_REQUESTS = [
    ("not JSON", b"{this is not json", "not JSON"),
    ("not UTF-8", b"\xff\xfe\x00garbage", "not JSON"),
    ("a list", json.dumps([1, 2, 3]).encode(), "not a JSON object"),
    ("version 2", {"version": 2}, "unsupported version"),
    ("version true", {"version": True}, "unsupported version"),
    ("version text", {"version": "1"}, "unsupported version"),
    ("no version", {"version": None}, "unsupported version"),
    ("other id", {"id": "a" * 32}, "id does not match the file name"),
    ("no id", {"id": None}, "id does not match the file name"),
    ("from as number", {"from": POOR}, "bad from id"),
    ("from not digits", {"from": "abc"}, "bad from id"),
    ("from negative", {"from": "-5"}, "bad from id"),
    ("from zero", {"from": "0"}, "bad from id"),
    ("from too big", {"from": str(2 ** 63)}, "bad from id"),
    ("from unicode digits", {"from": "٣٣٣"}, "bad from id"),
    ("to missing", {"to": None}, "bad to id"),
    ("to with spaces", {"to": " 111 "}, "bad to id"),
    ("from equals to", {"from": str(THIEF)}, "from equals to"),
    ("amount zero", {"amount": 0}, "amount must be positive"),
    ("amount negative", {"amount": -10000}, "amount must be positive"),
    ("amount text", {"amount": "10000"}, "bad amount"),
    ("amount float", {"amount": 10.5}, "bad amount"),
    ("amount true", {"amount": True}, "bad amount"),
    ("amount missing", {"amount": None}, "bad amount"),
]


@pytest.mark.parametrize("kind, content, reason", INVALID_REQUESTS, ids=[i[0] for i in INVALID_REQUESTS])
def test_invalid_requests_are_never_applied(tmp_path, caplog, kind, content, reason):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    async def scenario(pool):
        cog = make_cog(pool, tmp_path)
        before = await xp_table(pool)
        request_id = uuid.uuid4().hex
        if isinstance(content, bytes):
            folder = requests_folder(tmp_path)
            folder.mkdir(parents=True)
            request_path(tmp_path, request_id).write_bytes(content)
        else:
            overrides = dict(content)
            amount = overrides.pop("amount", 1000)
            write_request(tmp_path, POOR, THIEF, amount, request_id=request_id, **overrides)

        await cog.settle_xp_transfers()
        await cog.settle_xp_transfers()             # not retried for ever: moved away

        assert await xp_table(pool) == before
        assert await transfer_rows(pool) == {}
        assert not request_path(tmp_path, request_id).exists()
        assert (requests_folder(tmp_path) / "rejected" / f"{request_id}.json").exists()
        result = read_result(tmp_path, request_id)
        assert set(result) == RESULT_KEYS | {"error"}
        assert result["id"] == request_id and result["moved"] == 0 and result["error"] == reason
        assert cog.role_sync_calls == [] and cog.flush_calls == 0
        warnings = [r.getMessage() for r in transfer_logs(caplog, logging.WARNING)]
        assert len(warnings) == 1 and reason in warnings[0]

    run(scenario)


def test_an_invalid_request_whose_result_cannot_be_written_stays(tmp_path):
    async def scenario(pool):
        cog = make_cog(pool, tmp_path)
        request_id = write_request(tmp_path, POOR, THIEF, 0)
        real_write = cog._write_xp_transfer_result

        def broken_write(folder, result):
            raise PermissionError(13, "the results folder is not writable")

        cog._write_xp_transfer_result = broken_write
        await cog.settle_xp_transfers()
        assert request_path(tmp_path, request_id).exists()            # not lost
        assert not (requests_folder(tmp_path) / "rejected").exists()

        cog._write_xp_transfer_result = real_write
        await cog.settle_xp_transfers()
        assert read_result(tmp_path, request_id)["error"] == "amount must be positive"
        assert (requests_folder(tmp_path) / "rejected" / f"{request_id}.json").exists()

    run(scenario)


# ---------- exactly once, whatever fails ----------

async def _settle_with_crash(pool, tmp_path, break_cog):
    """Settle one theft with a crash injected by break_cog, then restart and settle again.

    Returns (request id, cog of the restart)."""
    total_before = await total_xp(pool)
    request_id = write_request(tmp_path, POOR, THIEF, 10000)

    crashing = make_cog(pool, tmp_path)
    break_cog(crashing)
    with pytest.raises(Crash):
        await crashing.settle_xp_transfers()

    restarted = make_cog(pool, tmp_path)          # nothing kept in memory survives
    await restarted.settle_xp_transfers()
    await restarted.settle_xp_transfers()         # and later passes change nothing

    table = await xp_table(pool)
    assert table[POOR] == ("biedak", 0, 7) and table[THIEF] == ("zlodziej", 4100, 7)   # once
    assert await total_xp(pool) == total_before
    assert not request_path(tmp_path, request_id).exists()
    assert leftover_files(requests_folder(tmp_path)) == []
    rows = await transfer_rows(pool)
    assert list(rows) == [request_id] and rows[request_id]["moved"] == 4000
    assert rows[request_id]["reported_at"] is not None
    return request_id, restarted


def test_crash_before_the_transaction(tmp_path):
    async def scenario(pool):
        def break_cog(cog):
            async def crashing_flush():
                raise Crash()
            cog.flush_pending_xp = crashing_flush

        request_id, _ = await _settle_with_crash(pool, tmp_path, break_cog)
        assert read_result(tmp_path, request_id)["moved"] == 4000

    run(scenario)


def test_crash_inside_the_transaction_rolls_everything_back(tmp_path):
    async def scenario(pool):
        state = {}

        def break_cog(cog):
            original = cog._apply_xp_transfer

            async def crash_before_commit(connection, request, thief_name):
                await original(connection, request, thief_name)     # all of it done...
                state["inside"] = True
                raise Crash()                                        # ...but never committed
            cog._apply_xp_transfer = crash_before_commit

        request_id, _ = await _settle_with_crash(pool, tmp_path, break_cog)
        assert state == {"inside": True}
        assert read_result(tmp_path, request_id)["moved"] == 4000

    run(scenario)


def test_crash_after_the_commit_before_the_result(tmp_path):
    async def scenario(pool):
        def break_cog(cog):
            def crash(folder, result):
                raise Crash()
            cog._write_xp_transfer_result = crash

        request_id, restarted = await _settle_with_crash(pool, tmp_path, break_cog)
        result = read_result(tmp_path, request_id)
        assert result["moved"] == 4000 and result["requested"] == 10000
        stored = (await transfer_rows(pool))[request_id]["applied_at"]
        assert abs((dt.datetime.fromisoformat(result["applied"]) - stored).total_seconds()) < 1
        assert restarted.role_sync_calls == []      # the restart moved nothing itself

    run(scenario)


def test_crash_after_the_result_before_it_is_marked(tmp_path):
    async def scenario(pool):
        def break_cog(cog):
            async def crash(request_id):
                raise Crash()
            cog._mark_xp_transfer_reported = crash

        request_id, _ = await _settle_with_crash(pool, tmp_path, break_cog)
        assert read_result(tmp_path, request_id)["moved"] == 4000   # answered (again), same numbers

    run(scenario)


def test_crash_after_the_result_before_the_delete(tmp_path):
    async def scenario(pool):
        def break_cog(cog):
            async def crash(path):
                raise Crash()
            cog._delete_xp_transfer_request = crash

        # The RPG bot reads and removes the result in between: it must not come back.
        total_before = await total_xp(pool)
        request_id = write_request(tmp_path, POOR, THIEF, 10000)
        crashing = make_cog(pool, tmp_path)
        break_cog(crashing)
        with pytest.raises(Crash):
            await crashing.settle_xp_transfers()
        assert read_result(tmp_path, request_id)["moved"] == 4000
        result_path(tmp_path, request_id).unlink()

        restarted = make_cog(pool, tmp_path)
        await restarted.settle_xp_transfers()
        assert not request_path(tmp_path, request_id).exists()
        assert not result_path(tmp_path, request_id).exists()      # not told twice
        assert (await xp_table(pool))[THIEF][1] == 4100 and await total_xp(pool) == total_before

    run(scenario)


def test_a_database_error_inside_the_transaction_keeps_the_request(tmp_path):
    async def scenario(pool):
        cog = make_cog(pool, tmp_path)
        before = await xp_table(pool)
        # The thief's gain fails inside the database after the victim's loss and the
        # transfer row were already written in the same transaction.
        await pool.execute(
            "ALTER TABLE chatters_information_discord "
            f"ADD CONSTRAINT untouchable CHECK (chtr_id <> {THIEF} OR xp <= 100)"
        )
        request_id = write_request(tmp_path, POOR, THIEF, 10000)

        await cog.settle_xp_transfers()
        assert await xp_table(pool) == before
        assert await transfer_rows(pool) == {}                      # rolled back as well
        assert request_path(tmp_path, request_id).exists()
        assert not results_folder(tmp_path).exists()
        assert cog.role_sync_calls == []

        await pool.execute("ALTER TABLE chatters_information_discord DROP CONSTRAINT untouchable")
        await cog.settle_xp_transfers()
        assert (await xp_table(pool))[THIEF][1] == 4100
        assert read_result(tmp_path, request_id)["moved"] == 4000

    run(scenario)


def test_a_result_that_cannot_be_written_is_written_later(tmp_path):
    async def scenario(pool):
        cog = make_cog(pool, tmp_path)
        total_before = await total_xp(pool)
        request_id = write_request(tmp_path, POOR, THIEF, 10000)
        real_write = cog._write_xp_transfer_result

        def broken_write(folder, result):
            raise PermissionError(13, "the RPG bot holds the result file open")

        cog._write_xp_transfer_result = broken_write
        await cog.settle_xp_transfers()
        await cog.settle_xp_transfers()
        assert (await xp_table(pool))[THIEF][1] == 4100             # moved once, not twice
        assert request_path(tmp_path, request_id).exists()          # kept for the result
        assert cog.role_sync_calls == [{POOR, THIEF}]                # roles follow the XP anyway

        cog._write_xp_transfer_result = real_write
        await cog.settle_xp_transfers()
        assert read_result(tmp_path, request_id)["moved"] == 4000
        assert not request_path(tmp_path, request_id).exists()
        assert (await xp_table(pool))[THIEF][1] == 4100 and await total_xp(pool) == total_before

    run(scenario)


def test_a_request_that_cannot_be_deleted_is_never_settled_twice(tmp_path, monkeypatch):
    """Windows: a backup or an antivirus holds the request open right after the result."""
    async def scenario(pool):
        locked = {"on": True}
        real_unlink = Path.unlink

        def unlink(self, *args, **kwargs):
            if locked["on"] and self.parent == requests_folder(tmp_path):
                raise PermissionError(13, "the file is used by another process")
            return real_unlink(self, *args, **kwargs)

        async def no_wait(seconds):
            return None

        monkeypatch.setattr(Path, "unlink", unlink)
        monkeypatch.setattr(discord_xp.asyncio, "sleep", no_wait)

        cog = make_cog(pool, tmp_path)
        total_before = await total_xp(pool)
        request_id = write_request(tmp_path, POOR, THIEF, 10000)
        await cog.settle_xp_transfers()
        assert request_path(tmp_path, request_id).exists()
        assert read_result(tmp_path, request_id)["moved"] == 4000
        result_path(tmp_path, request_id).unlink()                  # the RPG bot took it

        await cog.settle_xp_transfers()                             # later passes...
        await cog.settle_xp_transfers()
        restarted = make_cog(pool, tmp_path)                        # ...and a restart
        await restarted.settle_xp_transfers()
        assert not result_path(tmp_path, request_id).exists()       # neither answers again
        assert (await xp_table(pool))[THIEF][1] == 4100             # nor settles again

        locked["on"] = False                                        # the other program is done
        await restarted.settle_xp_transfers()
        assert not request_path(tmp_path, request_id).exists()
        assert not result_path(tmp_path, request_id).exists()
        assert await total_xp(pool) == total_before
        assert restarted._xp_transfers_awaiting_cleanup() == {}

    run(scenario)


def test_a_request_restored_from_a_backup_is_only_removed(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    async def scenario(pool):
        request_id = write_request(tmp_path, POOR, THIEF, 10000)
        copy = request_path(tmp_path, request_id).read_bytes()
        await make_cog(pool, tmp_path).settle_xp_transfers()
        result_path(tmp_path, request_id).unlink()                  # the RPG bot took it
        after = await xp_table(pool)

        # Days later: a backup of the data folder is restored, the request is back.
        request_path(tmp_path, request_id).write_bytes(copy)
        cog = make_cog(pool, tmp_path)
        await cog.settle_xp_transfers()
        assert await xp_table(pool) == after
        assert not request_path(tmp_path, request_id).exists()
        assert not result_path(tmp_path, request_id).exists()
        assert cog.role_sync_calls == []

        # The same id with other numbers: the settled transfer stands.
        write_request(tmp_path, VICTIM, THIEF, 99999, request_id=request_id)
        await cog.settle_xp_transfers()
        assert await xp_table(pool) == after
        assert not request_path(tmp_path, request_id).exists()
        assert any("differs" in r.getMessage() for r in transfer_logs(caplog, logging.WARNING))

    run(scenario)


def _pause_after_the_claim(cog, claimed, gate, fail=False):
    original = cog._claim_xp_transfer

    async def paused_claim(connection, request):
        applied_at = await original(connection, request)
        claimed.set()
        await gate.wait()
        if fail:
            raise RuntimeError("the first process fails inside its transaction")
        return applied_at

    cog._claim_xp_transfer = paused_claim


def _record_the_claim(cog, events):
    original = cog._claim_xp_transfer

    async def recorded_claim(connection, request):
        applied_at = await original(connection, request)
        events.append(applied_at)
        return applied_at

    cog._claim_xp_transfer = recorded_claim


@pytest.mark.parametrize("first_fails", [False, True], ids=["first-commits", "first-rolls-back"])
def test_two_horus_processes_settle_one_request_once(tmp_path, caplog, first_fails):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    async def scenario(pool):
        other_pool = await asyncpg.create_pool(f"{SERVER}/{DATABASE}", min_size=1, max_size=2)
        try:
            first = make_cog(pool, tmp_path)
            second = make_cog(other_pool, tmp_path)            # another process: its own lock
            assert await first._ensure_xp_transfer_table()
            total_before = await total_xp(pool)
            request_id = write_request(tmp_path, POOR, THIEF, 10000)
            claimed, gate, second_claims = asyncio.Event(), asyncio.Event(), []
            _pause_after_the_claim(first, claimed, gate, fail=first_fails)
            _record_the_claim(second, second_claims)

            async def run_second():
                await claimed.wait()                             # the first one holds the id
                task = asyncio.create_task(second.settle_xp_transfers())
                await asyncio.sleep(0.5)
                assert second_claims == []                       # waiting on the PRIMARY KEY
                gate.set()
                await task

            await asyncio.gather(first.settle_xp_transfers(), run_second())

            if first_fails:
                assert second_claims[0] is not None              # the second one settled it
            else:
                assert second_claims == [None]                   # lost cleanly: "already settled"
            table = await xp_table(pool)
            assert table[POOR][1] == 0 and table[THIEF][1] == 4100   # once
            assert await total_xp(pool) == total_before
            assert list(await transfer_rows(pool)) == [request_id]
            assert read_result(tmp_path, request_id)["moved"] == 4000
            assert not request_path(tmp_path, request_id).exists()
            settled = [r for r in transfer_logs(caplog, logging.INFO) if "Settled XP transfer" in r.getMessage()]
            assert len(settled) == 1
            errors = transfer_logs(caplog, logging.ERROR)
            assert len(errors) == (1 if first_fails else 0)       # only the injected failure
        finally:
            await other_pool.close()

    run(scenario)


# ---------- pending XP, the summary file, the table ----------

def test_unsaved_chat_xp_is_saved_first_and_can_be_taken(tmp_path):
    async def scenario(pool):
        cog = make_cog(pool, tmp_path)
        del cog.flush_pending_xp                    # the real one this time
        cog.pending_xp = defaultdict(int, {POOR: 500})
        cog.pending_usernames = {POOR: "biedak"}
        cog.pending_reaction_bonuses = {}
        cog.pending_lock = asyncio.Lock()
        total_before = await total_xp(pool) + 500
        request_id = write_request(tmp_path, POOR, THIEF, 10000)

        await cog.settle_xp_transfers()

        # 4000 saved + 500 unsaved: all of it taken. (Without the flush: 4000 now, 500 back later.)
        assert read_result(tmp_path, request_id)["moved"] == 4500
        table = await xp_table(pool)
        assert table[POOR] == ("biedak", 0, 7) and table[THIEF][1] == 4600
        assert not cog.pending_xp and await total_xp(pool) == total_before

    run(scenario)


def test_a_failing_flush_settles_against_the_saved_xp(tmp_path, caplog):
    async def scenario(pool):
        cog = make_cog(pool, tmp_path)

        async def broken_flush():
            raise RuntimeError("the pending XP cannot be saved")

        cog.flush_pending_xp = broken_flush
        request_id = write_request(tmp_path, POOR, THIEF, 10000)
        await cog.settle_xp_transfers()
        assert read_result(tmp_path, request_id)["moved"] == 4000
        assert any("pending XP" in r.getMessage() for r in transfer_logs(caplog, logging.ERROR))

    run(scenario)


def test_the_summary_file_import_works_unchanged_next_to_it(tmp_path):
    async def scenario(pool):
        cog = make_cog(pool, tmp_path)
        summary = tmp_path / "expSummary.json"
        summary.write_text(json.dumps({str(THIEF): 250, str(VICTIM): -500}), encoding="utf-8")
        request_id = write_request(tmp_path, POOR, THIEF, 10000)
        lock_held = []
        original = cog._apply_xp_transfer

        async def checked(connection, request, thief_name):
            lock_held.append(cog.external_xp_lock.locked())
            return await original(connection, request, thief_name)

        cog._apply_xp_transfer = checked

        await cog._external_xp_pass()

        table = await xp_table(pool)
        assert table[THIEF][1] == 100 + 250 + 4000 and table[VICTIM][1] == 29500
        assert json.loads(summary.read_text(encoding="utf-8")) == {}       # re-created empty
        assert list(tmp_path.glob("expSummary.processing.*")) == []
        assert read_result(tmp_path, request_id)["moved"] == 4000
        assert lock_held == [True]                                          # never interleaved

        # A settlement that blows up does not stop the next summary import.
        async def broken_settlement():
            raise RuntimeError("boom")

        cog.settle_xp_transfers = broken_settlement
        summary.write_text(json.dumps({str(THIEF): 50}), encoding="utf-8")
        await cog._external_xp_pass()
        assert (await xp_table(pool))[THIEF][1] == 4400

    run(scenario)


def test_the_table_creation_is_idempotent(tmp_path):
    async def scenario(pool):
        assert await make_cog(pool, tmp_path)._ensure_xp_transfer_table()
        request_id = write_request(tmp_path, POOR, THIEF, 10000)
        await make_cog(pool, tmp_path).settle_xp_transfers()
        for _ in range(3):                                   # every restart runs it again
            assert await make_cog(pool, tmp_path)._ensure_xp_transfer_table()
        assert list(await transfer_rows(pool)) == [request_id]    # nothing lost

        # A table created exactly as the protocol lists it (no reported_at) gets the column.
        await pool.execute("DROP TABLE discord_xp_transfers")
        await pool.execute(
            """
            CREATE TABLE discord_xp_transfers (transfer_id TEXT PRIMARY KEY, from_id BIGINT NOT NULL,
            to_id BIGINT NOT NULL, requested BIGINT NOT NULL, moved BIGINT NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now())
            """
        )
        assert await make_cog(pool, tmp_path)._ensure_xp_transfer_table()
        columns = await pool.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'discord_xp_transfers'"
        )
        assert "reported_at" in {row["column_name"] for row in columns}

    run(scenario)


def test_a_table_that_cannot_be_created_stops_only_the_transfers(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    async def scenario(pool):
        # Something else already has the name: ALTER TABLE on a view fails, as a missing
        # permission or a broken database would.
        await pool.execute("CREATE VIEW discord_xp_transfers AS SELECT 'x'::TEXT AS transfer_id")
        cog = make_cog(pool, tmp_path)
        await cog.cog_load()                                 # start-up: never raises
        summary = tmp_path / "expSummary.json"
        request_id = write_request(tmp_path, POOR, THIEF, 10000)

        for pass_number in range(3):
            summary.write_text(json.dumps({str(VICTIM): 100}), encoding="utf-8")
            await cog._external_xp_pass()

        table = await xp_table(pool)
        assert table[VICTIM][1] == 30300                     # the summary import still works
        assert table[POOR][1] == 4000                        # the theft waits
        assert request_path(tmp_path, request_id).exists()
        assert not results_folder(tmp_path).exists()
        table_errors = [r for r in transfer_logs(caplog, logging.ERROR) if "discord_xp_transfers" in r.getMessage()]
        assert len(table_errors) == 1                        # once per start, not every pass

        await pool.execute("DROP VIEW discord_xp_transfers")
        await cog._external_xp_pass()                        # repaired: settled without a restart
        assert read_result(tmp_path, request_id)["moved"] == 4000
        assert any("ready now" in r.getMessage() for r in transfer_logs(caplog, logging.INFO))

    run(scenario)


# ---------- the folder ----------

def test_a_missing_or_empty_folder_is_silent(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)

    async def scenario(pool):
        cog = make_cog(pool, tmp_path)
        await cog.settle_xp_transfers()                      # no folder at all
        assert not requests_folder(tmp_path).exists()        # never created by Horus
        assert not results_folder(tmp_path).exists()
        requests_folder(tmp_path).mkdir()
        await cog.settle_xp_transfers()                      # an empty folder
        assert transfer_logs(caplog) == []
        assert cog.flush_calls == 0 and await transfer_rows(pool) == {}   # not even the database

    run(scenario)


def test_names_that_do_not_match_are_ignored(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)

    async def scenario(pool):
        cog = make_cog(pool, tmp_path)
        folder = requests_folder(tmp_path)
        folder.mkdir()
        hex_id = uuid.uuid4().hex
        content = json.dumps({"version": 1, "id": hex_id, "from": str(POOR), "to": str(THIEF), "amount": 10})
        ignored = [
            f"{hex_id}.json.tmp",              # the RPG bot is still writing it
            hex_id.upper() + ".json",
            hex_id[:31] + ".json",
            hex_id + "0.json",
            hex_id + ".JSON",
            hex_id + ".txt",
            hex_id,
            "notes.json",
        ]
        for name in ignored:
            (folder / name).write_text(content, encoding="utf-8")
        (folder / f"{uuid.uuid4().hex}.json").mkdir()      # a folder with a request's name
        (folder / "rejected").mkdir()
        (folder / "rejected" / f"{hex_id}.json").write_text(content, encoding="utf-8")
        before = sorted(path.name for path in folder.rglob("*"))

        await cog.settle_xp_transfers()

        assert sorted(path.name for path in folder.rglob("*")) == before
        assert not results_folder(tmp_path).exists()
        assert transfer_logs(caplog) == [] and await transfer_rows(pool) == {}

    run(scenario)


def test_configured_folders_are_used(tmp_path):
    async def scenario(pool):
        cog = make_cog(pool, tmp_path)
        cog.config["external_xp_transfers_dir"] = str(tmp_path / "in")
        cog.config["external_xp_transfer_results_dir"] = str(tmp_path / "out")
        request_id = uuid.uuid4().hex
        (tmp_path / "in").mkdir()
        (tmp_path / "in" / f"{request_id}.json").write_text(json.dumps(
            {"version": 1, "id": request_id, "from": str(POOR), "to": str(THIEF), "amount": 10}
        ), encoding="utf-8")
        await cog.settle_xp_transfers()
        assert json.loads((tmp_path / "out" / f"{request_id}.json").read_text(encoding="utf-8"))["moved"] == 10
        assert not results_folder(tmp_path).exists()

    run(scenario)


def test_the_config_defaults_and_validation(tmp_path):
    config = dict(discord_xp.DEFAULT_CONFIG)
    assert config["external_xp_transfers_dir"] is None
    assert config["external_xp_transfer_results_dir"] is None
    config["external_xp_file"] = str(tmp_path / "data" / "expSummary.json")
    discord_xp.DiscordXP._validate_config(config)
    assert discord_xp.DiscordXP._resolve_xp_transfer_folders(config) == (
        tmp_path / "data" / "xp_transfers",
        tmp_path / "data" / "xp_transfer_results",
    )

    for key, value in [
        ("external_xp_transfers_dir", ""),
        ("external_xp_transfers_dir", 5),
        ("external_xp_transfer_results_dir", "   "),
        ("external_xp_transfer_results_dir", str(tmp_path / "data" / "xp_transfers")),
        ("external_xp_transfer_results_dir", str(tmp_path / "data" / "xp_transfers" / "rejected")),
    ]:
        broken = dict(config, **{key: value})
        with pytest.raises(ValueError):
            discord_xp.DiscordXP._validate_config(broken)


# ---------- roles ----------

def test_role_sync_for_both_and_its_failure_is_harmless(tmp_path, caplog):
    async def scenario(pool):
        cog = make_cog(pool, tmp_path)

        async def broken_role_sync(discord_ids, **kwargs):
            raise RuntimeError("Discord is down")

        cog._update_roles_for_users = broken_role_sync
        request_id = write_request(tmp_path, VICTIM, THIEF, 10000)
        await cog.settle_xp_transfers()                       # does not raise
        assert read_result(tmp_path, request_id)["moved"] == 10000
        assert not request_path(tmp_path, request_id).exists()
        assert any("XP roles" in r.getMessage() for r in transfer_logs(caplog, logging.ERROR))
        assert not cog.external_xp_lock.locked()

        quiet = make_cog(pool, tmp_path)
        quiet.config["role_rewards_enabled"] = False
        write_request(tmp_path, VICTIM, THIEF, 10000)
        await quiet.settle_xp_transfers()
        assert quiet.role_sync_calls == []

    run(scenario)


# ---------- BIGINT ----------

def test_huge_numbers_never_overflow_bigint(tmp_path):
    async def scenario(pool):
        cog = make_cog(pool, tmp_path)
        total_before = await total_xp(pool)

        # An absurd amount: simply everything the victim has; stored as the BIGINT ceiling.
        absurd = write_request(tmp_path, VICTIM, THIEF, 10 ** 30)
        await cog.settle_xp_transfers()
        result = read_result(tmp_path, absurd)
        assert result["moved"] == 30000 and result["requested"] == MAX_BIGINT
        assert (await xp_table(pool))[VICTIM][1] == 0 and await total_xp(pool) == total_before

        # A thief at the top of BIGINT: only what still fits is moved, nothing is lost.
        await pool.execute("UPDATE chatters_information_discord SET xp = $1 WHERE chtr_id = $2",
                           MAX_BIGINT - 5, THIEF)
        near_top = write_request(tmp_path, POOR, THIEF, 10000)
        await cog.settle_xp_transfers()
        assert read_result(tmp_path, near_top)["moved"] == 5
        table = await xp_table(pool)
        assert table[THIEF][1] == MAX_BIGINT and table[POOR][1] == 3995

        # A victim at the top of BIGINT and a thief with nothing.
        await pool.execute("UPDATE chatters_information_discord SET xp = $1 WHERE chtr_id = $2",
                           MAX_BIGINT, BROKE)
        everything = write_request(tmp_path, BROKE, NEWCOMER, MAX_BIGINT)
        await cog.settle_xp_transfers()
        assert read_result(tmp_path, everything)["moved"] == MAX_BIGINT
        table = await xp_table(pool)
        assert table[BROKE][1] == 0 and table[NEWCOMER][1] == MAX_BIGINT
        assert leftover_files(requests_folder(tmp_path)) == []

    run(scenario)


# ---------- one bad file never blocks the queue (review, 2026-11-22) ----------

def write_raw_request(data_folder, raw_content, request_id=None, age_seconds=100):
    """A request file with any bytes in it, made older than the requests written after it."""
    request_id = request_id or uuid.uuid4().hex
    folder = requests_folder(data_folder)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{request_id}.json"
    path.write_bytes(raw_content)
    stat = path.stat()
    os.utime(path, (stat.st_atime - age_seconds, stat.st_mtime - age_seconds))
    return request_id


def padded_valid_request(request_id, padding):
    """A valid request followed by `padding` spaces: still JSON, but far too big."""
    content = {"version": 1, "id": request_id, "from": str(POOR), "to": str(THIEF), "amount": 10}
    return json.dumps(content).encode() + b" " * padding


@pytest.mark.parametrize(
    "kind, build_content, reason",
    [
        # json.loads raises RecursionError, which is not a ValueError.
        ("nested under the size limit", lambda request_id: b"[" * 60000, "not JSON"),
        ("nested over the size limit", lambda request_id: b"[" * 200000, "too large"),
        ("valid but padded over the limit", lambda request_id: padded_valid_request(request_id, 70000), "too large"),
    ],
    ids=["nested-small", "nested-big", "padded"],
)
def test_a_broken_request_is_rejected_and_never_blocks_the_others(tmp_path, caplog, kind, build_content, reason):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    async def scenario(pool):
        cog = make_cog(pool, tmp_path)
        bad_id = uuid.uuid4().hex
        write_raw_request(tmp_path, build_content(bad_id), request_id=bad_id)
        good_id = write_request(tmp_path, VICTIM, THIEF, 1000)     # newer: settled after it
        total_before = await total_xp(pool)

        await cog._external_xp_pass()

        assert read_result(tmp_path, good_id)["moved"] == 1000
        assert not request_path(tmp_path, good_id).exists()
        bad_result = read_result(tmp_path, bad_id)
        assert bad_result["error"] == reason and bad_result["moved"] == 0
        assert (requests_folder(tmp_path) / "rejected" / f"{bad_id}.json").exists()
        assert not request_path(tmp_path, bad_id).exists()
        assert await total_xp(pool) == total_before
        assert transfer_logs(caplog, logging.ERROR) == []

    run(scenario)


def test_a_lone_surrogate_in_an_invalid_request_is_answered(tmp_path, caplog):
    """'\\ud800' is valid JSON but cannot be written as UTF-8: the error result is ASCII."""
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    async def scenario(pool):
        cog = make_cog(pool, tmp_path)
        bad_id = uuid.uuid4().hex
        write_raw_request(
            tmp_path,
            ('{"version": 2, "id": "%s", "from": "\\ud800", "to": "111", "amount": 5}' % bad_id).encode(),
            request_id=bad_id,
        )
        good_id = write_request(tmp_path, VICTIM, THIEF, 1000)

        await cog._external_xp_pass()

        assert read_result(tmp_path, good_id)["moved"] == 1000
        raw_result = result_path(tmp_path, bad_id).read_bytes()
        raw_result.decode("ascii")                                  # pure ASCII
        bad_result = json.loads(raw_result)
        assert bad_result["error"] == "unsupported version" and bad_result["from"] == "\ud800"
        assert (requests_folder(tmp_path) / "rejected" / f"{bad_id}.json").exists()
        assert transfer_logs(caplog, logging.ERROR) == []

    run(scenario)


def test_an_unexpected_error_with_one_request_is_logged_once_and_skips_only_it(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    async def scenario(pool):
        cog = make_cog(pool, tmp_path)
        bad_id = write_raw_request(tmp_path, b"{}")
        good_id = write_request(tmp_path, VICTIM, THIEF, 1000)
        real_parse = cog._parse_xp_transfer_request

        def parse(request_id, raw_content):
            if request_id == bad_id:
                raise MemoryError("whatever a strange file does")
            return real_parse(request_id, raw_content)

        cog._parse_xp_transfer_request = parse
        for _ in range(3):
            await cog._external_xp_pass()

        assert read_result(tmp_path, good_id)["moved"] == 1000
        assert request_path(tmp_path, bad_id).exists()              # kept, tried again later
        problems = [r for r in transfer_logs(caplog) if bad_id in r.getMessage()]
        assert len(problems) == 1                                   # once, not every 10 s

        cog._parse_xp_transfer_request = real_parse                 # the cause is gone
        await cog._external_xp_pass()
        assert read_result(tmp_path, bad_id)["error"] == "unsupported version"

    run(scenario)


def test_a_table_dropped_at_run_time_is_created_again(tmp_path, caplog):
    """A database restored from a backup older than the table, while Horus keeps running."""
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    async def scenario(pool):
        cog = make_cog(pool, tmp_path)
        write_request(tmp_path, VICTIM, THIEF, 1)
        await cog.settle_xp_transfers()
        await pool.execute("DROP TABLE discord_xp_transfers")
        first_id = write_request(tmp_path, VICTIM, THIEF, 1000)
        second_id = write_request(tmp_path, POOR, THIEF, 1000)
        caplog.clear()

        for _ in range(3):
            await cog.settle_xp_transfers()

        assert read_result(tmp_path, first_id)["moved"] == 1000
        assert read_result(tmp_path, second_id)["moved"] == 1000
        assert set(await transfer_rows(pool)) == {first_id, second_id}
        assert leftover_files(requests_folder(tmp_path)) == []
        assert len(transfer_logs(caplog, logging.ERROR)) == 1      # one line, not one per request and pass

    run(scenario)


def test_a_row_locked_by_another_session_does_not_stall_the_loop(tmp_path, caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    monkeypatch.setattr(discord_xp, "XP_TRANSFER_LOCK_TIMEOUT_MILLISECONDS", 300)

    async def scenario(pool):
        cog = make_cog(pool, tmp_path)
        assert await cog._ensure_xp_transfer_table()
        total_before = await total_xp(pool)
        locked_id = write_raw_request(
            tmp_path,
            json.dumps({"version": 1, "id": "0" * 32, "from": str(VICTIM), "to": str(THIEF),
                        "amount": 1000}).encode(),
            request_id="0" * 32,
        )
        later_id = write_request(tmp_path, POOR, THIEF, 1000)

        # A forgotten psql session: BEGIN; SELECT ... FOR UPDATE; and nothing more.
        other = await asyncpg.connect(f"{SERVER}/{DATABASE}")
        transaction = other.transaction()
        await transaction.start()
        await other.execute("SELECT 1 FROM chatters_information_discord WHERE chtr_id = $1 FOR UPDATE", VICTIM)
        try:
            for _ in range(3):
                await asyncio.wait_for(cog._external_xp_pass(), 4)  # does not wait for the lock
            assert not cog.external_xp_lock.locked()
            # Both wait (the later one too, so the order of the queue is kept) and nothing moved.
            assert request_path(tmp_path, locked_id).exists() and request_path(tmp_path, later_id).exists()
            assert await transfer_rows(pool) == {} and await total_xp(pool) == total_before
            assert transfer_logs(caplog, logging.ERROR) == []
            assert len([r for r in transfer_logs(caplog, logging.WARNING) if "lock" in r.getMessage()]) == 1
        finally:
            await transaction.rollback()
            await other.close()

        await cog._external_xp_pass()
        assert read_result(tmp_path, locked_id)["moved"] == 1000
        assert read_result(tmp_path, later_id)["moved"] == 1000
        assert await total_xp(pool) == total_before

    run(scenario)
