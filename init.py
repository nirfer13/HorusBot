import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
import os
import asyncpg
from asyncpg.pool import create_pool

from globals.globalvariables import DebugMode

# token and other needed variables will be hidden in .env file
load_dotenv()
description = 'AlterMMO Discord Horus Bot, Development in progres'
intents = discord.Intents.default()
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
if __name__ == '__main__':
    for file in os.listdir("cogs"):
        if file.endswith(".py"):
            extension = file[:-3]
            try:
                bot.load_extension(f"cogs.{extension}")
                print(f"Loaded extension '{extension}'")
            except Exception as e:
                exception = f"{type(e).__name__}: {e}"
                print(f"Failed to load extension {extension}\n{exception}")
    try:
        bot.loop.run_until_complete(create_db_pool())
    except:
       print("Database unreachable.")
    
    bot.run(os.environ.get("TOKEN"))


