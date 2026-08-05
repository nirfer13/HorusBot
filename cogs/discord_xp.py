import asyncio
import datetime as dt
import json
import logging
import random
import time
import os
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import discord
from discord.ext import commands


logger = logging.getLogger(__name__)

CONFIG_FILE = Path("discord_xp_config.json")
RANKING_IMAGE_FILE = Path("images/ranking.png")

LEVEL_UP_CHANNEL_ID = 776379796367212594

LEVEL_UP_IMAGE_DIRECTORY = Path(
    r"C:\Programowanie\HorusBot\images\level_up"
)

LEVEL_UP_IMAGE_EXTENSIONS = {
    ".webp",
    ".gif",
}

DEFAULT_CONFIG = {
    "message_xp_min": 25,
    "message_xp_max": 40,
    "message_cooldown_seconds": 60,

    "reaction_bonus_xp": 25,
    "reaction_threshold": 3,

    "voice_xp_enabled": True,
    "voice_xp_interval_seconds": 300,
    "voice_xp_min": 10,
    "voice_xp_max": 20,
    "voice_require_non_bot_companion": True,

    # Per-channel voice XP multipliers: {"channel_id": multiplier}.
    # Channels that are not listed use the default multiplier of 1.0.
    "voice_channel_xp_multipliers": {
        # Scena Barda: half of the normal voice XP.
        "1056200069952589924": 0.5,
    },

    "save_interval_min_seconds": 300,
    "save_interval_max_seconds": 600,

    "streak_bonus_per_day": 0.02,
    "maximum_streak_bonus": 3.0,

    "guild_tag_bonus_enabled": True,
    "guild_tag_bonus_tag": "ANTY",
    "guild_tag_bonus_multiplier": 0.25,

    "voice_group_bonus_enabled": True,
    "voice_group_bonus_min_members": 3,
    "voice_group_bonus_per_member_above_one": 0.20,

    "role_rewards_enabled": False,
    "guild_id": 686137998177206281,

    "xp_bar_length": 20,

    "external_xp_enabled": True,
    "external_xp_file": "C:\\Programowanie\\AlterMMO-Bot-RPG\\expSummary.json",
    "external_xp_check_interval_seconds": 60,

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
        self.voice_xp_task: Optional[asyncio.Task] = None

        self.external_xp_task: Optional[asyncio.Task] = None
        self.external_xp_lock = asyncio.Lock()
        self.voice_group_bonus_state: Dict[int, int] = {}

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
        
        if config["voice_xp_interval_seconds"] < 10:
            raise ValueError(
                "voice_xp_interval_seconds must be at least 10."
            )

        if config["voice_xp_min"] < 0:
            raise ValueError(
                "voice_xp_min cannot be negative."
            )

        if config["voice_xp_max"] < config["voice_xp_min"]:
            raise ValueError(
                "voice_xp_max cannot be lower than voice_xp_min."
            )

        channel_multipliers = config[
            "voice_channel_xp_multipliers"
        ]

        if not isinstance(channel_multipliers, dict):
            raise ValueError(
                "voice_channel_xp_multipliers must be a mapping "
                "of channel id to multiplier."
            )

        for channel_id, multiplier in channel_multipliers.items():
            try:
                int(channel_id)
            except (TypeError, ValueError):
                raise ValueError(
                    "voice_channel_xp_multipliers keys must be "
                    "channel ids."
                )

            if not isinstance(multiplier, (int, float)):
                raise ValueError(
                    "voice_channel_xp_multipliers values must be "
                    "numbers."
                )

            if multiplier < 0:
                raise ValueError(
                    "voice_channel_xp_multipliers values cannot "
                    "be negative."
                )

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """
        Start periodic Discord XP tasks.
        """
        if self.flush_task is None or self.flush_task.done():
            self.flush_task = self.bot.loop.create_task(
                self._xp_flush_loop()
            )
            logger.info("Discord XP flush task started.")

        if (
            self.config["voice_xp_enabled"]
            and (
                self.voice_xp_task is None
                or self.voice_xp_task.done()
            )
        ):
            self.voice_xp_task = self.bot.loop.create_task(
                self._voice_xp_loop()
            )
            logger.info(
                "Discord voice XP task started. Interval: %s seconds.",
                self.config["voice_xp_interval_seconds"],
            )

        if (
            self.config["external_xp_enabled"]
            and (
                self.external_xp_task is None
                or self.external_xp_task.done()
            )
        ):
            self.external_xp_task = self.bot.loop.create_task(
                self._external_xp_loop()
            )

            logger.info(
                "External XP import task started. Interval: %s seconds.",
                self.config["external_xp_check_interval_seconds"],
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

    def cog_unload(self) -> None:
        """
        Cancel background tasks and schedule a final XP save.
        """
        if self.flush_task is not None:
            self.flush_task.cancel()

        if self.voice_xp_task is not None:
            self.voice_xp_task.cancel()

        if not self.bot.is_closed():
            self.bot.loop.create_task(
                self.flush_pending_xp()
            )

        if self.external_xp_task is not None:
            self.external_xp_task.cancel()

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
        gained_xp = self._apply_xp_multipliers(
            base_xp=base_xp,
            streak=streak,
            member=member
        )
        await self._add_pending_xp(
            discord_id=discord_id,
            username=member.name,
            gained_xp=gained_xp,
        )

        logger.info(
            "User %s gained %s XP. Base: %s, streak: %s, ANTY tag: %s.",
            member.name,
            gained_xp,
            base_xp,
            streak,
            self._has_xp_guild_tag(member),
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

    def _apply_xp_multipliers(
        self,
        base_xp: int,
        streak: int,
        member: Optional[discord.Member] = None,
        additional_bonus: float = 0.0,
    ) -> int:
        """
        Apply streak, guild-tag and optional additional XP bonuses.

        Bonuses are additive:
        x1.00 + streak bonus + guild-tag bonus + additional bonus.
        """
        bonus_per_day = float(
            self.config["streak_bonus_per_day"]
        )

        maximum_streak_bonus = float(
            self.config["maximum_streak_bonus"]
        )

        streak_bonus = min(
            streak * bonus_per_day,
            maximum_streak_bonus,
        )

        guild_tag_bonus = 0.0

        if self._has_xp_guild_tag(member):
            guild_tag_bonus = float(
                self.config["guild_tag_bonus_multiplier"]
            )

        multiplier = (
            1.0
            + streak_bonus
            + guild_tag_bonus
            + max(0.0, additional_bonus)
        )

        return round(base_xp * multiplier)
    
    def _is_active_voice_member(
    self,
    member: discord.Member,
    ) -> bool:
        """
        Check whether a member can count toward voice XP and group bonuses.
        """
        if member.bot:
            return False

        voice_state = member.voice

        if voice_state is None:
            return False

        if voice_state.channel is None:
            return False

        # Muted microphone is allowed, deafened audio is not.
        if voice_state.deaf or voice_state.self_deaf:
            return False

        if voice_state.afk:
            return False

        return True

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
                        previous_streak = current_streak

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

                streak_was_incremented = (
                    new_streak > previous_streak
                )

                reached_streak_milestone = (
                    new_streak > 0
                    and new_streak % 10 == 0
                )

                if (
                    streak_was_incremented
                    and reached_streak_milestone
                ):
                    await self._send_streak_milestone_notification(
                        username=username,
                        streak=new_streak,
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

    async def _external_xp_loop(self) -> None:
        """
        Periodically import XP generated by another bot.
        """
        interval = int(
            self.config["external_xp_check_interval_seconds"]
        )

        while not self.bot.is_closed():
            try:
                await asyncio.sleep(interval)

                if not self.config["external_xp_enabled"]:
                    continue

                await self.import_external_xp()

            except asyncio.CancelledError:
                break

            except Exception:
                logger.exception(
                    "Unexpected error in the external XP import loop."
                )

    async def import_external_xp(self) -> None:
        """
        Import XP from expSummary.json.

        The source file is moved to a processing file before it is read.
        This reduces the risk of deleting XP written concurrently by
        another bot.
        """
        async with self.external_xp_lock:
            source_file = Path(
                self.config["external_xp_file"]
            )

            # First retry files left after an earlier database failure.
            processing_files = sorted(
                source_file.parent.glob(
                    f"{source_file.stem}.processing.*{source_file.suffix}"
                )
            )

            if processing_files:
                for processing_file in processing_files:
                    await self._process_external_xp_file(
                        processing_file
                    )

            if not source_file.exists():
                self._create_empty_external_xp_file(source_file)
                return

            try:
                if source_file.stat().st_size == 0:
                    return
            except OSError:
                logger.exception(
                    "Could not inspect external XP file: %s.",
                    source_file,
                )
                return

            processing_file = source_file.with_name(
                f"{source_file.stem}.processing."
                f"{uuid.uuid4().hex}{source_file.suffix}"
            )

            try:
                # Atomic rename when both paths are on the same filesystem.
                os.replace(
                    source_file,
                    processing_file,
                )
            except FileNotFoundError:
                # The other process may have changed the file meanwhile.
                return
            except OSError:
                logger.exception(
                    "Could not claim external XP file: %s.",
                    source_file,
                )
                return

            # Create a new empty source only if another process has not
            # already recreated it.
            self._create_empty_external_xp_file(source_file)

            await self._process_external_xp_file(
                processing_file
            )

    async def _send_streak_milestone_notification(
        self,
        username: str,
        streak: int,
    ) -> None:
        """
        Send a simple notification after reaching each 10-streak milestone.

        The username is displayed as plain text, without pinging the user.
        """
        channel = self.bot.get_channel(
            LEVEL_UP_CHANNEL_ID
        )

        if channel is None:
            try:
                channel = await self.bot.fetch_channel(
                    LEVEL_UP_CHANNEL_ID
                )
            except (
                discord.NotFound,
                discord.Forbidden,
                discord.HTTPException,
            ):
                logger.exception(
                    "Could not find streak notification channel %s.",
                    LEVEL_UP_CHANNEL_ID,
                )
                return

        try:
            await channel.send(
                f"🔥 **{discord.utils.escape_markdown(username)}** "
                f"osiągnął **{streak} dni streaka**!",
                allowed_mentions=discord.AllowedMentions.none(),
            )

        except discord.Forbidden:
            logger.exception(
                "Bot cannot send streak notification "
                "in channel %s.",
                LEVEL_UP_CHANNEL_ID,
            )

        except discord.HTTPException:
            logger.exception(
                "Could not send streak notification "
                "for user %s.",
                username,
            )

    async def _send_level_up_notification(
        self,
        member: discord.Member,
        rank: Dict[str, Any],
        total_xp: int,
    ) -> None:
        """
        Send a level-up notification with a random GIF or WEBP image.
        """
        channel = self.bot.get_channel(
            LEVEL_UP_CHANNEL_ID
        )

        if channel is None:
            try:
                channel = await self.bot.fetch_channel(
                    LEVEL_UP_CHANNEL_ID
                )
            except (
                discord.NotFound,
                discord.Forbidden,
                discord.HTTPException,
            ):
                logger.exception(
                    "Could not find level-up channel %s.",
                    LEVEL_UP_CHANNEL_ID,
                )
                return

        role_id = rank.get("role_id")
        rank_name = str(
            rank.get("name", "Nieznana ranga")
        )

        rank_display = (
            f"<@&{int(role_id)}>"
            if role_id
            else rank_name
        )

        embed = discord.Embed(
            title="Awans!",
            description=(
                f"{member.mention} osiąga nową rangę!\n\n"
                f"**Nowa ranga:** {rank_display}\n"
                f"**Łączne XP:** {total_xp:,}"
            ),
            color=0xD9A441,
        )

        embed.set_thumbnail(
            url=member.display_avatar.url
        )

        embed.set_footer(
            text="Gratulacje! Kolejny etap został osiągnięty."
        )

        image_files = []

        if LEVEL_UP_IMAGE_DIRECTORY.exists():
            image_files = [
                file_path
                for file_path in LEVEL_UP_IMAGE_DIRECTORY.iterdir()
                if (
                    file_path.is_file()
                    and file_path.suffix.lower()
                    in LEVEL_UP_IMAGE_EXTENSIONS
                )
            ]
        else:
            logger.warning(
                "Level-up image directory does not exist: %s",
                LEVEL_UP_IMAGE_DIRECTORY,
            )

        try:
            if image_files:
                selected_image = random.choice(
                    image_files
                )

                attachment_name = (
                    f"level_up{selected_image.suffix.lower()}"
                )

                image_attachment = discord.File(
                    selected_image,
                    filename=attachment_name,
                )

                embed.set_image(
                    url=f"attachment://{attachment_name}"
                )

                await channel.send(
                    embed=embed,
                    file=image_attachment,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                await channel.send(
                    embed=embed,
                    allowed_mentions=discord.AllowedMentions.none(),
                )

        except discord.Forbidden:
            logger.exception(
                "Bot cannot send level-up notification "
                "in channel %s.",
                LEVEL_UP_CHANNEL_ID,
            )

        except discord.HTTPException:
            logger.exception(
                "Discord error while sending level-up "
                "notification for user %s.",
                member.id,
            )

    def _get_highest_member_xp_rank(
        self,
        member: discord.Member,
    ) -> Optional[Dict[str, Any]]:
        """
        Return the highest configured XP rank currently held by the member.
        """
        member_role_ids = {
            role.id
            for role in member.roles
        }

        held_ranks = [
            rank
            for rank in self.config["ranks"]
            if (
                rank.get("role_id")
                and int(rank["role_id"]) in member_role_ids
            )
        ]

        if not held_ranks:
            return None

        return max(
            held_ranks,
            key=lambda rank: int(
                rank["required_xp"]
            ),
        )

    @staticmethod
    def _create_empty_external_xp_file(
        file_path: Path,
    ) -> None:
        """
        Create an empty JSON object only when the file does not exist.

        The exclusive creation mode prevents overwriting a newly created
        file containing XP from the other bot.
        """
        try:
            file_path.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            with file_path.open(
                "x",
                encoding="utf-8",
            ) as file:
                json.dump({}, file)

        except FileExistsError:
            pass

    async def _process_external_xp_file(
        self,
        processing_file: Path,
    ) -> None:
        """
        Validate and save XP from one claimed processing file.

        The file is deleted only after a successful database transaction.
        On failure it remains on disk and will be retried later.
        """
        try:
            with processing_file.open(
                "r",
                encoding="utf-8",
            ) as file:
                raw_data = json.load(file)

        except json.JSONDecodeError:
            logger.exception(
                "External XP file contains invalid JSON: %s.",
                processing_file,
            )
            return

        except OSError:
            logger.exception(
                "Could not read external XP file: %s.",
                processing_file,
            )
            return

        if not isinstance(raw_data, dict):
            logger.error(
                "External XP file must contain a JSON object: %s.",
                processing_file,
            )
            return

        xp_by_user: Dict[int, int] = defaultdict(int)

        for raw_discord_id, raw_xp in raw_data.items():
            try:
                discord_id = int(raw_discord_id)
                gained_xp = int(raw_xp)
            except (TypeError, ValueError):
                logger.warning(
                    "Ignoring invalid external XP entry: id=%r, xp=%r.",
                    raw_discord_id,
                    raw_xp,
                )
                continue

            if discord_id <= 0:
                logger.warning(
                    "Ignoring invalid Discord ID: %s.",
                    discord_id,
                )
                continue

            if gained_xp <= 0:
                continue

            xp_by_user[discord_id] += gained_xp

        if not xp_by_user:
            try:
                processing_file.unlink()
            except OSError:
                logger.exception(
                    "Could not remove empty processing file: %s.",
                    processing_file,
                )
            return

        guild = self.bot.get_guild(
            int(self.config["guild_id"])
        )

        records = []

        for discord_id, gained_xp in xp_by_user.items():
            member = (
                guild.get_member(discord_id)
                if guild is not None
                else None
            )

            username = (
                member.name
                if member is not None
                else str(discord_id)
            )

            records.append(
                (
                    discord_id,
                    username,
                    gained_xp,
                )
            )

        try:
            async with self.bot.pg_con.acquire() as connection:
                async with connection.transaction():
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
                            chtr_name = CASE
                                WHEN EXCLUDED.chtr_name
                                    = EXCLUDED.chtr_id::TEXT
                                    THEN chatters_information_discord.chtr_name
                                ELSE EXCLUDED.chtr_name
                            END,
                            xp = (
                                chatters_information_discord.xp
                                + EXCLUDED.xp
                            )
                        """,
                        records,
                    )

            processing_file.unlink()

            logger.info(
                "Imported external XP for %s users. Total XP: %s.",
                len(xp_by_user),
                sum(xp_by_user.values()),
            )

        except Exception:
            logger.exception(
                "Could not import external XP from %s. "
                "The processing file was preserved for retry.",
                processing_file,
            )
            return

        if self.config["role_rewards_enabled"]:
            await self._update_roles_for_users(
                set(xp_by_user.keys())
            )

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

                        guild = self.bot.get_guild(
                            int(self.config["guild_id"])
                        )

                        member = (
                            guild.get_member(discord_id)
                            if guild is not None
                            else None
                        )

                        reaction_xp = self._apply_xp_multipliers(
                            base_xp=self.config["reaction_bonus_xp"],
                            streak=streak,
                            member=member,
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
        send_notifications: bool = True,
        xp_overrides: Optional[Dict[int, int]] = None,
    ) -> None:
        """
        Assign the correct XP role to selected Discord users.

        xp_overrides can provide XP values for users who do not have
        a database record, for example 0 XP during a full guild sync.
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

        discord_id_list = list(discord_ids)

        rows = await self.bot.pg_con.fetch(
            """
            SELECT
                chtr_id,
                xp
            FROM chatters_information_discord
            WHERE chtr_id = ANY($1::BIGINT[])
            """,
            discord_id_list,
        )

        xp_by_user = {
            int(row["chtr_id"]): int(row["xp"])
            for row in rows
        }

        if xp_overrides:
            for discord_id, xp in xp_overrides.items():
                xp_by_user.setdefault(
                    int(discord_id),
                    int(xp),
                )

        configured_role_ids = {
            int(rank["role_id"])
            for rank in self.config["ranks"]
            if rank.get("role_id")
        }

        for discord_id in discord_id_list:
            member = guild.get_member(
                discord_id
            )

            if member is None:
                try:
                    member = await guild.fetch_member(
                        discord_id
                    )
                except (
                    discord.NotFound,
                    discord.Forbidden,
                    discord.HTTPException,
                ):
                    logger.warning(
                        "Could not find guild member %s.",
                        discord_id,
                    )
                    continue

            total_xp = xp_by_user.get(
                discord_id,
                0,
            )

            target_rank, _ = self._get_rank_data(
                total_xp
            )

            previous_rank = self._get_highest_member_xp_rank(
                member
            )

            previous_threshold = (
                int(previous_rank["required_xp"])
                if previous_rank is not None
                else -1
            )

            target_threshold = (
                int(target_rank["required_xp"])
                if target_rank is not None
                else -1
            )

            target_role_id = None

            if (
                target_rank is not None
                and target_rank.get("role_id")
            ):
                target_role_id = int(
                    target_rank["role_id"]
                )

            roles_to_remove = [
                role
                for role in member.roles
                if (
                    role.id in configured_role_ids
                    and role.id != target_role_id
                )
            ]

            target_role = (
                guild.get_role(target_role_id)
                if target_role_id is not None
                else None
            )

            already_has_target_role = (
                target_role is not None
                and target_role in member.roles
            )

            is_real_rank_up = (
                target_rank is not None
                and target_threshold > previous_threshold
                and not already_has_target_role
            )

            try:
                if roles_to_remove:
                    await member.remove_roles(
                        *roles_to_remove,
                        reason="Synchronizacja rangi XP",
                    )

                if target_role_id is None:
                    continue

                if target_role is None:
                    logger.warning(
                        "XP role %s was not found.",
                        target_role_id,
                    )
                    continue

                if not already_has_target_role:
                    await member.add_roles(
                        target_role,
                        reason="Synchronizacja rangi XP",
                    )

                if (
                    is_real_rank_up
                    and send_notifications
                ):
                    await self._send_level_up_notification(
                        member=member,
                        rank=target_rank,
                        total_xp=total_xp,
                    )

            except discord.Forbidden:
                logger.exception(
                    "Bot cannot update XP roles for user %s.",
                    member.id,
                )

            except discord.HTTPException:
                logger.exception(
                    "Discord error while updating XP roles "
                    "for user %s.",
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
    
    def _get_voice_group_bonus(
        self,
        eligible_member_count: int,
    ) -> float:
        """
        Calculate the voice group bonus.

        Example with 20% per member above one:
        2 members = 0%
        3 members = 40%
        4 members = 60%
        """
        if not self.config["voice_group_bonus_enabled"]:
            return 0.0

        minimum_members = int(
            self.config["voice_group_bonus_min_members"]
        )

        if eligible_member_count < minimum_members:
            return 0.0

        bonus_per_member = float(
            self.config[
                "voice_group_bonus_per_member_above_one"
            ]
        )

        return (
            eligible_member_count - 1
        ) * bonus_per_member
    
    def _get_voice_channel_multiplier(
        self,
        channel_id: int,
    ) -> float:
        """
        Return the voice XP multiplier configured for a channel.

        Channels without their own entry use 1.0.
        """
        channel_multipliers = self.config[
            "voice_channel_xp_multipliers"
        ]

        multiplier = channel_multipliers.get(
            str(channel_id)
        )

        if multiplier is None:
            return 1.0

        return float(multiplier)

    async def _send_voice_group_bonus_notification(
        self,
        channel: discord.abc.GuildChannel,
        member_count: int,
        group_bonus: float,
    ) -> None:
        """
        Announce a newly activated or increased voice group bonus.
        """
        leveling_channel = self.bot.get_channel(
            LEVEL_UP_CHANNEL_ID
        )

        if leveling_channel is None:
            try:
                leveling_channel = await self.bot.fetch_channel(
                    LEVEL_UP_CHANNEL_ID
                )
            except (
                discord.NotFound,
                discord.Forbidden,
                discord.HTTPException,
            ):
                logger.exception(
                    "Could not find voice bonus notification channel %s.",
                    LEVEL_UP_CHANNEL_ID,
                )
                return

        bonus_percent = round(group_bonus * 100)

        embed = discord.Embed(
            title="Premia grupowa na kanale głosowym!",
            description=(
                f"Na kanale {channel.mention} znajduje się "
                f"**{member_count} aktywnych użytkowników**.\n\n"
                f"Aktywowano premię **+{bonus_percent}% XP** "
                "za aktywność głosową."
            ),
            color=0x57F287,
        )

        try:
            await leveling_channel.send(
                embed=embed,
                allowed_mentions=discord.AllowedMentions.none(),
            )

        except discord.Forbidden:
            logger.exception(
                "Bot cannot send voice group bonus notification "
                "in channel %s.",
                LEVEL_UP_CHANNEL_ID,
            )

        except discord.HTTPException:
            logger.exception(
                "Could not send voice bonus notification "
                "for voice channel %s.",
                channel.id,
            )

    async def _voice_xp_loop(self) -> None:
        """
        Grant voice XP to eligible users at the configured interval.
        """
        interval = int(
            self.config["voice_xp_interval_seconds"]
        )

        while not self.bot.is_closed():
            try:
                # Users must remain eligible until the next five-minute tick.
                await asyncio.sleep(interval)

                if not self.config["voice_xp_enabled"]:
                    continue

                await self._grant_voice_xp()

            except asyncio.CancelledError:
                break

            except Exception:
                logger.exception(
                    "Unexpected error in the Discord voice XP loop."
                )

    async def _grant_voice_xp(self) -> None:
        """
        Grant voice XP and apply group bonuses per voice channel.
        """
        rewarded_users = 0
        total_gained_xp = 0
        currently_scanned_channel_ids = set()

        voice_xp_min = int(
            self.config["voice_xp_min"]
        )

        voice_xp_max = int(
            self.config["voice_xp_max"]
        )

        for guild in self.bot.guilds:
            voice_channels = [
                *guild.voice_channels,
                *guild.stage_channels,
            ]

            for channel in voice_channels:
                currently_scanned_channel_ids.add(
                    channel.id
                )

                eligible_members = [
                    member
                    for member in channel.members
                    if self._is_active_voice_member(member)
                ]

                eligible_member_count = len(
                    eligible_members
                )

                # At least two eligible people are required for voice XP.
                if eligible_member_count == 0:
                    self.voice_group_bonus_state[
                        channel.id
                    ] = 0
                    continue

                group_bonus = self._get_voice_group_bonus(
                    eligible_member_count
                )

                previous_member_count = (
                    self.voice_group_bonus_state.get(
                        channel.id,
                        0,
                    )
                )

                previous_group_bonus = (
                    self._get_voice_group_bonus(
                        previous_member_count
                    )
                )

                # Notify only when a new or higher bonus becomes active.
                if (
                    group_bonus > 0
                    and group_bonus > previous_group_bonus
                ):
                    await self._send_voice_group_bonus_notification(
                        channel=channel,
                        member_count=eligible_member_count,
                        group_bonus=group_bonus,
                    )

                # Update state even when the bonus decreases.
                # This allows a future increase to trigger again.
                self.voice_group_bonus_state[
                    channel.id
                ] = eligible_member_count

                channel_multiplier = (
                    self._get_voice_channel_multiplier(
                        channel.id
                    )
                )

                for member in eligible_members:
                    try:
                        streak = await self._get_or_update_chatstreak(
                            discord_id=member.id,
                            username=member.name,
                        )

                        base_xp = random.randint(
                            voice_xp_min,
                            voice_xp_max,
                        )

                        gained_xp = self._apply_xp_multipliers(
                            base_xp=base_xp,
                            streak=streak,
                            member=member,
                            additional_bonus=group_bonus,
                        )

                        # Per-channel scaling is applied last,
                        # so it also scales the bonuses.
                        gained_xp = round(
                            gained_xp * channel_multiplier
                        )

                        await self._add_pending_xp(
                            discord_id=member.id,
                            username=member.name,
                            gained_xp=gained_xp,
                        )

                        rewarded_users += 1
                        total_gained_xp += gained_xp

                        logger.info(
                            "User %s gained %s voice XP. "
                            "Base: %s, streak: %s, "
                            "members: %s, group bonus: +%.0f%%, "
                            "channel: %s, channel multiplier: x%.2f.",
                            member.name,
                            gained_xp,
                            base_xp,
                            streak,
                            eligible_member_count,
                            group_bonus * 100,
                            channel.name,
                            channel_multiplier,
                        )

                    except Exception:
                        logger.exception(
                            "Could not grant voice XP to user %s.",
                            member.id,
                        )

        # Remove stale channels that no longer exist or are unavailable.
        stale_channel_ids = (
            set(self.voice_group_bonus_state)
            - currently_scanned_channel_ids
        )

        for channel_id in stale_channel_ids:
            self.voice_group_bonus_state.pop(
                channel_id,
                None,
            )

        if rewarded_users:
            logger.info(
                "Voice XP tick completed. Users: %s, total XP: %s.",
                rewarded_users,
                total_gained_xp,
            )
    def _has_xp_guild_tag(
        self,
        member: Optional[discord.Member],
    ) -> bool:
        """
        Return True when the member displays the configured Discord guild tag.
        """
        if member is None:
            return False

        if not self.config["guild_tag_bonus_enabled"]:
            return False

        primary_guild = member.primary_guild

        if primary_guild is None:
            return False

        member_tag = primary_guild.tag

        if not member_tag:
            return False

        configured_tag = str(
            self.config["guild_tag_bonus_tag"]
        )

        return member_tag.casefold() == configured_tag.casefold()

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

            guild_tag_bonus = 0.0

            if self._has_xp_guild_tag(target_member):
                guild_tag_bonus = float(
                    self.config["guild_tag_bonus_multiplier"]
                )

            streak_multiplier = (
                1.0
                + current_streak_bonus
                + guild_tag_bonus
            )

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

            if guild_tag_bonus > 0:
                multiplier_description = (
                    f"×{streak_multiplier:.2f}\n"
                    f"`+{guild_tag_bonus:.2f} za tag ANTY`"
                )
            else:
                multiplier_description = (
                    f"×{streak_multiplier:.2f}"
                )

            embed.add_field(
                name="Mnożnik XP",
                value=multiplier_description,
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

        guild_tag_bonus = 0.0

        if self._has_xp_guild_tag(target_member):
            guild_tag_bonus = float(
                self.config["guild_tag_bonus_multiplier"]
            )

        streak_multiplier = (
            1.0
            + current_streak_bonus
            + guild_tag_bonus
        )

        if guild_tag_bonus > 0:
            multiplier_description = (
                f"×{streak_multiplier:.2f}\n"
                f"`+{guild_tag_bonus:.2f} za tag ANTY`"
            )
        else:
            multiplier_description = (
                f"×{streak_multiplier:.2f}"
            )

        embed.add_field(
            name="Mnożnik XP",
            value=multiplier_description,
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

    @commands.command(name="importexternalxp")
    @commands.has_permissions(administrator=True)
    async def import_external_xp_command(
        self,
        ctx: commands.Context,
    ) -> None:
        """
        Manually import XP from the external JSON file.
        """
        await self.import_external_xp()

        await ctx.send(
            "Zakończono próbę importu zewnętrznego XP."
        )

    @commands.command(name="addxp")
    @commands.has_permissions(administrator=True)
    async def add_xp(
        self,
        ctx: commands.Context,
        discord_id: int,
        amount: int,
    ) -> None:
        """
        Add a specific amount of XP to a Discord user.

        Usage:
        $addxp 291836779495948288 500
        """
        if amount <= 0:
            await ctx.send(
                "Ilość XP musi być większa od zera."
            )
            return

        guild = ctx.guild
        member = (
            guild.get_member(discord_id)
            if guild is not None
            else None
        )

        username = (
            member.name
            if member is not None
            else str(discord_id)
        )

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
                        VALUES ($1, $2, $3, 0)
                        ON CONFLICT (chtr_id)
                        DO UPDATE SET
                            chtr_name = CASE
                                WHEN EXCLUDED.chtr_name
                                    = EXCLUDED.chtr_id::TEXT
                                    THEN chatters_information_discord.chtr_name
                                ELSE EXCLUDED.chtr_name
                            END,
                            xp = (
                                chatters_information_discord.xp
                                + EXCLUDED.xp
                            )
                        """,
                        discord_id,
                        username,
                        amount,
                    )

            if self.config["role_rewards_enabled"]:
                await self._update_roles_for_users(
                    {discord_id}
                )

            await ctx.send(
                f"Dodano **{amount:,} XP** użytkownikowi "
                f"<@{discord_id}>."
            )

            logger.info(
                "Administrator %s added %s XP to user %s.",
                ctx.author.id,
                amount,
                discord_id,
            )

        except Exception:
            logger.exception(
                "Could not manually add XP to user %s.",
                discord_id,
            )

            await ctx.send(
                "Nie udało się dodać XP. Sprawdź logi bota."
            )

    @commands.command(name="syncxproles")
    @commands.has_permissions(administrator=True)
    async def sync_xp_roles(
        self,
        ctx: commands.Context,
    ) -> None:
        guild = self.bot.get_guild(
            int(self.config["guild_id"])
        )

        if guild is None:
            await ctx.send(
                "Nie znaleziono skonfigurowanego serwera."
            )
            return

        try:
            members = [
                member
                async for member in guild.fetch_members(
                    limit=None
                )
            ]
        except discord.Forbidden:
            logger.exception(
                "Bot cannot fetch guild members."
            )

            await ctx.send(
                "Bot nie ma uprawnień do pobrania listy użytkowników."
            )
            return

        human_members = [
            member
            for member in members
            if not member.bot
        ]

        discord_ids = {
            member.id
            for member in human_members
        }

        await self._update_roles_for_users(
            discord_ids=discord_ids,
            send_notifications=False,
            xp_overrides={
                member.id: 0
                for member in human_members
            },
        )

        await ctx.send(
            f"Zsynchronizowano rangi XP dla "
            f"**{len(human_members)}** użytkowników serwera."
        )

async def setup(bot):
    await bot.add_cog(DiscordXP(bot))