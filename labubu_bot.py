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
from PIL import Image, ImageDraw, ImageFont
from io import BytesIO

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
DATA_API_BASE_URL = 'https://europe.albion-online-data.com/api/v2/stats'
ITEMS_JSON_URL = 'https://raw.githubusercontent.com/ao-data/ao-bin-dumps/master/formatted/items.json'
ITEM_RENDER_URL = 'https://render.albiononline.com/v1/sprite'

# --- IMAGE GENERATION ---
def generate_kill_image(event):
    """Generates a kill image from scratch using official item sprites."""
    BG_COLOR = (47, 49, 54, 255)
    TEXT_COLOR = (220, 221, 222)
    FAME_COLOR = (255, 170, 56)
    ITEM_SIZE = 90
    PADDING = 10
    slots = ["Head", "Armor", "Shoes", "Cape", "MainHand", "OffHand"]
    
    img = Image.new('RGBA', (ITEM_SIZE * 6 + PADDING * 5, ITEM_SIZE * 2 + PADDING * 3 + 40), BG_COLOR)
    draw = ImageDraw.Draw(img)
    
    try:
        font = ImageFont.truetype("arial.ttf", 15)
    except IOError:
        font = ImageFont.load_default()

    for i, player_type in enumerate(['Killer', 'Victim']):
        player = event[player_type]
        y_offset = i * (ITEM_SIZE + PADDING)
        ip = f"{player.get('AverageItemPower', 0):.0f} IP"
        draw.text((PADDING, y_offset + 5), player['Name'], font=font, fill=TEXT_COLOR)
        draw.text((PADDING, y_offset + 25), ip, font=font, fill=TEXT_COLOR)
        
        for j, slot in enumerate(slots):
            item = player.get('Equipment', {}).get(slot)
            if item:
                item_id = f"{item['Type']}"
                if item.get('Enchantment', 0) > 0:
                    item_id += f"@{item.get('Enchantment')}"
                item_url = f"{ITEM_RENDER_URL}/{item_id}?quality={item.get('Quality', 1)}"
                try:
                    response = requests.get(item_url, stream=True)
                    if response.status_code == 200:
                        item_img = Image.open(BytesIO(response.content)).convert("RGBA")
                        item_img = item_img.resize((ITEM_SIZE, ITEM_SIZE))
                        img.paste(item_img, (j * (ITEM_SIZE + PADDING), y_offset + 40), item_img)
                except Exception:
                    pass

    fame = event['TotalVictimKillFame']
    draw.text((PADDING, ITEM_SIZE * 2 + PADDING * 2 + 10), f"Fame: {fame:,}", font=font, fill=FAME_COLOR)

    buffer = BytesIO()
    img.save(buffer, format='PNG')
    buffer.seek(0)
    return buffer

# --- Other Helper Functions ---
def search_player(name):
    response = requests.get(f"{OFFICIAL_API_BASE_URL}/search?q={name}")
    if response.status_code == 200 and response.json().get('players'):
        for player in response.json()['players']:
            if player.get('Name', '').lower() == name.lower(): return player
        return response.json()['players'][0]
    return None

def get_player_events(player_id):
    response = requests.get(f"{OFFICIAL_API_BASE_URL}/events/player/{player_id}/kills")
    if response.status_code == 200: return response.json()
    return []

def parse_item_query(query):
    query = query.lower()
    tier_match = re.search(r't([1-8])(\.[1-4])?', query)
    tier, enchantment = (None, 0)
    if tier_match:
        tier = int(tier_match.group(1))
        if tier_match.group(2): enchantment = int(tier_match.group(2)[1:])
        query = query.replace(tier_match.group(0), "").strip()
    quality_map = {"normal": 1, "good": 2, "outstanding": 3, "excellent": 4, "masterpiece": 5}
    quality_name, quality_num = (None, None)
    for q_name, q_num in quality_map.items():
        if q_name in query:
            quality_name, quality_num = q_name.capitalize(), q_num
            query = query.replace(q_name, "").strip()
            break
    return {"base_name": query, "tier": tier, "enchantment": enchantment, "quality_name": quality_name, "quality_num": quality_num}

def search_base_item_in_db(base_name):
    """FIXED: Searches for a base item using the new 'base_friendly_name' field."""
    if db is None: return None
    items_collection = db['items']
    # Use a case-insensitive regex for an EXACT match on the base name.
    query = {"base_friendly_name": {"$regex": f"^{re.escape(base_name)}$", "$options": "i"}}
    item = items_collection.find_one(query)
    return item

def get_item_prices(item_unique_name, quality=None):
    params = {'qualities': quality} if quality else {}
    response = requests.get(f"{DATA_API_BASE_URL}/prices/{item_unique_name}", params=params)
    if response.status_code == 200: return response.json()
    return None

def format_time_ago(timestamp_str):
    if timestamp_str is None: return ""
    last_update = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
    now = datetime.now(timezone.utc)
    delta = now - last_update
    hours, remainder = divmod(int(delta.total_seconds()), 3600)
    minutes, _ = divmod(remainder, 60)
    return f"{hours}h {minutes}m ago" if hours > 0 else f"{minutes}m ago"

async def _initialize_item_database():
    """FIXED: Populates the database with a searchable 'base_friendly_name'."""
    if db is None: return
    items_collection = db['items']
    if items_collection.count_documents({}) < 1000:
        print("Item database is empty. Populating now...")
        try:
            response = requests.get(ITEMS_JSON_URL)
            response.raise_for_status()
            all_items = response.json()
            
            items_to_insert = []
            prefixes_to_strip = ["Elder's ", "Grandmaster's ", "Master's ", "Expert's ", "Adept's ", "Journeyman's ", "Novice's "]
            
            for item in all_items:
                friendly_name = item.get('LocalizedNames', {}).get('EN-US')
                unique_name = item.get('UniqueName')
                if friendly_name and unique_name:
                    base_friendly_name = friendly_name
                    for prefix in prefixes_to_strip:
                        if base_friendly_name.startswith(prefix):
                            base_friendly_name = base_friendly_name[len(prefix):]
                            break
                    
                    items_to_insert.append({
                        '_id': unique_name,
                        'unique_name': unique_name,
                        'friendly_name': friendly_name,
                        'base_friendly_name': base_friendly_name # The new searchable field
                    })
            
            if items_to_insert:
                items_collection.delete_many({})
                items_collection.insert_many(items_to_insert)
                print(f"SUCCESS: Item database populated with {len(items_to_insert)} items.")
        except Exception as e:
            print(f"FATAL: Could not populate item database: {e}")

# --- BOT EVENTS & TASKS ---
@bot.event
async def on_ready():
    print(f"--- BOT IS ONLINE AS {bot.user} ---")
    global db
    try:
        print("Attempting to connect to database...")
        mongo_client = pymongo.MongoClient(MONGO_CONNECTION_STRING, serverSelectionTimeoutMS=5000)
        mongo_client.admin.command('ismaster')
        db = mongo_client['labubu_bot_db']
        print("SUCCESS: Database connection established.")
        await _initialize_item_database()
        if KILLBOARD_CHANNEL_ID:
            check_player_events.start()
            print("Killboard task started.")
    except Exception as e:
        print(f"FATAL: Database connection failed: {e}")
        db = None

@tasks.loop(seconds=60)
async def check_player_events():
    if db is None or not KILLBOARD_CHANNEL_ID: return
    channel = bot.get_channel(KILLBOARD_CHANNEL_ID)
    if not channel: return
    
    players_collection = db['registered_players']
    events_collection = db['processed_events']
    for player_doc in players_collection.find():
        player_id = player_doc['player_data']['Id']
        events = get_player_events(player_id)
        for event in events:
            event_id = str(event['EventId'])
            if events_collection.find_one({'_id': event_id}) is None:
                killer, victim = event['Killer'], event['Victim']
                title = f"{killer['Name']} killed {victim['Name']}"
                killboard_url = f"https://albiononline.com/en/killboard/kill/{event_id}"
                embed = discord.Embed(title=title, url=killboard_url, color=discord.Color.from_rgb(47, 49, 54))
                
                killer_guild = f"[{killer.get('GuildName', 'N/A')}]" + (f" [{killer.get('AllianceName')}]" if killer.get('AllianceName') else "")
                victim_guild = f"[{victim.get('GuildName', 'N/A')}]" + (f" [{victim.get('AllianceName')}]" if victim.get('AllianceName') else "")
                participants = [p['Name'] for p in event.get('Participants', []) if p['Id'] != killer['Id']]
                
                desc = f"**Killer :** {killer['Name']} - {killer_guild}\n**Victim :** {victim['Name']} - {victim_guild}"
                if participants: desc += f"\n**Participants :** {', '.join(participants)}"
                embed.description = desc
                
                embed.set_thumbnail(url="https://assets.albiononline.com/assets/images/killboard/kill__event.png")
                
                image_buffer = generate_kill_image(event)
                file = discord.File(fp=image_buffer, filename="kill.png")
                embed.set_image(url="attachment://kill.png")
                
                event_time = datetime.fromisoformat(event['TimeStamp'].replace('Z', '+00:00'))
                embed.timestamp = event_time
                embed.set_footer(text="Server : Europe")
                
                await channel.send(embed=embed, file=file)
                events_collection.insert_one({'_id': event_id})
        await asyncio.sleep(3)

@check_player_events.before_loop
async def before_check_player_events():
    await bot.wait_until_ready()

# --- BOT COMMANDS ---
@bot.command(name='price')
async def price(ctx, *, query: str):
    await ctx.send(f"🔍 Processing query for `{query}`...")
    parsed_query = parse_item_query(query)
    base_item_data = search_base_item_in_db(parsed_query['base_name'])
    if not base_item_data:
        return await ctx.send(f"❌ Could not find a base item matching `{parsed_query['base_name']}`.")
    
    base_unique_name = base_item_data['unique_name']
    # Use the tier from the query, or default to T4 if not specified
    tier_to_use = parsed_query['tier'] if parsed_query['tier'] is not None else 4
    final_unique_name = re.sub(r'T[1-8]', f"T{tier_to_use}", base_unique_name)
    
    if parsed_query['enchantment'] > 0:
        final_unique_name += f"@{parsed_query['enchantment']}"

    prices = get_item_prices(final_unique_name, quality=parsed_query['quality_num'])
    if not prices: return await ctx.send(f"Could not fetch price data for `{final_unique_name}`.")

    title_parts = []
    enchant_str = f".{parsed_query['enchantment']}" if parsed_query['enchantment'] > 0 else ""
    title_parts.append(f"T{tier_to_use}{enchant_str}")
    if parsed_query['quality_name']: title_parts.append(parsed_query['quality_name'])
    title_parts.append(base_item_data['friendly_name'])
    
    embed = discord.Embed(title=f"{' '.join(title_parts)} / Europe Server 🌍", color=discord.Color.dark_blue())
    embed.set_thumbnail(url=f"{ITEM_RENDER_URL}/{final_unique_name}?quality=1")

    sell_orders, buy_orders = [], []
    quality_map = {1: "Normal", 2: "Good", 3: "Outstanding", 4: "Excellent", 5: "Masterpiece"}
    eu_cities = ["Caerleon", "Thetford", "Fort Sterling", "Lymhurst", "Bridgewatch", "Martlock", "Brecilien"]

    for p in prices:
        if p.get('city') in eu_cities:
            q = quality_map.get(p.get('quality'), "N/A")
            if p.get('sell_price_min') > 0: sell_orders.append(f"**{p['city']} ({q}):** {p['sell_price_min']:,} - *{format_time_ago(p.get('sell_price_min_date'))}*")
            if p.get('buy_price_max') > 0: buy_orders.append(f"**{p['city']} ({q}):** {p['buy_price_max']:,} - *{format_time_ago(p.get('buy_price_max_date'))}*")

    if sell_orders: embed.add_field(name="Sell Orders", value="\n".join(sell_orders), inline=False)
    if buy_orders: embed.add_field(name="Buy Orders", value="\n".join(buy_orders), inline=False)
    if not sell_orders and not buy_orders: return await ctx.send(f"No recent price data found for `{final_unique_name}`.")
    embed.set_footer(text="Data provided by The Albion Online Data Project.")
    await ctx.send(embed=embed)

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
    if BOT_TOKEN:
        print("INFO: Bot thread started, attempting to log in...")
        bot.run(BOT_TOKEN)
    else:
        print("FATAL: BOT_TOKEN not found in bot thread.")

print("INFO: Script is being imported by Gunicorn, starting bot thread...")
bot_thread = Thread(target=run_bot)
bot_thread.start()
