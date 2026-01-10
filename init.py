import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
import os
import asyncpg
import asyncio
from asyncpg.pool import create_pool

from globals.globalvariables import DebugMode

# token and other needed variables will be hidden in .env file
load_dotenv()
description = 'AlterMMO Discord Horus Bot, Development in progres'
intents = discord.Intents.all()
intents.members = True

#commands prefix == $
bot = commands.Bot(
    command_prefix='$',
    description=description,
    intents=intents)

async def on_error(self, err, *args, **kwargs):
    raise

async def on_command_error(self, ctx, exc):
    raise getattr(exc, "original", exc)

async def create_db_pool():
    #Establishing the connection
    if DebugMode == False:
        bot.pg_con = await asyncpg.create_pool(os.environ.get("DATABASE_URL"))
    else:
        bot.pg_con = await asyncpg.create_pool(os.environ.get("HEROKU_POSTGRESQL_BRONZE_URL"))  

    print("Connected to database. Pool created.")

#loads cogs as extentions to bot
CORE_FIRST = [
    "functions_twitch",
    "functions_kick",
]

async def main():

    try:
        await create_db_pool()
    except Exception as e:
        print(f"Database unreachable. {type(e).__name__}: {e}")

    # Najpierw core
    for extension in CORE_FIRST:
        try:
            await bot.load_extension(f"cogs.{extension}")
            print(f"Loaded core extension '{extension}'")
        except Exception as e:
            print(f"Failed to load core extension {extension}\n{type(e).__name__}: {e}")

    # Potem reszta
    loaded = set(CORE_FIRST)
    for file in os.listdir("cogs"):
        if not file.endswith(".py"):
            continue
        extension = file[:-3]
        if extension in loaded:
            continue
        try:
            await bot.load_extension(f"cogs.{extension}")
            print(f"Loaded extension '{extension}'")
        except Exception as e:
            print(f"Failed to load extension {extension}\n{type(e).__name__}: {e}")

    

if __name__ == "__main__":
    asyncio.run(main())
    bot.run(os.environ.get("TOKEN"))
    print("Bot started successfully.")


