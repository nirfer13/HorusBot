import asyncio
import datetime as dt
import json
import logging
import random
import re
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

# Largest value of a PostgreSQL BIGINT (Discord ids, XP amounts).
MAX_BIGINT = 2 ** 63 - 1

# No XP total comes near this; a bigger deduction means the same: zero.
MAX_DEDUCTION = 2 ** 62

# XP transfers (an XP theft in the RPG bot), protocol version 1: one request
# file per theft, settled by Horus in one transaction, answered by a result
# file. Only "<32 lower-case hex>.json" is a request; the RPG bot's temporary
# "<id>.json.tmp" and anything else in the folder are ignored.
XP_TRANSFER_PROTOCOL_VERSION = 1
XP_TRANSFER_FILE_PATTERN = re.compile(r"[0-9a-f]{32}\.json")
XP_TRANSFER_DISCORD_ID_PATTERN = re.compile(r"[0-9]{1,20}")
XP_TRANSFER_REQUESTS_FOLDER_NAME = "xp_transfers"
XP_TRANSFER_RESULTS_FOLDER_NAME = "xp_transfer_results"
XP_TRANSFER_REJECTED_FOLDER_NAME = "rejected"

# A request is about 150 bytes. Anything far bigger is garbage (a broken
# write, a bug) and is rejected without being read whole, so one huge file
# cannot exhaust the memory or stop the queue.
XP_TRANSFER_MAX_REQUEST_BYTES = 64 * 1024

# How long a settlement waits for a row lock (the victim's or thief's row
# held by another session, or the same id claimed by a second Horus) before
# it gives up until the next pass. Without it a forgotten "BEGIN; SELECT ...
# FOR UPDATE" would stop the external XP loop for as long as it is held.
XP_TRANSFER_LOCK_TIMEOUT_MILLISECONDS = 5000

# PostgreSQL error codes the settlement reacts to.
SQLSTATE_UNDEFINED_TABLE = "42P01"
SQLSTATE_UNDEFINED_COLUMN = "42703"
SQLSTATE_LOCK_NOT_AVAILABLE = "55P03"

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

    # XP transfer folders (XP thefts settled by Horus). None means the
    # folder next to external_xp_file: "xp_transfers" for the requests,
    # "xp_transfer_results" for the results. Settled only while
    # external_xp_enabled is on.
    "external_xp_transfers_dir": None,
    "external_xp_transfer_results_dir": None,

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

        for folder_key in (
            "external_xp_transfers_dir",
            "external_xp_transfer_results_dir",
        ):
            folder_value = config.get(folder_key)

            if folder_value is not None and (
                not isinstance(folder_value, str)
                or not folder_value.strip()
            ):
                raise ValueError(
                    f"{folder_key} must be a folder path or null."
                )

        (
            requests_folder,
            results_folder,
        ) = DiscordXP._resolve_xp_transfer_folders(config)

        def same_folder(first: Path, second: Path) -> bool:
            return os.path.normcase(
                os.path.abspath(first)
            ) == os.path.normcase(os.path.abspath(second))

        # Results are named like requests ("<id>.json"): in the request
        # folder (or its rejected folder) Horus would read its own results
        # as new requests, or overwrite rejected requests with them.
        if same_folder(requests_folder, results_folder) or same_folder(
            requests_folder / XP_TRANSFER_REJECTED_FOLDER_NAME,
            results_folder,
        ):
            raise ValueError(
                "external_xp_transfer_results_dir must not be the "
                "XP transfer request folder or its rejected folder."
            )

    async def cog_load(self) -> None:
        """
        Create the tables this cog owns when the extension is loaded.

        Never raises: a missing table only stops XP transfers from being
        settled (the requests wait in their folder); everything else in
        this cog, the external XP file import included, keeps working.
        """
        if self.config["external_xp_enabled"]:
            await self._ensure_xp_transfer_table()

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

                await self._external_xp_pass()

            except asyncio.CancelledError:
                break

            except Exception:
                logger.exception(
                    "Unexpected error in the external XP import loop."
                )

    async def _external_xp_pass(self) -> None:
        """
        One pass of the external XP loop: the summary file, then the XP
        transfers.

        The summary file goes first so that fight XP the RPG bot wrote
        before a theft is already saved when the theft is settled. Each
        part has its own error handling: a broken transfer request never
        stops the fight XP import, and the other way round. Each part
        holds external_xp_lock for its whole work, so they never
        interleave.
        """
        try:
            await self.import_external_xp()
        except Exception:
            logger.exception(
                "Unexpected error while importing the external XP file."
            )

        try:
            await self.settle_xp_transfers()
        except Exception:
            logger.exception(
                "Unexpected error while settling XP transfers."
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
                    if processing_file in self._imported_external_files():
                        # Already saved to the database; only the file
                        # could not be removed then. Never import twice.
                        await self._remove_imported_external_file(
                            processing_file
                        )
                        continue

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

    def _imported_external_files(self) -> set:
        """
        Processing files whose XP is already in the database but which
        could not be deleted yet (kept in memory until they are gone).
        """
        imported = getattr(
            self,
            "_imported_external_xp_files",
            None,
        )

        if imported is None:
            imported = set()
            self._imported_external_xp_files = imported

        return imported

    async def _remove_imported_external_file(
        self,
        processing_file: Path,
    ) -> bool:
        """
        Delete a processing file after its XP was saved.

        On Windows the delete fails while another program has the file
        open (a backup of the folder, an antivirus scan). Such a reader is
        done within moments, so the delete is retried. A file that still
        cannot be deleted is remembered, so that it is not imported again.
        """
        for attempt in range(5):
            try:
                processing_file.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                if attempt < 4:
                    await asyncio.sleep(0.2)
                    continue

                logger.exception(
                    "External XP from %s was saved, but the file could "
                    "not be removed. It will not be imported again.",
                    processing_file,
                )
                self._imported_external_files().add(processing_file)
                return False

            self._imported_external_files().discard(processing_file)
            return True

        return False

    async def _process_external_xp_file(
        self,
        processing_file: Path,
    ) -> None:
        """
        Validate and save XP from one claimed processing file.

        A positive amount is added to the user. A negative amount is a
        deduction (the RPG bot writes one for the victim of an XP theft):
        it is taken from the user's XP, never below zero, and never
        creates a row for a user who has none.

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

            if discord_id <= 0 or discord_id > MAX_BIGINT:
                logger.warning(
                    "Ignoring invalid Discord ID: %s.",
                    discord_id,
                )
                continue

            if gained_xp == 0:
                continue

            # Signed: a negative amount is a deduction.
            xp_by_user[discord_id] += gained_xp

        gains_by_user = {
            discord_id: xp_change
            for discord_id, xp_change in xp_by_user.items()
            if xp_change > 0
        }

        # Clamped: a deduction bigger than any XP total gives the same
        # result (zero) and always fits the database type. Without it one
        # absurd number would block the whole file for ever.
        deductions_by_user = {
            discord_id: max(xp_change, -MAX_DEDUCTION)
            for discord_id, xp_change in xp_by_user.items()
            if xp_change < 0
        }

        if not gains_by_user and not deductions_by_user:
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

        for discord_id, gained_xp in gains_by_user.items():
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

        if deductions_by_user:
            # XP earned in chat is kept in memory for a few minutes before
            # it is saved. Save it first: otherwise a deduction would be
            # floored against too low a total and the unsaved XP would
            # come back on top of it.
            try:
                await self.flush_pending_xp()
            except Exception:
                logger.exception(
                    "Could not save pending XP before a deduction. "
                    "The deduction is applied to the saved XP."
                )

        # Users whose XP really changed in the database.
        changed_user_ids = set(gains_by_user.keys())
        deducted_user_ids = set()

        try:
            async with self.bot.pg_con.acquire() as connection:
                # Gains and deductions of one file are saved together
                # or not at all.
                async with connection.transaction():
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

                    for discord_id, xp_change in (
                        deductions_by_user.items()
                    ):
                        # No INSERT: a user without a row has no XP to
                        # lose. GREATEST keeps the XP from going below 0.
                        updated_id = await connection.fetchval(
                            """
                            UPDATE chatters_information_discord
                            SET xp = GREATEST(xp + $2::BIGINT, 0)
                            WHERE chtr_id = $1::BIGINT
                            RETURNING chtr_id
                            """,
                            discord_id,
                            xp_change,
                        )

                        if updated_id is not None:
                            deducted_user_ids.add(discord_id)

            changed_user_ids.update(deducted_user_ids)

            logger.info(
                "Imported external XP for %s users. Total XP: %s. "
                "Deducted XP from %s of %s users. Total deduction: %s.",
                len(gains_by_user),
                sum(gains_by_user.values()),
                len(deducted_user_ids),
                len(deductions_by_user),
                -sum(deductions_by_user.values()),
            )

        except Exception:
            logger.exception(
                "Could not import external XP from %s. "
                "The processing file was preserved for retry.",
                processing_file,
            )
            return

        # The XP is saved. A failing delete must not make the file look
        # "not imported": it would be applied a second time.
        await self._remove_imported_external_file(processing_file)

        if self.config["role_rewards_enabled"]:
            # A deduction can move a user to a lower rank: the role sync
            # handles both directions (no notification on the way down).
            await self._update_roles_for_users(
                changed_user_ids
            )

    # ---------- XP transfers (XP theft), protocol version 1 ----------
    #
    # The RPG bot writes one request per theft into the request folder;
    # Horus moves min(amount, victim's XP) from the victim to the thief in
    # ONE transaction, writes a result and only then deletes the request.
    # The table discord_xp_transfers makes it exactly once: its PRIMARY KEY
    # is the request id, so a request seen again (a crash after the
    # commit, a file that could not be deleted, a second Horus process, a
    # copy restored from a backup) never moves XP a second time.

    @staticmethod
    def _resolve_xp_transfer_folders(
        config: Dict[str, Any],
    ) -> Tuple[Path, Path]:
        """
        Return the request and result folders of the XP transfers.

        Both default to folders next to external_xp_file (the RPG bot's
        data folder), so the feature works without new config keys.
        """
        data_folder = Path(config["external_xp_file"]).parent

        requests_folder = config.get("external_xp_transfers_dir")
        results_folder = config.get("external_xp_transfer_results_dir")

        return (
            Path(requests_folder)
            if requests_folder
            else data_folder / XP_TRANSFER_REQUESTS_FOLDER_NAME,
            Path(results_folder)
            if results_folder
            else data_folder / XP_TRANSFER_RESULTS_FOLDER_NAME,
        )

    def _xp_transfers_awaiting_cleanup(self) -> Dict[str, str]:
        """
        Requests whose result this process already wrote but which are
        still in the request folder: {request_id: "delete" | "reject"}.

        The next pass only retries the delete (or the move into the
        rejected folder) and does not write the result again: the RPG bot
        may already have read and removed it, and a second copy would tell
        the thief about the same theft twice.
        """
        awaiting_cleanup = getattr(
            self,
            "_xp_transfer_cleanup_by_id",
            None,
        )

        if awaiting_cleanup is None:
            awaiting_cleanup = {}
            self._xp_transfer_cleanup_by_id = awaiting_cleanup

        return awaiting_cleanup

    def _log_xp_transfer_problem_once(
        self,
        problem_key: str,
        message: str,
        *args: Any,
        level: int = logging.WARNING,
    ) -> None:
        """
        Log a problem that would otherwise repeat at every pass (every
        10 seconds in production) only once per process.
        """
        logged_problems = getattr(
            self,
            "_xp_transfer_logged_problems",
            None,
        )

        if logged_problems is None:
            logged_problems = set()
            self._xp_transfer_logged_problems = logged_problems

        if problem_key in logged_problems:
            return

        logged_problems.add(problem_key)
        logger.log(level, message, *args, exc_info=True)

    async def _ensure_xp_transfer_table(self) -> bool:
        """
        Create discord_xp_transfers when it does not exist yet.

        Idempotent. On failure it returns False and logs once per process;
        it is tried again (silently) at the next pass that has requests,
        so a database that was down at start-up does not disable the
        transfers until a restart.
        """
        if getattr(self, "_xp_transfer_table_ready", False):
            return True

        try:
            async with self.bot.pg_con.acquire() as connection:
                await connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS discord_xp_transfers (
                        transfer_id TEXT PRIMARY KEY,
                        from_id BIGINT NOT NULL,
                        to_id BIGINT NOT NULL,
                        requested BIGINT NOT NULL,
                        moved BIGINT NOT NULL,
                        applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        reported_at TIMESTAMPTZ
                    )
                    """
                )

                # reported_at is not in the protocol's column list (the RPG
                # bot never reads this table). A table created without it
                # gets it here; ALTER only when it is missing, because ALTER
                # TABLE locks the table even when there is nothing to do.
                has_reported_at = await connection.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_schema = current_schema()
                            AND table_name = 'discord_xp_transfers'
                            AND column_name = 'reported_at'
                    )
                    """
                )

                if not has_reported_at:
                    await connection.execute(
                        """
                        ALTER TABLE discord_xp_transfers
                        ADD COLUMN IF NOT EXISTS reported_at TIMESTAMPTZ
                        """
                    )

        except Exception:
            if not getattr(
                self,
                "_xp_transfer_table_failure_logged",
                False,
            ):
                self._xp_transfer_table_failure_logged = True
                logger.exception(
                    "Could not create the table discord_xp_transfers. "
                    "XP transfers are not settled until it exists; the "
                    "requests wait in their folder. The external XP file "
                    "import is not affected."
                )
            return False

        if getattr(self, "_xp_transfer_table_failure_logged", False):
            logger.info(
                "The table discord_xp_transfers is ready now. "
                "XP transfers are settled again."
            )

        self._xp_transfer_table_ready = True
        self._xp_transfer_table_failure_logged = False
        return True

    def _list_xp_transfer_requests(
        self,
        requests_folder: Path,
    ) -> List[Path]:
        """
        Return the request files, oldest first.

        Oldest first because two thefts from one victim depend on the
        order: the first one takes what is there, the second the rest.
        A missing folder is simply "nothing to do" and is not logged.
        """
        try:
            with os.scandir(requests_folder) as folder_entries:
                entries = list(folder_entries)
        except FileNotFoundError:
            return []
        except OSError:
            self._log_xp_transfer_problem_once(
                f"scan:{requests_folder}",
                "Could not read the XP transfer folder %s.",
                requests_folder,
            )
            return []

        sortable_requests = []

        for entry in entries:
            if not XP_TRANSFER_FILE_PATTERN.fullmatch(entry.name):
                continue

            try:
                if not entry.is_file():
                    continue

                modified_at = entry.stat().st_mtime
            except OSError:
                # Gone meanwhile, or not readable yet: the next pass.
                continue

            sortable_requests.append(
                (modified_at, entry.name, Path(entry.path))
            )

        sortable_requests.sort()

        return [
            request_path
            for _, _, request_path in sortable_requests
        ]

    @staticmethod
    def _parse_xp_transfer_discord_id(
        raw_discord_id: Any,
    ) -> Optional[int]:
        """
        Return a Discord id given as a string of digits, else None.
        """
        if not isinstance(raw_discord_id, str):
            return None

        if not XP_TRANSFER_DISCORD_ID_PATTERN.fullmatch(raw_discord_id):
            return None

        discord_id = int(raw_discord_id)

        if discord_id <= 0 or discord_id > MAX_BIGINT:
            return None

        return discord_id

    @classmethod
    def _parse_xp_transfer_request(
        cls,
        request_id: str,
        raw_content: bytes,
    ) -> Tuple[Dict[str, Any], Optional[str]]:
        """
        Validate one request file.

        Returns (request, None) for a valid request, or (fields for the
        error result, short reason) for an invalid one.
        """
        result_fields: Dict[str, Any] = {
            "id": request_id,
            "from": None,
            "to": None,
            "requested": None,
        }

        if len(raw_content) > XP_TRANSFER_MAX_REQUEST_BYTES:
            return result_fields, "too large"

        try:
            # utf-8-sig: a byte order mark written by some editor or tool
            # must not make a valid request invalid.
            data = json.loads(raw_content.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            # RecursionError: deeply nested brackets ("[[[[..."), which
            # json.loads does not report as a ValueError.
            return result_fields, "not JSON"

        if not isinstance(data, dict):
            return result_fields, "not a JSON object"

        raw_from = data.get("from")
        raw_to = data.get("to")
        raw_amount = data.get("amount")

        if isinstance(raw_from, str):
            result_fields["from"] = raw_from

        if isinstance(raw_to, str):
            result_fields["to"] = raw_to

        # bool is an int in Python: True must not be an amount of 1.
        amount_is_integer = (
            isinstance(raw_amount, int)
            and not isinstance(raw_amount, bool)
        )

        if amount_is_integer:
            result_fields["requested"] = raw_amount

        version = data.get("version")

        if (
            not isinstance(version, int)
            or isinstance(version, bool)
            or version != XP_TRANSFER_PROTOCOL_VERSION
        ):
            return result_fields, "unsupported version"

        if data.get("id") != request_id:
            return result_fields, "id does not match the file name"

        from_id = cls._parse_xp_transfer_discord_id(raw_from)

        if from_id is None:
            return result_fields, "bad from id"

        to_id = cls._parse_xp_transfer_discord_id(raw_to)

        if to_id is None:
            return result_fields, "bad to id"

        if from_id == to_id:
            return result_fields, "from equals to"

        if not amount_is_integer:
            return result_fields, "bad amount"

        if raw_amount <= 0:
            return result_fields, "amount must be positive"

        return {
            "id": request_id,
            "from_id": from_id,
            "to_id": to_id,
            # More than any XP total means the same (everything the victim
            # has) and always fits the BIGINT columns.
            "requested": min(raw_amount, MAX_BIGINT),
        }, None

    @staticmethod
    def _format_xp_transfer_time(
        moment: dt.datetime,
    ) -> str:
        """
        UTC ISO-8601 text for the result files.
        """
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=dt.timezone.utc)

        return moment.astimezone(dt.timezone.utc).isoformat(
            timespec="seconds"
        )

    @staticmethod
    def _write_xp_transfer_result(
        results_folder: Path,
        result: Dict[str, Any],
    ) -> None:
        """
        Write "<id>.json" atomically: a temporary file in the same folder,
        then os.replace. The RPG bot never sees a half-written result.

        The temporary name never matches the request / result pattern and
        is unique, so two Horus processes cannot write into the same one.
        Raises OSError when the result cannot be written.
        """
        results_folder.mkdir(parents=True, exist_ok=True)

        final_path = results_folder / f"{result['id']}.json"
        temporary_path = results_folder / (
            f"{result['id']}.json.{uuid.uuid4().hex}.tmp"
        )

        try:
            with temporary_path.open("w", encoding="utf-8") as file:
                # ensure_ascii: an error result echoes the request's
                # "from" / "to", which may hold a lone surrogate ("\ud800"
                # is valid JSON) that cannot be encoded as UTF-8. Escaped,
                # every string can be written; the JSON reads the same.
                json.dump(result, file, ensure_ascii=True)
                file.flush()
                os.fsync(file.fileno())

            os.replace(temporary_path, final_path)

        except BaseException:
            try:
                temporary_path.unlink()
            except OSError:
                pass
            raise

    async def _delete_xp_transfer_request(
        self,
        request_path: Path,
    ) -> bool:
        """
        Delete a request whose result is written.

        Retried like the external XP processing file (a backup or an
        antivirus may hold the file open for a moment). A request that
        still cannot be deleted stays in _xp_transfers_awaiting_cleanup:
        it is never settled again anyway (the table knows it), and its
        result is not written again.
        """
        for attempt in range(5):
            try:
                request_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                if attempt < 4:
                    await asyncio.sleep(0.2)
                    continue

                self._log_xp_transfer_problem_once(
                    f"delete:{request_path}",
                    "The XP transfer %s is settled and its result written, "
                    "but the request could not be removed. It is not "
                    "settled again; the delete is retried.",
                    request_path,
                )
                return False

            return True

        return False

    async def _reject_xp_transfer_request(
        self,
        request_path: Path,
        result_fields: Dict[str, Any],
        reason: str,
        results_folder: Path,
    ) -> None:
        """
        Answer an invalid request with an error result and move it into
        the rejected folder, so that it is not retried for ever.

        Nothing is applied. The error result comes first: when it cannot be
        written the request stays where it is and is answered later.
        """
        request_id = result_fields["id"]
        awaiting_cleanup = self._xp_transfers_awaiting_cleanup()

        if awaiting_cleanup.get(request_id) != "reject":
            error_result = {
                "version": XP_TRANSFER_PROTOCOL_VERSION,
                "id": request_id,
                "from": result_fields["from"],
                "to": result_fields["to"],
                "requested": result_fields["requested"],
                "moved": 0,
                "applied": self._format_xp_transfer_time(
                    dt.datetime.now(dt.timezone.utc)
                ),
                "error": reason,
            }

            try:
                self._write_xp_transfer_result(
                    results_folder,
                    error_result,
                )
            except OSError:
                self._log_xp_transfer_problem_once(
                    f"result:{request_id}",
                    "Could not write the error result of the rejected XP "
                    "transfer %s. The request stays for the next pass.",
                    request_id,
                )
                return

            awaiting_cleanup[request_id] = "reject"

            logger.warning(
                "Rejected XP transfer request %s: %s.",
                request_path,
                reason,
            )

        rejected_folder = (
            request_path.parent / XP_TRANSFER_REJECTED_FOLDER_NAME
        )

        try:
            rejected_folder.mkdir(parents=True, exist_ok=True)
            os.replace(
                request_path,
                rejected_folder / request_path.name,
            )
        except FileNotFoundError:
            pass
        except OSError:
            self._log_xp_transfer_problem_once(
                f"reject:{request_path}",
                "Could not move the rejected XP transfer request %s into "
                "%s. The move is retried.",
                request_path,
                rejected_folder,
            )
            return

        awaiting_cleanup.pop(request_id, None)

    async def settle_xp_transfers(self) -> None:
        """
        Settle the XP transfer requests written by the RPG bot.

        Holds external_xp_lock, so a settlement never interleaves with the
        import of the external XP file. The XP roles are synchronised after
        the lock is released: Discord calls must not hold up the next
        import, and a role failure must not touch a finished settlement.
        """
        async with self.external_xp_lock:
            changed_user_ids = await self._settle_xp_transfer_requests()

        if not changed_user_ids:
            return

        if not self.config["role_rewards_enabled"]:
            return

        try:
            # A theft can move the victim down a rank and the thief up:
            # the role sync handles both directions.
            await self._update_roles_for_users(changed_user_ids)
        except Exception:
            logger.exception(
                "XP transfers were settled, but the XP roles of %s "
                "could not be updated.",
                sorted(changed_user_ids),
            )

    async def _settle_xp_transfer_requests(self) -> set:
        """
        One pass over the request folder. Returns the users whose XP
        changed. Must be called with external_xp_lock held.
        """
        requests_folder, results_folder = (
            self._resolve_xp_transfer_folders(self.config)
        )

        request_paths = self._list_xp_transfer_requests(requests_folder)

        if not request_paths:
            return set()

        valid_requests = []

        for request_path in request_paths:
            try:
                request = await self._read_xp_transfer_request(
                    request_path,
                    results_folder,
                )
            except Exception:
                # Whatever one strange file does, the requests after it
                # are still settled. The file stays and is tried again;
                # logged once, not at every pass.
                self._log_xp_transfer_problem_once(
                    f"request:{request_path}",
                    "Unexpected error while reading the XP transfer "
                    "request %s. It is skipped and tried again at the "
                    "next pass.",
                    request_path,
                    level=logging.ERROR,
                )
                continue

            if request is not None:
                valid_requests.append(request)

        if not valid_requests:
            return set()

        if not await self._ensure_xp_transfer_table():
            return set()

        # XP earned in chat is kept in memory for a few minutes before it
        # is saved. Save it first: the victim's unsaved XP can then be
        # taken, and it does not come back on top of the theft later.
        try:
            await self.flush_pending_xp()
        except Exception:
            logger.exception(
                "Could not save pending XP before settling XP transfers. "
                "They are settled against the saved XP."
            )

        changed_user_ids = set()

        for request in valid_requests:
            try:
                changed_user_ids.update(
                    await self._settle_xp_transfer(
                        request,
                        results_folder,
                    )
                )
            except Exception as error:
                # Nothing is deleted: the request is tried again at the
                # next pass, and the table keeps a commit from being
                # applied twice.
                sqlstate = getattr(error, "sqlstate", None)

                if sqlstate == SQLSTATE_LOCK_NOT_AVAILABLE:
                    # A row is held by another session. The requests after
                    # this one wait as well: they may take from the same
                    # victim, and the oldest must be settled first.
                    self._log_xp_transfer_problem_once(
                        f"lock:{request['id']}",
                        "The XP transfer %s waits for a row lock held by "
                        "another database session. It and the requests "
                        "after it are tried again at the next pass.",
                        request["id"],
                    )
                    break

                self._log_xp_transfer_problem_once(
                    f"settle:{request['id']}:{sqlstate or type(error).__name__}",
                    "Could not settle the XP transfer %s. "
                    "The request was preserved for retry.",
                    request["id"],
                    level=logging.ERROR,
                )

                if sqlstate in (
                    SQLSTATE_UNDEFINED_TABLE,
                    SQLSTATE_UNDEFINED_COLUMN,
                ):
                    # The table vanished while Horus runs (a database
                    # restored from an older backup): create it again at
                    # the next pass. Every other request would fail the
                    # same way now, so they wait for that pass too.
                    self._xp_transfer_table_ready = False
                    break

        return changed_user_ids

    async def _read_xp_transfer_request(
        self,
        request_path: Path,
        results_folder: Path,
    ) -> Optional[Dict[str, Any]]:
        """
        Read and validate one request file.

        Returns the valid request, or None when there is nothing to settle
        now (only a delete was due, the file is gone or not readable yet,
        or it was invalid and is rejected here).
        """
        request_id = request_path.name[: -len(".json")]
        awaiting_cleanup = self._xp_transfers_awaiting_cleanup()

        if awaiting_cleanup.get(request_id) == "delete":
            # Settled and answered by this process; only the delete
            # failed. Never settle or answer it again.
            if await self._delete_xp_transfer_request(request_path):
                awaiting_cleanup.pop(request_id, None)
            return None

        try:
            # One byte over the limit is enough to know it is too large;
            # the rest is never read.
            with request_path.open("rb") as request_file:
                raw_content = request_file.read(
                    XP_TRANSFER_MAX_REQUEST_BYTES + 1
                )
        except FileNotFoundError:
            return None
        except OSError:
            self._log_xp_transfer_problem_once(
                f"read:{request_path}",
                "Could not read the XP transfer request %s. "
                "It is tried again at the next pass.",
                request_path,
            )
            return None

        request, reason = self._parse_xp_transfer_request(
            request_id,
            raw_content,
        )

        if reason is not None:
            await self._reject_xp_transfer_request(
                request_path,
                request,
                reason,
                results_folder,
            )
            return None

        request["path"] = request_path
        return request

    async def _settle_xp_transfer(
        self,
        request: Dict[str, Any],
        results_folder: Path,
    ) -> set:
        """
        Settle one valid request, answer it and remove it.

        Returns the users whose XP changed (for the role sync).
        """
        request_id = request["id"]
        request_path = request["path"]

        settlement = await self._apply_xp_transfer_in_database(request)

        changed_user_ids = set()

        if settlement["newly_settled"]:
            logger.info(
                "Settled XP transfer %s: from %s to %s, requested %s, "
                "moved %s.",
                request_id,
                settlement["from_id"],
                settlement["to_id"],
                settlement["requested"],
                settlement["moved"],
            )

            if settlement["moved"] > 0:
                changed_user_ids = {
                    settlement["from_id"],
                    settlement["to_id"],
                }

        else:
            if (
                settlement["from_id"] != request["from_id"]
                or settlement["to_id"] != request["to_id"]
                or settlement["requested"] != request["requested"]
            ):
                logger.warning(
                    "The XP transfer request %s differs from the transfer "
                    "already settled under this id. Nothing is applied; "
                    "the settled transfer stands.",
                    request_id,
                )

            if settlement["reported"]:
                # Answered and removed before (a copy restored from a
                # backup, or a request that could not be deleted before a
                # restart). The RPG bot has its result already.
                logger.info(
                    "XP transfer %s was settled and answered before. "
                    "The repeated request is removed.",
                    request_id,
                )
                await self._delete_xp_transfer_request(request_path)
                return changed_user_ids

            if not request_path.exists():
                # Another Horus process finished it meanwhile.
                return changed_user_ids

            logger.info(
                "XP transfer %s was settled before but not answered. "
                "Writing its result again.",
                request_id,
            )

        result = {
            "version": XP_TRANSFER_PROTOCOL_VERSION,
            "id": request_id,
            "from": str(settlement["from_id"]),
            "to": str(settlement["to_id"]),
            "requested": settlement["requested"],
            "moved": settlement["moved"],
            "applied": self._format_xp_transfer_time(
                settlement["applied_at"]
            ),
        }

        try:
            self._write_xp_transfer_result(results_folder, result)
        except OSError:
            # The XP is moved; the request stays, so the result is written
            # at a later pass (the table keeps it from being applied again).
            self._log_xp_transfer_problem_once(
                f"result:{request_id}",
                "The XP transfer %s is settled, but its result could not "
                "be written. The request stays for the next pass.",
                request_id,
            )
            return changed_user_ids

        awaiting_cleanup = self._xp_transfers_awaiting_cleanup()
        awaiting_cleanup[request_id] = "delete"

        await self._mark_xp_transfer_reported(request_id)

        if await self._delete_xp_transfer_request(request_path):
            awaiting_cleanup.pop(request_id, None)

        return changed_user_ids

    async def _apply_xp_transfer_in_database(
        self,
        request: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Claim and apply one transfer in ONE transaction.
        """
        guild = self.bot.get_guild(
            int(self.config["guild_id"])
        )

        member = (
            guild.get_member(request["to_id"])
            if guild is not None
            else None
        )

        thief_name = (
            member.name
            if member is not None
            else str(request["to_id"])
        )

        async with self.bot.pg_con.acquire() as connection:
            async with connection.transaction():
                # LOCAL: only this transaction; the pooled connection goes
                # back with its normal setting. A lock that is not free in
                # time raises LockNotAvailableError, the transaction rolls
                # back and the request waits for the next pass, so the
                # loop (and the summary-file import) goes on.
                await connection.execute(
                    "SET LOCAL lock_timeout = "
                    f"{int(XP_TRANSFER_LOCK_TIMEOUT_MILLISECONDS)}"
                )

                return await self._apply_xp_transfer(
                    connection,
                    request,
                    thief_name,
                )

    async def _claim_xp_transfer(
        self,
        connection,
        request: Dict[str, Any],
    ) -> Optional[dt.datetime]:
        """
        Insert the transfer id. Returns applied_at, or None when the id is
        already there (settled before, or by another Horus process: its
        insert makes this one wait until it commits or rolls back).
        """
        return await connection.fetchval(
            """
            INSERT INTO discord_xp_transfers (
                transfer_id,
                from_id,
                to_id,
                requested,
                moved
            )
            VALUES ($1, $2, $3, $4, 0)
            ON CONFLICT (transfer_id) DO NOTHING
            RETURNING applied_at
            """,
            request["id"],
            request["from_id"],
            request["to_id"],
            request["requested"],
        )

    async def _apply_xp_transfer(
        self,
        connection,
        request: Dict[str, Any],
        thief_name: str,
    ) -> Dict[str, Any]:
        """
        The body of the settlement transaction.
        """
        applied_at = await self._claim_xp_transfer(connection, request)

        if applied_at is None:
            stored = await connection.fetchrow(
                """
                SELECT
                    from_id,
                    to_id,
                    requested,
                    moved,
                    applied_at,
                    reported_at
                FROM discord_xp_transfers
                WHERE transfer_id = $1
                """,
                request["id"],
            )

            if stored is None:
                raise RuntimeError(
                    f"XP transfer {request['id']} conflicts, "
                    "but its row is missing."
                )

            return {
                "newly_settled": False,
                "reported": stored["reported_at"] is not None,
                "from_id": int(stored["from_id"]),
                "to_id": int(stored["to_id"]),
                "requested": int(stored["requested"]),
                "moved": int(stored["moved"]),
                "applied_at": stored["applied_at"],
            }

        from_id = request["from_id"]
        to_id = request["to_id"]

        # Both rows locked, always in the same order (two transfers in
        # opposite directions must not deadlock). The thief may have no
        # row yet: there is nothing to lock then.
        locked_rows = await connection.fetch(
            """
            SELECT
                chtr_id,
                xp
            FROM chatters_information_discord
            WHERE chtr_id = ANY($1::BIGINT[])
            ORDER BY chtr_id
            FOR UPDATE
            """,
            [from_id, to_id],
        )

        xp_by_user = {
            int(row["chtr_id"]): int(row["xp"])
            for row in locked_rows
        }

        victim_xp = xp_by_user.get(from_id)
        moved = 0

        if victim_xp is not None and victim_xp > 0:
            thief_xp = max(xp_by_user.get(to_id, 0), 0)

            # The thief gets exactly what the victim loses. The last term
            # keeps the thief's total inside BIGINT: without it a failing
            # UPDATE would block this request for ever.
            moved = min(
                request["requested"],
                victim_xp,
                MAX_BIGINT - thief_xp,
            )

        if moved > 0:
            await connection.execute(
                """
                UPDATE chatters_information_discord
                SET xp = xp - $2::BIGINT
                WHERE chtr_id = $1::BIGINT
                """,
                from_id,
                moved,
            )

            # Like the external XP gain: a thief without a row gets one;
            # a stored name is not replaced by the bare id.
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
                to_id,
                thief_name,
                moved,
            )

            await connection.execute(
                """
                UPDATE discord_xp_transfers
                SET moved = $2::BIGINT
                WHERE transfer_id = $1
                """,
                request["id"],
                moved,
            )

        return {
            "newly_settled": True,
            "reported": False,
            "from_id": from_id,
            "to_id": to_id,
            "requested": request["requested"],
            "moved": moved,
            "applied_at": applied_at,
        }

    async def _mark_xp_transfer_reported(
        self,
        request_id: str,
    ) -> None:
        """
        Remember in the table that the result was written.

        A request seen again after that (a copy restored from a backup,
        or one that could not be deleted before a restart) is then only
        removed, not answered a second time. A failure here is harmless:
        at worst the result is written once more.
        """
        try:
            await self.bot.pg_con.execute(
                """
                UPDATE discord_xp_transfers
                SET reported_at = now()
                WHERE transfer_id = $1
                    AND reported_at IS NULL
                """,
                request_id,
            )
        except Exception:
            self._log_xp_transfer_problem_once(
                f"report:{request_id}",
                "Could not mark the XP transfer %s as answered.",
                request_id,
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
        Manually import XP from the external JSON file and settle the
        waiting XP transfers.
        """
        await self.import_external_xp()
        await self.settle_xp_transfers()

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