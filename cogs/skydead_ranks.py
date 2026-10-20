# -*- coding: utf-8 -*-
"""
SkyDead (World of Warcraft) kill-count ranks -> Discord roles.

SkyDead is a module of the Alterage Twitch bot: a WoW add-on reports kills of
opposite-faction Skyborne players and the bot stores them in skydead_kills,
with WoW characters linked to Twitch accounts in skydead_links
(twitch-rpg-game-alterage/guide/skydead.md).

Every 10 minutes (and once on startup) the all-time kill count of every linked
Twitch account is read - the same count the bot's `!skydead` rank uses: all
kills of all characters linked to that account. A viewer linked to Discord via
`!polacz` gets the role of the highest rank reached; lower SkyDead rank roles
are removed. Roles are never taken away from people missing in the data.
Accounts without a linked Discord are announced once per rank with a !polacz
hint.

- Ranks (kills -> name) are read from ../twitch-rpg-game-alterage/guide/skydead.yaml
  (the bot's source of truth); the defaults below are used when it is missing.
- Roles: SKYDEAD_RANK_1..6 in cogs/discord_roles.txt when set; otherwise the
  role with the rank's name, created on the first sync when missing (needs
  Manage Roles). Created/found IDs are kept in STATE_FILE, so a role can be
  renamed or recoloured on Discord afterwards.
- Wieczny WoWiarz: an account whose WoW achievements (skydead_achievements, OR of the masks of its linked
  characters) reach `achievements.required` of 8 gets that role (name from skydead.yaml, created like the
  rank roles, SKYDEAD_WOWIARZ_ROLE overrides). It is never taken away; unlinked accounts are announced once.
- Read-only DB access: queries run inside a READ ONLY transaction on bot.pg_con.
  If the query fails (also: SkyDead tables not created yet), nothing changes.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import asyncpg
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

# {"roles": {"1": role_id, ...}, "announced": {twitch_login: threshold}}
STATE_FILE = "skydead_ranks.json"

SKYDEAD_YAML = Path(__file__).resolve().parents[2] / "twitch-rpg-game-alterage" / "guide" / "skydead.yaml"

# Fallback when skydead.yaml cannot be read (same values as the bot's guide).
DEFAULT_RANKS: List[Tuple[int, str]] = [
    (1, "Zwiadowca Nieba"),
    (5, "Łowca Skyborne"),
    (15, "Pogromca Wiatru"),
    (30, "Łamacz Skrzydeł"),
    (60, "Postrach Zephras"),
    (100, "Pan Upadłych Niebios"),
]

# Colours of the roles Horus creates, lowest -> highest rank (sky to night).
RANK_COLOURS = [0x9FD3F5, 0x5DADE2, 0x3A7BD5, 0x6C5CE7, 0x8E44AD, 0x4A148C]

COUNTS_QUERY = """
SELECT l.twitch_login AS login, COUNT(k.kill_id) AS kills, ci.discord_id
FROM skydead_links l
JOIN skydead_kills k ON k.killer_key = l.character_key
LEFT JOIN LATERAL (
    SELECT discord_id
    FROM chatters_information
    WHERE lower(trim(chtr_name)) = lower(trim(l.twitch_login))
      AND discord_id IS NOT NULL AND discord_id::text <> ''
    LIMIT 1
) ci ON TRUE
GROUP BY l.twitch_login, ci.discord_id
HAVING COUNT(k.kill_id) >= $1
ORDER BY kills DESC;
"""

# Wieczny WoWiarz: OR masek osiągnięć wszystkich postaci powiązanych z kontem Twitch (bot: skydead_achievements).
WOWIARZ_QUERY = """
SELECT l.twitch_login AS login, BIT_OR(a.mask) AS mask, ci.discord_id
FROM skydead_achievements a
JOIN skydead_links l ON l.character_key = a.character_key
LEFT JOIN LATERAL (
    SELECT discord_id
    FROM chatters_information
    WHERE lower(trim(chtr_name)) = lower(trim(l.twitch_login))
      AND discord_id IS NOT NULL AND discord_id::text <> ''
    LIMIT 1
) ci ON TRUE
GROUP BY l.twitch_login, ci.discord_id;
"""
WOWIARZ_COLOUR = 0xF5C518
DEFAULT_ACHV_RANK = ("Wieczny WoWiarz", 5)

_RANK_LINE = re.compile(r"^\s*-\s*\{\s*kills:\s*(\d+)\s*,\s*name:\s*([^}]+?)\s*\}\s*$")


def load_ranks(path: Path = SKYDEAD_YAML) -> List[Tuple[int, str]]:
    """[(kills, name)] ascending, from the `ranks:` list of skydead.yaml."""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning("SkyDead ranks: %s unreadable (%s), using defaults.", path, e)
        return list(DEFAULT_RANKS)
    ranks: List[Tuple[int, str]] = []
    inside = False
    for line in text.splitlines():
        if re.match(r"^\s*ranks:\s*$", line):
            inside = True
            continue
        if not inside:
            continue
        m = _RANK_LINE.match(line)
        if m:
            ranks.append((int(m.group(1)), m.group(2).strip().strip("'\"")))
        elif line.strip() and not line.strip().startswith("#"):
            break
    if not ranks:
        logger.warning("SkyDead ranks: no `ranks:` list in %s, using defaults.", path)
        return list(DEFAULT_RANKS)
    return sorted(ranks)


def popcount(mask: int) -> int:
    return bin(int(mask or 0) & 0xFF).count("1")


def load_achievement_rank(path: Path = SKYDEAD_YAML) -> Tuple[str, int]:
    """(nazwa rangi, ile osiągnięć potrzeba) z bloku `achievements:` w skydead.yaml."""
    name, required = DEFAULT_ACHV_RANK
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return name, required
    inside, indent = False, 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lead = len(line) - len(line.lstrip())
        if stripped == "achievements:":
            inside, indent = True, lead
            continue
        if not inside:
            continue
        if lead <= indent:
            break  # koniec bloku
        key, _, value = stripped.partition(":")
        value = value.split("#", 1)[0].strip().strip('"').strip("'")
        if key == "rank_name" and value:
            name = value
        elif key == "required" and value.isdigit():
            required = int(value)
    return name, required


def tier_for(kills: int, ranks: List[Tuple[int, str]]) -> int:
    best = -1
    for idx, (threshold, _) in enumerate(ranks):
        if kills >= threshold:
            best = idx
    return best


def _parse_discord_id(raw) -> Optional[int]:
    try:
        value = int(str(raw).strip())
    except Exception:
        return None
    return value if value > 0 else None


@dataclass
class Hunter:
    login: str
    kills: int
    discord_id: Optional[int]
    tier: int  # index into ranks / roles


class SkyDeadRanks(commands.Cog, name="SkyDeadRanks"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.config = RolesConfigTwitch()
        self.ranks = load_ranks()
        raw = self.config._raw
        self.config_roles: List[int] = [
            RolesConfigTwitch._parse_int(raw.get(f"SKYDEAD_RANK_{i}", "0"), 0)
            for i in range(1, len(self.ranks) + 1)
        ]
        self.achv_rank_name, self.achv_required = load_achievement_rank()
        self.config_wowiarz = RolesConfigTwitch._parse_int(raw.get("SKYDEAD_WOWIARZ_ROLE", "0"), 0)
        state = self._load_state()
        self._role_ids: Dict[str, int] = state.get("roles", {})
        self._announced: Dict[str, int] = state.get("announced", {})
        self._started = False
        self._missing_tables_logged = False

    def cog_unload(self):
        self.sync_loop.cancel()

    @commands.Cog.listener()
    async def on_ready(self):
        if not self._started:
            self._started = True
            self.sync_loop.start()

    # ---------- State ----------

    @staticmethod
    def _load_state() -> dict:
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as file:
                data = json.load(file)
            return {
                "roles": {str(k): int(v) for k, v in (data.get("roles") or {}).items()},
                "announced": {str(k): int(v) for k, v in (data.get("announced") or {}).items()},
            }
        except FileNotFoundError:
            return {}
        except Exception as e:
            logger.warning("Could not read %s: %s", STATE_FILE, e)
            return {}

    def _save_state(self) -> None:
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as file:
                json.dump({"roles": self._role_ids, "announced": self._announced}, file, indent=2,
                          ensure_ascii=False)
        except Exception as e:
            logger.warning("Could not write %s: %s", STATE_FILE, e)

    # ---------- Roles ----------

    async def _resolve_roles(self, guild, dry_run: bool) -> Tuple[List[Optional[discord.Role]], List[str]]:
        """Role per rank: discord_roles.txt -> remembered ID -> same name -> create."""
        roles: List[Optional[discord.Role]] = []
        created: List[str] = []
        changed = False
        for idx, (_, name) in enumerate(self.ranks):
            key = str(idx + 1)
            role = None
            for rid in (self.config_roles[idx], self._role_ids.get(key, 0)):
                if rid:
                    role = guild.get_role(rid)
                    if role is not None:
                        break
            if role is None:
                role = discord.utils.find(lambda r, n=name: r.name.casefold() == n.casefold(), guild.roles)
            if role is None and not dry_run:
                try:
                    role = await guild.create_role(
                        name=name,
                        colour=discord.Colour(RANK_COLOURS[min(idx, len(RANK_COLOURS) - 1)]),
                        mentionable=False,
                        hoist=False,
                        reason="SkyDead: ranga za zabójstwa Skyborne w WoW",
                    )
                    created.append(name)
                    logger.info("SkyDead role created: %s (%s)", role.name, role.id)
                except discord.HTTPException as e:
                    logger.error("Could not create SkyDead role %s: %s", name, e)
            if role is None and dry_run:
                created.append(name)
            if role is not None and self._role_ids.get(key) != role.id:
                self._role_ids[key] = role.id
                changed = True
            roles.append(role)
        if changed and not dry_run:
            self._save_state()
        return roles, created

    # ---------- DB ----------

    async def _fetch_hunters(self) -> Optional[List[Hunter]]:
        try:
            async with self.bot.pg_con.acquire() as conn:
                async with conn.transaction(readonly=True):
                    rows = await conn.fetch(COUNTS_QUERY, self.ranks[0][0])
        except asyncpg.exceptions.UndefinedTableError:
            if not self._missing_tables_logged:
                logger.info("SkyDead tables not in the database yet, ranks skipped.")
                self._missing_tables_logged = True
            return None
        except Exception as e:
            logger.error("SkyDead query failed, roles left unchanged: %s: %s", type(e).__name__, e)
            return None
        self._missing_tables_logged = False

        hunters: List[Hunter] = []
        for r in rows:
            kills = int(r["kills"] or 0)
            tier = tier_for(kills, self.ranks)
            if tier < 0:
                continue
            hunters.append(Hunter(
                login=str(r["login"]).strip().lower(),
                kills=kills,
                discord_id=_parse_discord_id(r["discord_id"]),
                tier=tier,
            ))
        return hunters

    # ---------- Sync ----------

    @tasks.loop(minutes=SYNC_MINUTES)
    async def sync_loop(self):
        await self.sync_roles()

    @sync_loop.before_loop
    async def _before_sync_loop(self):
        await self.bot.wait_until_ready()

    @sync_loop.error
    async def _sync_loop_error(self, error):
        logger.exception("SkyDead sync loop crashed.", exc_info=error)

    async def sync_roles(self, dry_run: bool = False) -> Optional[dict]:
        guild = self.bot.get_guild(self.config.guild_id)
        if guild is None:
            logger.error("Guild %s not found.", self.config.guild_id)
            return None

        lines: List[str] = []
        ranks = await self._sync_kill_ranks(guild, dry_run, lines)
        wowiarz = await self._sync_wowiarz(guild, dry_run, lines)
        if ranks is None and wowiarz is None:
            return None
        ranks = ranks or {"promoted": 0, "created": [], "hunters": 0}
        wowiarz = wowiarz or {"granted": 0, "created": [], "accounts": 0}

        messages = self._chunk(lines)
        if not dry_run and messages:
            await self._announce(messages)
        return {"promoted": ranks["promoted"], "created": ranks["created"] + wowiarz["created"],
                "messages": messages, "hunters": ranks["hunters"],
                "wowiarz": wowiarz["granted"], "wowiarz_accounts": wowiarz["accounts"]}

    async def _sync_kill_ranks(self, guild, dry_run: bool, lines: List[str]) -> Optional[dict]:
        hunters = await self._fetch_hunters()
        if hunters is None:
            return None

        roles, created = await self._resolve_roles(guild, dry_run)
        if not dry_run and any(r is None for r in roles):
            logger.error("Missing SkyDead rank role(s), roles left unchanged.")
            return None

        # One Discord account may be linked to several Twitch logins: keep the best.
        linked: Dict[int, Hunter] = {}
        for h in hunters:
            if h.discord_id is not None:
                prev = linked.get(h.discord_id)
                if prev is None or h.kills > prev.kills:
                    linked[h.discord_id] = h

        promoted = 0

        for discord_id, h in linked.items():
            member = guild.get_member(discord_id)
            if member is None:
                logger.info("SkyDead hunter '%s' (%s) is not on the server, skipped.", h.login, discord_id)
                continue
            role = roles[h.tier]
            rank_name = role.name if role is not None else self.ranks[h.tier][1]
            if role is None:  # dry run before the roles exist
                promoted += 1
                lines.append(self._linked_line(member.id, rank_name, h))
                continue
            lower = [r for i, r in enumerate(roles) if r is not None and i != h.tier and r in member.roles]
            if role in member.roles and not lower:
                continue
            if dry_run:
                if role not in member.roles:
                    promoted += 1
                    lines.append(self._linked_line(member.id, rank_name, h))
                continue
            try:
                if role not in member.roles:
                    await member.add_roles(role, reason=f"SkyDead: {h.kills} zabójstw Skyborne")
                    promoted += 1
                    lines.append(self._linked_line(member.id, rank_name, h))
                    logger.info("SkyDead role %s added: %s (%s, %d kills)", role.name, member, h.login, h.kills)
                if lower:
                    await member.remove_roles(*lower, reason="SkyDead: wyższa ranga")
            except discord.HTTPException as e:
                logger.error("Could not update SkyDead roles for %s: %s", member, e)

        state_changed = False
        for h in hunters:
            if h.discord_id is not None:
                continue
            threshold = self.ranks[h.tier][0]
            if self._announced.get(h.login, 0) >= threshold:
                continue
            role = roles[h.tier]
            lines.append(self._unlinked_line(role.name if role is not None else self.ranks[h.tier][1], h))
            if not dry_run:
                self._announced[h.login] = threshold
                state_changed = True

        if state_changed:
            self._save_state()
        return {"promoted": promoted, "created": created, "hunters": len(hunters)}

    # ---------- Wieczny WoWiarz (osiągnięcia konta) ----------

    async def _fetch_wowiarze(self) -> Optional[List[Tuple[str, int, Optional[int]]]]:
        """[(login, maska konta, discord_id)] - OR masek wszystkich postaci powiązanych z kontem Twitch."""
        try:
            async with self.bot.pg_con.acquire() as conn:
                async with conn.transaction(readonly=True):
                    rows = await conn.fetch(WOWIARZ_QUERY)
        except asyncpg.exceptions.UndefinedTableError:
            return None  # bot jeszcze nie utworzył skydead_achievements - nic do zrobienia
        except Exception as e:
            logger.error("SkyDead achievements query failed, role left unchanged: %s: %s", type(e).__name__, e)
            return None
        return [(str(r["login"]).strip().lower(), int(r["mask"] or 0), _parse_discord_id(r["discord_id"]))
                for r in rows]

    async def _resolve_wowiarz_role(self, guild, dry_run: bool) -> Tuple[Optional[discord.Role], List[str]]:
        name = self.achv_rank_name
        role = None
        for rid in (self.config_wowiarz, self._role_ids.get("wowiarz", 0)):
            if rid:
                role = guild.get_role(rid)
                if role is not None:
                    break
        if role is None:
            role = discord.utils.find(lambda r: r.name.casefold() == name.casefold(), guild.roles)
        created: List[str] = []
        if role is None:
            created.append(name)
            if dry_run:
                return None, created
            try:
                role = await guild.create_role(name=name, colour=discord.Colour(WOWIARZ_COLOUR), mentionable=False,
                                               hoist=False, reason="SkyDead: 5 z 8 osiągnięć w WoW")
                logger.info("SkyDead role created: %s (%s)", role.name, role.id)
            except discord.HTTPException as e:
                logger.error("Could not create role %s: %s", name, e)
                return None, created
        if self._role_ids.get("wowiarz") != role.id and not dry_run:
            self._role_ids["wowiarz"] = role.id
            self._save_state()
        return role, created

    async def _sync_wowiarz(self, guild, dry_run: bool, lines: List[str]) -> Optional[dict]:
        accounts = await self._fetch_wowiarze()
        if accounts is None:
            return None
        earned = [(login, mask, did) for login, mask, did in accounts if popcount(mask) >= self.achv_required]
        if not earned:
            return {"granted": 0, "created": [], "accounts": 0}
        role, created = await self._resolve_wowiarz_role(guild, dry_run)
        if role is None and not dry_run:
            return None
        granted = 0
        state_changed = False
        for login, mask, discord_id in earned:
            if discord_id is None:
                key = "wowiarz:" + login
                if not self._announced.get(key):
                    lines.append(self._wowiarz_unlinked_line(login, mask))
                    if not dry_run:
                        self._announced[key] = 1
                        state_changed = True
                continue
            member = guild.get_member(discord_id)
            if member is None:
                continue
            if role is not None and role in member.roles:
                continue  # ranga raz na zawsze - nigdy jej nie zdejmujemy
            granted += 1
            lines.append(self._wowiarz_line(member.id, mask))
            if dry_run:
                continue
            try:
                await member.add_roles(role, reason=f"SkyDead: {popcount(mask)}/8 osiągnięć w WoW")
                logger.info("Role %s added: %s (%s)", role.name, member, login)
            except discord.HTTPException as e:
                logger.error("Could not add role %s to %s: %s", role.name, member, e)
        if state_changed:
            self._save_state()
        return {"granted": granted, "created": created, "accounts": len(earned)}

    def _wowiarz_line(self, discord_id: int, mask: int) -> str:
        return (f"🍺 <@{discord_id}> zdobywa rangę **{self.achv_rank_name}** za **{popcount(mask)}/8** osiągnięć "
                f"w WoW (SkyDead na {TWITCH_LINK})!")

    def _wowiarz_unlinked_line(self, login: str, mask: int) -> str:
        name = discord.utils.escape_markdown(login)
        return (f"🍺 **{name}** ma już **{popcount(mask)}/8** osiągnięć w WoW (SkyDead na {TWITCH_LINK})! Aby dostać "
                f"rangę **{self.achv_rank_name}** na Discordzie, połącz konto komendą `!polacz <ID Discorda>` "
                f"na czacie Twitcha.")

    # ---------- Announcements ----------

    @staticmethod
    def _kills_text(n: int) -> str:
        if n == 1:
            return "1 zabójstwo"
        if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
            return f"{n} zabójstwa"
        return f"{n} zabójstw"

    def _linked_line(self, discord_id: int, rank_name: str, h: Hunter) -> str:
        return (f"⚔️ <@{discord_id}> zdobywa rangę **{rank_name}** za **{self._kills_text(h.kills)}** "
                f"Skyborne w WoW (SkyDead na {TWITCH_LINK})!")

    def _unlinked_line(self, rank_name: str, h: Hunter) -> str:
        name = discord.utils.escape_markdown(h.login)
        return (f"⚔️ **{name}** ma już **{self._kills_text(h.kills)}** Skyborne w WoW (SkyDead na {TWITCH_LINK})! "
                f"Aby dostać rangę **{rank_name}** na Discordzie, połącz konto komendą "
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

    @commands.command(name="skydead_sync")
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    async def skydead_sync(self, ctx: commands.Context, mode: str = ""):
        """Manual SkyDead rank sync. `$skydead_sync podglad` = dry run."""
        dry_run = mode.lower() in ("podglad", "podgląd", "dry")
        async with ctx.typing():
            result = await self.sync_roles(dry_run=dry_run)
        if result is None:
            await ctx.send("Synchronizacja SkyDead nie powiodła się (szczegóły w logach). Role bez zmian.")
            return
        created = result["created"]
        extra = ""
        if created:
            extra = (" | do utworzenia: " if dry_run else " | utworzone role: ") + ", ".join(created)
        await ctx.send(f"{'[PODGLĄD] ' if dry_run else ''}SkyDead: {result['hunters']} kont z rangą, "
                       f"+{result['promoted']} rang; {self.achv_rank_name}: {result['wowiarz_accounts']} kont, "
                       f"+{result['wowiarz']}{extra}.")
        if dry_run:
            for msg in result["messages"]:
                await ctx.send(msg, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SkyDeadRanks(bot))
