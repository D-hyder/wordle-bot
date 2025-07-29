
from flask import Flask
import threading
import os
import discord
from discord.ext import commands
import re
import json
import os
from pathlib import Path

# Intents
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

DATA_FILE = Path("scores.json")

def load_scores():
    if not DATA_FILE.exists():
        DATA_FILE.write_text("{}")
    with open(DATA_FILE, "r") as f:
        return json.load(f)

def save_scores(scores):
    # Compact JSON to save memory
    with open(DATA_FILE, "w") as f:
        json.dump(scores, f, separators=(',', ':'))

@bot.event
async def on_ready():
    print(f"Bot is ready as {bot.user}")

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    # Match Wordle format (supports commas in number)
    match = re.search(r"Wordle\s+([\d,]+)\s+(\d|X)/6", message.content)
    if match:
        wordle_number = match.group(1).replace(",", "")  # e.g. 1,402 → 1402
        tries = match.group(2)
        tries = 7 if tries == "X" else int(tries)

        scores = load_scores()
        user_id = str(message.author.id)

        # Initialize user if new
        if user_id not in scores:
            scores[user_id] = {"total": 0, "games": {}}

        # Update total (subtract previous score if overwriting same day)
        if wordle_number in scores[user_id]["games"]:
            prev_tries = scores[user_id]["games"][wordle_number]
            scores[user_id]["total"] -= prev_tries

        scores[user_id]["games"][wordle_number] = tries
        scores[user_id]["total"] += tries

        save_scores(scores)

        await message.channel.send(f"Recorded Wordle #{wordle_number} — {tries} tries for {message.author.display_name}!")

    await bot.process_commands(message)

@bot.command(name="leaderboard")
async def leaderboard(ctx):
    scores = load_scores()
    if not scores:
        await ctx.send("No scores yet.")
        return

    sorted_scores = sorted(scores.items(), key=lambda x: x[1]["total"])
    leaderboard_lines = []
    for user_id, data in sorted_scores:
        user = await bot.fetch_user(int(user_id))
        leaderboard_lines.append(f"**{user.display_name}** — {data['total']} tries")

    leaderboard_text = "__**Wordle Leaderboard**__\n" + "\n".join(leaderboard_lines)
    await ctx.send(leaderboard_text)

@bot.command(name="resetweek")
@commands.has_permissions(administrator=True)
async def resetweek(ctx):
    save_scores({})
    await ctx.send("Scores have been reset for the new week!")

# Flask app setup (minimal for health checks only)
app = Flask(__name__)

@app.route('/')
def home():
    return "OK"

@app.route('/health')
def health():
    return "UP"

def run_flask():
    # Minimal Flask server - only for health checks
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)

if __name__ == "__main__":
    # Start Flask in a separate thread
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    
    # Start Discord bot (requires TOKEN environment variable)
    discord_token = os.getenv('TOKEN')
    if discord_token:
        bot.run(discord_token)
    else:
        print("Please set TOKEN environment variable")
        # Run just Flask if no Discord token
        run_flask()
