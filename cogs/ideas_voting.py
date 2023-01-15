import asyncio
import datetime as dt
import json
import random
import re
import os
import discord
from discord.ext import commands, tasks

global CommandChannelID,  LogChannelID, VoteChannelID, HorusID, GuildID, SpamerID, votesReq
#VoteChannelID = 1028340292895645696 #Debug
VoteChannelID = 1059731255786229770
CommandChannelID = 776379796367212594
LogChannelID = 1057198781206106153
HorusID = 1004008220437778523
GuildID = 686137998177206281
SpamerID = 1061338676338118707
votesReq = 8

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

class ideas_voting(commands.Cog, name="ideas_voting"):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        print("Bot ready...")

    async def is_channel(ctx):
        return ctx.channel.id == CommandChannelID or ctx.channel.id == 1057198781206106153

    async def idea_support(self, ctx, users: set, author: discord.User, success: bool):
    
        filename="idea_authors.json"
        Channel = self.bot.get_channel(CommandChannelID)

        with open(filename,'r+') as file:
            # First we load existing data into a dict.
            file_data = json.load(file)
           
            for user in users:
                id = str(user.id)
                if id != str(author.id):
                    if id in file_data.keys():
                        file_data[id] += 0.15
                    else:
                        file_data[id] = 0.15

            if success:
                id = str(author.id)
                if id in file_data.keys():
                    file_data[id] += 1
                else:
                    file_data[id] = 1

            json_object = json.dumps(file_data, indent=4)
            # Sets file's current position at offset.
            file.seek(0)
            file.truncate(0) # need '0' when using r+
            file.write(json_object)

            role1 = discord.utils.get(ctx.guild.roles, id=1062660624066297926) #Bystrzak
            role2 = discord.utils.get(ctx.guild.roles, id=1062660719528656916) #Intelektualista
            role3 = discord.utils.get(ctx.guild.roles, id=1062660709042892802) #Geniusz

            for user in users:
                id = str(user.id)
                if id in file_data.keys() and user.id != 1061222744617922620:
                    if file_data[id] >= 3 and file_data[id] < 7 and role1 not in user.roles:
                        await user.add_roles(role1)
                        await Channel.send("<@" + str(user.id) + ">! Używasz mózgu do czegoś więcej niż do wyboru runek w lidze. Gratulacje! <:Siur:717731500883181710>")
                    if file_data[id] >= 7 and file_data[id] < 15 and role2 not in user.roles:
                        await user.remove_roles(role1)
                        await user.add_roles(role2)
                        await Channel.send("<@" + str(user.id) + ">! Dzięki Tobie AlterMMO staje się innowacyjnym miejscem w internecie. <:PeepoGlad:833236606495883275>")
                    if file_data[id] >= 15 and role3 not in user.roles:
                        await user.remove_roles(role2)
                        await user.add_roles(role3)
                        await Channel.send("<@" + str(user.id) + ">! Jeteś naszym Einsteinem. Z Tobą zatkniemy flagę AlterMMO na Marsie. <:5head:882184634786521149>")

        if success:
            await Channel.send("<@" + str(author.id)+ ">, Twój pomysł został przesłany do administracji i być może zostanie zaimplementowany. Wymyśliłeś już " + str(file_data[str(author.id)]) + " pomysłów!")

    @commands.command(name="pomysl")
    @commands.cooldown(1, 60*60*23, commands.BucketType.user)
    @commands.check(is_channel)
    async def emote_command(self, ctx, idea: str):

        await ctx.message.add_reaction("ℹ️")

        def _check(r, u):
            return(
                r.emoji in VOTES.keys()
                and r.message.id == msg.id
            )
        
        color = 0xE5F507

        if len(idea) < 25:
            await ctx.send("<@" + str(ctx.author.id) + ">, za krótki opis pomysłu. Spróbuj rozpisać się bardziej, żeby każdy zrozumiał o co chodzi. <:madge:882184635474386974>")
        elif not len(idea.split()) > 1:
            await ctx.send("<@" + str(ctx.author.id) + ">, pomysł w jednym słowie? <:madge:882184635474386974>")
        else:

            embed = discord.Embed(
                title="Co myślicie o takim pomyśle?",
                description=(f"\n{idea}"),
                color=color,
                timestamp=dt.datetime.utcnow()
            )
            embed.set_thumbnail(url='https://www.altermmo.pl/wp-content/uploads/monkaHmm.png')
            embed.set_footer(text=f"Dodana przez {ctx.author.display_name}", icon_url=ctx.author.avatar_url)
            Channel = self.bot.get_channel(VoteChannelID)
            msg = await Channel.send(embed=embed)
            cache_msg = discord.utils.get(self.bot.cached_messages, id=msg.id)

            for emoji in list(VOTES.keys()):
                await msg.add_reaction(emoji)

            posReaction = 0
            negReaction = 0
            try:
                while (posReaction < votesReq and negReaction < votesReq):
                        reaction, _ = await self.bot.wait_for("reaction_add", timeout=60*60*24, check=_check)
                        posReaction = cache_msg.reactions[0].count
                        negReaction = cache_msg.reactions[1].count
                        print("Reactions: " + str(posReaction) + " " + str(negReaction))

                reactions1 = cache_msg.reactions[0]
                reactions2 = cache_msg.reactions[1]
                reacters = set()
                async for user in reactions1.users():
                    reacters.add(user)
                async for user in reactions2.users():
                    if user not in reacters:
                        reacters.add(user)
                    else:
                        print("User duplicated.")
                print(reacters)

                if posReaction >= votesReq:
                    print("Positive reactions won.")
                    await self.idea_support(ctx, reacters, ctx.author, True)
                    await msg.delete()

                    Channel = self.bot.get_channel(CommandChannelID)
                    await Channel.send("Pomysł został zwtierdzony przez społeczność, został przesłany do adminsitracji i zostanie tam przedyskutowany.\n\n" + str(idea))
                    Channel = self.bot.get_channel(LogChannelID)
                    await Channel.send("<@" + str(291836779495948288) + ">\n\n" + str(idea))
                
                else:
                    print("Negative reactions won.")
                    await self.idea_support(ctx, reacters, ctx.author, False)
                    await msg.delete()

            except asyncio.TimeoutError:
                await msg.delete()

    @emote_command.error
    async def emotecommand_cooldown(self, ctx, error):
        if isinstance(error, commands.CommandOnCooldown):
            print("Command on cooldown.")
            await ctx.send('Poczekaj na odnowienie komendy! Zostało ' + str(round(error.retry_after/60/60, 2)) + ' godzin/y <:Bedge:970576892874854400>.')
        if isinstance(error, commands.MissingRequiredArgument):
            print("Invoke error.")
            await ctx.send("<@" + str(ctx.author.id) + "> Coś źle napisałeś. Żeby zaproponować pomysł wpisz go w cudzysłowie np. *$pomysl \"Usunąć AlterMMO z internetów.\"*. <:FeelsOkayMan:794117830822854656>")


def setup(bot):
    bot.add_cog(ideas_voting(bot))