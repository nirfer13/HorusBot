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
        print("Bot ready...")

    @commands.Cog.listener()
    async def on_message(self, ctx):
        if ctx.channel.id == NewsChannelID and ctx.author.id != 291836779495948288 and ctx.author.id != HorusID:
            print("News detected.")

            listContent = (ctx.content).split("\n")
            Channel = self.bot.get_channel(CommandChannelID)
            oldMsg = self.bot.get_channel(NewsChannelID).history(limit=10)
            oldMsg = await oldMsg.flatten()

            contentList = []
            for msg in oldMsg[1:]:
                contentList.append(msg.content)

            try:
                title = listContent[0]
                enter1 = listContent[1]
                desc = listContent[2]
                enter2 = listContent[3]
                url = listContent[4]

                #News Verification
                if ctx.content in contentList:
                    await Channel.send("<@" + str(ctx.author.id) + ">, nie duplikuj newsów...")
                    await Channel.send(ctx.content)
                    await ctx.delete()
                elif len(title) < 10 or len(title) > 70:
                    await Channel.send("<@" + str(ctx.author.id) + ">, tytuł newsa powinien zawierać od 10 do 70 znaków. Spróbuj ponownie.")
                    await Channel.send(ctx.content)
                    await ctx.delete()                
                elif not(enter1 == "\n" or enter1 == ""):
                    print(enter1)
                    await Channel.send("<@" + str(ctx.author.id) + ">, tytuł newsa oddziel od treści pustą linią. Możesz to zrobić wciskając Shift + Enter. Przykład poniżej.")
                    await Channel.send("\n`Tytuł newsa (od 10 do 70 znaków)\n\nTreść newsa (od 150 do 700 znaków)\n\nLink do źródła (https://...)`")
                    await Channel.send(ctx.content)
                    await ctx.delete()
                elif len(desc) < 150:
                    await Channel.send("<@" + str(ctx.author.id) + ">, za krótki opis newsa. Powinien mieć co najmniej 150 znaków.")
                    await Channel.send(ctx.content)
                    await ctx.delete()
                elif len(desc) > 750:
                    await Channel.send("<@" + str(ctx.author.id) + ">, za długi opis newsa. Powinien mieć maksymalnie 750 znaków.")
                    await Channel.send(ctx.content)
                    await ctx.delete()
                elif not(enter2 == "\n" or enter1 == ""):
                    await Channel.send("<@" + str(ctx.author.id) + ">, treść newsa oddziel od linku do źródła pustą linią. Możesz to zrobić wciskając Shift + Enter. Przykład poniżej.")
                    await Channel.send("\n`Tytuł newsa (od 10 do 70 znaków)\n\nTreść newsa (od 150 do 700 znaków)\n\nLink do źródła (https://...)`")
                    await Channel.send(ctx.content)
                    await ctx.delete()
                elif "https://" not in url:
                    await Channel.send("<@" + str(ctx.author.id) + ">, w ostatniej linii podaj link do źródła newsa zaczynający się od *https://...*")
                    await Channel.send("\n`Tytuł newsa (od 10 do 70 znaków)\n\nTreść newsa (od 150 do 700 znaków)\n\nLink do źródła (https://...)`")
                    await Channel.send(ctx.content)
                    await ctx.delete()
                else:
                    await self.news_support(ctx, ctx.author)
            except:
                await Channel.send("<@" + str(ctx.author.id) + ">, upewnij się, że news ma odpowiednią strukturę. Powinno to wyglądać tak jak poniżej. Puste linie możesz dodawać wciskając SHIFT + ENTER.")
                await Channel.send("\n`Tytuł newsa (od 10 do 70 znaków)\n\nTreść newsa (od 150 do 700 znaków)\n\nLink do źródła (https://...)`")
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
                file_data[id] += 1
            else:
                file_data[id] = 1

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
                desc = "Kanał poświęcony nowinkom ze świata gier online. Każdy news jest weryfikowany przed bota, a później administrację. Jeśli będziesz dodawał poprawne newsy, to z czasem dostaniesz specjalne rangi, które zwiększą szanse na wygraną w giveawayu. **Skup się na logicznej treści i poprawnej ortografii oraz interpunkcji.** \n\nFormat newsa powinnien wyglądać następująco:\n\n`Tytuł newsa (od 10 do 70 znaków)\n\nTreść newsa (od 150 do 700 znaków)\n\nLink do źródła (https://...)`"
                #Embed create   
                emb=discord.Embed(title='Jak pisać newsy?', description=desc, color=0x34C6EB)
                emb.set_thumbnail(url="https://www.altermmo.pl/wp-content/uploads/peepoG.png")
                emb.set_footer(text='Bardzo dziękuję za wkład w AlterMMO!')
                await Channel.send(embed=emb)

def setup(bot):
    bot.add_cog(news(bot))