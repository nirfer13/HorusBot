import asyncio
import datetime as dt
import json
import random
import re
import os

import discord
from discord.ext import commands, tasks

global AnnouceChannelID, CommandChannelID,  LogChannelID, HorusID, GuildID, SpamerID
#AnnouceChannelID = 1028340292895645696 #Debug
AnnouceChannelID = 776383677814669343
CommandChannelID = 776379796367212594
LogChannelID = 1057198781206106153
HorusID = 1004008220437778523
GuildID = 686137998177206281
SpamerID = 1061338676338118707

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

class messages_analysis(commands.Cog, name="messages_analysis"):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        print("Bot ready...")

        #function to get context
        channel = self.bot.get_channel(LogChannelID)
        msg = await channel.fetch_message(1057204065706188820)
        ctx = await self.bot.get_context(msg)
        await ctx.send("Horus gotowy do szpiegowania.")

        self.task = self.bot.loop.create_task(self.msg1(ctx))
        print("Task started.")
        

    async def msg1(self, ctx):
        print("Loop check 1.")
        start = (dt.datetime.utcnow() + dt.timedelta(hours=2))
        while(True):
            timestamp = (dt.datetime.utcnow() + dt.timedelta(hours=2))
            print(str(timestamp.strftime("%a %H:%M")))
            if timestamp.strftime("%a %H:%M") == "Mon 18:00" and start != "Mon":
                start = "Mon"
                await self.show_weekly(ctx)
                await self.clear_weekly(ctx)
            elif timestamp.strftime("%a") != "Mon":
                start = (dt.datetime.utcnow() + dt.timedelta(hours=2))

            await asyncio.sleep(60)

    async def show_weekly(self, ctx):
        filename="weekly_ranking.json"

        with open(filename,'r') as file:
            # First we load existing data into a dict.
            file_data = json.load(file)
            print(file_data)
            ranking = dict(sorted(file_data.items(), key=lambda item: item[1], reverse=True))

        rankingString = ""
        x=1
        for Person in ranking.items():
            user = self.bot.get_user(int(Person[0]))
            if user:
                if x==1:
                    rankingString += str(x) + ". **<@" + str(user.id) + ">** - " + str(Person[1]) + " wiadomości - **zdobywa rangę <@&" + str(SpamerID) + ">** i może użyć komendy *$spamer*.\n"
                    my_role = discord.utils.get(ctx.guild.roles, id=SpamerID)
                    members = my_role.members
                    if members:
                        for member in members:
                             await member.remove_roles(my_role)
                    print("Spamer roles removed.")
                    guild = self.bot.get_guild(GuildID)
                    user = guild.get_member(user.id)
                    await user.add_roles(my_role)
                    print("Spamer role granted.")
                else:
                    rankingString += str(x) + ". **" + user.name + "** - " + str(Person[1]) + " wiadomości.\n"
                x+=1
                if x >= 11:
                    break

        #Embed create   
        emb=discord.Embed(title='Ranking spamerów tygodnia!', description=rankingString, color=0x34C6EB)
        emb.set_image(url="https://www.altermmo.pl/wp-content/uploads/writingcat.gif")
        emb.set_footer(text='Piszcie dalej, niech klawiatury płoną!')
        Channel = self.bot.get_channel(AnnouceChannelID)
        await Channel.send(embed=emb)

    async def clear_weekly(self, ctx):
        filename="weekly_ranking.json"

        with open(filename,'r+') as file:
            empty_data = {'1061222744617922620': 0}

            json_object = json.dumps(empty_data, indent=4)
            # Sets file's current position at offset.
            file.seek(0)
            file.truncate(0) # need '0' when using r+
            file.write(json_object)


    @commands.Cog.listener()
    async def on_message(self, ctx):
        print("Message detected.")

        #ctx = await self.bot.get_context(ctx)
        with open("weekly_ranking.json",'r+') as file:

            # First we load existing data into a dict.
            file_data = json.load(file)
           
            id=str(ctx.author.id)

            if id in file_data.keys():
                file_data[id] += 1
            else:
                file_data[id] = 1

            json_object = json.dumps(file_data, indent=4)
            # Sets file's current position at offset.
            file.seek(0)
            file.truncate(0) # need '0' when using r+
            file.write(json_object)

        if "anime" in (ctx.content).lower() and "varrakas" in (ctx.content).lower() and ctx.author.id != 1061222744617922620:

            ctx = await self.bot.get_context(ctx)
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
        else:
            pass

    @commands.command(name="weeklyranking")
    @commands.has_permissions(administrator=True)
    async def weekly_ranking(self,ctx):
        await self.show_weekly(ctx)
        await self.clear_weekly(ctx)

    @commands.command(name="spamer")
    async def spamer(self,ctx):
        my_role = discord.utils.get(ctx.guild.roles, id=SpamerID)
        if my_role in ctx.message.author.roles:
            gif = "flexgifs/" + random.choice(os.listdir("flexgifs/"))
            await ctx.channel.send(file=discord.File(gif))
            await ctx.channel.send('Tak, oto największy spammer tygodnia, czyli <@' + format(ctx.message.author.id) + '> <:Nerdge:984770661702578227>')
        else:
            await ctx.channel.send('Pfff, leszczyku discordowy, jak Ty nawet zdania sklecić nie umiesz... <:Pepega:936907616293093377>')

async def setup(bot):
    await bot.add_cog(messages_analysis(bot))