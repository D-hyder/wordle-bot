import discord
from discord.ext import commands
from discord.ext import tasks
import re
import json
import os
from pathlib import Path
from flask import Flask
from threading import Thread
from datetime import datetime, timedelta, date
import pytz

# === File Paths ===
DATA_FILE = Path("/tmp/scores.json")
INIT_FILE = Path("scores.json")

if not DATA_FILE.exists() and INIT_FILE.exists():
    DATA_FILE.write_text(INIT_FILE.read_text())

# === Constants ===
CENTRAL_TZ = pytz.timezone("America/Chicago")
WORDLE_START_DATE = datetime(2021, 6, 19)

# === Bot Setup ===
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# === Load/Save Functions ===
def load_scores():
    if not DATA_FILE.exists():
        DATA_FILE.write_text("{}")
    with open(DATA_FILE, "r") as f:
        return json.load(f)

def save_scores(scores):
    with open(DATA_FILE, "w") as f:
        json.dump(scores, f, separators=(',', ':'))

# === Wordle Helper ===
# Anchor (efficient reference)
anchor_wordle_number = 1509
anchor_date = date(2025, 8, 6)

# Wordle number to date
def wordle_to_date(wordle_num):
    delta = wordle_num - anchor_wordle_number
    return anchor_date + timedelta(days=delta)

# Date to Wordle number
def date_to_wordle(some_date):
    delta = (some_date - anchor_date).days
    return anchor_wordle_number + delta
  
# === Scheduler ===
@tasks.loop(minutes=1)
async def daily_penalty_check():
    now = datetime.now(CENTRAL_TZ)
    if now.hour == 0 and now.minute == 1:  # 12:01 AM CST
        scores = load_scores()
        yesterday = now.date() - timedelta(days=1)
        wordle_num = get_wordle_number_for_date(yesterday)

        joined_users = {uid for uid in scores if "joined" in scores[uid] and scores[uid]["joined"]}
        penalized = []

        for uid in joined_users:
            if wordle_num not in scores[uid]["games"]:
                scores[uid]["games"][wordle_num] = 7
                scores[uid]["total"] += 7
                penalized.append(uid)

        if penalized:
            channel = discord.utils.get(bot.get_all_channels(), name="general")
            if channel:
                mentions = ", ".join(f"<@{uid}>" for uid in penalized)
                await channel.send(f"⏰ Auto-penalty: {mentions} were given 7 tries for missing Wordle #{wordle_num}.")
        save_scores(scores)

# === Bot Events ===
@bot.event
async def on_ready():
    print(f"Bot is ready as {bot.user}")
    daily_penalty_check.start()

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    match = re.search(r"Wordle\s+([\d,]+)\s+(\d|X)/6", message.content)
    if match:
        wordle_number = match.group(1).replace(",", "")
        tries = 7 if match.group(2) == "X" else int(match.group(2))
        user_id = str(message.author.id)

        scores = load_scores()
        if user_id not in scores:
            scores[user_id] = {"total": 0, "games": {}, "joined": True, "wins": 0}

        if wordle_number in scores[user_id]["games"]:
            scores[user_id]["total"] -= scores[user_id]["games"][wordle_number]

        scores[user_id]["games"][wordle_number] = tries
        scores[user_id]["total"] += tries

        save_scores(scores)
        await message.channel.send(f"✅ Wordle #{wordle_number} recorded — {tries} tries for {message.author.display_name}!")

    await bot.process_commands(message)

# === Commands ===
@bot.command()
async def leaderboard(ctx):
    scores = load_scores()
    if not scores:
        await ctx.send("No scores yet.")
        return
    sorted_scores = sorted(scores.items(), key=lambda x: x[1]["total"])
    lines = [
        f"**{await bot.fetch_user(int(uid))}** — {data['total']} tries over {len(data['games'])} games"
        for uid, data in sorted_scores
    ]
    await ctx.send("__**🏆 Wordle Leaderboard**__\n" + "\n".join(lines))

@bot.command()
async def joinwordle(ctx):
    scores = load_scores()
    uid = str(ctx.author.id)
    if uid not in scores:
        scores[uid] = {"total": 0, "games": {}, "joined": True, "wins": 0}
    else:
        scores[uid]["joined"] = True
    save_scores(scores)
    await ctx.send(f"{ctx.author.mention} joined the daily Wordle challenge!")

@bot.command()
async def leavewordle(ctx):
    scores = load_scores()
    uid = str(ctx.author.id)
    if uid in scores:
        scores[uid]["joined"] = False
        save_scores(scores)
        await ctx.send(f"{ctx.author.mention} left the daily Wordle challenge.")

@bot.command()
@commands.has_permissions(administrator=True)
async def resetweek(ctx):
    scores = load_scores()
    winner = min((
        (uid, data["total"]) for uid, data in scores.items() if data["games"]
    ), key=lambda x: x[1], default=(None, None))

    if winner[0]:
        scores[winner[0]]["wins"] = scores[winner[0]].get("wins", 0) + 1
        winner_name = (await bot.fetch_user(int(winner[0]))).display_name
        await ctx.send(f"🎉 Congrats {winner_name} for winning the week with {winner[1]} total tries!")

    for uid in scores:
        scores[uid]["games"] = {}
        scores[uid]["total"] = 0
    save_scores(scores)

@bot.command()
async def wins(ctx):
    scores = load_scores()
    lines = [
        f"**{await bot.fetch_user(int(uid))}** — {data.get('wins', 0)} wins"
        for uid, data in scores.items() if data.get("wins", 0) > 0
    ]
    if lines:
        await ctx.send("__**🥇 Weekly Wins**__\n" + "\n".join(lines))
    else:
        await ctx.send("No wins recorded yet.")

# === Flask Setup ===
app = Flask(__name__)
@app.route("/")
def home():
    return "OK"

def run_flask():
    app.run(host="0.0.0.0", port=10000)

# === Start Bot ===
if __name__ == "__main__":
    Thread(target=run_flask).start()
    TOKEN = os.getenv("TOKEN")
    if TOKEN:
        bot.run(TOKEN)
    else:
        print("TOKEN not set")
