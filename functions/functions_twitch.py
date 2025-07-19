"""File to sum up the experience from drops."""

import discord
import asyncio
from discord.ext import commands

#Import Globals
from globals.globalvariables import DebugMode


class FunctionsTwitch(commands.Cog, name="FunctionsTwitch"):
    """Twitch class, which is used to integrate Twitch accounts with Discord."""
    def __init__(self, bot):
        self.bot = bot

    global create_ttv_dc_database
    async def create_ttv_dc_database(self):
        """Create discord_twitch table if does not exist."""

        table_creation = """CREATE TABLE IF NOT EXISTS discord_twitch (
        DISCORD_ID  SERIAL PRIMARY KEY,
        TWITCH_NAME TEXT NOT NULL
        ); """
        await self.bot.pg_con.execute(table_creation)
        print("Table discord_twitch created successfully.")

    global assign_roles_watchtime

    async def assign_roles_watchtime(self):
        """Assign roles to users based on their watchtime from the database."""

        # Fetch users with sufficient watchtime from the database
        db_ranking_twitch = await self.bot.pg_con.fetch("""
            SELECT discord_id, chtr_time, chtr_issub 
            FROM chatters_information 
            WHERE chtr_time > 445000 AND discord_id IS NOT NULL
        """)

        # Get guild object
        guild = self.bot.get_guild(686137998177206281)
        if guild is None:
            print("Guild not found.")
            return

        # Cache members of the guild
        guild_members = {member.id: member for member in guild.members}

        # Cache all roles once
        all_roles = {role.id: role for role in guild.roles}

        # List of tier roles from lowest to highest
        tier_role_ids = [
            1044570191239057538,  # Tier 1
            1044570461511622716,  # Tier 2
            1044570672313147442,  # Tier 3
            1044570838365638667,  # Tier 4
            1175454109705441410,  # Tier 5
        ]
        tier_roles = [all_roles[rid] for rid in tier_role_ids if rid in all_roles]

        # Determine announcement channel
        chat_channel = self.bot.get_channel(881090112576962560 if DebugMode else 776379796367212594)

        # Consolidate users (in case of duplicates with lower time)
        user_dict = {}
        for user_id, time, is_sub in db_ranking_twitch:
            print(f"User: {user_id} - Time: {time/60/60:.2f}h - Sub: {is_sub}")
            if user_id in user_dict and time < user_dict[user_id]:
                continue
            user_dict[user_id] = time

        # Process each user
        for user_id, time in user_dict.items():
            member = guild_members.get(int(user_id))
            if not member:
                continue  # User not in server

            role_id, roles_to_remove = get_twitch_viewer_role(time)
            if role_id <= 0:
                continue  # No role to assign

            my_role = all_roles.get(role_id)
            if not my_role:
                continue

            # Skip if user already has this role or higher
            try:
                new_index = tier_roles.index(my_role)
            except ValueError:
                continue  # Role not in tier list

            current_index = -1
            for idx, role in enumerate(tier_roles):
                if role in member.roles:
                    current_index = idx

            if new_index > current_index:
                await member.add_roles(my_role)

                # Remove lower roles
                for remove_role_id in roles_to_remove:
                    role_to_remove = all_roles.get(remove_role_id)
                    if role_to_remove and role_to_remove in member.roles:
                        await member.remove_roles(role_to_remove)

                # Send notification
                if chat_channel:
                    await chat_channel.send(
                        f"<@{member.id}> zdobywa rolę {my_role} za śledzenie streamów na Twitchu!"
                    )


    global assign_roles_messages

    async def assign_roles_messages(self):
        """Assigns Discord roles to users based on message count stored in the database."""

        # Fetch users from the database who have more than 299 messages and a linked Discord ID
        db_ranking_twitch = await self.bot.pg_con.fetch("""
            SELECT discord_id, chtr_comments, chtr_isvip, chtr_ismod 
            FROM chatters_information 
            WHERE chtr_comments > 299 AND discord_id IS NOT NULL
        """)

        # Get the Discord guild (server)
        guild = self.bot.get_guild(686137998177206281)
        if guild is None:
            print("Guild not found.")
            return

        # Create a dictionary of all guild members for fast lookup
        guild_members = {member.id: member for member in guild.members}

        # Create a dictionary of all roles in the guild
        all_roles = {role.id: role for role in guild.roles}

        # Select the correct announcement channel based on debug mode
        chat_channel = self.bot.get_channel(881090112576962560 if DebugMode else 776379796367212594)

        # Create a dictionary to keep the highest message count per user
        user_dict = {}

        for user_id, messages, is_vip, is_mod in db_ranking_twitch:
            # Skip if user already added with a higher message count
            if user_id in user_dict and messages < user_dict[user_id]:
                continue
            user_dict[user_id] = messages

        for user_id, messages in user_dict.items():
            member = guild_members.get(int(user_id))
            if not member:
                continue  # Skip if the user is not on the server

            # Get the role ID and any roles to remove based on message count
            role_id, roles_to_remove = get_twitch_messager_role(messages)
            if role_id <= 0:
                continue  # No role assigned for this message count

            my_role = all_roles.get(role_id)
            if not my_role:
                continue  # Role does not exist on the server

            # Assign role if the user doesn't already have it
            if my_role not in member.roles:
                await member.add_roles(my_role)

                # Remove any lower roles the user should no longer have
                for remove_role_id in roles_to_remove:
                    remove_role = all_roles.get(remove_role_id)
                    if remove_role and remove_role in member.roles:
                        await member.remove_roles(remove_role)

                # Send an announcement in the chat channel
                if chat_channel:
                    await chat_channel.send(f"<@{member.id}> zdobywa rolę {my_role} za pisanie na Twitchu!")
        
    global assign_roles_vip_mod
    async def assign_roles_vip_mod(self):
        """Assigns VIP or MOD roles to Discord users based on Twitch data."""

        # Fetch users who are VIP or MOD on Twitch and have linked Discord accounts
        db_ranking_twitch = await self.bot.pg_con.fetch("""
            SELECT discord_id, chtr_isvip, chtr_ismod
            FROM chatters_information
            WHERE (chtr_isvip = TRUE OR chtr_ismod = TRUE)
            AND discord_id IS NOT NULL
        """)

        # Get guild (server) object
        guild = self.bot.get_guild(686137998177206281)
        if not guild:
            print("Guild not found.")
            return

        # Get role objects
        vip_role = discord.utils.get(guild.roles, id=964845150213906442)
        mod_role = discord.utils.get(guild.roles, id=969683014709825627)
        if not vip_role or not mod_role:
            print("One or more required roles not found.")
            return

        # Get channel for notifications
        chat_channel = self.bot.get_channel(776379796367212594)

        # Process users
        for user_id, is_vip, is_mod in db_ranking_twitch:
            member = guild.get_member(int(user_id))
            if not member:
                continue  # User is not in the server

            try:
                # Assign VIP role if needed
                if is_vip and vip_role not in member.roles:
                    await member.add_roles(vip_role)
                    if chat_channel:
                        await chat_channel.send(f"<@{member.id}> otrzymał {vip_role} za bycie VIPem na Twitchu!")

                # Assign MOD role if needed
                if is_mod and mod_role not in member.roles:
                    await member.add_roles(mod_role)
                    if chat_channel:
                        await chat_channel.send(f"<@{member.id}> otrzymał {mod_role} za bycie moderatorem na Twitchu!")

            except Exception as e:
                print(f"Error assigning role to {user_id}: {e}")


def get_twitch_viewer_role(watchtime: int):
    """Select proper viewer role to assign based on watchtime of the user."""

    # Podgladacz (1044570191239057538) - 125
    # Ogladacz (1044570461511622716) - 250
    # Widz (1044570672313147442) - 375
    # Pasjonata (1044570838365638667) - 500
    # Fanboy (1175454109705441410) - 750

    watchtime_h = watchtime / 60 / 60

    if watchtime_h >= 125 and watchtime_h < 250:
        role_id = 1044570191239057538
    elif watchtime_h >= 250 and watchtime_h < 375:
        role_id = 1044570461511622716
    elif watchtime_h >= 375 and watchtime_h < 500:
        role_id = 1044570672313147442
    elif watchtime_h >= 500 and watchtime_h < 750:
        role_id = 1044570838365638667
    elif watchtime_h >= 750:
        role_id = 1175454109705441410
    else:
        role_id = 0

    roles_to_remove = [1044570191239057538,
                    1044570461511622716,
                    1044570672313147442,
                    1044570838365638667,
                    1175454109705441410]

    if role_id in roles_to_remove:
        roles_to_remove.remove(role_id)

    return role_id, roles_to_remove

def get_twitch_messager_role(messages: int):
    """Select proper messager role to assign based on messages of the user."""

    # Analfabeta (1175474239890010134) - 300 - 200 (chtr_issub)
    # Niepisaty (1175474471637893221) - 600 - 350 (chtr_issub)
    # Czatownik (1175474738789875854) - 1250 - 625 (chtr_issub)
    # Skryba (1175474928162721833) - 2500 - 1250 (chtr_issub)
    # Wieszcz (1175475198854709330) - 5000 - 2500 (chtr_issub)

    if messages >= 1000 and messages < 3600:
        role_id = 1175474239890010134
    elif messages >= 3600 and messages < 7500:
        role_id = 1175474471637893221
    elif messages >= 7500 and messages < 15000:
        role_id = 1175474738789875854
    elif messages >= 15000 and messages < 30000:
        role_id = 1175474928162721833
    elif messages >= 30000:
        role_id = 1175475198854709330
    else:
        role_id = 0

    roles_to_remove = [1175474239890010134,
                    1175474471637893221,
                    1175474738789875854,
                    1175474928162721833,
                    1175475198854709330]

    if role_id in roles_to_remove:
        roles_to_remove.remove(role_id)

    return role_id, roles_to_remove

def setup(bot):

    """Load the FunctionsTwitchcog."""
    bot.add_cog(FunctionsTwitch(bot))
