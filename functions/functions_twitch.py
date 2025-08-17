# functions_twitch.py
"""Twitch integration: assigns Discord roles from Twitch data (watchtime, messages, VIP/MOD)."""

import discord
from discord.ext import commands
from typing import Dict, List, Tuple, Optional
from pathlib import Path

from globals.globalvariables import DebugMode


# ---------- Roles config loader (Twitch) ----------

class RolesConfigTwitch:
    """
    Loads Twitch-specific Discord role IDs from a text file (key=value).
    Uses keys:
      - TWT_VIEWER_TIER_1..5       (watchtime tiers)
      - TWT_MESSAGER_TIER_1..5     (message tiers)
      - TWT_VIP_ROLE, TWT_MOD_ROLE
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or Path(__file__).with_name("discord_roles.txt")
        self._raw: Dict[str, int] = {}
        self.viewer_tiers: List[int] = []
        self.messager_tiers: List[int] = []
        self.vip_role_id: int = 0
        self.mod_role_id: int = 0
        self.reload()

    def reload(self) -> None:
        self._raw = {}
        if not self.path.exists():
            print(f"[RolesConfigTwitch] File not found at {self.path}. Using empty defaults.")
            self._apply()
            return

        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith(";"):
                continue
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            try:
                self._raw[k.strip().upper()] = int(v.strip())
            except ValueError:
                print(f"[RolesConfigTwitch] Non-integer for {k}: {v}")

        self._apply()

    def _apply(self) -> None:
        self.viewer_tiers = [self._raw.get(f"TWT_VIEWER_TIER_{i}") for i in range(1, 6)]
        self.viewer_tiers = [rid for rid in self.viewer_tiers if isinstance(rid, int)]

        self.messager_tiers = [self._raw.get(f"TWT_MESSAGER_TIER_{i}") for i in range(1, 6)]
        self.messager_tiers = [rid for rid in self.messager_tiers if isinstance(rid, int)]

        self.vip_role_id = int(self._raw.get("TWT_VIP_ROLE", 0))
        self.mod_role_id = int(self._raw.get("TWT_MOD_ROLE", 0))


# ---------- Helpers ----------

def get_twitch_viewer_role(watchtime: int, viewer_tier_ids: List[int]) -> Tuple[int, List[int]]:
    # thresholds in hours: [125,250), [250,375), [375,500), [500,750), [750,∞)
    watchtime_h = watchtime / 60 / 60
    tiers = (viewer_tier_ids + [0, 0, 0, 0, 0])[:5]

    if 125 <= watchtime_h < 250:
        role_id = tiers[0]
    elif 250 <= watchtime_h < 375:
        role_id = tiers[1]
    elif 375 <= watchtime_h < 500:
        role_id = tiers[2]
    elif 500 <= watchtime_h < 750:
        role_id = tiers[3]
    elif watchtime_h >= 750:
        role_id = tiers[4]
    else:
        role_id = 0

    roles_to_remove = [rid for rid in tiers if rid]
    if role_id in roles_to_remove:
        roles_to_remove.remove(role_id)
    return role_id, roles_to_remove


def get_twitch_messager_role(messages: int, messager_tier_ids: List[int]) -> Tuple[int, List[int]]:
    # thresholds in messages: [1000,3600), [3600,7500), [7500,15000), [15000,30000), [30000,∞)
    tiers = (messager_tier_ids + [0, 0, 0, 0, 0])[:5]

    if 1000 <= messages < 3600:
        role_id = tiers[0]
    elif 3600 <= messages < 7500:
        role_id = tiers[1]
    elif 7500 <= messages < 15000:
        role_id = tiers[2]
    elif 15000 <= messages < 30000:
        role_id = tiers[3]
    elif messages >= 30000:
        role_id = tiers[4]
    else:
        role_id = 0

    roles_to_remove = [rid for rid in tiers if rid]
    if role_id in roles_to_remove:
        roles_to_remove.remove(role_id)
    return role_id, roles_to_remove


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

    async def assign_roles_watchtime(self):
        """Assign roles to users based on their watchtime from the database."""
        db_rows = await self.bot.pg_con.fetch("""
            SELECT discord_id, chtr_time, chtr_issub 
            FROM chatters_information 
            WHERE chtr_time > 445000 AND discord_id IS NOT NULL
        """)

        guild = self.bot.get_guild(686137998177206281)
        if guild is None:
            print("Guild not found.")
            return

        guild_members = {m.id: m for m in guild.members}
        all_roles = {r.id: r for r in guild.roles}

        tier_role_ids = self.roles.viewer_tiers
        tier_roles = [all_roles[rid] for rid in tier_role_ids if rid in all_roles]
        chat_channel = self.bot.get_channel(881090112576962560 if DebugMode else 776379796367212594)

        user_best: Dict[int, int] = {}
        for user_id, time, is_sub in db_rows:
            if user_id in user_best and time < user_best[user_id]:
                continue
            user_best[user_id] = time

        for user_id, time in user_best.items():
            member = guild_members.get(int(user_id))
            if not member:
                continue

            role_id, roles_to_remove = get_twitch_viewer_role(time, self.roles.viewer_tiers)
            if role_id <= 0:
                continue

            my_role = all_roles.get(role_id)
            if not my_role:
                continue

            try:
                new_index = tier_roles.index(my_role)
            except ValueError:
                continue

            current_index = -1
            for idx, role in enumerate(tier_roles):
                if role in member.roles:
                    current_index = max(current_index, idx)

            if new_index > current_index:
                await member.add_roles(my_role)
                for remove_id in roles_to_remove:
                    r = all_roles.get(remove_id)
                    if r and r in member.roles:
                        await member.remove_roles(r)

                if chat_channel:
                    await chat_channel.send(
                        f"<@{member.id}> zdobywa rolę {my_role} za śledzenie streamów na Twitchu!"
                    )

    async def assign_roles_messages(self):
        """Assigns Discord roles to users based on message count stored in the database (Twitch)."""
        db_rows = await self.bot.pg_con.fetch("""
            SELECT discord_id, chtr_comments, chtr_isvip, chtr_ismod 
            FROM chatters_information 
            WHERE chtr_comments > 299 AND discord_id IS NOT NULL
        """)

        guild = self.bot.get_guild(686137998177206281)
        if guild is None:
            print("Guild not found.")
            return

        guild_members = {m.id: m for m in guild.members}
        all_roles = {r.id: r for r in guild.roles}
        chat_channel = self.bot.get_channel(881090112576962560 if DebugMode else 776379796367212594)

        user_best: Dict[int, int] = {}
        for user_id, messages, _vip, _mod in db_rows:
            if user_id in user_best and messages < user_best[user_id]:
                continue
            user_best[user_id] = messages

        for user_id, messages in user_best.items():
            member = guild_members.get(int(user_id))
            if not member:
                continue

            role_id, roles_to_remove = get_twitch_messager_role(messages, self.roles.messager_tiers)
            if role_id <= 0:
                continue

            my_role = all_roles.get(role_id)
            if not my_role:
                continue

            if my_role not in member.roles:
                await member.add_roles(my_role)
                for remove_id in roles_to_remove:
                    r = all_roles.get(remove_id)
                    if r and r in member.roles:
                        await member.remove_roles(r)

                if chat_channel:
                    await chat_channel.send(f"<@{member.id}> zdobywa rolę {my_role} za pisanie na Twitchu!")

    async def assign_roles_vip_mod(self):
        """Assigns VIP or MOD roles to Discord users based on Twitch data."""
        db_rows = await self.bot.pg_con.fetch("""
            SELECT discord_id, chtr_isvip, chtr_ismod
            FROM chatters_information
            WHERE (chtr_isvip = TRUE OR chtr_ismod = TRUE)
            AND discord_id IS NOT NULL
        """)

        guild = self.bot.get_guild(686137998177206281)
        if not guild:
            print("Guild not found.")
            return

        vip_role = discord.utils.get(guild.roles, id=self.roles.vip_role_id) if self.roles.vip_role_id else None
        mod_role = discord.utils.get(guild.roles, id=self.roles.mod_role_id) if self.roles.mod_role_id else None
        if not vip_role or not mod_role:
            print("Required roles not found (TWT_VIP_ROLE/TWT_MOD_ROLE).")
            return

        chat_channel = self.bot.get_channel(776379796367212594)

        for user_id, is_vip, is_mod in db_rows:
            member = guild.get_member(int(user_id))
            if not member:
                continue

            try:
                if is_vip and vip_role not in member.roles:
                    await member.add_roles(vip_role)
                    if chat_channel:
                        await chat_channel.send(f"<@{member.id}> otrzymał {vip_role} (VIP Twitch).")

                if is_mod and mod_role not in member.roles:
                    await member.add_roles(mod_role)
                    if chat_channel:
                        await chat_channel.send(f"<@{member.id}> otrzymał {mod_role} (MOD Twitch).")

            except Exception as e:
                print(f"Error assigning role to {user_id}: {e}")


def setup(bot):
    bot.add_cog(FunctionsTwitch(bot))
