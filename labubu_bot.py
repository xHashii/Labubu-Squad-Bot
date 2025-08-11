import discord
from discord.ext import commands, tasks
import requests
import os
import json
import asyncio
import pymongo
import re
from flask import Flask
from threading import Thread
from datetime import datetime, timezone

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

# --- API HELPER FUNCTIONS ---
OFFICIAL_API_BASE_URL = 'https://gameinfo-ams.albiononline.com/api/gameinfo'
DATA_API_BASE_URL = 'https://www.albion-online-data.com/api/v2/stats'
ITEMS_JSON_URL = 'https://raw.githubusercontent.com/ao-data/ao-bin-dumps/master/formatted/items.json'

def search_player(name):
    """Searches for a player on the official EU server API."""
    response = requests.get(f"{OFFICIAL_API_BASE_URL}/search?q={name}")
    if response.status_code == 200 and response.json().get('players'):
        for player in response.json()['players']:
            if player.get('Name', '').lower() == name.lower():
                return player
        return response.json()['players'][0]
    return None

def get_player_events(player_id):
    """Gets recent kill/death events for a player from the official EU server API."""
    response = requests.get(f"{OFFICIAL_API_BASE_URL}/players/{player_id}/kills")
    if response.status_code == 200:
        return response.json()
    return []

def search_item_in_db(name):
    """Searches for an item in our local MongoDB item collection."""
    if db is None: return None
    items_collection = db['items']
    # Use a case-insensitive regex for an exact match
    query = {"friendly_name": {"$regex": f"^{re.escape(name)}$", "$options": "i"}}
    item = items_collection.find_one(query)
    return item

def get_item_prices(item_unique_name):
    """Gets item prices using the data project API."""
    response = requests.get(f"{DATA_API_BASE_URL}/prices/{item_unique_name}")
    if response.status_code == 200:
        return response.json()
    return None

def format_time_ago(timestamp_str):
    """Formats a UTC timestamp string into 'Xh Ym ago' format."""
    if timestamp_str is None: return ""
    last_update = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
    now = datetime.now(timezone.utc)
    delta = now - last_update
    
    hours, remainder = divmod(int(delta.total_seconds()), 3600)
    minutes, _ = divmod(remainder, 60)
    
    if hours > 0:
        return f"{hours}h {minutes}m ago"
    return f"{minutes}m ago"

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
    # ... (This entire task is unchanged and works correctly)
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
                embed = discord.Embed(title=title, description=f"**{event['Killer']['Name']}** defeated **{event['Victim']['Name']}**", color=color)
                kill_image_url = f"https://www.tools4albion.com/renderer/kill/{event_id}.png"
                embed.set_image(url=kill_image_url)
                embed.set_footer(text=f"Fame: {event['TotalVictimKillFame']:,} | Event ID: {event_id}")
                await channel.send(embed=embed)
                events_collection.insert_one({'_id': event_id})
        await asyncio.sleep(2)

@check_player_events.before_loop
async def before_check_player_events():
    await bot.wait_until_ready()

# --- BOT COMMANDS ---
@bot.command(name='price')
async def price(ctx, *, item_name: str):
    await ctx.send(f"🔍 Searching for `{item_name}` in the local database...")
    item_data = search_item_in_db(item_name)
    
    if not item_data:
        return await ctx.send(f"❌ Could not find an item named `{item_name}`. Please ensure it's spelled correctly or run `!updateitems` if you're the owner.")
        
    item_id = item_data['unique_name']
    found_name = item_data.get('friendly_name', item_id)
    
    prices = get_item_prices(item_id)
    if not prices:
        return await ctx.send(f"Could not fetch price data for `{found_name}`.")

    # --- NEW: Rebuilt Embed ---
    embed = discord.Embed(title=f"{found_name} / Europe Server 🌍", color=discord.Color.dark_blue())
    item_image_url = f"https://render.albiononline.com/v1/sprite/{item_id}?quality=1"
    embed.set_thumbnail(url=item_image_url)

    sell_orders = []
    buy_orders = []
    quality_map = {1: "Normal", 2: "Good", 3: "Outstanding", 4: "Excellent", 5: "Masterpiece"}
    eu_cities = ["Caerleon", "Thetford", "Fort Sterling", "Lymhurst", "Bridgewatch", "Martlock", "Brecilien"]

    for city_price in prices:
        city = city_price.get('city')
        if city in eu_cities:
            quality = quality_map.get(city_price.get('quality'), "N/A")
            # Sell Orders
            if city_price.get('sell_price_min') > 0:
                price = city_price['sell_price_min']
                age = format_time_ago(city_price.get('sell_price_min_date'))
                sell_orders.append(f"**{city} ({quality}):** {price:,} - *{age}*")
            # Buy Orders
            if city_price.get('buy_price_max') > 0:
                price = city_price['buy_price_max']
                age = format_time_ago(city_price.get('buy_price_max_date'))
                buy_orders.append(f"**{city} ({quality}):** {price:,} - *{age}*")

    if sell_orders:
        embed.add_field(name="Sell Orders", value="\n".join(sell_orders), inline=False)
    if buy_orders:
        embed.add_field(name="Buy Orders", value="\n".join(buy_orders), inline=False)

    if not sell_orders and not buy_orders:
        return await ctx.send(f"No recent price data found for `{found_name}` in major EU cities.")

    embed.set_footer(text="Data provided by The Albion Online Data Project.")
    await ctx.send(embed=embed)

@bot.command(name='updateitems', help='(Owner Only) Updates the local item database.')
@commands.is_owner()
async def updateitems(ctx):
    if db is None: return await ctx.send("❌ Cannot update items: Database is not connected.")
    
    await ctx.send("Starting item database update... This may take a few minutes. Please wait.")
    print("Item update process started by owner.")
    
    try:
        response = requests.get(ITEMS_JSON_URL)
        response.raise_for_status() # Will raise an error for bad status codes
        all_items = response.json()
        
        items_collection = db['items']
        update_count = 0
        
        for item in all_items:
            # We only care about items with an English name
            friendly_name = item.get('LocalizedNames', {}).get('EN-US')
            unique_name = item.get('UniqueName')
            
            if friendly_name and unique_name:
                # Use update_one with upsert to insert new items or update existing ones
                items_collection.update_one(
                    {'_id': unique_name},
                    {'$set': {'unique_name': unique_name, 'friendly_name': friendly_name}},
                    upsert=True
                )
                update_count += 1
        
        await ctx.send(f"✅ **Success!** Item database has been updated. Processed {update_count} items.")
        print(f"Item update process finished. Processed {update_count} items.")
        
    except Exception as e:
        await ctx.send(f"❌ **Error!** An exception occurred during the item update: {e}")
        print(f"Error during item update: {e}")

# ... (register, unregister, and guildinfo commands are unchanged) ...
@bot.command(name='register')
async def register(ctx, *, player_name: str):
    if db is None: return await ctx.send("❌ Command failed: The database is not connected.")
    player_data = search_player(player_name)
    if not player_data: return await ctx.send(f"❌ Could not find a player named `{player_name}` on the EU server.")
    db['registered_players'].update_one({'_id': ctx.author.id}, {'$set': {'player_data': player_data}}, upsert=True)
    await ctx.send(f"✅ **Success!** `{player_name}` is now being tracked.")
@bot.command(name='unregister')
async def unregister(ctx):
    if db is None: return await ctx.send("❌ Command failed: The database is not connected.")
    result = db['registered_players'].delete_one({'_id': ctx.author.id})
    if result.deleted_count > 0: await ctx.send("✅ **Removed!** You will no longer be tracked.")
    else: await ctx.send("❌ You are not currently registered.")
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
        bot.run(BOT_TKN)
    else:
        print("FATAL: BOT_TOKEN not found in bot thread.")

# --- SCRIPT EXECUTION STARTS HERE WHEN GUNICORN IMPORTS THE FILE ---
print("INFO: Script is being imported by Gunicorn, starting bot thread...")
bot_thread = Thread(target=run_bot)
bot_thread.start()
