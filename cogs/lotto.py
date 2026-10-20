# -*- coding: utf-8 -*-
"""
Function for giveaways. Win ratio calculated based on roles users have.
"""

import asyncio
import json
from pathlib import Path
import random
import discord
from datetime import datetime, timedelta
from discord.ext import commands
from typing import List

GIVEAWAY_ID = 796055059082248212


class Lotto(commands.Cog, name="lotto"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._running = False

    @commands.Cog.listener()
    async def on_ready(self):
        print("[Lotto] Ready.")
        if not self._running:
            self._running = True

    def _get_ft(self):
        """Return Lotto cog instance (or None if not loaded)."""
        ft = self.bot.get_cog("Lotto")
        if ft is None:
            print("[lotto] Lotto cog not loaded.")
        return ft
    
    #Choosing random user from users that reacted on given message, command takes ID
    #how to get ID of message: Message id is the last part of message link
    #right click on message copy link
    #f.e https://discord.com/channels/857296258594635816/857296258594635820/858771394191425577
    #858771394191425577 is the ID of the message
    @commands.command(name="lotto")
    async def lotto(self, ctx, ID: int):

        print("Lotto starting...")

        with open(Path(__file__).resolve().parents[1] / "ranksConfig.json", encoding="utf-8") as jsonFile:
            jsonObject = json.load(jsonFile)

        users: List[discord.Member] = []
        seen_user_ids = set()
        weights = []

        channel = await self.bot.fetch_channel(GIVEAWAY_ID)
        message = await channel.fetch_message(ID)

        print("Before reactions...")

        guild = message.guild

        for reaction in message.reactions:
            async for user in reaction.users():
                if user.bot:
                    continue

                member = guild.get_member(user.id)
                if member is not None and member.id not in seen_user_ids:
                    seen_user_ids.add(member.id)
                    users.append(member)

        print("Go through users...")

        for user in users:
            print("=====New User=======")
            print(user)

            ranksOfUser = [role.name for role in user.roles]
            temp_weight = 1.0

            for rank in jsonObject.keys():
                if rank in ranksOfUser:
                    print(rank)
                    print("===")
                    temp_weight += jsonObject[rank]

            if temp_weight > 1.0:
                temp_weight -= 1

            # zabezpieczenie, gdy primary_guild nie istnieje
            if getattr(user, "primary_guild", None) and user.primary_guild.tag == "ANTY":
                print("TAG ANTY")
                temp_weight += 3
            else:
                print("Brak ANTY")

            weights.append(temp_weight)
            print(temp_weight)

        if not users:
            await ctx.channel.send("Nie znaleziono żadnych uczestników giveawaya.")
            return

        total_weight = sum(weights)

        chances_text = []
        for user, weight in zip(users, weights):
            chance = (weight / total_weight) * 100
            chances_text.append(f"{user.mention}: **{chance:.2f}%** (waga: {weight})")

        print(weights)
        print(users)

        winner = random.choices(users, weights=weights, k=1)[0]
        winner_index = users.index(winner)
        winner_chance = (weights[winner_index] / total_weight) * 100

        result_message = (
            f"Giveawaya wygrał {winner.mention}! Gratulacje!\n"
            f"Jego szansa na wygraną wynosiła **{winner_chance:.2f}%**.\n\n"
        )

        await ctx.channel.send(result_message)

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Lotto(bot))
