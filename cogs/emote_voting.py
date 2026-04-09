import asyncio
import datetime as dt
import json
import random
import re
import os
import discord
from discord.ext import commands, tasks

from typing import Union
from pathlib import Path
from typing import Optional

global COMMAND_CHANNEL_ID,  LogChannelID, VOTE_CHANNEL_ID, HorusID, GuildID, SpamerID, VOTES_REQ
#VoteChannelID = 1028340292895645696 #Debug
VOTE_CHANNEL_ID = 1059731255786229770
COMMAND_CHANNEL_ID = 776379796367212594
LogChannelID = 1057198781206106153
HorusID = 1004008220437778523
GuildID = 686137998177206281
SpamerID = 1061338676338118707
VOTES_REQ = 6
VOTE_DURATION = 60 * 60 * 24  # 24h

DATA_FILE = Path("emote_votes.json")

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
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.vote_tasks: dict[int, asyncio.Task] = {}
        self.votes: dict[str, dict] = {}
        self._ready_once = False
        self._load_votes()

    def _load_votes(self):
        if DATA_FILE.exists():
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                self.votes = json.load(f)
        else:
            self.votes = {}

    def _save_votes(self):
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(self.votes, f, ensure_ascii=False, indent=2)

    def _vote_key(self, message_id: int) -> str:
        return str(message_id)

    async def cog_load(self):
        # In newer discord.py versions this is a good place for async init,
        # but channels/guilds may not be ready yet, so bootstrap is done in on_ready
        pass


    @commands.Cog.listener()
    async def on_ready(self):
        print("Emote voting module ready...")
        if self._ready_once:
            return
        self._ready_once = True

        channel = self.bot.get_channel(VOTE_CHANNEL_ID)
        if channel is None:
            channel = await self.bot.fetch_channel(VOTE_CHANNEL_ID)

        # 1) Load all messages except the very first one in the channel
        messages = [m async for m in channel.history(limit=None, oldest_first=True)]
        if messages:
            messages = messages[1:]  # skip first channel message

        # 2) Restore active votes from storage or history
        known_ids = set(self.votes.keys())

        for msg in messages:
            # Skip messages without embeds (not vote messages)
            if not msg.embeds:
                continue

            key = self._vote_key(msg.id)
            if key not in known_ids:
                # Optionally detect votes based on embed structure/title
                continue

            vote = self.votes[key]
            if vote["status"] != "active":
                continue

            # Restart watcher if it's not running
            if msg.id not in self.vote_tasks or self.vote_tasks[msg.id].done():
                self.vote_tasks[msg.id] = self.bot.loop.create_task(
                    self.watch_vote(msg.id)
                )

    async def is_channel(ctx):
        return ctx.channel.id == COMMAND_CHANNEL_ID  or ctx.channel.id == 1057198781206106153

    async def count_votes(self, message: discord.Message) -> tuple[int, int, set[int]]:
        yes_count = 0
        no_count = 0
        voters = set()

        # Find reactions by emoji, not by index
        yes_reaction = discord.utils.get(message.reactions, emoji="✅")
        no_reaction = discord.utils.get(message.reactions, emoji="❌")

        if yes_reaction is not None:
            async for user in yes_reaction.users():
                if user.bot:
                    continue
                voters.add(user.id)
            yes_count = len(voters)

        if no_reaction is not None:
            no_voters = set()
            async for user in no_reaction.users():
                if user.bot:
                    continue
                no_voters.add(user.id)

            # Count negative votes separately
            no_count = len(no_voters)
            voters |= no_voters

        return yes_count, no_count, voters
    
    async def finalize_vote(self, vote_message_id: int):
        key = self._vote_key(vote_message_id)
        vote = self.votes.get(key)
        if not vote or vote["status"] != "active":
            return

        channel = self.bot.get_channel(vote["channel_id"])
        if channel is None:
            channel = await self.bot.fetch_channel(vote["channel_id"])

        try:
            message = await channel.fetch_message(vote_message_id)
        except discord.NotFound:
            vote["status"] = "deleted"
            self._save_votes()
            return

        yes_count, no_count, voter_ids = await self.count_votes(message)

        guild = self.bot.get_guild(vote["guild_id"])
        if guild is None:
            guild = await self.bot.fetch_guild(vote["guild_id"])

        author = guild.get_member(vote["author_id"])
        if author is None:
            author = await guild.fetch_member(vote["author_id"])

        command_channel = self.bot.get_channel(COMMAND_CHANNEL_ID)
        if command_channel is None:
            command_channel = await self.bot.fetch_channel(COMMAND_CHANNEL_ID)

        accepted = yes_count >= VOTES_REQ and yes_count > no_count

        try:
            if accepted:
                image_path = vote["image_path"]
                with open(image_path, "rb") as f:
                    image_bytes = f.read()

                await self.emote_support(
                    guild=guild,
                    voter_ids=voter_ids,
                    author=author,
                    accepted=True
                )

                emoji_result = await guild.create_custom_emoji(
                    name=vote["emoji_name"],
                    image=image_bytes,
                    reason=f"Approved emoji from {author} ({author.id})"
                )

                emoji = discord.utils.get(guild.emojis, name=vote["emoji_name"])
                await command_channel.send(
                    f"Emotka została dodana - Można jej użyć przez wpisanie `:{vote['emoji_name']}:`"
                )
                await command_channel.send(emoji)
            else:
                await command_channel.send(
                    f"Emotka nie została zaakceptowana - `{vote['emoji_name']}`"
                )
                await self.emote_support(
                    guild=guild,
                    voter_ids=voter_ids,
                    author=author,
                    accepted=False
                )

        finally:
            vote["status"] = "finished"
            self._save_votes()

            try:
                await message.delete()
            except discord.HTTPException:
                pass

    async def watch_vote(self, vote_message_id: int):
        key = self._vote_key(vote_message_id)
        vote = self.votes.get(key)
        if not vote or vote["status"] != "active":
            return

        created_ts = vote["created_ts"]
        now = discord.utils.utcnow().timestamp()
        
        remaining = max(0, created_ts + VOTE_DURATION - now)
        print("Vote %s, remaining %s", str(vote_message_id), str(remaining))

        # Wait until vote expiration
        await asyncio.sleep(remaining)
        await self.finalize_vote(vote_message_id)

    # async def emote_support(self, ctx, users: set, author: discord.User, success: bool):
    
    #     filename="emote_authors.json"
    #     Channel = self.bot.get_channel(COMMAND_CHANNEL_ID )

    #     with open(filename,'r+') as file:
    #         # First we load existing data into a dict.
    #         file_data = json.load(file)
           
    #         for user in users:
    #             id = str(user.id)
    #             if id != str(author.id):
    #                 if id in file_data.keys():
    #                     file_data[id] += 0.15
    #                 else:
    #                     file_data[id] = 0.15

    #         if success:
    #             id = str(author.id)
    #             if id in file_data.keys():
    #                 file_data[id] += 1
    #             else:
    #                 file_data[id] = 1

    #         json_object = json.dumps(file_data, indent=4)
    #         # Sets file's current position at offset.
    #         file.seek(0)
    #         file.truncate(0) # need '0' when using r+
    #         file.write(json_object)

    #         role1 = discord.utils.get(ctx.guild.roles, id=1061338700191117483) #Illustrator
    #         role2 = discord.utils.get(ctx.guild.roles, id=1061392998564372672) #Artysta
    #         role3 = discord.utils.get(ctx.guild.roles, id=1061393011155681300) #Mistrz sztuki

    #         for user in users:
    #             id = str(user.id)
    #             if id in file_data.keys() and user.id != 1061222744617922620:
    #                 if file_data[id] >= 3 and file_data[id] < 8 and role1 not in user.roles:
    #                     await user.add_roles(role1)
    #                     await Channel.send("<@" + str(user.id) + ">! Masz gust, jeśli chodzi o emotki! Dzięki temu zgarnąłeś rangę Illustratora. Gratulacje! <:Siur:717731500883181710>")
    #                 if file_data[id] >= 8 and file_data[id] < 20 and role2 not in user.roles:
    #                     await user.remove_roles(role1)
    #                     await user.add_roles(role2)
    #                     await Channel.send("<@" + str(user.id) + ">! Dzięki Tobie to miejsce staje się kolorowe. Aż za kolorowe. <:Kermitpls:790963160106008607>")
    #                 if file_data[id] >= 20 and role3 not in user.roles:
    #                     await user.remove_roles(role2)
    #                     await user.add_roles(role3)
    #                     await Channel.send("<@" + str(user.id) + ">! Może już wystarczy? Wszędzie te Twoje emotki... <:MonkaS:882181709100097587> ")

    #     if success:
    #         await Channel.send("<@" + str(author.id)+ ">, Twoja emotka została dodana do serwera. Twój wkład w rozwój emotek wyliczono na " + str(file_data[str(author.id)]) + " punktów!")

    async def emote_support(
        self,
        guild: discord.Guild,
        voter_ids: set[int],
        author: Union[discord.Member, discord.User],
        accepted: bool
        ):
        filename = "emote_authors.json"

        channel = self.bot.get_channel(COMMAND_CHANNEL_ID)
        if channel is None:
            channel = await self.bot.fetch_channel(COMMAND_CHANNEL_ID)

        # Load existing score data safely
        if os.path.exists(filename):
            with open(filename, "r", encoding="utf-8") as file:
                try:
                    file_data = json.load(file)
                except json.JSONDecodeError:
                    file_data = {}
        else:
            file_data = {}

        # Add points for voters except the author
        for user_id in voter_ids:
            if user_id != author.id:
                key = str(user_id)
                if key in file_data:
                    file_data[key] += 0.15
                else:
                    file_data[key] = 0.15

        # Add bonus point for the emoji author if vote succeeded
        if accepted:
            author_key = str(author.id)
            if author_key in file_data:
                file_data[author_key] += 1
            else:
                file_data[author_key] = 1

        # Save updated score data
        with open(filename, "w", encoding="utf-8") as file:
            json.dump(file_data, file, indent=4, ensure_ascii=False)

        role1 = guild.get_role(1061338700191117483)  # Illustrator
        role2 = guild.get_role(1061392998564372672)  # Artysta
        role3 = guild.get_role(1061393011155681300)  # Mistrz sztuki

        # Update roles for all voters
        for user_id in voter_ids:
            if user_id == 1061222744617922620:
                continue

            key = str(user_id)
            if key not in file_data:
                continue

            member = guild.get_member(user_id)
            if member is None:
                try:
                    member = await guild.fetch_member(user_id)
                except discord.NotFound:
                    continue
                except discord.HTTPException:
                    continue

            points = file_data[key]

            if points >= 3 and points < 8:
                if role1 and role1 not in member.roles:
                    await member.add_roles(role1)
                    await channel.send(
                        f"<@{member.id}>! Masz gust, jeśli chodzi o emotki! "
                        f"Dzięki temu zgarnąłeś rangę Illustratora. Gratulacje! "
                        f"<:Siur:717731500883181710>"
                    )

            elif points >= 8 and points < 20:
                if role2 and role2 not in member.roles:
                    if role1 and role1 in member.roles:
                        await member.remove_roles(role1)
                    await member.add_roles(role2)
                    await channel.send(
                        f"<@{member.id}>! Dzięki Tobie to miejsce staje się kolorowe. "
                        f"Aż za kolorowe. <:Kermitpls:790963160106008607>"
                    )

            elif points >= 20:
                if role3 and role3 not in member.roles:
                    if role2 and role2 in member.roles:
                        await member.remove_roles(role2)
                    await member.add_roles(role3)
                    await channel.send(
                        f"<@{member.id}>! Może już wystarczy? "
                        f"Wszędzie te Twoje emotki... <:MonkaS:882181709100097587>"
                    )

        if accepted:
            await channel.send(
                f"<@{author.id}>, Twoja emotka została dodana do serwera. "
                f"Twój wkład w rozwój emotek wyliczono na "
                f"{file_data[str(author.id)]} punktów!"
            )

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.channel_id != VOTE_CHANNEL_ID:
            return

        key = self._vote_key(payload.message_id)
        vote = self.votes.get(key)
        if not vote or vote["status"] != "active":
            return

        # Optional: early threshold check could be done here

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        if payload.channel_id != VOTE_CHANNEL_ID:
            return

        key = self._vote_key(payload.message_id)
        vote = self.votes.get(key)
        if not vote or vote["status"] != "active":
            return

    @commands.command(name="emotka")
    @commands.cooldown(2, 60 * 60 * 23, commands.BucketType.user)
    async def emote_command(self, ctx: commands.Context, emotename: str):
        if not ctx.message.attachments:
            await ctx.send(f"<@{ctx.author.id}>, załącz zdjęcie emotki w formacie .png lub .gif.")
            return

        attachment = ctx.message.attachments[0]
        if attachment.content_type not in ("image/png", "image/gif"):
            await ctx.send(
                f"<@{ctx.author.id}>, niedozwolony format. Dozwolne są .png oraz .gif."
            )
            return

        # Name validation
        if len(emotename) > 15:
            await ctx.send(f"<@{ctx.author.id}>, nazwa za długa.")
            return

        if len(emotename.split()) > 1:
            await ctx.send(f"<@{ctx.author.id}>, nazwa musi być jednym wyrazem.")
            return

        if not emotename.isascii():
            await ctx.send(f"<@{ctx.author.id}>, nazwa zawiera niedozwolone znaki.")
            return

        if emotename in [e.name for e in ctx.guild.emojis]:
            await ctx.send(f"<@{ctx.author.id}>, emotka już istnieje.")
            return

        await ctx.message.add_reaction("🆕")

        image_bytes = await attachment.read()
        os.makedirs("emote_uploads", exist_ok=True)
        image_path = f"emote_uploads/{ctx.message.id}_{emotename}"
        if attachment.content_type == "image/png":
            image_path += ".png"
        else:
            image_path += ".gif"

        with open(image_path, "wb") as f:
            f.write(image_bytes)

        embed = discord.Embed(
            title=f"Dodać emotkę **{emotename.upper()}**?",
            description="Macie 24h na głosowanie!",
            colour=0x00BF28,
            timestamp=discord.utils.utcnow()
        )
        embed.set_image(url=attachment.url)
        embed.set_footer(
            text=f"Zaproponowana przez {ctx.author.display_name}",
            icon_url=ctx.author.display_avatar.url
        )

        vote_channel = self.bot.get_channel(VOTE_CHANNEL_ID)
        if vote_channel is None:
            vote_channel = await self.bot.fetch_channel(VOTE_CHANNEL_ID)

        vote_message = await vote_channel.send(embed=embed)

        for emoji in VOTES.keys():
            await vote_message.add_reaction(emoji)

        # Store vote metadata persistently
        self.votes[self._vote_key(vote_message.id)] = {
            "vote_message_id": vote_message.id,
            "guild_id": ctx.guild.id,
            "channel_id": vote_channel.id,
            "author_id": ctx.author.id,
            "emoji_name": emotename,
            "image_path": image_path,
            "source_message_id": ctx.message.id,
            "created_ts": discord.utils.utcnow().timestamp(),
            "status": "active",
        }
        self._save_votes()

        # Start async watcher
        self.vote_tasks[vote_message.id] = self.bot.loop.create_task(
            self.watch_vote(vote_message.id)
        )




    # @commands.command(name="emotka")
    # @commands.cooldown(2, 60*60*23, commands.BucketType.user)
    # @commands.check(is_channel)
    # async def emote_command(self, ctx, emotename: str):

    #     await ctx.message.add_reaction("🆕")

    #     def _check(r, u):
    #         return(
    #             r.emoji in VOTES.keys()
    #             and r.message.id == msg.id
    #         )

    #     def isEnglish(s):
    #         try:
    #             s.encode(encoding='utf-8').decode('ascii')
    #         except UnicodeDecodeError:
    #             return False
    #         else:
    #             return True

    #     emojilist = []
    #     for emoji in ctx.guild.emojis:
    #         emojilist.append(emoji.name)
        
    #     color = 0x00BF28
    #     if len(emotename) > 15:
    #         await ctx.send("<@" + str(ctx.author.id) + ">, za długa nazwa emotki. Wybierz coś krótszego, np. *$emotka \"Poggers\"*. Pamiętaj również, żeby wkleić emotkę w tej samej wiadomości! <:madge:882184635474386974>")
    #     elif len(emotename.split()) > 1:
    #         await ctx.send("<@" + str(ctx.author.id) + ">, nazwa emotki powinna być jednym słowem np. *$emotka \"Poggers\"*. Pamiętaj również, żeby wkleić emotkę w tej samej wiadomości! <:madge:882184635474386974>")
    #     elif not isEnglish(emotename):
    #         await ctx.send("<@" + str(ctx.author.id) + ">, nazwa emotki prawdopodobnie zawiera dziwne znaki np. polskie litery. Spóbuj jeszcze raz. <:madge:882184635474386974>")
    #     elif not emotename:
    #         await ctx.send("<@" + str(ctx.author.id) + ">, dodaj nazwę emotki w cudzysłowie np. *$emotka \"Poggers\"*. Pamiętaj również, żeby wkleić emotkę w tej samej wiadomości! <:madge:882184635474386974>")
    #     elif emotename in emojilist:
    #         await ctx.send("<@" + str(ctx.author.id) + ">, na tym serwerze istnieje już emotka o takiej nazwie. Sprawdź czy to nie ta sama. <:madge:882184635474386974>")
    #     elif ctx.message.attachments:


    #         if ctx.message.attachments[0].content_type == "image/png" or ctx.message.attachments[0].content_type == "image/gif":

    #             embed = discord.Embed(
    #                 title="Czy chcecie dodać emotkę **" + emotename.upper() + "** do serwera?",
    #                 description=(f"\nPamiętacje, że emotki powinny być zgodne z zasadami Discorda. Sprawdźcie czy również taka emotka nie występuje już na serwerze."),
    #                 color=color,
    #                 timestamp=dt.datetime.utcnow()
    #             )
    #             embed.set_image(url=ctx.message.attachments[0].url)
    #             embed.set_footer(
    #                 text=f"Dodana przez {ctx.author.display_name}",
    #                 icon_url=ctx.author.display_avatar.url
    #             )
    #             Channel = self.bot.get_channel(VOTE_CHANNEL_ID)
    #             msg = await Channel.send(embed=embed)
    #             cache_msg = discord.utils.get(self.bot.cached_messages, id=msg.id)

    #             for emoji in list(VOTES.keys()):
    #                 await msg.add_reaction(emoji)

    #             posReaction = 0
    #             negReaction = 0
    #             try:
    #                 while (posReaction < VOTES_REQ and negReaction < VOTES_REQ):
    #                         reaction, _ = await self.bot.wait_for("reaction_add", timeout=60*60*12, check=_check)
    #                         posReaction = cache_msg.reactions[0].count
    #                         negReaction = cache_msg.reactions[1].count
    #                         print("Reactions: " + str(posReaction) + " " + str(negReaction))

    #                 reactions1 = cache_msg.reactions[0]
    #                 reactions2 = cache_msg.reactions[1]
    #                 reacters = set()
    #                 async for user in reactions1.users():
    #                     reacters.add(user)
    #                 async for user in reactions2.users():
    #                     if user not in reacters:
    #                         reacters.add(user)
    #                     else:
    #                         print("User duplicated.")
    #                 print(reacters)

    #                 if posReaction >= VOTES_REQ:
    #                     print("Positive reactions won.")
    #                     await self.emote_support(ctx, reacters, ctx.author, True)
    #                     await msg.delete()
    #                     image = await ctx.message.attachments[0].read()
    #                     await ctx.message.guild.create_custom_emoji(name = emotename, image = image)
    #                     Channel = self.bot.get_channel(COMMAND_CHANNEL_ID )
    #                     await Channel.send("Emotka została dodana do serwera. Można ją wywołać wpisując \:" + str(emotename) + "\:")
    #                     await Channel.send(ctx.message.attachments[0].url)
                    
    #                 else:
    #                     print("Negative reactions won.")
    #                     await self.emote_support(ctx, reacters, ctx.author, False)
    #                     await msg.delete()

    #             except asyncio.TimeoutError:
    #                 await msg.delete()
    #         else:
    #             await ctx.send("<@" + str(ctx.author.id) + ">, zły format emotki. Możliwe formaty to .gif oraz .png. <:madge:882184635474386974>")
    #     else:
    #         await ctx.send("<@" + str(ctx.author.id) + ">, dodaj emotkę do wiadomości. <:madge:882184635474386974>")

    # @emote_command.error
    # async def emotecommand_cooldown(self, ctx, error):
    #     if isinstance(error, commands.CommandOnCooldown):
    #         print("Command on cooldown.")
    #         await ctx.send('Poczekaj na odnowienie komendy! Zostało ' + str(round(error.retry_after/60/60, 2)) + ' godzin/y <:Bedge:970576892874854400>.')
    #     if isinstance(error, commands.MissingRequiredArgument):
    #         print("Invoke error.")
    #         await ctx.send("<@" + str(ctx.author.id) + "> Coś źle napisałeś. Żeby zaproponować emotkę wklej emotkę w wiadomości oraz podaj nazwę, pod którą będzie wywoływana np. *$emotka \"Poggers\"*. Pamiętaj o cudzysłowie przy nazwie. <:FeelsOkayMan:794117830822854656>")

async def setup(bot):
    await bot.add_cog(emote_voting(bot))
