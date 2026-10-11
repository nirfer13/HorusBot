# -*- coding: utf-8 -*-
"""
Twitch sub gifts -> Discord tier roles.

Every 10 minutes (and once on startup) viewer_sub_gifts is read and every
gifter linked via !polacz gets the role of the highest tier reached
(TWT_SUBGIFT_TIER_1..6 / TWT_SUBGIFT_THRESHOLDS in cogs/discord_roles.txt).
Lower tier roles are removed. Roles are never taken away from people missing
in the data. Gifters without a linked account are announced once per tier
with a !polacz hint.

- Read-only: queries run inside a READ ONLY transaction on bot.pg_con.
- If any query fails, no role is touched until the next successful sync.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

import discord
from discord.ext import commands, tasks

from cogs.functions_twitch import RolesConfigTwitch

try:
    from globals.globalvariables import DebugMode  # type: ignore
except Exception:
    DebugMode = False

logger = logging.getLogger(__name__)

SYNC_MINUTES = 10

TWITCH_LINK = "[Twitchu](<https://www.twitch.tv/altermmo_pl>)"

# {chatter_key: highest threshold already announced} for unlinked gifters.
STATE_FILE = "sub_gifts_announced.json"

GIFTERS_QUERY = """
SELECT g.chatter_key, g.chatter_name, g.gifts, ci.discord_id
FROM viewer_sub_gifts g
LEFT JOIN LATERAL (
    SELECT discord_id
    FROM chatters_information
    WHERE lower(trim(chtr_name)) = lower(trim(g.chatter_key))
      AND discord_id IS NOT NULL AND discord_id::text <> ''
    LIMIT 1
) ci ON TRUE
WHERE g.gifts >= $1
ORDER BY g.gifts DESC;
"""


@dataclass
class Gifter:
    key: str
    name: str
    gifts: int
    discord_id: Optional[int]
    tier: int  # index into tier roles / thresholds


def _parse_discord_id(raw) -> Optional[int]:
    try:
        value = int(str(raw).strip())
    except Exception:
        return None
    return value if value > 0 else None


def _tier_for(gifts: int, thresholds: List[int]) -> int:
    best = -1
    for idx, th in enumerate(thresholds):
        if gifts >= th:
            best = idx
    return best


class TwitchSubGifts(commands.Cog, name="TwitchSubGifts"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.config = RolesConfigTwitch()
        raw = self.config._raw
        self.tier_roles: List[int] = [
            RolesConfigTwitch._parse_int(raw.get(f"TWT_SUBGIFT_TIER_{i}", "0"), 0) for i in range(1, 7)
        ]
        self.thresholds: List[int] = RolesConfigTwitch._parse_int_list(
            raw.get("TWT_SUBGIFT_THRESHOLDS", ""), 6, [10, 25, 50, 100, 200, 500]
        )
        self._announced: Dict[str, int] = self._load_state()
        self._started = False

    def cog_unload(self):
        self.sync_loop.cancel()

    @commands.Cog.listener()
    async def on_ready(self):
        if not self._started:
            self._started = True
            self.sync_loop.start()

    # ---------- State ----------

    @staticmethod
    def _load_state() -> Dict[str, int]:
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as file:
                return {str(k): int(v) for k, v in json.load(file).items()}
        except FileNotFoundError:
            return {}
        except Exception as e:
            logger.warning("Could not read %s: %s", STATE_FILE, e)
            return {}

    def _save_state(self) -> None:
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as file:
                json.dump(self._announced, file, indent=2)
        except Exception as e:
            logger.warning("Could not write %s: %s", STATE_FILE, e)

    # ---------- DB ----------

    async def _fetch_gifters(self) -> Optional[List[Gifter]]:
        try:
            async with self.bot.pg_con.acquire() as conn:
                async with conn.transaction(readonly=True):
                    rows = await conn.fetch(GIFTERS_QUERY, min(self.thresholds))
        except Exception as e:
            logger.error("Sub gifts query failed, roles left unchanged: %s: %s", type(e).__name__, e)
            return None

        gifters: List[Gifter] = []
        for r in rows:
            gifts = int(r["gifts"] or 0)
            tier = _tier_for(gifts, self.thresholds)
            if tier < 0:
                continue
            gifters.append(Gifter(
                key=r["chatter_key"],
                name=(r["chatter_name"] or r["chatter_key"]).strip(),
                gifts=gifts,
                discord_id=_parse_discord_id(r["discord_id"]),
                tier=tier,
            ))
        return gifters

    # ---------- Sync ----------

    @tasks.loop(minutes=SYNC_MINUTES)
    async def sync_loop(self):
        await self.sync_roles()

    @sync_loop.before_loop
    async def _before_sync_loop(self):
        await self.bot.wait_until_ready()

    @sync_loop.error
    async def _sync_loop_error(self, error):
        logger.exception("Sub gifts sync loop crashed.", exc_info=error)

    async def sync_roles(self, dry_run: bool = False) -> Optional[dict]:
        guild = self.bot.get_guild(self.config.guild_id)
        if guild is None:
            logger.error("Guild %s not found.", self.config.guild_id)
            return None
        roles = [guild.get_role(rid) if rid else None for rid in self.tier_roles]
        if any(r is None for r in roles):
            logger.error("Missing sub gift tier role(s): %s", self.tier_roles)
            return None

        gifters = await self._fetch_gifters()
        if gifters is None:
            return None

        # One Discord account may be linked to several Twitch logins: keep the best.
        linked: Dict[int, Gifter] = {}
        for g in gifters:
            if g.discord_id is not None:
                prev = linked.get(g.discord_id)
                if prev is None or g.gifts > prev.gifts:
                    linked[g.discord_id] = g

        lines: List[str] = []
        promoted = 0

        for discord_id, g in linked.items():
            member = guild.get_member(discord_id)
            if member is None:
                logger.info("Sub gifter '%s' (%s) is not on the server, skipped.", g.name, discord_id)
                continue
            role = roles[g.tier]
            lower = [r for i, r in enumerate(roles) if i != g.tier and r in member.roles]
            if role in member.roles and not lower:
                continue
            if dry_run:
                if role not in member.roles:
                    promoted += 1
                    lines.append(self._linked_line(member.id, role, g))
                continue
            try:
                if role not in member.roles:
                    await member.add_roles(role, reason=f"Twitch sub gifts: {g.gifts}")
                    promoted += 1
                    lines.append(self._linked_line(member.id, role, g))
                    logger.info("Sub gift role %s added: %s (%s, %d gifts)", role.name, member, g.name, g.gifts)
                if lower:
                    await member.remove_roles(*lower, reason="Twitch sub gifts: tier change")
            except discord.HTTPException as e:
                logger.error("Could not update sub gift roles for %s: %s", member, e)

        state_changed = False
        for g in gifters:
            if g.discord_id is not None:
                continue
            threshold = self.thresholds[g.tier]
            if self._announced.get(g.key, 0) >= threshold:
                continue
            logger.info("Sub gifter '%s' (%d gifts) has no linked Discord account.", g.name, g.gifts)
            lines.append(self._unlinked_line(roles[g.tier], g))
            if not dry_run:
                self._announced[g.key] = threshold
                state_changed = True

        if state_changed:
            self._save_state()

        messages = self._chunk(lines)
        if not dry_run and messages:
            await self._announce(messages)
        return {"promoted": promoted, "messages": messages}

    # ---------- Announcements ----------

    @staticmethod
    def _linked_line(discord_id: int, role, g: Gifter) -> str:
        return (f"🎁 <@{discord_id}> zdobywa rangę **{role.name}** za **{g.gifts}** "
                f"podarowanych subów na {TWITCH_LINK}! Dziękujemy!")

    @staticmethod
    def _unlinked_line(role, g: Gifter) -> str:
        name = discord.utils.escape_markdown(g.name)
        return (f"🎁 **{name}** ma już **{g.gifts}** podarowanych subów na {TWITCH_LINK}! "
                f"Aby dostać rangę **{role.name}** na Discordzie, połącz konto komendą "
                f"`!polacz <ID Discorda>` na czacie Twitcha.")

    @staticmethod
    def _chunk(lines: List[str]) -> List[str]:
        chunks: List[str] = []
        buf = ""
        for line in lines:
            if buf and len(buf) + len(line) + 1 > 1900:
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
                await channel.send(
                    msg, allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True)
                )
            except discord.HTTPException as e:
                logger.error("Could not send announcement: %s", e)

    # ---------- Commands ----------

    @commands.command(name="subgifts_sync")
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    async def subgifts_sync(self, ctx: commands.Context, mode: str = ""):
        """Manual sub gifts role sync. `$subgifts_sync podglad` = dry run."""
        dry_run = mode.lower() in ("podglad", "podgląd", "dry")
        async with ctx.typing():
            result = await self.sync_roles(dry_run=dry_run)
        if result is None:
            await ctx.send("Synchronizacja nie powiodła się (szczegóły w logach). Role bez zmian.")
            return
        await ctx.send(f"{'[PODGLĄD] ' if dry_run else ''}Sub gifty: +{result['promoted']} rang.")
        if dry_run:
            for msg in result["messages"]:
                await ctx.send(msg, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TwitchSubGifts(bot))
