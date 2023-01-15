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
votesReq = 7

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

class emote_voting(commands.Cog, name="emote_voting"):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        print("Bot ready...")

    async def is_channel(ctx):
        return ctx.channel.id == CommandChannelID or ctx.channel.id == 1057198781206106153

    async def emote_support(self, ctx, users: set, author: discord.User, success: bool):
    
        filename="emote_authors.json"
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

            role1 = discord.utils.get(ctx.guild.roles, id=1061338700191117483) #Illustrator
            role2 = discord.utils.get(ctx.guild.roles, id=1061392998564372672) #Artysta
            role3 = discord.utils.get(ctx.guild.roles, id=1061393011155681300) #Mistrz sztuki

            for user in users:
                id = str(user.id)
                if id in file_data.keys() and user.id != 1061222744617922620:
                    if file_data[id] >= 3 and file_data[id] < 8 and role1 not in user.roles:
                        await user.add_roles(role1)
                        await Channel.send("<@" + str(user.id) + ">! Masz gust, jeśli chodzi o emotki! Dzięki temu zgarnąłeś rangę Illustratora. Gratulacje! <:Siur:717731500883181710>")
                    if file_data[id] >= 8 and file_data[id] < 20 and role2 not in user.roles:
                        await user.remove_roles(role1)
                        await user.add_roles(role2)
                        await Channel.send("<@" + str(user.id) + ">! Dzięki Tobie to miejsce staje się kolorowe. Aż za kolorowe. <:Kermitpls:790963160106008607>")
                    if file_data[id] >= 20 and role3 not in user.roles:
                        await user.remove_roles(role2)
                        await user.add_roles(role3)
                        await Channel.send("<@" + str(user.id) + ">! Może już wystarczy? Wszędzie te Twoje emotki... <:MonkaS:882181709100097587> ")

        if success:
            await Channel.send("<@" + str(author.id)+ ">, Twoja emotka została dodana do serwera. Twój wkład w rozwój emotek wyliczono na " + str(file_data[str(author.id)]) + " punktów!")

    @commands.command(name="emotka")
    @commands.cooldown(2, 60*60*23, commands.BucketType.user)
    @commands.check(is_channel)
    async def emote_command(self, ctx, emotename: str):

        await ctx.message.add_reaction("🆕")

        def _check(r, u):
            return(
                r.emoji in VOTES.keys()
                and r.message.id == msg.id
            )

        def isEnglish(s):
            try:
                s.encode(encoding='utf-8').decode('ascii')
            except UnicodeDecodeError:
                return False
            else:
                return True

        emojilist = []
        for emoji in ctx.guild.emojis:
            emojilist.append(emoji.name)
        
        color = 0x00BF28
        if len(emotename) > 15:
            await ctx.send("<@" + str(ctx.author.id) + ">, za długa nazwa emotki. Wybierz coś krótszego, np. *$emotka \"Poggers\"*. Pamiętaj również, żeby wkleić emotkę w tej samej wiadomości! <:madge:882184635474386974>")
        elif len(emotename.split()) > 1:
            await ctx.send("<@" + str(ctx.author.id) + ">, nazwa emotki powinna być jednym słowem np. *$emotka \"Poggers\"*. Pamiętaj również, żeby wkleić emotkę w tej samej wiadomości! <:madge:882184635474386974>")
        elif not isEnglish(emotename):
            await ctx.send("<@" + str(ctx.author.id) + ">, nazwa emotki prawdopodobnie zawiera dziwne znaki np. polskie litery. Spóbuj jeszcze raz. <:madge:882184635474386974>")
        elif not emotename:
            await ctx.send("<@" + str(ctx.author.id) + ">, dodaj nazwę emotki w cudzysłowie np. *$emotka \"Poggers\"*. Pamiętaj również, żeby wkleić emotkę w tej samej wiadomości! <:madge:882184635474386974>")
        elif emotename in emojilist:
            await ctx.send("<@" + str(ctx.author.id) + ">, na tym serwerze istnieje już emotka o takiej nazwie. Sprawdź czy to nie ta sama. <:madge:882184635474386974>")
        elif ctx.message.attachments:


            if ctx.message.attachments[0].content_type == "image/png" or ctx.message.attachments[0].content_type == "image/gif":

                embed = discord.Embed(
                    title="Czy chcecie dodać emotkę **" + emotename.upper() + "** do serwera?",
                    description=(f"\nPamiętacje, że emotki powinny być zgodne z zasadami Discorda. Sprawdźcie czy również taka emotka nie występuje już na serwerze."),
                    color=color,
                    timestamp=dt.datetime.utcnow()
                )
                embed.set_image(url=ctx.message.attachments[0].url)
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
                            reaction, _ = await self.bot.wait_for("reaction_add", timeout=60*60*12, check=_check)
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
                        await self.emote_support(ctx, reacters, ctx.author, True)
                        await msg.delete()
                        image = await ctx.message.attachments[0].read()
                        await ctx.message.guild.create_custom_emoji(name = emotename, image = image)
                        Channel = self.bot.get_channel(CommandChannelID)
                        await Channel.send("Emotka została dodana do serwera. Można ją wywołać wpisując \:" + str(emotename) + "\:")
                        await Channel.send(ctx.message.attachments[0].url)
                    
                    else:
                        print("Negative reactions won.")
                        await self.emote_support(ctx, reacters, ctx.author, False)
                        await msg.delete()

                except asyncio.TimeoutError:
                    await msg.delete()
            else:
                await ctx.send("<@" + str(ctx.author.id) + ">, zły format emotki. Możliwe formaty to .gif oraz .png. <:madge:882184635474386974>")
        else:
            await ctx.send("<@" + str(ctx.author.id) + ">, dodaj emotkę do wiadomości. <:madge:882184635474386974>")

    @emote_command.error
    async def emotecommand_cooldown(self, ctx, error):
        if isinstance(error, commands.CommandOnCooldown):
            print("Command on cooldown.")
            await ctx.send('Poczekaj na odnowienie komendy! Zostało ' + str(round(error.retry_after/60/60, 2)) + ' godzin/y <:Bedge:970576892874854400>.')
        if isinstance(error, commands.MissingRequiredArgument):
            print("Invoke error.")
            await ctx.send("<@" + str(ctx.author.id) + "> Coś źle napisałeś. Żeby zaproponować emotkę wklej emotkę w wiadomości oraz podaj nazwę, pod którą będzie wywoływana np. *$emotka \"Poggers\"*. Pamiętaj o cudzysłowie przy nazwie. <:FeelsOkayMan:794117830822854656>")

def setup(bot):
    bot.add_cog(emote_voting(bot))
