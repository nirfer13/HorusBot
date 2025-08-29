# -*- coding: utf-8 -*-
"""
Twitch integration: assigns Discord roles from Twitch data (watchtime, messages, VIP/MOD).
- Reads thresholds, guild_id, channel ids and role ids from a key=value file: discord_roles.txt
- Exposes a Cog: FunctionsTwitch
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional

import discord
from discord.ext import commands

# Global debug switch (optional)
try:
    from globals.globalvariables import DebugMode  # type: ignore
except Exception:
    DebugMode = False


# ---------- Config loader ----------

class RolesConfigTwitch:
    """
    Supported keys in discord_roles.txt:
      # Role IDs (int)
      - TWT_VIEWER_TIER_1..6         (watchtime tiers)
      - TWT_MESSAGER_TIER_1..6       (message tiers)
      - TWT_VIP_ROLE
      - TWT_MOD_ROLE

      # Thresholds (CSV of 6 ints; index 0 => tier_1)
      - TWT_WATCH_THRESHOLDS_H=125,250,375,500,750,1500   (watchtime in HOURS)
      - TWT_MSG_THRESHOLDS=1000,3600,7500,15000,30000,60000

      # Discord targets (int)
      - GUILD_ID
      - CHANNEL_ANNOUNCE_ID
      - CHANNEL_ANNOUNCE_ID_DEBUG    (optional when DebugMode is True)
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or Path(__file__).with_name("discord_roles.txt")
        self._raw: Dict[str, str] = {}

        self.viewer_tiers: List[int] = []
        self.messager_tiers: List[int] = []
        self.vip_role_id: int = 0
        self.mod_role_id: int = 0

        self.watch_thresholds_h: List[int] = []
        self.msg_thresholds: List[int] = []

        self.guild_id: int = 0
        self.channel_announce_id: int = 0
        self.channel_announce_id_debug: int = 0

        self.reload()

    @staticmethod
    def _parse_int(value: str, default: int = 0) -> int:
        try:
            return int(value)
        except Exception:
            return default

    @staticmethod
    def _parse_int_list(value: str, expected: int, default: List[int]) -> List[int]:
        if not value:
            return list(default)
        parts = [p.strip() for p in re.split(r"[,\s;]+", value) if p.strip()]
        out: List[int] = []
        for p in parts[:expected]:
            try:
                out.append(int(p))
            except Exception:
                pass
        if not out:
            return list(default)
        if len(out) < expected:
            out += default[len(out):expected]
        else:
            out = out[:expected]
        return out

    def reload(self) -> None:
        self._raw = {}
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith(("#", ";")) or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                self._raw[k.strip().upper()] = v.strip()
        else:
            print(f"[RolesConfigTwitch] File not found at {self.path}. Using defaults.")

        self._apply()

    def _apply(self) -> None:
        # Role IDs: watchtime tiers
        tiers: List[int] = []
        for i in range(1, 7):
            rid = self._parse_int(self._raw.get(f"TWT_VIEWER_TIER_{i}", "0"), 0)
            if rid:
                tiers.append(rid)
        self.viewer_tiers = tiers[:6]

        # Role IDs: message tiers
        tiers = []
        for i in range(1, 7):
            rid = self._parse_int(self._raw.get(f"TWT_MESSAGER_TIER_{i}", "0"), 0)
            if rid:
                tiers.append(rid)
        self.messager_tiers = tiers[:6]

        self.vip_role_id = self._parse_int(self._raw.get("TWT_VIP_ROLE", "0"), 0)
        self.mod_role_id = self._parse_int(self._raw.get("TWT_MOD_ROLE", "0"), 0)

        # Thresholds (defaults mirror your current logic)
        default_watch_h = [125, 250, 375, 500, 750, 1500]
        default_msg = [1000, 3600, 7500, 15000, 30000, 60000]
        self.watch_thresholds_h = self._parse_int_list(self._raw.get("TWT_WATCH_THRESHOLDS_H", ""), 6, default_watch_h)
        self.msg_thresholds = self._parse_int_list(self._raw.get("TWT_MSG_THRESHOLDS", ""), 6, default_msg)

        # Discord IDs
        self.guild_id = self._parse_int(self._raw.get("GUILD_ID", "0"), 0)
        self.channel_announce_id = self._parse_int(self._raw.get("CHANNEL_ANNOUNCE_ID", "0"), 0)
        self.channel_announce_id_debug = self._parse_int(self._raw.get("CHANNEL_ANNOUNCE_ID_DEBUG", "0"), 0)


# ---------- Helpers ----------

def _tier_for_value(value: float, thresholds: List[int]) -> int:
    """Return tier index [0..5] via greedy '>= threshold' rule; if below tier_1 -> -1."""
    best = -1
    for idx, th in enumerate(thresholds[:6]):
        if value >= th:
            best = idx
    return best


def _roles_to_remove(keep_role_id: int, tier_role_ids: List[int]) -> List[int]:
    r = [rid for rid in tier_role_ids if rid]
    if keep_role_id in r:
        try:
            r.remove(keep_role_id)
        except ValueError:
            pass
    return r


# ---------- Cog ----------

class FunctionsTwitch(commands.Cog, name="FunctionsTwitch"):
    """
    Cog responsible for Twitch→Discord role syncing.
    Expects bot.pg_con (asyncpg connection) and proper member intents enabled.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.roles = RolesConfigTwitch()

    async def create_ttv_dc_database(self) -> None:
        """Create discord_twitch table if does not exist."""
        table_creation = """
        CREATE TABLE IF NOT EXISTS discord_twitch (
            DISCORD_ID  BIGINT PRIMARY KEY,
            TWITCH_NAME TEXT NOT NULL
        );"""
        await self.bot.pg_con.execute(table_creation)
        print("[FunctionsTwitch] Table discord_twitch ensured.")

    # --- WATCHTIME ---

    async def assign_roles_watchtime(self) -> None:
        """
        Assign roles to users based on their watchtime from the database.
        DB field chtr_time is expected in **seconds**; converted to hours.
        Requires table: chatters_information(discord_id BIGINT, chtr_time BIGINT, ...)
        """
        rows = await self.bot.pg_con.fetch("""
            SELECT discord_id, chtr_time
            FROM chatters_information
            WHERE chtr_time >= 1 AND discord_id IS NOT NULL
        """)

        guild_id = self.roles.guild_id
        if not guild_id:
            print("[FunctionsTwitch] Missing GUILD_ID in config.")
            return

        guild = self.bot.get_guild(int(guild_id))
        if guild is None:
            print("[FunctionsTwitch] Guild not found or bot not in guild.")
            return

        channel_id = (self.roles.channel_announce_id_debug if DebugMode and self.roles.channel_announce_id_debug
                      else self.roles.channel_announce_id)
        announce_ch = self.bot.get_channel(int(channel_id)) if channel_id else None

        guild_members = {m.id: m for m in guild.members}
        all_roles = {r.id: r for r in guild.roles}
        tiers = (self.roles.viewer_tiers + [0, 0, 0, 0, 0, 0])[:6]

        # best watchtime per user
        best: Dict[int, int] = {}
        for user_id, time_sec in rows:
            curr = int(time_sec or 0)
            prev = best.get(user_id, 0)
            if curr > prev:
                best[user_id] = curr

        for user_id, time_sec in best.items():
            member = guild_members.get(int(user_id))
            if not member:
                continue

            watch_h = (time_sec or 0) / 3600.0
            tier_idx = _tier_for_value(watch_h, self.roles.watch_thresholds_h)
            if tier_idx < 0:
                continue

            role_id = tiers[tier_idx]
            if role_id <= 0:
                continue

            role = all_roles.get(role_id)
            if not role:
                continue

            if role not in member.roles:
                try:
                    await member.add_roles(role, reason=f"Twitch watchtime: {int(watch_h)}h")
                    # remove other watchtime tiers
                    for remove_id in _roles_to_remove(role_id, self.roles.viewer_tiers):
                        r = all_roles.get(remove_id)
                        if r and r in member.roles:
                            await member.remove_roles(r, reason="Twitch watchtime: tier change")

                    if announce_ch:
                        await announce_ch.send(
                            f"<@{member.id}> zdobywa rolę **{role.name}** za watchtime na **Twitchu** "
                            f"({int(watch_h)}h)!"
                        )
                except Exception as e:
                    print(f"[FunctionsTwitch] Error assigning watchtime role to {user_id}: {e}")

    # --- MESSAGES ---

    async def assign_roles_messages(self) -> None:
        """
        Assign roles to users based on message count from the database.
        Requires table: chatters_information(discord_id BIGINT, chtr_comments INT, ...)
        """
        rows = await self.bot.pg_con.fetch("""
            SELECT discord_id, chtr_comments
            FROM chatters_information
            WHERE chtr_comments >= 1 AND discord_id IS NOT NULL
        """)

        guild_id = self.roles.guild_id
        if not guild_id:
            print("[FunctionsTwitch] Missing GUILD_ID in config.")
            return

        guild = self.bot.get_guild(int(guild_id))
        if guild is None:
            print("[FunctionsTwitch] Guild not found or bot not in guild.")
            return

        channel_id = (self.roles.channel_announce_id_debug if DebugMode and self.roles.channel_announce_id_debug
                      else self.roles.channel_announce_id)
        announce_ch = self.bot.get_channel(int(channel_id)) if channel_id else None

        guild_members = {m.id: m for m in guild.members}
        all_roles = {r.id: r for r in guild.roles}
        tiers = (self.roles.messager_tiers + [0, 0, 0, 0, 0, 0])[:6]

        # best messages per user
        best: Dict[int, int] = {}
        for user_id, messages in rows:
            curr = int(messages or 0)
            prev = best.get(user_id, 0)
            if curr > prev:
                best[user_id] = curr

        for user_id, messages in best.items():
            member = guild_members.get(int(user_id))
            if not member:
                continue

            tier_idx = _tier_for_value(messages, self.roles.msg_thresholds)
            if tier_idx < 0:
                continue

            role_id = tiers[tier_idx]
            if role_id <= 0:
                continue

            role = all_roles.get(role_id)
            if not role:
                continue

            if role not in member.roles:
                try:
                    await member.add_roles(role, reason=f"Twitch messages: {messages}")
                    # remove other message tiers
                    for remove_id in _roles_to_remove(role_id, self.roles.messager_tiers):
                        r = all_roles.get(remove_id)
                        if r and r in member.roles:
                            await member.remove_roles(r, reason="Twitch messages: tier change")

                    if announce_ch:
                        await announce_ch.send(
                            f"<@{member.id}> zdobywa rolę **{role.name}** za pisanie na **Twitchu** "
                            f"({messages} wiadomości)!"
                        )
                except Exception as e:
                    print(f"[FunctionsTwitch] Error assigning message role to {user_id}: {e}")

    # --- VIP / MOD ---

    async def assign_roles_vip_mod(self) -> None:
        """
        Assign VIP / MOD roles based on chtr_isvip / chtr_ismod flags.
        Requires table: chatters_information(discord_id BIGINT, chtr_isvip BOOL, chtr_ismod BOOL)
        """
        rows = await self.bot.pg_con.fetch("""
            SELECT discord_id, chtr_isvip, chtr_ismod
            FROM chatters_information
            WHERE (chtr_isvip = TRUE OR chtr_ismod = TRUE)
              AND discord_id IS NOT NULL
        """)

        guild_id = self.roles.guild_id
        if not guild_id:
            print("[FunctionsTwitch] Missing GUILD_ID in config.")
            return

        guild = self.bot.get_guild(int(guild_id))
        if not guild:
            print("[FunctionsTwitch] Guild not found or bot not in guild.")
            return

        vip_role = discord.utils.get(guild.roles, id=self.roles.vip_role_id) if self.roles.vip_role_id else None
        mod_role = discord.utils.get(guild.roles, id=self.roles.mod_role_id) if self.roles.mod_role_id else None
        if not vip_role and not mod_role:
            print("[FunctionsTwitch] Missing TWT_VIP_ROLE and/or TWT_MOD_ROLE in config.")
            return

        channel_id = (self.roles.channel_announce_id_debug if DebugMode and self.roles.channel_announce_id_debug
                      else self.roles.channel_announce_id)
        announce_ch = self.bot.get_channel(int(channel_id)) if channel_id else None

        for user_id, is_vip, is_mod in rows:
            member = guild.get_member(int(user_id))
            if not member:
                continue

            try:
                if is_vip and vip_role and vip_role not in member.roles:
                    await member.add_roles(vip_role, reason="Twitch VIP flag")
                    if announce_ch:
                        await announce_ch.send(f"<@{member.id}> otrzymał {vip_role.name} (VIP Twitch).")

                if is_mod and mod_role and mod_role not in member.roles:
                    await member.add_roles(mod_role, reason="Twitch MOD flag")
                    if announce_ch:
                        await announce_ch.send(f"<@{member.id}> otrzymał {mod_role.name} (MOD Twitch).")

            except Exception as e:
                print(f"[FunctionsTwitch] Error assigning VIP/MOD role to {user_id}: {e}")


def setup(bot: commands.Bot) -> None:
    bot.add_cog(FunctionsTwitch(bot))
