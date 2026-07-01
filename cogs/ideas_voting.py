import asyncio
import datetime as dt
import json
import os
import discord
from discord.ext import commands
from typing import Union
from pathlib import Path

# Kanały / ustawienia
VOTE_CHANNEL_ID = 1059731255786229770
COMMAND_CHANNEL_ID = 776379796367212594
LOG_CHANNEL_ID = 1057198781206106153
HORUS_ID = 1004008220437778523
GUILD_ID = 686137998177206281
SPAMER_ID = 1061338676338118707

VOTES_REQ = 8
VOTE_DURATION = 60 * 60 * 24  # 24h

DATA_FILE = Path("idea_votes.json")
AUTHORS_FILE = Path("idea_authors.json")

VOTES = {
    "✅": 0,
    "❌": 1,
}


class ideas_voting(commands.Cog, name="ideas_voting"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.vote_tasks: dict[int, asyncio.Task] = {}
        self.votes: dict[str, dict] = {}
        self._ready_once = False
        self._load_votes()

    def _load_votes(self):
        if DATA_FILE.exists():
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                try:
                    self.votes = json.load(f)
                except json.JSONDecodeError:
                    self.votes = {}
        else:
            self.votes = {}

    def _save_votes(self):
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(self.votes, f, ensure_ascii=False, indent=2)

    def _vote_key(self, message_id: int) -> str:
        return str(message_id)

    @commands.Cog.listener()
    async def on_ready(self):
        print("Ideas voting module ready...")
        if self._ready_once:
            return
        self._ready_once = True

        channel = self.bot.get_channel(VOTE_CHANNEL_ID)
        if channel is None:
            channel = await self.bot.fetch_channel(VOTE_CHANNEL_ID)

        # Przywróć tylko głosowania zapisane jako aktywne.
        for key, vote in list(self.votes.items()):
            if vote.get("status") != "active":
                continue

            vote_message_id = int(key)
            if vote_message_id not in self.vote_tasks or self.vote_tasks[vote_message_id].done():
                self.vote_tasks[vote_message_id] = self.bot.loop.create_task(
                    self.watch_vote(vote_message_id)
                )

    async def cog_load(self):
        # Dla discord.py 2.x. W 1.7.3 ta metoda po prostu nie jest potrzebna.
        pass

    async def is_channel(ctx):
        return ctx.channel.id == COMMAND_CHANNEL_ID or ctx.channel.id == LOG_CHANNEL_ID

    async def count_votes(self, message: discord.Message) -> tuple[int, int, set[int]]:
        yes_count = 0
        no_count = 0
        voters = set()

        yes_reaction = discord.utils.get(message.reactions, emoji="✅")
        no_reaction = discord.utils.get(message.reactions, emoji="❌")

        if yes_reaction is not None:
            yes_voters = set()
            async for user in yes_reaction.users():
                if user.bot:
                    continue
                yes_voters.add(user.id)
            yes_count = len(yes_voters)
            voters |= yes_voters

        if no_reaction is not None:
            no_voters = set()
            async for user in no_reaction.users():
                if user.bot:
                    continue
                no_voters.add(user.id)
            no_count = len(no_voters)
            voters |= no_voters

        return yes_count, no_count, voters

    async def idea_support(
        self,
        guild: discord.Guild,
        voter_ids: set[int],
        author: Union[discord.Member, discord.User],
        accepted: bool,
    ):
        channel = self.bot.get_channel(COMMAND_CHANNEL_ID)
        if channel is None:
            channel = await self.bot.fetch_channel(COMMAND_CHANNEL_ID)

        if AUTHORS_FILE.exists():
            with open(AUTHORS_FILE, "r", encoding="utf-8") as file:
                try:
                    file_data = json.load(file)
                except json.JSONDecodeError:
                    file_data = {}
        else:
            file_data = {}

        # Punkty za głosowanie, bez autora pomysłu.
        for user_id in voter_ids:
            if user_id == author.id:
                continue
            key = str(user_id)
            file_data[key] = file_data.get(key, 0) + 0.15

        # Punkt dla autora tylko jeśli pomysł przeszedł.
        if accepted:
            author_key = str(author.id)
            file_data[author_key] = file_data.get(author_key, 0) + 1

        with open(AUTHORS_FILE, "w", encoding="utf-8") as file:
            json.dump(file_data, file, indent=4, ensure_ascii=False)

        role1 = guild.get_role(1062660624066297926)  # Bystrzak
        role2 = guild.get_role(1062660719528656916)  # Intelektualista
        role3 = guild.get_role(1062660709042892802)  # Geniusz

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

            if 3 <= points < 7:
                if role1 and role1 not in member.roles:
                    await member.add_roles(role1)
                    await channel.send(
                        f"<@{member.id}>! Używasz mózgu do czegoś więcej niż do wyboru runek w lidze. "
                        f"Gratulacje! <:Siur:717731500883181710>"
                    )

            elif 7 <= points < 15:
                if role2 and role2 not in member.roles:
                    if role1 and role1 in member.roles:
                        await member.remove_roles(role1)
                    await member.add_roles(role2)
                    await channel.send(
                        f"<@{member.id}>! Dzięki Tobie AlterMMO staje się innowacyjnym miejscem w internecie. "
                        f"<:PeepoGlad:833236606495883275>"
                    )

            elif points >= 15:
                if role3 and role3 not in member.roles:
                    if role2 and role2 in member.roles:
                        await member.remove_roles(role2)
                    if role1 and role1 in member.roles:
                        await member.remove_roles(role1)
                    await member.add_roles(role3)
                    await channel.send(
                        f"<@{member.id}>! Jesteś naszym Einsteinem. "
                        f"Z Tobą zatkniemy flagę AlterMMO na Marsie. <:5head:882184634786521149>"
                    )

        if accepted:
            await channel.send(
                f"<@{author.id}>, Twój pomysł został przesłany do administracji i być może zostanie zaimplementowany. "
                f"Wymyśliłeś już {file_data[str(author.id)]} pomysłów!"
            )

    async def finalize_vote(self, vote_message_id: int):
        key = self._vote_key(vote_message_id)
        vote = self.votes.get(key)
        if not vote or vote.get("status") != "active":
            return

        vote_channel = self.bot.get_channel(vote["channel_id"])
        if vote_channel is None:
            vote_channel = await self.bot.fetch_channel(vote["channel_id"])

        try:
            message = await vote_channel.fetch_message(vote_message_id)
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

        log_channel = self.bot.get_channel(LOG_CHANNEL_ID)
        if log_channel is None:
            log_channel = await self.bot.fetch_channel(LOG_CHANNEL_ID)

        accepted = yes_count >= VOTES_REQ and yes_count > no_count
        idea = vote["idea"]

        try:
            if accepted:
                await self.idea_support(
                    guild=guild,
                    voter_ids=voter_ids,
                    author=author,
                    accepted=True,
                )

                await command_channel.send(
                    "Pomysł został zatwierdzony przez społeczność, przesłany do administracji "
                    "i zostanie tam przedyskutowany.\n\n"
                    f"{idea}"
                )

                await log_channel.send(
                    f"<@291836779495948288>\n\n{idea}"
                )
            else:
                await self.idea_support(
                    guild=guild,
                    voter_ids=voter_ids,
                    author=author,
                    accepted=False,
                )
                await command_channel.send(
                    f"Pomysł nie został zaakceptowany przez społeczność.\n\n{idea}"
                )

        finally:
            vote["status"] = "finished"
            vote["yes_count"] = yes_count
            vote["no_count"] = no_count
            self._save_votes()

            try:
                await message.delete()
            except discord.HTTPException:
                pass

    async def watch_vote(self, vote_message_id: int):
        key = self._vote_key(vote_message_id)
        vote = self.votes.get(key)
        if not vote or vote.get("status") != "active":
            return

        created_ts = vote["created_ts"]
        now = discord.utils.utcnow().timestamp()
        remaining = max(0, created_ts + VOTE_DURATION - now)
        print(f"Idea vote {vote_message_id}, remaining {remaining}")

        await asyncio.sleep(remaining)
        await self.finalize_vote(vote_message_id)

    @commands.command(name="pomysl")
    @commands.cooldown(1, 60 * 60 * 23, commands.BucketType.user)
    @commands.check(is_channel)
    async def idea_command(self, ctx: commands.Context, *, idea: str):
        idea = idea.strip()

        if len(idea) < 25:
            await ctx.send(
                f"<@{ctx.author.id}>, za krótki opis pomysłu. Spróbuj rozpisać się bardziej, "
                f"żeby każdy zrozumiał, o co chodzi. <:madge:882184635474386974>"
            )
            return

        if len(idea.split()) <= 1:
            await ctx.send(
                f"<@{ctx.author.id}>, pomysł w jednym słowie? <:madge:882184635474386974>"
            )
            return

        await ctx.message.add_reaction("ℹ️")

        embed = discord.Embed(
            title="Co myślicie o takim pomyśle?",
            description=idea,
            color=0xE5F507,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(
            text=f"Dodany przez {ctx.author.display_name}",
            icon_url=ctx.author.display_avatar.url,
        )

        vote_channel = self.bot.get_channel(VOTE_CHANNEL_ID)
        if vote_channel is None:
            vote_channel = await self.bot.fetch_channel(VOTE_CHANNEL_ID)

        vote_message = await vote_channel.send(embed=embed)

        for emoji in VOTES.keys():
            await vote_message.add_reaction(emoji)

        self.votes[self._vote_key(vote_message.id)] = {
            "vote_message_id": vote_message.id,
            "guild_id": ctx.guild.id,
            "channel_id": vote_channel.id,
            "author_id": ctx.author.id,
            "idea": idea,
            "source_message_id": ctx.message.id,
            "created_ts": discord.utils.utcnow().timestamp(),
            "status": "active",
        }
        self._save_votes()

        self.vote_tasks[vote_message.id] = self.bot.loop.create_task(
            self.watch_vote(vote_message.id)
        )

    @idea_command.error
    async def idea_command_error(self, ctx: commands.Context, error):
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(
                "Poczekaj na odnowienie komendy! Zostało "
                f"{round(error.retry_after / 60 / 60, 2)} godzin/y <:Bedge:970576892874854400>."
            )
            return

        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(
                f"<@{ctx.author.id}> Coś źle napisałeś. Żeby zaproponować pomysł, wpisz np. "
                f"*$pomysl Usunąć AlterMMO z internetów.* <:FeelsOkayMan:794117830822854656>"
            )
            return

        raise error


async def setup(bot):
    await bot.add_cog(ideas_voting(bot))
