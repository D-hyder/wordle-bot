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
    with open(INIT_FILE, "r") as f:
        data = json.load(f)
        if "players" in data:
            del data["players"]
        DATA_FILE.write_text(json.dumps(data, separators=(',', ':')))

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
# Helper: check if key is a real user record (ignore internal metadata)
def _is_user_record(k, v):
    return isinstance(v, dict) and not str(k).startswith("_") and ("total" in v and "games" in v)

# Ensure a meta bucket exists (for duel state)
_DEF_META = {"duel": None}

def ensure_meta(scores: dict):
    if not isinstance(scores, dict):
        return {"_meta": dict(_DEF_META)}
    meta = scores.get("_meta")
    if not isinstance(meta, dict):
        scores["_meta"] = dict(_DEF_META)
    else:
        for k, v in _DEF_META.items():
            scores["_meta"].setdefault(k, v)
    return scores

# Use universal Wordle epoch to avoid timezone/anchor drift
WORDLE_EPOCH = date(2021, 6, 19)  # Wordle #0 release date (universal)

def wordle_to_date(wordle_num: int) -> date:
    return WORDLE_EPOCH + timedelta(days=int(wordle_num))

def date_to_wordle(some_date: date) -> int:
    return (some_date - WORDLE_EPOCH).days

# === Scheduler ===
@tasks.loop(hours=1)
async def daily_penalty_check():
    now = datetime.now(CENTRAL_TZ)
    if now.hour == 0:  # run once during the midnight hour (any minute)
        scores = load_scores()
        yesterday = now.date() - timedelta(days=1)
        wordle_num = date_to_wordle(yesterday)

        joined_users = {
            uid for uid, data in scores.items()
            if isinstance(data, dict) and data.get("joined")
        }
        penalized = []

        for uid in joined_users:
            if str(wordle_num) not in scores[uid]["games"]:
                scores[uid]["games"][str(wordle_num)] = 7
                scores[uid]["total"] += 7
                penalized.append(uid)

        if penalized:
            channel = discord.utils.get(bot.get_all_channels(), name="general")
            if channel:
                mentions = ", ".join(f"<@{uid}>" for uid in penalized)
                await channel.send(
                    f"⏰ Auto-penalty: {mentions} were given 7 tries for missing Wordle #{wordle_num}."
                )
        save_scores(scores)



MISSING_CHANNEL_ID = "900458273117982791"  # optional channel ID

@tasks.loop(hours=1)
async def nightly_missing_alert():
    now = datetime.now(CENTRAL_TZ)
    if now.hour == 20:  # 8 PM Central
        scores = load_scores()
        today = now.date()
        wordle_num = str(date_to_wordle(today))

        joined_users = {
            uid for uid, data in scores.items()
            if isinstance(data, dict) and data.get("joined")
        }
        missing_ids = [uid for uid in joined_users if wordle_num not in scores[uid]["games"]]
        if not missing_ids:
            return

        channel = None
        if MISSING_CHANNEL_ID and MISSING_CHANNEL_ID.isdigit():
            channel = bot.get_channel(int(MISSING_CHANNEL_ID))
        if channel is None:
            channel = discord.utils.get(bot.get_all_channels(), name="general")
        if channel is None:
            return

        names = []
        for uid in missing_ids:
            try:
                user = await bot.fetch_user(int(uid))
                names.append(user.display_name)
            except Exception:
                pass
                
        if names:
            mentions = ", ".join(f"<@{uid}>" for uid in missing_ids)
            await channel.send(f"⏰ Reminder: {mentions} still need to submit today’s Wordle!")

# === Bot Events ===
@bot.event
async def on_ready():
    print(f"Bot is ready as {bot.user}")
    daily_penalty_check.start()
    nightly_missing_alert.start()

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    match = re.search(r"Wordle\s+([\d,]+)\s+(\d|X)/6", message.content)
    if match:
        wordle_number = match.group(1).replace(",", "")  # store keys as strings
        tries = 7 if match.group(2) == "X" else int(match.group(2))
        user_id = str(message.author.id)

        scores = load_scores()
        ensure_meta(scores)
        if user_id not in scores:
            scores[user_id] = {"total": 0, "games": {}, "joined": True, "wins": 0}

        if wordle_number in scores[user_id]["games"]:
            scores[user_id]["total"] -= scores[user_id]["games"][wordle_number]

        scores[user_id]["games"][wordle_number] = tries
        scores[user_id]["total"] += tries

        # === Carry-over duel check ===
        duel = scores.get("_meta", {}).get("duel")
        if duel and wordle_number == str(duel.get("wordle")):
            players = (duel.get("players") or [])[:2]
            if len(players) == 2 and players[0] in scores and players[1] in scores:
                p1, p2 = players
                have_p1 = wordle_number in scores[p1]["games"]
                have_p2 = wordle_number in scores[p2]["games"]
                if have_p1 and have_p2:
                    t1 = scores[p1]["games"][wordle_number]
                    t2 = scores[p2]["games"][wordle_number]
                    if t1 != t2:
                        winner_id = p1 if t1 < t2 else p2
                        scores[winner_id]["wins"] = scores[winner_id].get("wins", 0) + 1
                        scores["_meta"]["duel"] = None
                        save_scores(scores)
                        winner_user = await bot.fetch_user(int(winner_id))
                        await message.channel.send(f"👑 Sudden-death duel decided! Congrats {winner_user.display_name}!")
                    else:
                        next_wordle = int(wordle_number) + 1
                        scores["_meta"]["duel"]["wordle"] = next_wordle
                        save_scores(scores)
                        await message.channel.send(f"⚔️ Duel tied again on Wordle #{wordle_number}. Carrying over to #{next_wordle}.")

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
    # Only real user records (ignore internal keys)
    entries = [(uid, data) for uid, data in scores.items()
               if isinstance(data, dict) and not str(uid).startswith("_") and "total" in data and "games" in data]
    entries.sort(key=lambda x: x[1]["total"])  # ascending

    lines = []
    i = 0
    while i < len(entries):
        # group by same total (tie block)
        same = [entries[i]]
        j = i + 1
        while j < len(entries) and entries[j][1]["total"] == entries[i][1]["total"]:
            same.append(entries[j])
            j += 1
        rank = i + 1  # competition ranking (1,2,2,4)
        medal = ""
        if rank == 1:
            medal = "👑 "
        elif rank == 2:
            medal = "🥈 "
        elif rank == 3:
            medal = "🥉 "
        for uid, data in same:
            user = await bot.fetch_user(int(uid))
            gp = len(data["games"])
            lines.append(f"{medal}**{user.display_name}** — {data['total']} tries over {gp} games")
        i = j

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
    ensure_meta(scores)

    entries = [(uid, data) for uid, data in scores.items() if _is_user_record(uid, data)]
    if not entries:
        await ctx.send("No scores to reset.")
        return

    entries.sort(key=lambda x: x[1]["total"])  # ascending
    top_total = entries[0][1]["total"]
    tied = [uid for uid, data in entries if data["total"] == top_total]

    if len(tied) == 1:
        winner_id = tied[0]
        scores[winner_id]["wins"] = scores[winner_id].get("wins", 0) + 1
        winner_user = await bot.fetch_user(int(winner_id))
        await ctx.send(f"🎉 Congrats {winner_user.display_name} for winning the week with {top_total} total tries!")
    else:
        # Set up sudden-death duel on the NEXT day's Wordle (e.g., Monday after Sunday reset)
        tomorrow_cst = datetime.now(CENTRAL_TZ).date() + timedelta(days=1)
        duel_wordle = int(date_to_wordle(tomorrow_cst))
        scores["_meta"]["duel"] = {"players": tied, "wordle": duel_wordle}
        names = []
        for uid in tied:
            u = await bot.fetch_user(int(uid))
            names.append(u.display_name)
        await ctx.send(f"⚔️ Weekly tie! Sudden-death duel on Wordle #{duel_wordle}: {', '.join(names)}")

    # Reset week but keep wins/joined (duel state kept in _meta)
    for uid, data in list(scores.items()):
        if _is_user_record(uid, data):
            data["games"] = {}
            data["total"] = 0
            scores[uid] = data

    save_scores(scores)
    await ctx.send("Scores have been reset for the new week!")


@bot.command()
async def wins(ctx):
    scores = load_scores()
    lines = [
        f"**{await bot.fetch_user(int(uid))}** — {data.get('wins', 0)} wins"
        for uid, data in scores.items()
        if isinstance(data, dict) and data.get("wins", 0) > 0
    ]
    if lines:
        await ctx.send("__**🥇 Weekly Wins**__\n" + "\n".join(lines))
    else:
        await ctx.send("No wins recorded yet.")

@bot.command()
async def missing(ctx):
    scores = load_scores()
    today = datetime.now(CENTRAL_TZ).date()
    wordle_num = str(date_to_wordle(today))

    joined_users = {
        uid for uid, data in scores.items()
        if isinstance(data, dict) and data.get("joined")
    }
    missing = [
        await bot.fetch_user(int(uid)) for uid in joined_users
        if wordle_num not in scores[uid]["games"]
    ]

    if missing:
        await ctx.send("__**📋 Players Missing Today's Wordle**__\n" + ", ".join(user.name for user in missing))
    else:
        await ctx.send("✅ Everyone has submitted today's Wordle!")

@bot.command()
@commands.has_permissions(administrator=True)
async def backup(ctx):
    """Create a backup of the scores file."""
    scores = load_scores()
    backup_file = f"/tmp/scores_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(backup_file, "w") as f:
        json.dump(scores, f, indent=2)
    await ctx.send(f"💾 Backup created: `{backup_file}`")

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
