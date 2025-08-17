# -*- coding: utf-8 -*-
"""
Periodic runner that calls FunctionsKick methods every 30 minutes,
and exposes a manual !sync_kick command.
IMPORTANT: Load the FunctionsKick extension BEFORE this one.
"""

import asyncio
from datetime import datetime, timedelta
from discord.ext import commands


class KickSync(commands.Cog, name="kick_sync"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.task: asyncio.Task | None = None
        self._running = False

    # ---- lifecycle ----
    @commands.Cog.listener()
    async def on_ready(self):
        print("[kick_sync] Ready.")
        if not self._running:
            self._running = True
            # start background loop once
            self.task = self.bot.loop.create_task(self._loop())

    def cog_unload(self):
        if self.task and not self.task.done():
            self.task.cancel()

    # ---- utils ----
    def _get_fk(self):
        """Return FunctionsKick cog instance (or None if not loaded)."""
        fk = self.bot.get_cog("FunctionsKick")
        if fk is None:
            print("[kick_sync] FunctionsKick cog not loaded. Load it before kick_sync.")
        return fk

    # ---- loop ----
    async def _loop(self):
        print("[kick_sync] Loop started.")
        # simple "edge-safe" stepper to avoid double-run in the same minute
        last_minute_run = None  # type: int | None
        try:
            while True:
                now = datetime.utcnow() + timedelta(hours=2)  # CEST-ish offset if you use UTC storage
                minute = now.minute
                if minute % 30 == 0 and minute != last_minute_run:
                    fk = self._get_fk()
                    if fk:
                        try:
                            print("[kick_sync] Kick check - messages.")
                            await fk.assign_roles_messages()
                            print("[kick_sync] Kick check - VIP/MOD.")
                            await fk.assign_roles_vip_mod()
                            print("[kick_sync] Synchronization completed.")
                        except Exception as e:
                            print(f"[kick_sync] Error while syncing: {e}")
                    last_minute_run = minute
                    # sleep ~35s to skip the minute's remainder safely
                    await asyncio.sleep(35)
                else:
                    await asyncio.sleep(5)
        except asyncio.CancelledError:
            print("[kick_sync] Loop cancelled, exiting cleanly.")

    # ---- command ----
    @commands.command(name="sync_kick")
    @commands.guild_only()
    async def sync_kick(self, ctx: commands.Context):
        """Manual trigger for Kick role synchronization."""
        fk = self._get_fk()
        if not fk:
            await ctx.send("Brak coga FunctionsKick – załaduj go przed kick_sync.")
            return

        await ctx.trigger_typing()
        try:
            print("[kick_sync] Manual check - messages.")
            await fk.assign_roles_messages()
            print("[kick_sync] Manual check - VIP/MOD.")
            await fk.assign_roles_vip_mod()
            await ctx.send("Role z Kicka zsynchronizowane.")
        except Exception as e:
            await ctx.send(f"Wystąpił błąd podczas synchronizacji: {e}")


def setup(bot: commands.Bot) -> None:
    bot.add_cog(KickSync(bot))
