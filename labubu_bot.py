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
ALBION_GUILD_ID = os.environ.get('ALBION_GUILD_ID') # Renamed for clarity
MONGO_CONNECTION_STRING = os.environ.get('MONGO_CONNECTION_STRING')
GUILD_NAME = 'Labubu Squad' # Hardcoded guild name for the info command

# Safely get the Killboard Channel ID
raw_channel_id = os.environ.get('KILLBOARD_CHANNEL_ID')
KILLBOARD_CHANNEL_ID = int(raw_channel_id) if raw_channel_id else None

# --- WEB SERVER (for Render Health Check) ---
app = Flask(__name__)
@app.route('/')
def home():
    return "I'm alive!"

def run_web_server():
    app.run(host='0.0.0.0', port=10000)

# --- BOT SETUP ---
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# --- MONGODB DATABASE CONNECTION ---
try:
    mongo_client = pymongo.MongoClient(MONGO_CONNECTION_STRING)
    db = mongo_client['labubu_bot_db']
    players_collection = db['registered_players']
    events_collection = db['processed_events']
    print("Successfully connected to MongoDB Atlas.")
except Exception as e:
    print(f"Error connecting to MongoDB: {e}")
    db = None

# --- API HELPER FUNCTIONS (ALL INCLUDED) ---
API_BASE_URL = 'https://www.tools4albion.com/api/gameinfo'

def search_player(name):
    response = requests.get(f"{API_BASE_URL}/search?search={name}")
    if response.status_code == 200 and response.json().get('players'):
        return response.json()['players'][0]
    return None

def get_player_events(player_id):
    response = requests.get(f"{API_BASE_URL}/events/player/{player_id}?limit=10")
    if response.status_code == 200:
        return response.json()
    return []

def search_item(name):
    response = requests.get(f"{API_BASE_URL}/search?search={name}")
    if response.status_code == 200 and response.json().get('items'):
        return response.json()['items'][0]
    return None

def get_item_prices(item_id):
    response = requests.get(f"{API_BASE_URL}/prices/{item_id}")
    if response.status_code == 200:
        return response.json()
    return None

# --- BOT EVENTS & TASKS ---
@bot.event
async def on_ready():
    print(f'Bot is logged in as {bot.user}')
    if db and KILLBOARD_CHANNEL_ID:
        check_player_events.start()
        print('Killboard tracking is now active.')
    else:
        if not db: print("Could not start killboard tracking due to DB connection issue.")
        if not KILLBOARD_CHANNEL_ID: print("Could not start killboard tracking because KILLBOARD_CHANNEL_ID is not set.")
    print('------')

@tasks.loop(seconds=60)
async def check_player_events():
    if not KILLBOARD_CHANNEL_ID: return
    channel = bot.get_channel(KILLBOARD_CHANNEL_ID)
    if not channel or not db: return

    for player_doc in players_collection.find():
        player_data = player_doc['player_data']
        player_id = player_data['Id']
        player_name = player_data['Name']
        events = get_player_events(player_id)
        
        for event in events:
            event_id = str(event['EventId'])
            if events_collection.find_one({'_id': event_id}) is None:
                is_kill = event['Killer']['Id'] == player_id
                title = f"DEATH: {player_name} was killed!"
                color = discord.Color.red()
                if is_kill:
                    title = f"KILL: {player_name} got a kill!"
                    color = discord.Color.green()

                kill_image_url = f"https://www.tools4albion.com/renderer/kill/{event['EventId']}.png"
                embed = discord.Embed(title=title, description=f"**{event['Killer']['Name']}** defeated **{event['Victim']['Name']}**", color=color)
                embed.set_image(url=kill_image_url)
                embed.set_footer(text=f"Fame: {event['TotalVictimKillFame']:,}")
                
                await channel.send(embed=embed)
                events_collection.insert_one({'_id': event_id})
        
        await asyncio.sleep(2)

# --- BOT COMMANDS (ALL INCLUDED) ---
@bot.command(name='register', help='Register your Albion Online name for killboard tracking.')
async def register(ctx, *, player_name: str):
    if not db: return await ctx.send("Database connection is not available.")
    player_data = search_player(player_name)
    if not player_data:
        return await ctx.send(f"❌ Could not find a player named `{player_name}`.")
    
    players_collection.update_one({'_id': ctx.author.id}, {'$set': {'player_data': player_data}}, upsert=True)
    await ctx.send(f"✅ **Success!** `{player_data['Name']}` is now being tracked.")

@bot.command(name='unregister', help='Stop tracking your Albion name on the killboard.')
async def unregister(ctx):
    if not db: return await ctx.send("Database connection is not available.")
    result = players_collection.delete_one({'_id': ctx.author.id})
    if result.deleted_count > 0:
        await ctx.send("✅ **Removed!** You will no longer be tracked.")
    else:
        await ctx.send("❌ You are not currently registered.")

@bot.command(name='price', help='Check item prices. Usage: !price <item_name>')
async def price(ctx, *, item_name: str):
    await ctx.send(f"🔍 Searching for `{item_name}`...")
    item_data = search_item(item_name)
    if not item_data:
        return await ctx.send(f"❌ Could not find an item named `{item_name}`.")
        
    item_id = item_data['ItemId']
    found_name = item_data['Name']
    prices = get_item_prices(item_id)
    if not prices:
        return await ctx.send(f"Could not fetch price data for `{found_name}`.")

    embed = discord.Embed(title=f"Price Check: {found_name}", color=discord.Color.blue())
    item_image_url = f"https://www.tools4albion.com/renderer/item/{item_id}.png"
    embed.set_thumbnail(url=item_image_url)
    price_info = "\n".join([f"**{city_price['city']}:** {city_price['price']:,} silver" for city_price in prices])
    embed.add_field(name="Market Prices", value=price_info, inline=False)
    embed.set_footer(text="Prices are updated periodically by Tools4Albion.")
    await ctx.send(embed=embed)

@bot.command(name='guildinfo', help='Shows information about the guild.')
async def guildinfo(ctx):
    if not ALBION_GUILD_ID:
        return await ctx.send("The Albion Guild ID has not been configured by the bot owner.")
    embed = discord.Embed(
        title=f"Squad Info: {GUILD_NAME}",
        description="The official guild information for the Labubu Squad.",
        color=discord.Color.gold()
    )
    embed.add_field(name="Guild Name", value=GUILD_NAME, inline=True)
    embed.add_field(name="Albion Guild ID", value=ALBION_GUILD_ID, inline=True)
    embed.set_footer(text="A guild of mischievous monsters.")
    await ctx.send(embed=embed)

# --- RUN THE BOT ---
if __name__ == "__main__":
    print("INFO: Script execution started.")
    if not all([BOT_TOKEN, MONGO_CONNECTION_STRING, KILLBOARD_CHANNEL_ID, ALBION_GUILD_ID]):
        print("FATAL: One or more required environment variables are missing. Please check all variables.")
    else:
        print("INFO: All required environment variables are present.")
        web_thread = Thread(target=run_web_server)
        web_thread.start()
        print("INFO: Flask web server thread started.")
        try:
            print("INFO: Attempting to run bot.run(BOT_TOKEN)...")
            bot.run(BOT_TOKEN)
        except Exception as e:
            print(f"FATAL: An unexpected error occurred while starting the bot: {e}")
