# -*- coding: utf-8 -*-
"""
Periodic runner that calls FunctionsTwitch methods every 30 minutes,
and exposes a manual !sync_twitch command.
IMPORTANT: Load the FunctionsTwitch extension BEFORE this one.
"""

import asyncio
from datetime import datetime, timedelta
from discord.ext import commands


class TwitchSync(commands.Cog, name="twitch_sync"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.task: asyncio.Task | None = None
        self._running = False

    @commands.Cog.listener()
    async def on_ready(self):
        print("[twitch_sync] Ready.")
        if not self._running:
            self._running = True
            self.task = self.bot.loop.create_task(self._loop())

    def cog_unload(self):
        if self.task and not self.task.done():
            self.task.cancel()

    def _get_ft(self):
        """Return FunctionsTwitch cog instance (or None if not loaded)."""
        ft = self.bot.get_cog("FunctionsTwitch")
        if ft is None:
            print("[twitch_sync] FunctionsTwitch cog not loaded. Load it before twitch_sync.")
        return ft

    async def _loop(self):
        print("[twitch_sync] Loop started.")
        last_minute_run = None  # type: int | None
        try:
            while True:
                now = datetime.utcnow() + timedelta(hours=2)  # align with your UTC+2 pattern
                minute = now.minute
                if minute % 30 == 0 and minute != last_minute_run:
                    ft = self._get_ft()
                    if ft:
                        try:
                            print("[twitch_sync] Twitch check - messages.")
                            await ft.assign_roles_messages()
                            print("[twitch_sync] Twitch check - watchtime.")
                            await ft.assign_roles_watchtime()
                            print("[twitch_sync] Twitch check - VIP/MOD.")
                            await ft.assign_roles_vip_mod()
                            print("[twitch_sync] Synchronization completed.")
                        except Exception as e:
                            print(f"[twitch_sync] Error while syncing: {e}")
                    last_minute_run = minute
                    await asyncio.sleep(35)  # skip the rest of this minute safely
                else:
                    await asyncio.sleep(5)
        except asyncio.CancelledError:
            print("[twitch_sync] Loop cancelled, exiting cleanly.")

    @commands.command(name="sync_twitch")
    @commands.guild_only()
    async def sync_twitch(self, ctx: commands.Context):
        """Manual trigger for Twitch role synchronization."""
        ft = self._get_ft()
        if not ft:
            await ctx.send("Brak coga FunctionsTwitch – załaduj go przed twitch_sync.")
            return

        await ctx.trigger_typing()
        try:
            print("[twitch_sync] Manual check - messages.")
            await ft.assign_roles_messages()
            print("[twitch_sync] Manual check - watchtime.")
            await ft.assign_roles_watchtime()
            print("[twitch_sync] Manual check - VIP/MOD.")
            await ft.assign_roles_vip_mod()
            await ctx.send("Role z Twitcha zsynchronizowane.")
        except Exception as e:
            await ctx.send(f"Wystąpił błąd podczas synchronizacji: {e}")


def setup(bot: commands.Bot) -> None:
    bot.add_cog(TwitchSync(bot))
