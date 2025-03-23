import asyncio
import datetime as dt
import json
import random
import re
import os
import discord
from discord.ext import commands, tasks

global NewsChannelID, LogChannelID, CommandChannelID, HorusID, GuildID
NewsChannelID = 687199567669624873
LogChannelID = 1057198781206106153
CommandChannelID = 776379796367212594
#CommandChannelID = 1028340292895645696 #Debug
HorusID = 1061222744617922620
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




class news(commands.Cog, name="news"):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        print("News module ready...")
        self.seen_links = []
        self.current_link = None

        Channel = self.bot.get_channel(CommandChannelID)
        oldMsg = self.bot.get_channel(NewsChannelID).history(limit=10)
        oldMsg = await oldMsg.flatten()

        for msg in oldMsg[1:]:
            result = self.contains_link(msg.content)
            self.seen_links.append(self.current_link)

    def contains_link(self, text):
        # Wyrażenie regularne do wykrywania linków
        url_pattern = re.compile(r'https?://\S+|www\.\S+')
        match = url_pattern.search(text)

        if match:
            self.current_link = match.group().rstrip('\/')
            return True
        return False

    def duplicate_link(self):
        if self.current_link not in self.seen_links:
            self.seen_links.append(self.current_link)
            return True
        return False

    @commands.Cog.listener()
    async def on_message(self, ctx):
        if ctx.channel.id == NewsChannelID and ctx.author.id != 2918367794959482880 and ctx.author.id != HorusID:
            print("News detected.")
    
            try:
                Channel = self.bot.get_channel(CommandChannelID)

                #News Verification
                if not self.contains_link(ctx.content):
                    await Channel.send("<@" + str(ctx.author.id) + ">, upewnij się, że załączyłeś linka w pełnej formie.")
                    await Channel.send(ctx.content)
                    await ctx.delete()
                elif not self.duplicate_link():
                    await Channel.send("<@" + str(ctx.author.id) + ">, nie duplikuj linków.")
                    await Channel.send(ctx.content)
                    await ctx.delete()

                else:
                    await self.news_support(ctx, ctx.author)
            except:
                await Channel.send("<@" + str(ctx.author.id) + ">, upewnij się, że załączyłeś linka w pełnej formie.")
                await Channel.send(ctx.content)
                await ctx.delete()

    async def news_support(self, ctx, author: discord.User):
    
        filename="news_authors.json"
        Channel = self.bot.get_channel(CommandChannelID)

        with open(filename,'r+') as file:
            # First we load existing data into a dict.
            file_data = json.load(file)

            id = str(author.id)
            if id in file_data.keys():
                file_data[id] += 0.25
            else:
                file_data[id] = 0.25

            json_object = json.dumps(file_data, indent=4)
            # Sets file's current position at offset.
            file.seek(0)
            file.truncate(0) # need '0' when using r+
            file.write(json_object)

            role1 = discord.utils.get(ctx.guild.roles, id=1061601737250709544) #Bazgroła
            role2 = discord.utils.get(ctx.guild.roles, id=1061601744754327672) #Pismak
            role3 = discord.utils.get(ctx.guild.roles, id=1061601741470175302) #Dziennikarz

            await Channel.send("<@" + str(ctx.author.id) + ">, brawo! Udało Ci się poprawnie dodać newsa! <a:PepoG:936907752155021342> Masz już " + str(file_data[id]) + " poprawnych newsów na koncie.")

            id = str(author.id)
            if id in file_data.keys() and author.id != 1061222744617922620:
                if file_data[id] >= 8 and file_data[id] < 20 and role1 not in author.roles:
                    await author.add_roles(role1)
                    await Channel.send("<@" + str(author.id) + ">! Świetnie Ci idzie z śledzeniem rynku gier online. Dziękujemy za bieżące informacje! <:Siur:717731500883181710>")
                if file_data[id] >= 20 and file_data[id] < 50 and role2 not in author.roles:
                    await author.remove_roles(role1)
                    await author.add_roles(role2)
                    await Channel.send("<@" + str(author.id) + ">! Dzięki Tobie każdy wie o najmniejszej nowości w świecie gier online. Dziękujemy! <:peepoBlush:984769061340737586>")
                if file_data[id] >= 50 and role3 not in author.roles:
                    await author.remove_roles(role2)
                    await author.add_roles(role3)
                    await Channel.send("<@" + str(author.id) + ">! Chyba aspirujesz na redaktora AlterMMO, co? <:Siur:717731500883181710>")

    @commands.command(name="newsformat")
    @commands.has_permissions(administrator=True)
    async def news_format(self, ctx):
                await ctx.message.delete()
                Channel = self.bot.get_channel(NewsChannelID)
                desc = "Kanał poświęcony nowinkom ze świata gier online. Każdy news jest weryfikowany przed bota, a później administrację. Jeśli będziesz dodawał poprawne newsy, to z czasem dostaniesz specjalne rangi, które zwiększą szanse na wygraną w giveawayu. **Skup się na logicznej treści i poprawnej ortografii oraz interpunkcji.** \n\nNews powinien zawierać link do źródła (https://...)`"
                #Embed create   
                emb=discord.Embed(title='Jak pisać newsy?', description=desc, color=0x34C6EB)
                emb.set_thumbnail(url="https://www.altermmo.pl/wp-content/uploads/peepoG.png")
                emb.set_footer(text='Bardzo dziękuję za wkład w AlterMMO!')
                await Channel.send(embed=emb)

def setup(bot):
    bot.add_cog(news(bot))