import asyncio
import datetime as dt
import json
import random
import re
import os
import discord
from discord.ext import commands, tasks



global CommandChannelID, LogChannelID, VoteChannelID, RolesChannelID,HorusID, GuildID
CommandChannelID = 776379796367212594
#CommandChannelID = 1028340292895645696 #Debug
LogChannelID = 1057198781206106153
VoteChannelID = 1059731255786229770
#VoteChannelID = 1028340292895645696 #Debug
RolesChannelID = 688296443156365354
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

class reminders(commands.Cog, name="reminders"):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        print("Bot ready...")

        #function to get context
        channel = self.bot.get_channel(LogChannelID)
        msg = await channel.fetch_message(1057204065706188820)
        ctx = await self.bot.get_context(msg)

        self.task1 = self.bot.loop.create_task(self.vote_reminder(ctx))
        self.task2 = self.bot.loop.create_task(self.command_reminder(ctx))
        self.task3 = self.bot.loop.create_task(self.news_reminder(ctx))
        print("Reminder tasks started.")     

    async def vote_reminder(self, ctx):
        print("Vote reminder task started.")
        start = (dt.datetime.utcnow() + dt.timedelta(hours=2))
        while(True):
            timestamp = (dt.datetime.utcnow() + dt.timedelta(hours=2))
            if timestamp.strftime("%a %H:%M") == "Sat 20:00" and start != "Sat":
                start = "Sat"
                Channel = self.bot.get_channel(696932659833733131)
                desc = "Żeby zaproponować nowe emotki na serwer lub nowe utwory dla Barda należy skorzystać z komend na kanale <#" + str(CommandChannelID) + ">:\n\n- **$emotka \"nazwa emotki\" oraz należy wkleić emotkę w tej samej wiadomości** - Zaproponuj dodanie emotki na serwer Discord,\n- **$fantasy \"tytuł utworu z YouTube\"** - Zaproponuj dodanie utworu do playlisty fantasy,\n- **$party \"tytuł utworu z YouTube\"** - Zaproponuj dodanie utworu do playlisty imprezowej.\n\nWszystkie pozostałe komendy znajdziesz w przypiętej wiadomości (pinezka w prawej górnej części Discorda) na kanale <#" + str(CommandChannelID) + ">\nNa kanale <#" + str(VoteChannelID) + "> odbywają się tylko głosowania i nie można tam pisać.\nRangi, które możecie zdobyć, znajdziecie tutaj <#" + str(RolesChannelID) + ">."
                #Embed create   
                emb=discord.Embed(title='Jak stworzyć pomysł?', description=desc, color=0x34C6EB)
                emb.set_thumbnail(url="https://www.altermmo.pl/wp-content/uploads/monkaHmm.png")
                emb.set_footer(text='Twórzcie pomysły, żeby zdobywać rangi, rozwijać Discorda i zwiększyć szanse w giveawayu!')
                await Channel.send(embed=emb)
                
            elif timestamp.strftime("%a") != "Sat":
                start = (dt.datetime.utcnow() + dt.timedelta(hours=2))

            await asyncio.sleep(60)

    async def command_reminder(self, ctx):
        print("Command reminder task started.")
        start = (dt.datetime.utcnow() + dt.timedelta(hours=2))
        while(True):
            timestamp = (dt.datetime.utcnow() + dt.timedelta(hours=2))
            if timestamp.strftime("%a %H:%M") == "Tue 20:00" and start != "Tue":
                start = "Tue"
                Channel = self.bot.get_channel(CommandChannelID)
                desc = "Wszystkie dostępne komendy znajdziesz w przypiętej wiadomości na tym kanale (pinezka w prawym górnym rogu Discorda)."
                #Embed create   
                emb=discord.Embed(title='Jakie komendy są dostępne?', description=desc, color=0x34C6EB)
                emb.set_thumbnail(url="https://www.altermmo.pl/wp-content/uploads/monkaHmm.png")
                emb.set_footer(text='Miłej zabawy z botami!')
                await Channel.send(embed=emb)
                
            elif timestamp.strftime("%a") != "Tue":
                start = (dt.datetime.utcnow() + dt.timedelta(hours=2))

            await asyncio.sleep(60)

    async def news_reminder(self, ctx):
        print("News reminder task started.")
        start = (dt.datetime.utcnow() + dt.timedelta(hours=2))
        while(True):
            timestamp = (dt.datetime.utcnow() + dt.timedelta(hours=2))
            if timestamp.strftime("%a %H:%M") == "Thu 20:00" and start != "Thu":
                start = "Thu"
                Channel = self.bot.get_channel(NewsChannelID)
                desc = "Kanał poświęcony nowinkom ze świata gier online. Każdy news jest weryfikowany przed bota, a później administrację. Jeśli będziesz dodawał poprawne newsy, to z czasem dostaniesz specjalne rangi, które zwiększą szanse na wygraną w giveawayu. \n\nFormat newsa powinnien wyglądać następująco:\n\n`Tytuł newsa (od 10 do 70 znaków)\n\nTreść newsa (od 150 do 700 znaków)\n\nLink do źródła (https://...)`"
                await Channel.send("")
                #Embed create   
                emb=discord.Embed(title='Jak pisać newsy?', description=desc, color=0x34C6EB)
                emb.set_thumbnail(url="https://www.altermmo.pl/wp-content/uploads/peepoG.png")
                emb.set_footer(text='Bardzo dziękuję za wkład w AlterMMO!')
                await Channel.send(embed=emb)
                
            elif timestamp.strftime("%a") != "Thu":
                start = (dt.datetime.utcnow() + dt.timedelta(hours=2))

            await asyncio.sleep(60)


async def setup(bot):
    await bot.add_cog(reminders(bot))