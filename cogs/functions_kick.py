# functions_kick.py
# -*- coding: utf-8 -*-
"""Kick integration: assigns Discord roles from Kick data (messages, VIP/MOD).
   Thresholds, guild_id and channel_id are loaded from a key=value file.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Tuple, Optional

import discord
from discord.ext import commands

from globals.globalvariables import DebugMode


# ---------- Roles & config loader (Kick) ----------

class RolesConfigKick:
    """
    Loads Kick-specific Discord settings from a text file (key=value).
    Supported keys (all optional, but role IDs you use must exist in Discord):
      - KICK_MESSAGER_TIER_1..5     (role IDs for tiers)
      - KICK_VIP_ROLE, KICK_MOD_ROLE
      - KICK_MSG_THRESHOLDS         (CSV of 5 increasing ints; default: 1000,3600,7500,15000,30000)
      - GUILD_ID                    (Discord guild id)
      - CHANNEL_ANNOUNCE_ID         (target channel id for announcements)
      - CHANNEL_ANNOUNCE_ID_DEBUG   (optional separate channel id in DebugMode)
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or Path(__file__).with_name("discord_roles.txt")
        self._raw: Dict[str, str] = {}

        # Parsed fields
        self.messager_tiers: List[int] = []
        self.vip_role_id: int = 0
        self.mod_role_id: int = 0

        self.msg_thresholds: List[int] = []

        self.guild_id: int = 0
        self.channel_announce_id: int = 0
        self.channel_announce_id_debug: int = 0

        self.reload()

    def reload(self) -> None:
        self._raw = {}
        if not self.path.exists():
            print(f"[RolesConfigKick] File not found at {self.path}. Using defaults.")
            self._apply()
            return

        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith(";") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            self._raw[k.strip().upper()] = v.strip()

        self._apply()

    # ---- helpers ----
    @staticmethod
    def _parse_int(value: str, default: int = 0) -> int:
        try:
            return int(value)
        except Exception:
            return default

    @staticmethod
    def _parse_int_list(value: str, expected_len: int, default: List[int]) -> List[int]:
        if not value:
            return list(default)
        parts = [p.strip() for p in re.split(r"[,\s;]+", value) if p.strip()]
        out: List[int] = []
        for p in parts[:expected_len]:
            try:
                out.append(int(p))
            except Exception:
                pass
        # pad/trim
        if not out:
            return list(default)
        if len(out) < expected_len:
            out += default[len(out):expected_len]
        else:
            out = out[:expected_len]
        return out

    def _apply(self) -> None:
        # Role IDs
        tiers: List[int] = []
        for i in range(1, 6):
            raw = self._raw.get(f"KICK_MESSAGER_TIER_{i}", "")
            rid = self._parse_int(raw, 0)
            if rid:
                tiers.append(rid)
        self.messager_tiers = tiers[:5]

        self.vip_role_id = self._parse_int(self._raw.get("KICK_VIP_ROLE", "0"), 0)
        self.mod_role_id = self._parse_int(self._raw.get("KICK_MOD_ROLE", "0"), 0)

        # Thresholds for messages (defaults same jak u Ciebie)
        default_msg = [1000, 3600, 7500, 15000, 30000]
        self.msg_thresholds = self._parse_int_list(self._raw.get("KICK_MSG_THRESHOLDS", ""), 5, default_msg)

        # Discord IDs
        self.guild_id = self._parse_int(self._raw.get("GUILD_ID", "0"), 0)
        self.channel_announce_id = self._parse_int(self._raw.get("CHANNEL_ANNOUNCE_ID", "0"), 0)
        self.channel_announce_id_debug = self._parse_int(self._raw.get("CHANNEL_ANNOUNCE_ID_DEBUG", "0"), 0)


# ---------- Helpers ----------

def _tier_for_messages(messages: int, thresholds: List[int]) -> int:
    """
    Return tier index [0..4] by messages only.
    Greedy highest tier whose threshold <= messages. If none -> -1.
    """
    best = -1
    for idx, th in enumerate(thresholds[:5]):
        if messages >= th:
            best = idx
    return best


def _roles_to_remove(keep_role_id: int, messager_role_ids: List[int]) -> List[int]:
    r = [rid for rid in messager_role_ids if rid]
    if keep_role_id in r:
        try:
            r.remove(keep_role_id)
        except ValueError:
            pass
    return r


# ---------- Cog ----------

class FunctionsKick(commands.Cog, name="FunctionsKick"):
    """Kick class, which is used to integrate Kick accounts with Discord."""
    def __init__(self, bot):
        self.bot = bot
        self.roles = RolesConfigKick()

    async def create_kick_dc_database(self):
        """Create discord_kick table if does not exist."""
        table_creation = """CREATE TABLE IF NOT EXISTS discord_kick (
        DISCORD_ID  SERIAL PRIMARY KEY,
        KICK_NAME   TEXT NOT NULL
        ); """
        await self.bot.pg_con.execute(table_creation)
        print("Table discord_kick created successfully.")

    async def assign_roles_messages(self):
        """
        Assign Discord roles to users based on message count stored for Kick.
        Uses per-tier message thresholds from config.
        """
        db_rows = await self.bot.pg_con.fetch("""
            SELECT discord_id, chtr_comments
            FROM chatters_information_kick
            WHERE chtr_comments >= 1 AND discord_id IS NOT NULL
        """)

        # Guild & announce channel
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

        # pick best messages per discord_id if duplicates exist
        best: Dict[int, int] = {}
        for user_id, messages in db_rows:
            curr = int(messages or 0)
            prev = best.get(user_id, 0)
            if curr > prev:
                best[user_id] = curr

        tiers = (self.roles.messager_tiers + [0, 0, 0, 0, 0])[:5]

        for user_id, messages in best.items():
            member = guild_members.get(int(user_id))
            if not member:
                continue

            tier_idx = _tier_for_messages(messages, self.roles.msg_thresholds)
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
                    # remove other tier roles
                    for remove_id in _roles_to_remove(role_id, self.roles.messager_tiers):
                        r = all_roles.get(remove_id)
                        if r and r in member.roles:
                            await member.remove_roles(r)

                    if chat_channel:
                        await chat_channel.send(
                            f"<@{member.id}> zdobywa rolę **{role}** za aktywność na **Kicku** "
                            f"({messages} wiadomości)!"
                        )
                except Exception as e:
                    print(f"[FunctionsKick] Error assigning message role to {user_id}: {e}")

    async def assign_roles_vip_mod(self):
        """Assign VIP or MOD roles to Discord users based on Kick data."""
        db_rows = await self.bot.pg_con.fetch("""
            SELECT discord_id, chtr_isvip, chtr_ismod
            FROM chatters_information_kick
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
            print("Required roles not found (KICK_VIP_ROLE/KICK_MOD_ROLE).")
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
                        await chat_channel.send(f"<@{member.id}> otrzymał {vip_role} (VIP Kick).")

                if is_mod and mod_role and mod_role not in member.roles:
                    await member.add_roles(mod_role)
                    if chat_channel:
                        await chat_channel.send(f"<@{member.id}> otrzymał {mod_role} (MOD Kick).")

            except Exception as e:
                print(f"[FunctionsKick] Error assigning role to {user_id}: {e}")


def setup(bot):
    bot.add_cog(FunctionsKick(bot))
