# functions_kick.py
"""Kick integration: assigns Discord roles from Kick data (messages, VIP/MOD)."""

import discord
from discord.ext import commands
from typing import Dict, List, Tuple, Optional
from pathlib import Path

from globals.globalvariables import DebugMode


# ---------- Roles config loader (Kick) ----------

class RolesConfigKick:
    """
    Loads Kick-specific Discord role IDs from a text file (key=value).
    Uses keys:
      - KICK_MESSAGER_TIER_1..5
      - KICK_VIP_ROLE, KICK_MOD_ROLE
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or Path(__file__).with_name("discord_roles.txt")
        self._raw: Dict[str, int] = {}
        self.messager_tiers: List[int] = []
        self.vip_role_id: int = 0
        self.mod_role_id: int = 0
        self.reload()

    def reload(self) -> None:
        self._raw = {}
        if not self.path.exists():
            print(f"[RolesConfigKick] File not found at {self.path}. Using empty defaults.")
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
                print(f"[RolesConfigKick] Non-integer for {k}: {v}")

        self._apply()

    def _apply(self) -> None:
        self.messager_tiers = [self._raw.get(f"KICK_MESSAGER_TIER_{i}") for i in range(1, 6)]
        self.messager_tiers = [rid for rid in self.messager_tiers if isinstance(rid, int)]

        self.vip_role_id = int(self._raw.get("KICK_VIP_ROLE", 0))
        self.mod_role_id = int(self._raw.get("KICK_MOD_ROLE", 0))


# ---------- Helpers ----------

def get_kick_messager_role(messages: int, messager_tier_ids: List[int]) -> Tuple[int, List[int]]:
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
        """Assign Discord roles to users based on message count stored for Kick."""
        db_rows = await self.bot.pg_con.fetch("""
            SELECT discord_id, chtr_comments, chtr_isvip, chtr_ismod 
            FROM chatters_information_kick 
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

            role_id, roles_to_remove = get_kick_messager_role(messages, self.roles.messager_tiers)
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
                    await chat_channel.send(f"<@{member.id}> zdobywa rolę {my_role} za pisanie na **Kicku**!")

    async def assign_roles_vip_mod(self):
        """Assign VIP or MOD roles to Discord users based on Kick data."""
        db_rows = await self.bot.pg_con.fetch("""
            SELECT discord_id, chtr_isvip, chtr_ismod
            FROM chatters_information_kick
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
            print("Required roles not found (KICK_VIP_ROLE/KICK_MOD_ROLE).")
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
                        await chat_channel.send(f"<@{member.id}> otrzymał {vip_role} (VIP Kick).")

                if is_mod and mod_role not in member.roles:
                    await member.add_roles(mod_role)
                    if chat_channel:
                        await chat_channel.send(f"<@{member.id}> otrzymał {mod_role} (MOD Kick).")

            except Exception as e:
                print(f"Error assigning role to {user_id}: {e}")


def setup(bot):
    bot.add_cog(FunctionsKick(bot))
