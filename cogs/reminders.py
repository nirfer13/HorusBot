import asyncio
import datetime as dt
import json
import random
import re
import os
import discord
from discord.ext import commands, tasks

global CommandChannelID, LogChannelID, VoteChannelID, NewsChannelID, RolesChannelID, HorusID, GuildID
CommandChannelID = 776379796367212594
#CommandChannelID = 1028340292895645696 #Debug
LogChannelID = 1057198781206106153
VoteChannelID = 1059731255786229770
#VoteChannelID = 1028340292895645696 #Debug
NewsChannelID = 687199567669624873
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
        self.task1 = None
        self.task2 = None
        self.task3 = None

    def cog_unload(self):
        for task in (self.task1, self.task2, self.task3):
            if task is not None and not task.done():
                task.cancel()

    def _loops_alive(self):
        return all(task is not None and not task.done()
                   for task in (self.task1, self.task2, self.task3))

    @commands.Cog.listener()
    async def on_ready(self):
        """Start the three weekly reminder loops - once.

        discord.py dispatches `on_ready` again on every re-IDENTIFY, and a dropped
        internet connection causes exactly that as soon as the gateway session is
        too old to RESUME. This used to create three MORE `while True` loops every
        time and overwrite self.task1/2/3, so each loop it replaced became
        unreachable and ran on forever: after three reconnects the Saturday reminder
        went out four times, and nothing short of restarting the bot could stop it.

        The guard also revives a loop that died on its own (an exception inside it
        kills only that task), which a plain "started once" flag would not.
        """
        print("Bot ready...")
        if self._loops_alive():
            return

        # The context is made by posting a message and deleting it, so it is made
        # only when a loop actually has to be started - not on every reconnect.
        channel = self.bot.get_channel(LogChannelID)

        msg = await channel.send("🔧 Przygotowuję kontekst...")
        ctx = await self.bot.get_context(msg)
        await msg.delete()

        if self.task1 is None or self.task1.done():
            self.task1 = self.bot.loop.create_task(self.vote_reminder(ctx))
        if self.task2 is None or self.task2.done():
            self.task2 = self.bot.loop.create_task(self.command_reminder(ctx))
        if self.task3 is None or self.task3.done():
            self.task3 = self.bot.loop.create_task(self.news_reminder(ctx))
        print("Reminder tasks started.")

    async def vote_reminder(self, ctx):
        print("Vote reminder task started.")
        start = (dt.datetime.utcnow() + dt.timedelta(hours=2))
        while(True):
            timestamp = (dt.datetime.utcnow() + dt.timedelta(hours=2))
            if timestamp.strftime("%a %H:%M") == "Sat 20:00" and start != "Sat":
                start = "Sat"
                Channel = self.bot.get_channel(CommandChannelID)
                desc = "Żeby zaproponować nowe emotki na serwer lub nowe utwory dla Barda należy skorzystać z komend na kanale <#" + str(CommandChannelID) + ">:\n\n- **$emotka \"nazwa emotki\" oraz należy wkleić emotkę w tej samej wiadomości** - Zaproponuj dodanie emotki na serwer Discord.\n\nWszystkie pozostałe komendy znajdziesz w przypiętej wiadomości (pinezka w prawej górnej części Discorda) na kanale <#" + str(CommandChannelID) + ">\nNa kanale <#" + str(VoteChannelID) + "> odbywają się tylko głosowania i nie można tam pisać.\nRangi, które możecie zdobyć, znajdziecie tutaj <#" + str(RolesChannelID) + ">."
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
            if timestamp.strftime("%a %H:%M") == "Thu 20:05" and start != "Thu":
                start = "Thu"
                Channel = self.bot.get_channel(NewsChannelID)
                desc = "Kanał poświęcony nowinkom ze świata gier online. Każdy news jest weryfikowany przed bota, a później administrację. Jeśli będziesz dodawał poprawne newsy, to z czasem dostaniesz specjalne rangi, które zwiększą szanse na wygraną w giveawayu. \n\nNews musi zawierać linka do źródła (https://...)`"

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