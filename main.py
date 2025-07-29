import discord
from discord.ext import commands
import re
import json
import os
from pathlib import Path

# Path for persistent storage on Render
DATA_DIR = Path("/var/data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
DATA_FILE = DATA_DIR / "scores.json"

# Load and save functions
def load_scores():
    if not DATA_FILE.exists():
        DATA_FILE.write_text("{}")
    with open(DATA_FILE, "r") as f:
        return json.load(f)

def save_scores(scores):
    with open(DATA_FILE, "w") as f:
        json.dump(scores, f, separators=(',', ':'))

# Discord bot setup
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"Bot is ready as {bot.user}")

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    # Detect Wordle message (e.g., "Wordle 1,402 3/6")
    match = re.search(r"Wordle\s+([\d,]+)\s+(\d|X)/6", message.content)
    if match:
        wordle_number = match.group(1).replace(",", "")
        tries = match.group(2)
        tries = 7 if tries == "X" else int(tries)

        scores = load_scores()
        user_id = str(message.author.id)

        # Initialize user if new
        if user_id not in scores:
            scores[user_id] = {"total": 0, "games": {}}

        # Update total (subtract previous if overwriting)
        if wordle_number in scores[user_id]["games"]:
            prev_tries = scores[user_id]["games"][wordle_number]
            scores[user_id]["total"] -= prev_tries

        scores[user_id]["games"][wordle_number] = tries
        scores[user_id]["total"] += tries

        save_scores(scores)

        await message.channel.send(
            f"Recorded Wordle #{wordle_number} — {tries} tries for {message.author.display_name}!"
        )

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

# Run bot
if __name__ == "__main__":
    TOKEN = os.getenv("TOKEN")
    if not TOKEN:
        print("Error: TOKEN environment variable not set")
    else:
        bot.run(TOKEN)
