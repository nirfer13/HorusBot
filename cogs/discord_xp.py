import asyncio
import datetime as dt
import json
import logging
import random
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import discord
from discord.ext import commands


logger = logging.getLogger(__name__)

CONFIG_FILE = Path("discord_xp_config.json")
RANKING_IMAGE_FILE = Path("images/ranking.png")

DEFAULT_CONFIG = {
    "message_xp_min": 25,
    "message_xp_max": 40,
    "message_cooldown_seconds": 60,

    "reaction_bonus_xp": 25,
    "reaction_threshold": 3,

    "save_interval_min_seconds": 300,
    "save_interval_max_seconds": 600,

    "streak_bonus_per_day": 0.10,
    "maximum_streak_bonus": 2.0,

    "role_rewards_enabled": False,
    "guild_id": 686137998177206281,

    "xp_bar_length": 20,

    "ranks": [
        # ...
    ],
}


class DiscordXP(commands.Cog, name="discord_xp"):
    def __init__(self, bot):
        self.bot = bot
        self.config = self._load_config()

        # XP waiting to be saved:
        # {discord_id: xp_amount}
        self.pending_xp: Dict[int, int] = defaultdict(int)

        # Latest known usernames:
        # {discord_id: username}
        self.pending_usernames: Dict[int, str] = {}

        # Messages waiting for reaction bonus processing:
        # {message_id: (discord_id, username)}
        self.pending_reaction_bonuses: Dict[
            int,
            Tuple[int, str],
        ] = {}

        # Cooldowns for ordinary message XP:
        # {discord_id: monotonic_timestamp}
        self.message_cooldowns: Dict[int, float] = {}

        # Daily streak cache:
        # {discord_id: (date, streak)}
        self.streak_cache: Dict[int, Tuple[dt.date, int]] = {}

        self.pending_lock = asyncio.Lock()
        self.streak_lock = asyncio.Lock()

        self.flush_task: Optional[asyncio.Task] = None

    def _load_config(self) -> Dict[str, Any]:
        """
        Load Discord XP settings from JSON.
        """
        if not CONFIG_FILE.exists():
            with CONFIG_FILE.open("w", encoding="utf-8") as file:
                json.dump(
                    DEFAULT_CONFIG,
                    file,
                    indent=4,
                    ensure_ascii=False,
                )

            return DEFAULT_CONFIG.copy()

        try:
            with CONFIG_FILE.open("r", encoding="utf-8") as file:
                loaded_config = json.load(file)
        except (OSError, json.JSONDecodeError):
            logger.exception(
                "Could not load Discord XP configuration."
            )
            return DEFAULT_CONFIG.copy()

        config = DEFAULT_CONFIG.copy()
        config.update(loaded_config)

        config["ranks"] = sorted(
            config.get("ranks", []),
            key=lambda rank: int(rank["required_xp"]),
        )

        self._validate_config(config)

        return config

    @staticmethod
    def _validate_config(config: Dict[str, Any]) -> None:
        """
        Validate configuration values.
        """
        if config["message_xp_min"] < 0:
            raise ValueError(
                "message_xp_min cannot be negative."
            )

        if (
            config["message_xp_max"]
            < config["message_xp_min"]
        ):
            raise ValueError(
                "message_xp_max cannot be lower than "
                "message_xp_min."
            )

        if config["message_cooldown_seconds"] < 0:
            raise ValueError(
                "message_cooldown_seconds cannot be negative."
            )

        if config["reaction_threshold"] < 1:
            raise ValueError(
                "reaction_threshold must be at least 1."
            )

        if config["streak_bonus_per_day"] < 0:
            raise ValueError(
                "streak_bonus_per_day cannot be negative."
            )

        if config["xp_bar_length"] < 5:
            raise ValueError(
                "xp_bar_length must be at least 5."
            )

        if (
            config["save_interval_max_seconds"]
            < config["save_interval_min_seconds"]
        ):
            raise ValueError(
                "Maximum save interval cannot be lower "
                "than minimum save interval."
            )
        if config["maximum_streak_bonus"] < 0:
            raise ValueError(
                "maximum_streak_bonus cannot be negative."
            )

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """
        Start the periodic XP database task.
        """
        if self.flush_task is None or self.flush_task.done():
            self.flush_task = self.bot.loop.create_task(
                self._xp_flush_loop()
            )
            logger.info("Discord XP task started.")

    def cog_unload(self) -> None:
        """
        Cancel the background task and schedule a final XP save.
        """
        if self.flush_task is not None:
            self.flush_task.cancel()

        if not self.bot.is_closed():
            self.bot.loop.create_task(
                self.flush_pending_xp()
            )

    @commands.Cog.listener()
    async def on_message(
        self,
        message: discord.Message,
    ) -> None:
        """
        Process ordinary Discord messages.
        """

        if message.guild is None:
            return

        if message.author.bot:
            return

        await self._try_grant_message_xp(
            message.author
        )

    async def _try_grant_message_xp(
        self,
        member: discord.Member,
    ) -> bool:
        """
        Grant XP if the user's message cooldown expired.
        """
        discord_id = member.id
        current_time = time.monotonic()

        cooldown_seconds = self.config[
            "message_cooldown_seconds"
        ]

        last_reward_time = self.message_cooldowns.get(
            discord_id
        )

        if last_reward_time is not None:
            elapsed_time = (
                current_time - last_reward_time
            )

            if elapsed_time < cooldown_seconds:
                return False

        # The cooldown is set before awaiting to prevent two nearly
        # simultaneous messages from both awarding XP.
        self.message_cooldowns[
            discord_id
        ] = current_time

        streak = await self._get_or_update_chatstreak(
            discord_id=discord_id,
            username=member.name,
        )
        base_xp = random.randint(
            self.config["message_xp_min"],
            self.config["message_xp_max"],
        )
        gained_xp = self._apply_streak_bonus(
            base_xp=base_xp,
            streak=streak,
        )
        await self._add_pending_xp(
            discord_id=discord_id,
            username=member.name,
            gained_xp=gained_xp,
        )

        logger.info(
            "User %s gained %s XP. Base: %s, streak: %s.",
            member.name,
            gained_xp,
            base_xp,
            streak,
        )

        return True

    def _apply_streak_bonus(
        self,
        base_xp: int,
        streak: int,
    ) -> int:
        """
        Apply the configured streak multiplier.

        maximum_streak_bonus limits only the bonus part.

        Example:
        maximum_streak_bonus = 2.0
        means a maximum bonus of +200%, so the final multiplier is x3.0.
        """
        bonus_per_day = float(
            self.config["streak_bonus_per_day"]
        )

        maximum_bonus = float(
            self.config["maximum_streak_bonus"]
        )

        streak_bonus = min(
            streak * bonus_per_day,
            maximum_bonus,
        )

        multiplier = 1 + streak_bonus

        return round(base_xp * multiplier)

    async def _get_or_update_chatstreak(
        self,
        discord_id: int,
        username: str,
    ) -> int:
        """
        Increment the user's chat streak once per calendar day.

        Rules:
        - First active day: streak becomes 1.
        - Additional messages on the same day: no change.
        - Activity on the following day: streak increases by 1.
        - Missing at least one full day: streak resets to 1.
        """
        from zoneinfo import ZoneInfo
        WARSAW_TIMEZONE = ZoneInfo("Europe/Warsaw")
        today = dt.datetime.now(WARSAW_TIMEZONE).date()

        cached_value = self.streak_cache.get(discord_id)

        if cached_value is not None and cached_value[0] == today:
            return cached_value[1]

        async with self.streak_lock:
            cached_value = self.streak_cache.get(discord_id)

            if cached_value is not None and cached_value[0] == today:
                return cached_value[1]

            try:
                async with self.bot.pg_con.acquire() as connection:
                    async with connection.transaction():
                        await connection.execute(
                            """
                            INSERT INTO chatters_information_discord (
                                chtr_id,
                                chtr_name,
                                xp,
                                chatstreak
                            )
                            VALUES ($1, $2, 0, 0)
                            ON CONFLICT (chtr_id)
                            DO UPDATE SET
                                chtr_name = EXCLUDED.chtr_name
                            """,
                            discord_id,
                            username,
                        )

                        activity_row = await connection.fetchrow(
                            """
                            SELECT last_active_date
                            FROM discord_chat_activity
                            WHERE chtr_id = $1
                            FOR UPDATE
                            """,
                            discord_id,
                        )

                        current_streak = await connection.fetchval(
                            """
                            SELECT chatstreak
                            FROM chatters_information_discord
                            WHERE chtr_id = $1
                            """,
                            discord_id,
                        )

                        current_streak = int(current_streak or 0)

                        if activity_row is None:
                            new_streak = 1
                        else:
                            last_active_date = activity_row["last_active_date"]

                            if last_active_date == today:
                                new_streak = current_streak
                            elif last_active_date == today - dt.timedelta(days=1):
                                new_streak = current_streak + 1
                            else:
                                new_streak = 1

                        await connection.execute(
                            """
                            UPDATE chatters_information_discord
                            SET
                                chtr_name = $2,
                                chatstreak = $3
                            WHERE chtr_id = $1
                            """,
                            discord_id,
                            username,
                            new_streak,
                        )

                        await connection.execute(
                            """
                            INSERT INTO discord_chat_activity (
                                chtr_id,
                                last_active_date
                            )
                            VALUES ($1, $2)
                            ON CONFLICT (chtr_id)
                            DO UPDATE SET
                                last_active_date = EXCLUDED.last_active_date
                            """,
                            discord_id,
                            today,
                        )

                self.streak_cache[discord_id] = (today, new_streak)

                logger.info(
                    "Updated Discord chat streak: user=%s, date=%s, streak=%s.",
                    username,
                    today,
                    new_streak,
                )

                return new_streak

            except Exception:
                logger.exception(
                    "Could not update Discord chat streak for user %s.",
                    discord_id,
                )
                raise
        
    async def _add_pending_xp(
        self,
        discord_id: int,
        username: str,
        gained_xp: int,
    ) -> None:
        """
        Add final calculated XP to the memory buffer.
        """
        if gained_xp <= 0:
            return

        async with self.pending_lock:
            self.pending_xp[discord_id] += gained_xp
            self.pending_usernames[discord_id] = username

    @commands.Cog.listener()
    async def on_raw_reaction_add(
        self,
        payload: discord.RawReactionActionEvent,
    ) -> None:
        """
        Check whether a message reached the reaction threshold.
        """
        if payload.guild_id is None:
            return

        if (
            self.bot.user is not None
            and payload.user_id == self.bot.user.id
        ):
            return

        channel = self.bot.get_channel(
            payload.channel_id
        )

        if channel is None:
            try:
                channel = await self.bot.fetch_channel(
                    payload.channel_id
                )
            except (
                discord.NotFound,
                discord.Forbidden,
                discord.HTTPException,
            ):
                return

        try:
            message = await channel.fetch_message(
                payload.message_id
            )
        except (
            discord.NotFound,
            discord.Forbidden,
            discord.HTTPException,
        ):
            return

        if message.author.bot:
            return

        total_reactions = sum(
            reaction.count
            for reaction in message.reactions
        )

        if (
            total_reactions
            < self.config["reaction_threshold"]
        ):
            return

        async with self.pending_lock:
            self.pending_reaction_bonuses[
                message.id
            ] = (
                message.author.id,
                message.author.name,
            )

    async def _xp_flush_loop(self) -> None:
        """
        Save pending XP every configured random interval.
        """
        while not self.bot.is_closed():
            delay = random.randint(
                self.config[
                    "save_interval_min_seconds"
                ],
                self.config[
                    "save_interval_max_seconds"
                ],
            )

            try:
                await asyncio.sleep(delay)
                await self.flush_pending_xp()

            except asyncio.CancelledError:
                break

            except Exception:
                logger.exception(
                    "Unexpected Discord XP flush error."
                )

    async def flush_pending_xp(self) -> None:
        """
        Save message XP and reaction XP to PostgreSQL.
        """
        async with self.pending_lock:
            if (
                not self.pending_xp
                and not self.pending_reaction_bonuses
            ):
                return

            message_xp_to_save = dict(
                self.pending_xp
            )

            usernames_to_save = dict(
                self.pending_usernames
            )

            reaction_bonuses_to_save = dict(
                self.pending_reaction_bonuses
            )

            self.pending_xp.clear()
            self.pending_usernames.clear()
            self.pending_reaction_bonuses.clear()

        changed_user_ids = set()

        try:
            reaction_xp_to_save: Dict[
                int,
                int,
            ] = defaultdict(int)

            async with self.bot.pg_con.acquire() as connection:
                async with connection.transaction():
                    for (
                        message_id,
                        reaction_data,
                    ) in reaction_bonuses_to_save.items():
                        discord_id, username = reaction_data

                        inserted_message_id = (
                            await connection.fetchval(
                                """
                                INSERT INTO discord_message_xp_bonus (
                                    message_id,
                                    chtr_id
                                )
                                VALUES ($1, $2)
                                ON CONFLICT (message_id)
                                DO NOTHING
                                RETURNING message_id
                                """,
                                message_id,
                                discord_id,
                            )
                        )

                        if inserted_message_id is None:
                            continue

                        streak = await self._get_or_update_chatstreak(
                            discord_id=discord_id,
                            username=username,
                        )

                        reaction_xp = self._apply_streak_bonus(
                            base_xp=self.config[
                                "reaction_bonus_xp"
                            ],
                            streak=streak,
                        )

                        reaction_xp_to_save[
                            discord_id
                        ] += reaction_xp

                        usernames_to_save[
                            discord_id
                        ] = username

                    total_xp_to_save: Dict[
                        int,
                        int,
                    ] = defaultdict(int)

                    for discord_id, gained_xp in (
                        message_xp_to_save.items()
                    ):
                        total_xp_to_save[
                            discord_id
                        ] += gained_xp

                    for discord_id, gained_xp in (
                        reaction_xp_to_save.items()
                    ):
                        total_xp_to_save[
                            discord_id
                        ] += gained_xp

                    records = [
                        (
                            discord_id,
                            usernames_to_save.get(
                                discord_id,
                                str(discord_id),
                            ),
                            gained_xp,
                        )
                        for discord_id, gained_xp
                        in total_xp_to_save.items()
                    ]

                    if records:
                        await connection.executemany(
                            """
                            INSERT INTO chatters_information_discord (
                                chtr_id,
                                chtr_name,
                                xp,
                                chatstreak
                            )
                            VALUES ($1, $2, $3, 0)
                            ON CONFLICT (chtr_id)
                            DO UPDATE SET
                                chtr_name = EXCLUDED.chtr_name,
                                xp = (
                                    chatters_information_discord.xp
                                    + EXCLUDED.xp
                                )
                            """,
                            records,
                    )

                    changed_user_ids.update(
                        total_xp_to_save.keys()
                    )

            logger.info(
                "Saved XP for %s Discord users. Total: %s.",
                len(changed_user_ids),
                sum(total_xp_to_save.values()),
            )

        except Exception:
            async with self.pending_lock:
                for discord_id, gained_xp in (
                    message_xp_to_save.items()
                ):
                    self.pending_xp[
                        discord_id
                    ] += gained_xp

                self.pending_usernames.update(
                    usernames_to_save
                )

                self.pending_reaction_bonuses.update(
                    reaction_bonuses_to_save
                )

            logger.exception(
                "Could not save Discord XP. "
                "Pending values were restored."
            )
            raise

        if self.config["role_rewards_enabled"]:
            await self._update_roles_for_users(
                changed_user_ids
            )

    def _get_rank_data(
        self,
        xp: int,
    ) -> Tuple[
        Optional[Dict[str, Any]],
        Optional[Dict[str, Any]],
    ]:
        """
        Return the current and next rank for the given XP.
        """
        current_rank = None
        next_rank = None

        for rank in self.config["ranks"]:
            required_xp = int(
                rank["required_xp"]
            )

            if xp >= required_xp:
                current_rank = rank
            else:
                next_rank = rank
                break

        return current_rank, next_rank
    
    def _format_rank(
        self,
        rank: Optional[Dict[str, Any]],
    ) -> str:
        """
        Return a Discord role mention when the rank has a valid role ID.
        Otherwise return the configured rank name.
        """
        if rank is None:
            return "Brak rangi"

        role_id = rank.get("role_id")

        if role_id:
            return f"<@&{int(role_id)}>"

        return str(rank.get("name", "Nieznana ranga"))

    async def _update_roles_for_users(
        self,
        discord_ids,
    ) -> None:
        """
        Assign the highest unlocked XP role and remove old XP roles.

        This method runs only when role_rewards_enabled is true.
        """
        if not discord_ids:
            return

        guild = self.bot.get_guild(
            int(self.config["guild_id"])
        )

        if guild is None:
            logger.warning(
                "XP guild %s was not found.",
                self.config["guild_id"],
            )
            return

        rows = await self.bot.pg_con.fetch(
            """
            SELECT
                info.chtr_id,
                info.chtr_name,
                info.xp,
                CASE
                    WHEN activity.last_active_date >= CURRENT_DATE - 1
                        THEN info.chatstreak
                    ELSE 0
                END AS chatstreak
            FROM chatters_information_discord AS info
            LEFT JOIN discord_chat_activity AS activity
                ON activity.chtr_id = info.chtr_id
            ORDER BY info.xp DESC, info.chtr_id ASC
            LIMIT 10
            """
        )

        configured_role_ids = {
            int(rank["role_id"])
            for rank in self.config["ranks"]
            if rank.get("role_id")
        }

        for row in rows:
            member = guild.get_member(
                row["chtr_id"]
            )

            if member is None:
                continue

            current_rank, _ = self._get_rank_data(
                row["xp"]
            )

            target_role_id = None

            if current_rank is not None:
                target_role_id = int(
                    current_rank["role_id"]
                )

            roles_to_remove = [
                role
                for role in member.roles
                if (
                    role.id in configured_role_ids
                    and role.id != target_role_id
                )
            ]

            if roles_to_remove:
                try:
                    await member.remove_roles(
                        *roles_to_remove,
                        reason="Aktualizacja rangi XP",
                    )
                except discord.HTTPException:
                    logger.exception(
                        "Could not remove old XP roles "
                        "from user %s.",
                        member.id,
                    )

            if target_role_id is None:
                continue

            target_role = guild.get_role(
                target_role_id
            )

            if target_role is None:
                logger.warning(
                    "XP role %s was not found.",
                    target_role_id,
                )
                continue

            if target_role in member.roles:
                continue

            try:
                await member.add_roles(
                    target_role,
                    reason="Osiągnięto próg XP",
                )
            except discord.HTTPException:
                logger.exception(
                    "Could not add XP role %s "
                    "to user %s.",
                    target_role_id,
                    member.id,
                )

    def _create_xp_bar(
        self,
        current_xp: int,
        current_threshold: int,
        next_threshold: int,
    ) -> str:
        """
        Create a text progress bar.
        """
        bar_length = int(
            self.config["xp_bar_length"]
        )

        required_range = (
            next_threshold - current_threshold
        )

        if required_range <= 0:
            progress = 1.0
        else:
            progress = (
                current_xp - current_threshold
            ) / required_range

        progress = max(
            0.0,
            min(progress, 1.0),
        )

        filled_length = round(
            progress * bar_length
        )

        empty_length = (
            bar_length - filled_length
        )

        return (
            "█" * filled_length
            + "░" * empty_length
        )

    @commands.command(name="xptop")
    async def xp_top(
        self,
        ctx: commands.Context,
    ) -> None:
        """
        Display the top 10 Discord users by XP.
        """
        await self.flush_pending_xp()

        rows = await self.bot.pg_con.fetch(
            """
            SELECT
                chtr_id,
                chtr_name,
                xp,
                chatstreak
            FROM chatters_information_discord
            ORDER BY xp DESC, chtr_id ASC
            LIMIT 10
            """
        )

        if not rows:
            await ctx.send(
                "Ranking XP jest obecnie pusty."
            )
            return

        ranking_lines = []

        medals = {
            1: "🥇",
            2: "🥈",
            3: "🥉",
        }

        for position, row in enumerate(
            rows,
            start=1,
        ):
            prefix = medals.get(
                position,
                f"**{position}.**",
            )

            ranking_lines.append(
                f"{prefix} <@{row['chtr_id']}> — "
                f"**{row['xp']:,} XP** "
                f"🔥 {row['chatstreak']}"
            )

        embed = discord.Embed(
            title="Ranking doświadczenia",
            description="\n".join(ranking_lines),
            color=0xD9A441,
        )

        embed.set_footer(
            text="🔥 oznacza aktualny dzienny streak"
        )

        if RANKING_IMAGE_FILE.exists():
            ranking_image = discord.File(
                str(RANKING_IMAGE_FILE),
                filename="ranking.png",
            )

            embed.set_thumbnail(
                url="attachment://ranking.png"
            )

            await ctx.send(
                file=ranking_image,
                embed=embed,
            )
        else:
            logger.warning(
                "Ranking image was not found: %s",
                RANKING_IMAGE_FILE,
            )

            await ctx.send(embed=embed)

    @commands.command(name="xp")
    async def show_xp(
        self,
        ctx: commands.Context,
        member: Optional[discord.Member] = None,
    ) -> None:
        """
        Display a user's XP progress to the next rank.

        Usage:
        $xp
        $xp @user
        """
        target_member = member or ctx.author

        await self.flush_pending_xp()

        row = await self.bot.pg_con.fetchrow(
            """
            WITH ranked_users AS (
                SELECT
                    info.chtr_id,
                    info.xp,
                    info.chatstreak,
                    activity.last_active_date,
                    ROW_NUMBER() OVER (
                        ORDER BY info.xp DESC, info.chtr_id ASC
                    ) AS ranking_position
                FROM chatters_information_discord AS info
                LEFT JOIN discord_chat_activity AS activity
                    ON activity.chtr_id = info.chtr_id
            )
            SELECT
                xp,
                ranking_position,
                CASE
                    WHEN last_active_date >= CURRENT_DATE - 1
                        THEN chatstreak
                    ELSE 0
                END AS chatstreak
            FROM ranked_users
            WHERE chtr_id = $1
            """,
            target_member.id,
            )

        if row is None:
            await ctx.send(
                f"{target_member.mention} nie zdobył "
                "jeszcze żadnego XP."
            )
            return

        current_xp = row["xp"]
        chatstreak = row["chatstreak"]
        ranking_position = row["ranking_position"]

        current_rank, next_rank = (
            self._get_rank_data(current_xp)
        )

        current_rank_display = self._format_rank(
            current_rank
        )

        next_rank_display = self._format_rank(
            next_rank
        )

        if next_rank is None:
            current_streak_bonus = min(
                chatstreak
                * float(self.config["streak_bonus_per_day"]),
                float(self.config["maximum_streak_bonus"]),
            )

            streak_multiplier = 1 + current_streak_bonus

            embed = discord.Embed(
                title=f"XP — {target_member.display_name}",
                description=(
                    f"**{current_xp:,} XP**\n\n"
                    f"`{'█' * int(self.config['xp_bar_length'])}`\n"
                    "**Osiągnięto najwyższą rangę.**"
                ),
                color=0xD9A441,
            )

            embed.set_thumbnail(
                url=target_member.display_avatar.url
            )

            embed.add_field(
                name="Aktualna ranga",
                value=current_rank_display,
                inline=True,
            )

            embed.add_field(
                name="Miejsce w rankingu",
                value=f"#{ranking_position}",
                inline=True,
            )

            embed.add_field(
                name="Streak",
                value=f"🔥 {chatstreak}",
                inline=True,
            )

            embed.add_field(
                name="Mnożnik XP",
                value=f"×{streak_multiplier:.2f}",
                inline=True,
            )

            await ctx.send(embed=embed)
            return

        current_threshold = (
            int(current_rank["required_xp"])
            if current_rank is not None
            else 0
        )

        next_threshold = int(
            next_rank["required_xp"]
        )

        progress_bar = self._create_xp_bar(
            current_xp=current_xp,
            current_threshold=current_threshold,
            next_threshold=next_threshold,
        )

        xp_since_rank = (
            current_xp - current_threshold
        )

        xp_required_for_rank = (
            next_threshold - current_threshold
        )

        xp_remaining = max(
            0,
            next_threshold - current_xp,
        )

        embed = discord.Embed(
            title=f"XP — {target_member.display_name}",
            description=(
                f"`{progress_bar}`\n"
                f"**{xp_since_rank:,} / "
                f"{xp_required_for_rank:,} XP**"
            ),
            color=0x5865F2,
        )

        embed.set_thumbnail(
                url=ctx.author.display_avatar.url
        )

        embed.add_field(
            name="Aktualna ranga",
            value=current_rank_display,
            inline=True,
        )

        embed.add_field(
            name="Następna ranga",
            value=next_rank_display,
            inline=True,
        )

        embed.add_field(
            name="Brakuje",
            value=f"{xp_remaining:,} XP",
            inline=True,
        )

        embed.add_field(
            name="Ranking",
            value=f"#{ranking_position}",
            inline=True,
        )

        embed.add_field(
            name="Streak",
            value=f"🔥 {chatstreak}",
            inline=True,
        )

        current_streak_bonus = min(
            chatstreak
            * float(self.config["streak_bonus_per_day"]),
            float(self.config["maximum_streak_bonus"]),
        )

        streak_multiplier = 1 + current_streak_bonus

        embed.add_field(
            name="Mnożnik XP",
            value=f"×{streak_multiplier:.2f}",
            inline=True,
        )

        await ctx.send(embed=embed)

    @commands.command(name="flushdiscordxp")
    @commands.has_permissions(administrator=True)
    async def flush_discord_xp(
        self,
        ctx: commands.Context,
    ) -> None:
        """
        Manually save pending XP.
        """
        await self.flush_pending_xp()

        await ctx.send(
            "Zapisano oczekujący EXP użytkowników."
        )


async def setup(bot):
    await bot.add_cog(DiscordXP(bot))