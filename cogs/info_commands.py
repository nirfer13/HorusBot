import asyncio
import datetime as dt
import json
import random
import re
import os

import discord
from discord.ext import commands, tasks

global AnnouceChannelID, CommandChannelID,  LogChannelID, HorusID, GuildID
#AnnouceChannelID = 1028340292895645696 #Debug
AnnouceChannelID = 696932659833733131
#CommandChannelID = 1057198781206106153 #Debug
CommandChannelID = 776379796367212594
LogChannelID = 1057198781206106153
HorusID = 1004008220437778523
GuildID = 686137998177206281

URL_REGEX = r"(?i)\b((?:https?://|www\d{0,3}[.]|[a-z0-9.\-]+[.][a-z]{2,4}/)(?:[^\s()<>]+|\(([^\s()<>]+|(\([^\s()<>]+\)))*\))+(?:\(([^\s()<>]+|(\([^\s()<>]+\)))*\)|[^\s`!()\[\]{};:'\".,<>?«»“”‘’]))"
OPTIONS = {
    "1️⃣": 0,
    "2⃣": 1,
    "3⃣": 2,
    "4⃣": 3,
    "5⃣": 4,
}
VOTES = {
    "✅": 0,
    "❌": 1
}

class info_commands(commands.Cog, name="info_commands"):
    def __init__(self, bot):
        self.bot = bot

    class InvalidCommand(commands.CommandError):
        pass

    @commands.command(name="chat")
    @commands.has_permissions(administrator=True)
    async def testcommand(self, ctx, string: str):
        channel = self.bot.get_channel(696932659833733131)
        await channel.send(string)

    @commands.command(name="najlepszemmorpg", aliases=["topmmorpg"])
    async def najlepszemmorpg(self, ctx):
        await ctx.message.add_reaction("<a:SpinAlter:739933507039527033>")
        await ctx.send("Najlepsze MMORPG według różnych kryteriów: https://www.altermmo.pl/tag/toplista/")

    @commands.command(name="spisgier", aliases=["spis"])
    async def spis(self, ctx):
        await ctx.message.add_reaction("<a:SpinAlter:739933507039527033>")
        await ctx.send("Spis wszystkich gier, o których powstały artykuły/filmy na AlterMMO: https://www.altermmo.pl/spis-gier/")

    @commands.command(name="yt", aliases=["youtube"])
    async def yt(self, ctx):
        await ctx.message.add_reaction("<a:SpinAlter:739933507039527033>")
        await ctx.send("Kanał AlterMMO na YouTube: https://www.youtube.com/@AlterMMO/")

    @commands.command(name="ttv", aliases=["twitch"])
    async def ttv(self, ctx):
        await ctx.message.add_reaction("<a:SpinAlter:739933507039527033>")
        await ctx.send("Kanał AlterMMO na Twitch: https://www.twitch.tv/altermmo_pl/")

    @commands.command(name="kiedystream", aliases=["wen"])
    async def kiedystream(self, ctx):
        await ctx.message.add_reaction("<a:SpinAlter:739933507039527033>")
        await ctx.send("Streamy na https://www.twitch.tv/altermmo_pl/ odbywają się w poniedziałki, środy, piątki i niedziele po godzinie 17.")

    @commands.command(name="giveaway", aliases=["losowanie"])
    async def giveaway(self, ctx):
        await ctx.message.add_reaction("<a:SpinAlter:739933507039527033>")
        await ctx.send("Aktualny giveaway znajdziesz na kanale <#796055059082248212>.")

    @commands.command(name="varrakastofananime")
    async def giveaway(self, ctx):
        await ctx.message.add_reaction("<a:SpinAlter:739933507039527033>")
        with open("varrakas.json",'r+') as file:
            # First we load existing data into a dict.
            file_data = json.load(file)
           
            id=str(355755282380488715)

            if id in file_data.keys():
                file_data[id] += 1
            else:
                file_data[id] = 1

            json_object = json.dumps(file_data, indent=4)
            # Sets file's current position at offset.
            file.seek(0)
            file.truncate(0) # need '0' when using r+
            file.write(json_object)

            await ctx.send("Varrakas został okrzyknięty fanem anime " + str(file_data[id]) + " razy. <:pepeYIKES:882183512092999680>")



def setup(bot):
    bot.add_cog(info_commands(bot))