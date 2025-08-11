import discord
import os

# --- MINIMAL TEST CONFIGURATION ---
BOT_TOKEN = os.environ.get('BOT_TOKEN')

# --- MINIMAL BOT SETUP ---
intents = discord.Intents.default() # Basic intents are enough for login
bot = discord.Client(intents=intents)

@bot.event
async def on_ready():
    # If this message prints in your Render logs, the bot has successfully logged in.
    print("--- SUCCESS! ---")
    print(f'Minimal test bot has logged in as {bot.user}')
    print("The problem is in the database connection or the background task.")
    print("You can now revert to your full code and check your MONGO_CONNECTION_STRING.")
    print("--- SUCCESS! ---")

# --- RUN THE MINIMAL BOT ---
if __name__ == "__main__":
    print("INFO: Starting minimal test bot...")
    if BOT_TOKEN:
        try:
            bot.run(BOT_TOKEN)
        except discord.errors.LoginFailure:
            print("FATAL: Login failed. The BOT_TOKEN is 100% incorrect or invalid.")
        except Exception as e:
            print(f"FATAL: An unexpected error occurred: {e}")
    else:
        print("FATAL: BOT_TOKEN environment variable not found.")
