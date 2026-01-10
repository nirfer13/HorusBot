# -*- coding: utf-8 -*-
"""
Function for giveaways. Win ratio calculated based on roles users have.
"""

import asyncio
import json
import random
import discord
from datetime import datetime, timedelta
from discord.ext import commands
from typing import List


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
        with open("ranksConfig.json", encoding='utf-8') as jsonFile:
            jsonObject = json.load(jsonFile)
        users: List[discord.Member] = []
        weights = []
        message = await ctx.channel.fetch_message(ID)
        for reaction in message.reactions:
            async for user in reaction.users():
                guild = message.guild
                if guild.get_member(user.id) is not None:
                    users.append(user)
        for user in users:				#użytkownicy, którzy zareagowali
            print("=====New User=======")
            print(user)
            print(user.primary_guild)
            ranksOfUser = []
            for role in user.roles:		
                ranksOfUser.append(role.name)           #wszystkie role użytkownika dopisujemy
            #weights.append(1)							#domyslnie szansa x1
            temp_weight = 1.0
            for rank in jsonObject.keys():              #sprawdzanie po skonfigurowanych rankach
                #print(ranksOfUser)
                if rank in ranksOfUser:
                    print(rank)
                    print("===")
                    temp_weight = temp_weight + jsonObject[rank]
            if temp_weight > 1.0:						#odjecie 1 gdy ktos ma bonus
                temp_weight -= 1
            if user.primary_guild.tag == "ANTY":
                print("TAG ANTY")
                temp_weight += 3
            else:
                print("Brak ANTY")
            weights.append(temp_weight)
            print(temp_weight)			
			
        print(weights)
        print(users)
        winner = random.choices(users, weights)
        await ctx.channel.send('Giveawaya wygrał <@' + str(winner[0].id) + '>! Gratulacje!')

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Lotto(bot))
