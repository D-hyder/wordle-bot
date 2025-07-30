import discord
from discord.ext import commands
import re
import json
import os
from pathlib import Path
from flask import Flask
from threading import Thread

# === File Storage Setup ===
DATA_FILE = Path("/tmp/scores.json")  # Temporary storage on Render
INIT_FILE = Path("scores.json")       # Initial file (optional from repo)

# Initialize /tmp with initial scores if available
if not DATA_FILE.exists() and INIT_FILE.exists():
    DATA_FILE.write_text(INIT_FILE.read_text())

def load_scores():
    if not DATA_FILE.exists():
        DATA_FILE.write_text("{}")
    with open(DATA_FILE, "r") as f:
        return json.load(f)

def save_scores(scores):
    with open(DATA_FILE, "w") as f:
        json.dump(scores, f, separators=(',', ':'))

# === Discord Bot Setup ===
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"[INFO] Bot is ready as {bot.user}")

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

        if user_id not in scores:
            scores[user_id] = {"total": 0, "games": {}}

        # Initialize daily players list if missing
        if "players" not in scores:
            scores["players"] = []

        # Update tries for this Wordle
        if wordle_number in scores[user_id]["games"]:
            prev_tries = scores[user_id]["games"][wordle_number]
            scores[user_id]["total"] -= prev_tries

        scores[user_id]["games"][wordle_number] = tries
        scores[user_id]["total"] += tries

        save_scores(scores)

        print(f"[LOG] Recorded Wordle #{wordle_number} for {message.author} ({tries} tries)")

        await message.channel.send(
            f"Recorded Wordle #{wordle_number} — {tries} tries for {message.author.display_name}!"
        )

    # Only process commands if they start with prefix to avoid duplicates
    if message.content.startswith("!"):
        await bot.process_commands(message)

# === Leaderboard Command ===
@bot.command(name="leaderboard")
async def leaderboard(ctx):
    scores = load_scores()
    if not scores:
        await ctx.send("No scores yet.")
        return

    # Filter out the "players" list from scoring data
    player_scores = {k: v for k, v in scores.items() if k != "players"}

    sorted_scores = sorted(player_scores.items(), key=lambda x: x[1]["total"])
    leaderboard_lines = []
    for user_id, data in sorted_scores:
        user = await bot.fetch_user(int(user_id))
        games_played = len(data["games"])
        leaderboard_lines.append(
            f"**{user.display_name}** — {data['total']} tries ({games_played} games)"
        )

    leaderboard_text = "__**Wordle Leaderboard**__\n" + "\n".join(leaderboard_lines)
    print("[LOG] Leaderboard requested")
    await ctx.send(leaderboard_text)

# === Reset Week Command ===
@bot.command(name="resetweek")
@commands.has_permissions(administrator=True)
async def resetweek(ctx):
    scores = load_scores()
    scores = {"players": scores.get("players", [])}  # Keep players list, reset scores
    save_scores(scores)
    print("[LOG] Scores reset by admin command")
    await ctx.send("Scores have been reset for the new week!")

# === Daily Players Opt-In/Out ===
@bot.command(name="joinwordle")
async def joinwordle(ctx):
    scores = load_scores()
    if "players" not in scores:
        scores["players"] = []
    user_id = str(ctx.author.id)

    if user_id not in scores["players"]:
        scores["players"].append(user_id)
        save_scores(scores)
        await ctx.send(f"{ctx.author.display_name} joined daily Wordle tracking!")
    else:
        await ctx.send(f"{ctx.author.display_name}, you're already in the daily list!")

@bot.command(name="leavewordle")
async def leavewordle(ctx):
    scores = load_scores()
    if "players" in scores and str(ctx.author.id) in scores["players"]:
        scores["players"].remove(str(ctx.author.id))
        save_scores(scores)
        await ctx.send(f"{ctx.author.display_name} left daily Wordle tracking.")
    else:
        await ctx.send(f"{ctx.author.display_name}, you weren't in the daily list.")

@bot.command(name="players")
async def players(ctx):
    scores = load_scores()
    if "players" not in scores or not scores["players"]:
        await ctx.send("No daily players have joined yet.")
        return

    # Fetch display names of all opted-in players
    names = []
    for user_id in scores["players"]:
        user = await bot.fetch_user(int(user_id))
        names.append(user.display_name)

    await ctx.send("**Daily Wordle Players:**\n" + ", ".join(names))

# === Helper: Get missing users for given Wordle number ===
def get_missing_for(scores, wordle_num):
    return [
        user_id for user_id in scores["players"]
        if user_id not in scores or str(wordle_num) not in scores[user_id]["games"]
    ]

# === Missing Command (non-ping) ===
@bot.command(name="missing")
async def missing(ctx):
    scores = load_scores()
    if not scores or "players" not in scores or not scores["players"]:
        await ctx.send("No daily players have joined yet.")
        return

    # Get two most recent Wordle numbers
    all_numbers = sorted({
        int(num) for user_data in scores.values()
        if isinstance(user_data, dict) and "games" in user_data
        for num in user_data["games"].keys()
    }, reverse=True)

    if not all_numbers:
        await ctx.send("No Wordle numbers found.")
        return

    track_numbers = all_numbers[:2]  # Max 2 puzzles

    message_parts = []
    for wordle_num in reversed(track_numbers):  # Show older first
        missing_users = get_missing_for(scores, wordle_num)
        if missing_users:
            names = ", ".join([ (await bot.fetch_user(int(uid))).display_name for uid in missing_users ])
            message_parts.append(f"Missing Wordle #{wordle_num}: {names}")

    if not message_parts:
        await ctx.send("Everyone has submitted the last two Wordles!")
    else:
        await ctx.send("\n".join(message_parts))

# === PingMissing Command (ping version) ===
@bot.command(name="pingmissing")
@commands.has_permissions(administrator=True)
async def pingmissing(ctx):
    scores = load_scores()
    if not scores or "players" not in scores or not scores["players"]:
        await ctx.send("No daily players have joined yet.")
        return

    # Get two most recent Wordle numbers
    all_numbers = sorted({
        int(num) for user_data in scores.values()
        if isinstance(user_data, dict) and "games" in user_data
        for num in user_data["games"].keys()
    }, reverse=True)

    if not all_numbers:
        await ctx.send("No Wordle numbers found.")
        return

    track_numbers = all_numbers[:2]  # Max 2 puzzles

    message_parts = []
    for wordle_num in reversed(track_numbers):  # Show older first
        # Check how many have submitted this Wordle
        submitted_players = [
            uid for uid in scores["players"]
            if uid in scores and str(wordle_num) in scores[uid].get("games", {})
        ]

        # Only ping if 2 or more players have submitted
        if len(submitted_players) < 2:
            continue

        # Missing players for this Wordle
        missing_users = get_missing_for(scores, wordle_num)
        if missing_users:
            mentions = ", ".join(f"<@{uid}>" for uid in missing_users)
            message_parts.append(f"Missing Wordle #{wordle_num}: {mentions}")

    if not message_parts:
        await ctx.send("No one to ping (either no scores yet or fewer than 2 submissions).")
    else:
        await ctx.send("\n".join(message_parts))

# === Backup Command ===
@bot.command(name="backup")
@commands.has_permissions(administrator=True)
async def backup(ctx):
    # Send current scores.json file as attachment
    if not DATA_FILE.exists():
        await ctx.send("No scores file found.")
        return

    await ctx.send(
        "Here is the current scores backup file:",
        file=discord.File(str(DATA_FILE), filename="scores.json")
    )

# === Flask Health Check for Render ===
app = Flask(__name__)

@app.route("/")
def home():
    return "OK"

def run_flask():
    app.run(host="0.0.0.0", port=10000)  # Render health check port

if __name__ == "__main__":
    Thread(target=run_flask).start()
    TOKEN = os.getenv("TOKEN")
    if not TOKEN:
        print("Error: TOKEN environment variable not set")
    else:
        bot.run(TOKEN)
