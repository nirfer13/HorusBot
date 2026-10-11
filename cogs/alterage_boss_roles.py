# -*- coding: utf-8 -*-
"""
Alterage boss rankings -> Discord role.

Every 10 minutes (and once on startup) the Top 1 attempt of every boss in
BOSS_IDS is read from the Alterage bot database. The Hero (Controller) and all
Supporters frozen in that attempt's contributors_snapshot get the role
ALTERAGE_TOP1_ROLE; everyone else holding it loses it.

- Read-only: queries run inside a READ ONLY transaction.
- DB: ALTERAGE_DATABASE_URL from env (preferably a SELECT-only account);
  falls back to bot.pg_con when it is not set.
- If any query fails, no role is touched until the next successful sync.
- Role ID, guild and announce channel come from cogs/discord_roles.txt.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

import asyncpg
import discord
from discord.ext import commands, tasks

from cogs.functions_twitch import RolesConfigTwitch

try:
    from globals.globalvariables import DebugMode  # type: ignore
except Exception:
    DebugMode = False

logger = logging.getLogger(__name__)

# Ranking Króla Szczurów i Ranking Lisza; rolę dostaje Top 1 każdego z nich
BOSS_IDS = ["rat_king_boss_t1", "lich_boss_t2"]
BOSS_NAMES = {
    "rat_king_boss_t1": "Króla Szczurów",
    "lich_boss_t2": "Lisza",
}

SYNC_MINUTES = 10

# Masked link to the stream; <> suppresses the embed preview.
ALTERAGE_LINK = "[Alterage](<https://www.twitch.tv/altermmo_pl>)"

# {boss_id: attempt id} of the last announced Top 1, to tell a new Top 1
# apart from a late `!polacz` of someone already in it.
STATE_FILE = "alterage_top1.json"

# Same ordering as the in-game ranking.
TOP1_QUERY = """
SELECT ba.id, ba.boss_id, ba.hero_display_name, ba.status,
       ba.damage_progress_pct, ba.turns, ba.resolved_at, ba.contributors_snapshot,
       c.controller_name, c.controller_twitch_user_id
FROM boss_attempts ba
LEFT JOIN characters c ON c.id = ba.character_id
WHERE ba.boss_id = $1
  AND ba.status IN ('victory', 'defeat')
ORDER BY ba.damage_progress_pct DESC NULLS LAST,
         (ba.status = 'victory') DESC,
         ba.turns ASC NULLS LAST,
         ba.resolved_at ASC
LIMIT 1;
"""

# Fallback until the Alterage bot adds boss_attempts.turns on its startup.
TOP1_QUERY_NO_TURNS = """
SELECT ba.id, ba.boss_id, ba.hero_display_name, ba.status,
       ba.damage_progress_pct, ba.resolved_at, ba.contributors_snapshot,
       c.controller_name, c.controller_twitch_user_id
FROM boss_attempts ba
LEFT JOIN characters c ON c.id = ba.character_id
WHERE ba.boss_id = $1
  AND ba.status IN ('victory', 'defeat')
ORDER BY ba.damage_progress_pct DESC NULLS LAST,
         (ba.status = 'victory') DESC,
         ba.resolved_at ASC
LIMIT 1;
"""

TURNS_COLUMN_QUERY = """
SELECT EXISTS (
    SELECT 1
    FROM information_schema.columns
    WHERE table_name = 'boss_attempts'
      AND column_name = 'turns'
      AND table_schema = ANY(current_schemas(false))
);
"""

DISCORD_IDS_QUERY = """
SELECT lower(trim(chtr_name)) AS login, discord_id
FROM chatters_information
WHERE discord_id IS NOT NULL AND discord_id::text <> ''
  AND lower(trim(chtr_name)) = ANY($1::text[]);
"""


@dataclass
class BossTop1:
    boss_id: str
    status: str
    damage_pct: Optional[float]
    turns: Optional[int]
    hero: str
    supporters: List[str] = field(default_factory=list)
    attempt_id: Optional[str] = None
    # [(nick, is_hero)] without a Discord account linked via !polacz
    unlinked: List[tuple] = field(default_factory=list)

    @property
    def boss_name(self) -> str:
        return BOSS_NAMES.get(self.boss_id, self.boss_id)

    def result_text(self) -> str:
        if self.status == "victory":
            if self.turns is not None:
                return f"zwycięstwo w **{self.turns}** turach"
            return "zwycięstwo"
        pct = f"{self.damage_pct:g}%" if self.damage_pct is not None else "?"
        return f"**{pct}** obrażeń bossa"


@dataclass
class TargetEntry:
    """Why a Discord user belongs to the target set."""
    nick: str
    # [(BossTop1, is_hero)]
    sources: List[tuple] = field(default_factory=list)


def _parse_snapshot(raw) -> List[str]:
    if raw is None:
        return []
    if isinstance(raw, (str, bytes)):
        try:
            raw = json.loads(raw)
        except Exception:
            logger.warning("Invalid contributors_snapshot JSON: %r", raw)
            return []
    if not isinstance(raw, list):
        return []
    nicks: List[str] = []
    for item in raw:
        if isinstance(item, dict):
            nick = str(item.get("nick") or "").strip()
            if nick:
                nicks.append(nick)
    return nicks


def _parse_discord_id(raw) -> Optional[int]:
    try:
        value = int(str(raw).strip())
    except Exception:
        return None
    return value if value > 0 else None


class AlterageBossRoles(commands.Cog, name="AlterageBossRoles"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.config = RolesConfigTwitch()
        self.role_id = int(self.config._raw.get("ALTERAGE_TOP1_ROLE", "0") or 0)
        self.pool: Optional[asyncpg.Pool] = None
        self._started = False
        # Last computed target set, to log which boss a lost role came from.
        self._last_targets: Dict[int, TargetEntry] = {}
        self._last_attempt_ids: Dict[str, str] = self._load_state()

    def cog_unload(self):
        self.sync_loop.cancel()
        if self.pool is not None:
            self.bot.loop.create_task(self.pool.close())

    @commands.Cog.listener()
    async def on_ready(self):
        if not self._started:
            self._started = True
            self.sync_loop.start()

    # ---------- State ----------

    @staticmethod
    def _load_state() -> Dict[str, str]:
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as file:
                data = json.load(file)
            return {str(k): str(v) for k, v in data.items()}
        except FileNotFoundError:
            return {}
        except Exception as e:
            logger.warning("Could not read %s: %s", STATE_FILE, e)
            return {}

    def _save_state(self) -> None:
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as file:
                json.dump(self._last_attempt_ids, file, indent=2)
        except Exception as e:
            logger.warning("Could not write %s: %s", STATE_FILE, e)

    def _is_new_top1(self, top: BossTop1, targets: Dict[int, TargetEntry], holders_before: Set[int]) -> bool:
        known = self._last_attempt_ids.get(top.boss_id)
        if known is not None:
            return known != top.attempt_id
        # No saved state: it is the same Top 1 if someone from it already had the role.
        return not any(
            discord_id in holders_before and any(src is top for src, _ in entry.sources)
            for discord_id, entry in targets.items()
        )

    # ---------- DB ----------

    async def _get_pool(self):
        dsn = os.getenv("ALTERAGE_DATABASE_URL")
        if not dsn:
            return self.bot.pg_con
        if self.pool is None:
            self.pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
            logger.info("Connected to the Alterage database (ALTERAGE_DATABASE_URL).")
        return self.pool

    async def _fetch_top1(self) -> Optional[tuple]:
        """
        Return ({discord_id: TargetEntry}, [BossTop1]) or None when the DB
        could not be read (then no role may be changed).
        """
        try:
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                async with conn.transaction(readonly=True):
                    has_turns = await conn.fetchval(TURNS_COLUMN_QUERY)
                    query = TOP1_QUERY if has_turns else TOP1_QUERY_NO_TURNS
                    if not has_turns:
                        logger.warning("boss_attempts.turns missing, ranking without turns.")

                    tops: List[BossTop1] = []
                    for boss_id in BOSS_IDS:
                        row = await conn.fetchrow(query, boss_id)
                        if row is None:
                            logger.info("No finished attempts for boss %s.", boss_id)
                            continue
                        hero = (row["controller_name"] or row["hero_display_name"] or "").strip()
                        damage = row["damage_progress_pct"]
                        tops.append(BossTop1(
                            boss_id=boss_id,
                            status=row["status"],
                            damage_pct=float(damage) if damage is not None else None,
                            turns=row["turns"] if has_turns else None,
                            hero=hero,
                            supporters=_parse_snapshot(row["contributors_snapshot"]),
                            attempt_id=str(row["id"]),
                        ))

                    logins: Set[str] = set()
                    for top in tops:
                        for nick in [top.hero, *top.supporters]:
                            if nick:
                                logins.add(nick.strip().lower())

                    id_rows = []
                    if logins:
                        id_rows = await conn.fetch(DISCORD_IDS_QUERY, sorted(logins))
        except Exception as e:
            logger.error("Alterage DB query failed, roles left unchanged: %s: %s", type(e).__name__, e)
            return None

        login_to_id: Dict[str, int] = {}
        for r in id_rows:
            discord_id = _parse_discord_id(r["discord_id"])
            if discord_id:
                login_to_id[r["login"]] = discord_id

        targets: Dict[int, TargetEntry] = {}
        for top in tops:
            people = [(top.hero, True)] + [(n, False) for n in top.supporters if n]
            for nick, is_hero in people:
                if not nick:
                    continue
                discord_id = login_to_id.get(nick.strip().lower())
                if discord_id is None:
                    logger.info("Top 1 %s: no linked Discord account for '%s', skipped.", top.boss_id, nick)
                    if not any(n.lower() == nick.strip().lower() for n, _ in top.unlinked):
                        top.unlinked.append((nick.strip(), is_hero))
                    continue
                entry = targets.setdefault(discord_id, TargetEntry(nick=nick))
                if not any(t is top for t, _ in entry.sources):
                    entry.sources.append((top, is_hero))

        return targets, tops

    # ---------- Sync ----------

    @tasks.loop(minutes=SYNC_MINUTES)
    async def sync_loop(self):
        await self.sync_roles()

    @sync_loop.before_loop
    async def _before_sync_loop(self):
        await self.bot.wait_until_ready()

    @sync_loop.error
    async def _sync_loop_error(self, error):
        logger.exception("Alterage sync loop crashed.", exc_info=error)

    async def sync_roles(self, dry_run: bool = False) -> Optional[dict]:
        if not self.role_id:
            logger.error("Missing ALTERAGE_TOP1_ROLE in discord_roles.txt.")
            return None
        guild = self.bot.get_guild(self.config.guild_id)
        if guild is None:
            logger.error("Guild %s not found.", self.config.guild_id)
            return None
        role = guild.get_role(self.role_id)
        if role is None:
            logger.error("Role %s not found on the guild.", self.role_id)
            return None

        fetched = await self._fetch_top1()
        if fetched is None:
            return None
        targets, tops = fetched

        current = {m.id for m in role.members}
        to_add = [i for i in targets if i not in current]
        to_remove = [i for i in current if i not in targets]

        added: List[int] = []
        removed: List[int] = []
        removed_names: List[str] = []

        for discord_id in to_add:
            entry = targets[discord_id]
            member = guild.get_member(discord_id)
            if member is None:
                logger.info("Top 1 '%s' (%s) is not on the server, skipped.", entry.nick, discord_id)
                continue
            details = "; ".join(
                f"{top.boss_id} {'Bohater' if is_hero else 'Wspierający'}, {top.status}"
                + (f" w {top.turns} turach" if top.status == "victory" and top.turns is not None else "")
                for top, is_hero in entry.sources
            )
            if dry_run:
                added.append(discord_id)
                continue
            try:
                await member.add_roles(role, reason=f"Alterage Top 1: {details}")
                added.append(discord_id)
                logger.info("Role added: %s (%s) - %s", member, entry.nick, details)
            except discord.HTTPException as e:
                logger.error("Could not add role to %s: %s", member, e)

        for discord_id in to_remove:
            member = guild.get_member(discord_id)
            if member is None:
                continue
            prev = self._last_targets.get(discord_id)
            prev_bosses = ", ".join(t.boss_id for t, _ in prev.sources) if prev else "unknown"
            if dry_run:
                removed.append(discord_id)
                removed_names.append(member.display_name)
                continue
            try:
                await member.remove_roles(role, reason="Alterage: poza Top 1 rankingów bossów")
                removed.append(discord_id)
                removed_names.append(member.display_name)
                logger.info("Role removed: %s - no longer Top 1 (was: %s)", member, prev_bosses)
            except discord.HTTPException as e:
                logger.error("Could not remove role from %s: %s", member, e)

        new_tops = {top.boss_id for top in tops if self._is_new_top1(top, targets, current)}
        messages = self._build_messages(role, targets, tops, added, removed_names, new_tops)
        if not dry_run:
            self._last_targets = targets
            attempt_ids = {top.boss_id: top.attempt_id for top in tops if top.attempt_id}
            if attempt_ids != self._last_attempt_ids:
                self._last_attempt_ids = attempt_ids
                self._save_state()
            if messages:
                await self._announce(messages)

        return {"tops": tops, "added": added, "removed": removed, "messages": messages}

    # ---------- Announcements ----------

    def _build_messages(self, role, targets, tops, added, removed_names, new_tops) -> List[str]:
        lines: List[str] = []

        for top in tops:
            heroes: List[str] = []
            supporters: List[str] = []
            for discord_id in added:
                for src, is_hero in targets[discord_id].sources:
                    if src is top:
                        (heroes if is_hero else supporters).append(f"<@{discord_id}>")
            is_new = top.boss_id in new_tops
            if not heroes and not supporters and not (is_new and top.unlinked):
                continue
            if is_new:
                lines.append(f"🏆 **{ALTERAGE_LINK} - Ranking {top.boss_name}** - nowy Top 1: {top.result_text()}!")
            else:
                lines.append(f"🔗 **{ALTERAGE_LINK} - Ranking {top.boss_name}** - synchronizacja Top 1 ({top.result_text()})")
            lines.append(f"Bohater: **{discord.utils.escape_markdown(top.hero)}**")
            got = [f"{h} (Bohater)" for h in heroes] + supporters
            if got:
                verb = "zdobywa" if len(got) == 1 else "zdobywają"
                lines.append(f"Rangę **{role.name}** {verb}: {', '.join(got)}")
            if top.unlinked:
                waiting = ", ".join(
                    f"**{discord.utils.escape_markdown(nick)}**" + (" (Bohater)" if is_hero else "")
                    for nick, is_hero in top.unlinked
                )
                lines.append(
                    f"Bez połączonego konta: {waiting}. Aby dostać rangę i dodatkowe szanse "
                    f"w giveawayu, połącz konto komendą `!polacz <ID Discorda>` na czacie Twitcha."
                )
            lines.append("")

        if removed_names:
            # Names only: losing the role should not ping anyone.
            lost = ", ".join(f"**{discord.utils.escape_markdown(n)}**" for n in removed_names)
            verb = "traci" if len(removed_names) == 1 else "tracą"
            lines.append(f"⚔️ Rangę **{role.name}** {verb} (poza Top 1): {lost}")

        text = "\n".join(lines).strip()
        if not text:
            return []
        # Discord message limit
        chunks: List[str] = []
        buf = ""
        for line in text.split("\n"):
            if len(buf) + len(line) + 1 > 1900:
                chunks.append(buf)
                buf = ""
            buf += line + "\n"
        if buf.strip():
            chunks.append(buf)
        return chunks

    async def _announce(self, messages: List[str]) -> None:
        channel_id = (self.config.channel_announce_id_debug
                      if DebugMode and self.config.channel_announce_id_debug
                      else self.config.channel_announce_id)
        channel = self.bot.get_channel(channel_id) if channel_id else None
        if channel is None:
            logger.error("Announce channel %s not found.", channel_id)
            return
        for msg in messages:
            try:
                await channel.send(msg, allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True))
            except discord.HTTPException as e:
                logger.error("Could not send announcement: %s", e)

    # ---------- Commands ----------

    @commands.command(name="alterage_sync")
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    async def alterage_sync(self, ctx: commands.Context, mode: str = ""):
        """Manual Alterage Top 1 sync. `$alterage_sync podglad` = dry run."""
        dry_run = mode.lower() in ("podglad", "podgląd", "dry")
        async with ctx.typing():
            result = await self.sync_roles(dry_run=dry_run)
        if result is None:
            await ctx.send("Synchronizacja nie powiodła się (szczegóły w logach). Role bez zmian.")
            return
        summary = (f"{'[PODGLĄD] ' if dry_run else ''}Top 1 bossów: "
                   + (", ".join(f"{t.boss_name}: {t.hero}" for t in result["tops"]) or "brak")
                   + f" | +{len(result['added'])} / -{len(result['removed'])}")
        await ctx.send(summary)
        if dry_run:
            for msg in result["messages"]:
                await ctx.send(msg, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AlterageBossRoles(bot))
