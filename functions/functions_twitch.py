# functions_twitch.py
# -*- coding: utf-8 -*-
"""Twitch integration: assigns Discord roles from Twitch data (watchtime, messages, VIP/MOD).
   Thresholds, guild_id and channel_id are loaded from a key=value file (discord_roles.txt).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Tuple, Optional

import discord
from discord.ext import commands

from globals.globalvariables import DebugMode


# ---------- Roles/config loader (Twitch) ----------

class RolesConfigTwitch:
    """
    Loads Twitch-specific Discord settings from a text file (key=value).
    Supported keys (all optional, but role IDs you use must exist in Discord):

      # Role IDs (int)
      - TWT_VIEWER_TIER_1..5         (watchtime tiers)
      - TWT_MESSAGER_TIER_1..5       (message tiers)
      - TWT_VIP_ROLE
      - TWT_MOD_ROLE

      # Thresholds (CSV of ints; 5 values; index 0 => tier_1, ... tier_5)
      - TWT_WATCH_THRESHOLDS_H=125,250,375,500,750   (watchtime in HOURS)
      - TWT_MSG_THRESHOLDS=1000,3600,7500,15000,30000

      # Discord targets (int)
      - GUILD_ID=...
      - CHANNEL_ANNOUNCE_ID=...
      - CHANNEL_ANNOUNCE_ID_DEBUG=...               (optional for DebugMode)
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or Path(__file__).with_name("discord_roles.txt")
        self._raw: Dict[str, str] = {}

        # Parsed
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

    # ---- helpers ----
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
        if not self.path.exists():
            print(f"[RolesConfigTwitch] File not found at {self.path}. Using defaults.")
            self._apply()
            return

        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith(";") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            self._raw[k.strip().upper()] = v.strip()

        self._apply()

    def _apply(self) -> None:
        # Role IDs
        self.viewer_tiers = []
        for i in range(1, 6):
            rid = self._parse_int(self._raw.get(f"TWT_VIEWER_TIER_{i}", "0"), 0)
            if rid:
                self.viewer_tiers.append(rid)
        self.viewer_tiers = self.viewer_tiers[:5]

        self.messager_tiers = []
        for i in range(1, 6):
            rid = self._parse_int(self._raw.get(f"TWT_MESSAGER_TIER_{i}", "0"), 0)
            if rid:
                self.messager_tiers.append(rid)
        self.messager_tiers = self.messager_tiers[:5]

        self.vip_role_id = self._parse_int(self._raw.get("TWT_VIP_ROLE", "0"), 0)
        self.mod_role_id = self._parse_int(self._raw.get("TWT_MOD_ROLE", "0"), 0)

        # Thresholds (defaults mirror your current logic)
        default_watch_h = [125, 250, 375, 500, 750]
        default_msg = [1000, 3600, 7500, 15000, 30000]

        self.watch_thresholds_h = self._parse_int_list(self._raw.get("TWT_WATCH_THRESHOLDS_H", ""), 5, default_watch_h)
        self.msg_thresholds = self._parse_int_list(self._raw.get("TWT_MSG_THRESHOLDS", ""), 5, default_msg)

        # Discord IDs (same keys as Kick)
        self.guild_id = self._parse_int(self._raw.get("GUILD_ID", "0"), 0)
        self.channel_announce_id = self._parse_int(self._raw.get("CHANNEL_ANNOUNCE_ID", "0"), 0)
        self.channel_announce_id_debug = self._parse_int(self._raw.get("CHANNEL_ANNOUNCE_ID_DEBUG", "0"), 0)


# ---------- Helpers ----------

def _tier_for_value(value: float, thresholds: List[int]) -> int:
    """
    Return tier index [0..4] using greedy '>= threshold' rule.
    If value is below tier_1 threshold -> -1.
    """
    best = -1
    for idx, th in enumerate(thresholds[:5]):
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
    """Twitch class, which is used to integrate Twitch accounts with Discord."""
    def __init__(self, bot):
        self.bot = bot
        self.roles = RolesConfigTwitch()

    async def create_ttv_dc_database(self):
        """Create discord_twitch table if does not exist."""
        table_creation = """CREATE TABLE IF NOT EXISTS discord_twitch (
        DISCORD_ID  SERIAL PRIMARY KEY,
        TWITCH_NAME TEXT NOT NULL
        ); """
        await self.bot.pg_con.execute(table_creation)
        print("Table discord_twitch created successfully.")

    # --- WATCHTIME ---

    async def assign_roles_watchtime(self):
        """
        Assign roles to users based on their watchtime from the database.
        DB field chtr_time is expected in **seconds**; we convert to hours.
        """
        db_rows = await self.bot.pg_con.fetch("""
            SELECT discord_id, chtr_time
            FROM chatters_information
            WHERE chtr_time >= 1 AND discord_id IS NOT NULL
        """)

        # Resolve guild & announce channel (same keys as in Kick)
        guild_id = self.roles.guild_id or 686137998177206281
        guild = self.bot.get_guild(int(guild_id))
        if guild is None:
            print("Guild not found.")
            return

        if DebugMode and self.roles.channel_announce_id_debug:
            channel_id = self.roles.channel_announce_id_debug
        else:
            channel_id = self.roles.channel_announce_id or (881090112576962560 if DebugMode else 776379796367212594)
        chat_channel = self.bot.get_channel(int(channel_id)) if channel_id else None

        guild_members = {m.id: m for m in guild.members}
        all_roles = {r.id: r for r in guild.roles}
        tiers = (self.roles.viewer_tiers + [0, 0, 0, 0, 0])[:5]

        # keep best watchtime per user
        best: Dict[int, int] = {}
        for user_id, time_sec in db_rows:
            curr = int(time_sec or 0)
            prev = best.get(user_id, 0)
            if curr > prev:
                best[user_id] = curr

        for user_id, time_sec in best.items():
            member = guild_members.get(int(user_id))
            if not member:
                continue

            watch_h = (time_sec or 0) / 3600.0  # seconds -> hours
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
                    await member.add_roles(role)
                    # remove other watchtime tiers
                    for remove_id in _roles_to_remove(role_id, self.roles.viewer_tiers):
                        r = all_roles.get(remove_id)
                        if r and r in member.roles:
                            await member.remove_roles(r)

                    if chat_channel:
                        hours_disp = int(watch_h)
                        await chat_channel.send(
                            f"<@{member.id}> zdobywa rolę **{role}** za watchtime na **Twitchu** "
                            f"({hours_disp}h)!"
                        )
                except Exception as e:
                    print(f"[FunctionsTwitch] Error assigning watchtime role to {user_id}: {e}")

    # --- MESSAGES ---

    async def assign_roles_messages(self):
        """
        Assign Discord roles to users based on message count stored in the database (Twitch).
        Uses per-tier message thresholds from config.
        """
        db_rows = await self.bot.pg_con.fetch("""
            SELECT discord_id, chtr_comments
            FROM chatters_information
            WHERE chtr_comments >= 1 AND discord_id IS NOT NULL
        """)

        guild_id = self.roles.guild_id or 686137998177206281
        guild = self.bot.get_guild(int(guild_id))
        if guild is None:
            print("Guild not found.")
            return

        if DebugMode and self.roles.channel_announce_id_debug:
            channel_id = self.roles.channel_announce_id_debug
        else:
            channel_id = self.roles.channel_announce_id or (881090112576962560 if DebugMode else 776379796367212594)
        chat_channel = self.bot.get_channel(int(channel_id)) if channel_id else None

        guild_members = {m.id: m for m in guild.members}
        all_roles = {r.id: r for r in guild.roles}
        tiers = (self.roles.messager_tiers + [0, 0, 0, 0, 0])[:5]

        # keep best messages per user
        best: Dict[int, int] = {}
        for user_id, messages in db_rows:
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
                    await member.add_roles(role)
                    # remove other message tiers
                    for remove_id in _roles_to_remove(role_id, self.roles.messager_tiers):
                        r = all_roles.get(remove_id)
                        if r and r in member.roles:
                            await member.remove_roles(r)

                    if chat_channel:
                        await chat_channel.send(
                            f"<@{member.id}> zdobywa rolę **{role}** za pisanie na **Twitchu** "
                            f"({messages} wiadomości)!"
                        )
                except Exception as e:
                    print(f"[FunctionsTwitch] Error assigning message role to {user_id}: {e}")

    # --- VIP / MOD ---

    async def assign_roles_vip_mod(self):
        """Assigns VIP or MOD roles to Discord users based on Twitch data."""
        db_rows = await self.bot.pg_con.fetch("""
            SELECT discord_id, chtr_isvip, chtr_ismod
            FROM chatters_information
            WHERE (chtr_isvip = TRUE OR chtr_ismod = TRUE)
            AND discord_id IS NOT NULL
        """)

        guild_id = self.roles.guild_id or 686137998177206281
        guild = self.bot.get_guild(int(guild_id))
        if not guild:
            print("Guild not found.")
            return

        vip_role = discord.utils.get(guild.roles, id=self.roles.vip_role_id) if self.roles.vip_role_id else None
        mod_role = discord.utils.get(guild.roles, id=self.roles.mod_role_id) if self.roles.mod_role_id else None
        if not vip_role and not mod_role:
            print("Required roles not found (TWT_VIP_ROLE/TWT_MOD_ROLE).")
            return

        if DebugMode and self.roles.channel_announce_id_debug:
            channel_id = self.roles.channel_announce_id_debug
        else:
            channel_id = self.roles.channel_announce_id or 776379796367212594
        chat_channel = self.bot.get_channel(int(channel_id)) if channel_id else None

        for user_id, is_vip, is_mod in db_rows:
            member = guild.get_member(int(user_id))
            if not member:
                continue

            try:
                if is_vip and vip_role and vip_role not in member.roles:
                    await member.add_roles(vip_role)
                    if chat_channel:
                        await chat_channel.send(f"<@{member.id}> otrzymał {vip_role} (VIP Twitch).")

                if is_mod and mod_role and mod_role not in member.roles:
                    await member.add_roles(mod_role)
                    if chat_channel:
                        await chat_channel.send(f"<@{member.id}> otrzymał {mod_role} (MOD Twitch).")

            except Exception as e:
                print(f"[FunctionsTwitch] Error assigning role to {user_id}: {e}")


def setup(bot):
    bot.add_cog(FunctionsTwitch(bot))
