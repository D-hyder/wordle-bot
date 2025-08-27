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
import asyncio, sys

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
_DEF_META = {
    "duel": None,
    "last_podium": {"gold": [], "silver": [], "bronze": []},
    "pending_podium": None  # used when a duel rolls into next week
}

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

async def build_leaderboard_text():
    scores = load_scores()
    ensure_meta(scores)
    if not scores:
        return "No scores yet."

    podium = scores["_meta"].get("last_podium", {"gold": [], "silver": [], "bronze": []})

    # Only real user records (ignore internal keys)
    entries = [(uid, data) for uid, data in scores.items()
               if isinstance(data, dict) and not str(uid).startswith("_")
               and "total" in data and "games" in data]
    # Current totals control display order (not medals)
    entries.sort(key=lambda x: x[1]["total"])

    def medal_for(uid: str) -> str:
        if uid in podium.get("gold", []): return "👑 "
        if uid in podium.get("silver", []): return "🥈 "
        if uid in podium.get("bronze", []): return "🥉 "
        return ""

    lines = []
    for uid, data in entries:
        user = await bot.fetch_user(int(uid))
        gp = len(data["games"])
        lines.append(f"{medal_for(uid)}**{user.display_name}** — {data['total']} tries over {gp} games")

    return "__**🏆 Wordle Leaderboard**__\n" + "\n".join(lines)

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
                    
                        # Build last week's podium now that duel is decided
                        ensure_meta(scores)
                        pending = scores["_meta"].get("pending_podium")
                        gold = [winner_id]
                        silver = []
                        bronze = []
                    
                        if pending and isinstance(pending, dict):
                            tied_first = pending.get("tied_first", [])
                            bronze = pending.get("bronze", [])
                            # Silver = everyone who was tied for first EXCEPT the winner
                            silver = [uid for uid in tied_first if uid != winner_id]
                        else:
                            # Fallback: if no pending data (shouldn’t happen), just set gold only
                            silver, bronze = [], []
                    
                        scores["_meta"]["last_podium"] = {"gold": gold, "silver": silver, "bronze": bronze}
                        scores["_meta"]["duel"] = None
                        scores["_meta"]["pending_podium"] = None
                    
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

        lb_text = await build_leaderboard_text()
        await message.channel.send(lb_text)

    await bot.process_commands(message)


# === Commands ===
@bot.command()
async def leaderboard(ctx):
    text = await build_leaderboard_text()
    await ctx.send(text)

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

    # Sort by weekly total ascending
    entries.sort(key=lambda x: x[1]["total"])
    top_total = entries[0][1]["total"]

    # Build competition ranks (1,2,2,4) to capture tie groups
    blocks = []
    i = 0
    while i < len(entries):
        same = [entries[i]]
        j = i + 1
        while j < len(entries) and entries[j][1]["total"] == entries[i][1]["total"]:
            same.append(entries[j])
            j += 1
        rank = i + 1
        blocks.append((rank, same))
        i = j

    # Helper to extract user IDs for a block
    def ids(block):
        return [uid for uid, _ in block]

    # Check tie for first
    rank1, block1 = blocks[0]
    tied_first = ids(block1)

    if len(tied_first) == 1:
        # Clear duel state
        scores["_meta"]["duel"] = None
        scores["_meta"]["pending_podium"] = None

        # Finalize podium now using competition ranking
        gold = tied_first
        silver = []
        bronze = []

        # Find rank 2 block (if any)
        if len(blocks) >= 2 and blocks[1][0] == 2:
            silver = ids(blocks[1][1])
        # Find rank 3 block (if any)
        if len(blocks) >= 3 and blocks[2][0] == 3:
            bronze = ids(blocks[2][1])

        # Store last week's podium
        scores["_meta"]["last_podium"] = {"gold": gold, "silver": silver, "bronze": bronze}

        # Increment wins and announce winner
        winner_id = gold[0]
        scores[winner_id]["wins"] = scores[winner_id].get("wins", 0) + 1
        winner_user = await bot.fetch_user(int(winner_id))
        await ctx.send(f"🎉 Congrats {winner_user.display_name} for winning the week with {top_total} total tries!")
    else:
        # Tie for first: create a duel for tomorrow's Wordle
        tomorrow_cst = datetime.now(CENTRAL_TZ).date() + timedelta(days=1)
        duel_wordle = int(date_to_wordle(tomorrow_cst))
        scores["_meta"]["duel"] = {"players": tied_first, "wordle": duel_wordle}

        # Precompute bronze block: competition ranking says silver will be the losers of the duel
        # so bronze is whatever has rank == (1 + len(tied_first)) + 1 (i.e., rank 3+ when k==2).
        # We can simply capture the first block whose rank >= 3:
        bronze_ids = []
        for r, blk in blocks:
            if r >= 3:
                bronze_ids = ids(blk)
                break

        scores["_meta"]["pending_podium"] = {
            "tied_first": tied_first,  # duel contestants
            "bronze": bronze_ids       # fixed bronze group regardless of duel result
        }

        # Announce tie + duel
        names = []
        for uid in tied_first:
            u = await bot.fetch_user(int(uid))
            names.append(u.display_name)
        await ctx.send(f"⚔️ Weekly tie! Sudden-death duel on Wordle #{duel_wordle}: {', '.join(names)}")

    # Reset week but keep wins/joined
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
    """Create a backup and upload it as a file in Discord."""
    scores = load_scores()
    ts = datetime.now(CENTRAL_TZ).strftime("%Y%m%d_%H%M%S")
    fn = f"scores_backup_{ts}.json"
    path = f"/tmp/{fn}"

    # write to /tmp (still handy if you want to shell in later)
    with open(path, "w") as f:
        json.dump(scores, f, indent=2)

    # upload the file to the channel
    await ctx.send(
        content="💾 Backup created:",
        file=discord.File(path, filename=fn)
    )


# === Flask Setup ===
app = Flask(__name__)

@app.route("/")
def home():
    return "OK"

def run_flask():
    app.run(host="0.0.0.0", port=10000)

@bot.event
async def on_ready():
    print(f"✅ Bot is ready as {bot.user} (guilds={len(bot.guilds)})")

# === Start Bot ===
if __name__ == "__main__":
    print("▶️  boot: entering main")
    Thread(target=run_flask).start()
    print("🌐 flask: sidecar started")

    TOKEN = os.getenv("TOKEN")
    print(f"🔑 env TOKEN present? {'yes' if TOKEN else 'no'}")

    if not TOKEN:
        print("❌ TOKEN not set — Discord bot will NOT start")
        # sys.exit(1)  # uncomment if you prefer failing the deploy instead of running Flask only
    else:
        async def start_with_backoff():
            delay = 30
            while True:
                try:
                    print("🔌 discord: attempting bot.start()")
                    await bot.start(TOKEN)
                except discord.HTTPException as e:
                    if getattr(e, "status", None) == 429:
                        print(f"⏳ discord: 429 rate limited; sleeping {delay}s")
                        await asyncio.sleep(delay)
                        delay = min(delay * 2, 600)
                        continue
                    print(f"⚠️  discord: HTTP error {getattr(e,'status','?')}; retrying in 60s")
                    await asyncio.sleep(60)
                except (asyncio.CancelledError, KeyboardInterrupt):
                    print("🛑 discord: shutdown requested")
                    break
                except Exception as ex:
                    print(f"💥 discord: unexpected: {ex}; retrying in 60s")
                    await asyncio.sleep(60)
                else:
                    print("✅ discord: bot.start() returned cleanly")
                    break

        asyncio.run(start_with_backoff())
