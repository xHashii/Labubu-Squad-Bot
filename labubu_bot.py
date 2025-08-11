import discord
from discord.ext import commands, tasks
import requests
import os
import json
import asyncio
import pymongo
from flask import Flask
from threading import Thread

# --- CONFIGURATION (from Environment Variables) ---
BOT_TOKEN = os.environ.get('BOT_TOKEN')
ALBION_GUILD_ID = os.environ.get('ALBION_GUILD_ID')
MONGO_CONNECTION_STRING = os.environ.get('MONGO_CONNECTION_STRING')
GUILD_NAME = 'Labubu Squad'

raw_channel_id = os.environ.get('KILLBOARD_CHANNEL_ID')
KILLBOARD_CHANNEL_ID = int(raw_channel_id) if raw_channel_id else None

# --- FLASK WEB SERVER SETUP (for Gunicorn) ---
app = Flask(__name__)
@app.route('/')
def home():
    return "I'm alive!"

# --- DISCORD BOT SETUP ---
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)
db = None # Initialize db as None

# --- BOT EVENTS & TASKS ---
@bot.event
async def on_ready():
    """This runs AFTER the bot has successfully logged in."""
    print(f"--- BOT IS ONLINE AS {bot.user} ---")
    
    global db
    try:
        print("Attempting to connect to database...")
        mongo_client = pymongo.MongoClient(MONGO_CONNECTION_STRING, serverSelectionTimeoutMS=5000)
        mongo_client.admin.command('ismaster')
        db = mongo_client['labubu_bot_db']
        print("SUCCESS: Database connection established.")
        
        if KILLBOARD_CHANNEL_ID:
            check_player_events.start()
            print("Killboard task started.")
        else:
            print("WARNING: Killboard task not started (KILLBOARD_CHANNEL_ID not set).")
            
    except Exception as e:
        print(f"FATAL: Database connection failed: {e}")
        db = None

@tasks.loop(seconds=60)
async def check_player_events():
    # FIXED: Changed 'if not db' to 'if db is None'
    if db is None or not KILLBOARD_CHANNEL_ID: return
    channel = bot.get_channel(KILLBOARD_CHANNEL_ID)
    if not channel: return
    
    players_collection = db['registered_players']
    events_collection = db['processed_events']
    for player_doc in players_collection.find():
        player_data = player_doc['player_data']
        player_id = player_data['Id']
        player_name = player_data['Name']
        events = get_player_events(player_id)
        for event in events:
            event_id = str(event['EventId'])
            if events_collection.find_one({'_id': event_id}) is None:
                is_kill = event['Killer']['Id'] == player_id
                title = f"DEATH: {player_name} was killed!" if not is_kill else f"KILL: {player_name} got a kill!"
                color = discord.Color.red() if not is_kill else discord.Color.green()
                kill_image_url = f"https://www.tools4albion.com/renderer/kill/{event['EventId']}.png"
                embed = discord.Embed(title=title, description=f"**{event['Killer']['Name']}** defeated **{event['Victim']['Name']}**", color=color)
                embed.set_image(url=kill_image_url)
                embed.set_footer(text=f"Fame: {event['TotalVictimKillFame']:,}")
                await channel.send(embed=embed)
                events_collection.insert_one({'_id': event_id})
        await asyncio.sleep(2)

@check_player_events.before_loop
async def before_check_player_events():
    await bot.wait_until_ready()

# --- BOT COMMANDS (Now with added DB connection checks) ---
@bot.command(name='register')
async def register(ctx, *, player_name: str):
    # FIXED: Changed 'if not db' to 'if db is None'
    if db is None: return await ctx.send("❌ Command failed: The database is not connected.")
    player_data = search_player(player_name)
    if not player_data: return await ctx.send(f"❌ Could not find a player named `{player_name}`.")
    db['registered_players'].update_one({'_id': ctx.author.id}, {'$set': {'player_data': player_data}}, upsert=True)
    await ctx.send(f"✅ **Success!** `{player_name}` is now being tracked.")

@bot.command(name='unregister')
async def unregister(ctx):
    # FIXED: Changed 'if not db' to 'if db is None'
    if db is None: return await ctx.send("❌ Command failed: The database is not connected.")
    result = db['registered_players'].delete_one({'_id': ctx.author.id})
    if result.deleted_count > 0: await ctx.send("✅ **Removed!** You will no longer be tracked.")
    else: await ctx.send("❌ You are not currently registered.")

@bot.command(name='price')
async def price(ctx, *, item_name: str):
    await ctx.send(f"🔍 Searching for `{item_name}`...")
    item_data = search_item(item_name)
    if not item_data: return await ctx.send(f"❌ Could not find an item named `{item_name}`.")
    item_id = item_data['ItemId']
    found_name = item_data['Name']
    prices = get_item_prices(item_id)
    if not prices: return await ctx.send(f"Could not fetch price data for `{found_name}`.")
    embed = discord.Embed(title=f"Price Check: {found_name}", color=discord.Color.blue())
    item_image_url = f"https://www.tools4albion.com/renderer/item/{item_id}.png"
    embed.set_thumbnail(url=item_image_url)
    price_info = "\n".join([f"**{city_price['city']}:** {city_price['price']:,} silver" for city_price in prices])
    embed.add_field(name="Market Prices", value=price_info, inline=False)
    embed.set_footer(text="Prices are updated periodically by Tools4Albion.")
    await ctx.send(embed=embed)

@bot.command(name='guildinfo')
async def guildinfo(ctx):
    if not ALBION_GUILD_ID: return await ctx.send("The Albion Guild ID has not been configured by the bot owner.")
    embed = discord.Embed(title=f"Squad Info: {GUILD_NAME}", description="The official guild information for the Labubu Squad.", color=discord.Color.gold())
    embed.add_field(name="Guild Name", value=GUILD_NAME, inline=True)
    embed.add_field(name="Albion Guild ID", value=ALBION_GUILD_ID, inline=True)
    embed.set_footer(text="A guild of mischievous monsters.")
    await ctx.send(embed=embed)

# --- BOT STARTUP LOGIC ---
def run_bot():
    """This function runs in a separate thread and starts the bot."""
    if BOT_TOKEN:
        print("INFO: Bot thread started, attempting to log in...")
        bot.run(BOT_TOKEN)
    else:
        print("FATAL: BOT_TOKEN not found in bot thread.")

# --- SCRIPT EXECUTION STARTS HERE WHEN GUNICORN IMPORTS THE FILE ---
print("INFO: Script is being imported by Gunicorn, starting bot thread...")
bot_thread = Thread(target=run_bot)
bot_thread.start()
