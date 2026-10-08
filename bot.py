"""
Raid Loot Share Bot (v3 - partial-sell aware drop/sell/gold/stock flow)
-------------------------------------------------------------------------
Flow:
  1. User manually creates a thread and, in the FIRST message of that
     thread (or the message it was created from, if using "right-click ->
     Create Thread"), mentions every player participating in the raid.
  2. /raid new stampprice:<X>   -> reads player mentions from that first
     message, creates the raid record for this thread.
  3. /drop item_name:<X> [stamp_qty:<N>] [stamper:<@player>]
        -> records ONE dropped unit of an item. Re-dropping the same item
           name by the same stamper (while any earlier units of that
           stack are still unsold) merges into that stack: displayed
           quantity goes up, and each unit keeps its own stamp count so a
           later partial sale can carry the exact stamps for the units
           actually sold.
  4. /gold amount:<X>   -> sets (overwrites) the raid's extra gold.
  5. /sell   -> pick a stock line from a dropdown (only lines with unsold
        units remaining), then a popup asks how many units were sold
        (prefilled with the full remaining amount, editable) and the
        total sold price for that batch. Selling fewer than the full
        remaining amount marks that line PARTIALLY SOLD; selling the last
        remaining unit marks it SOLD. Once every unit across every stack
        is sold, the payout is calculated and posted automatically.
  6. /stock  -> public message showing current stock (item/qty/stamper/
        status) and raid gold. AVAILABLE / PARTIALLY SOLD / SOLD.
  7. /confirm and /raid forceconfirm work as before: each player confirms
     receipt, and once everyone has confirmed the thread auto-archives.
  8. /playeredit -> creator/admin only. Fixes a mistagged player on the
     roster: pick the wrong player from a dropdown, then pick the correct
     one via Discord's native member picker, then confirm. Blocked for a
     given player once they're the stamper on an item that's already
     sold (their stamp bonus is already locked into a completed sale),
     and blocked entirely once the raid is no longer in_progress.

Data is stored as one JSON file per thread under DATA_DIR, so raids
survive a bot restart.

NOTE: reading @mentions from a message does NOT require Discord's
privileged Message Content Intent -- mentions are delivered as separate
metadata regardless of that intent (only content/embeds/attachments/
components are redacted without it).
"""

import os
import re
import json
import math
import uuid
import asyncio
import datetime
import tempfile
import traceback
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "raids")
os.makedirs(DATA_DIR, exist_ok=True)

# Bumped only when the JSON shape changes in a way that needs a migration.
# Files written before this field existed have no "schema_version" but are
# still current (they have "drops"); they pick the field up on the next save.
SCHEMA_VERSION = 3


class RaidDataError(Exception):
    """A raid file exists but can't be used (corrupt, legacy, or too new)."""


def _reject_constant(name: str):
    """json parse hook: refuse NaN/Infinity so an already-poisoned file fails
    with a clear error instead of loading a raid whose payout renders 'nan'."""
    raise ValueError(f"non-finite number {name} in raid file")


def _has_non_finite(value) -> bool:
    """True if the parsed structure contains a nan/inf float anywhere.

    parse_constant alone is not enough: json accepts overflowing literals
    like 1e999, which the float scanner turns into inf without ever calling
    the hook, so the loaded tree needs its own check."""
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, dict):
        return any(_has_non_finite(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_non_finite(v) for v in value)
    return False


# --------------------------------------------------------------------------
# Storage helpers
# --------------------------------------------------------------------------

# One asyncio.Lock per thread, created lazily. Every command is a
# read-modify-write of the whole raid dict, so overlapping handlers would
# otherwise lose each other's writes (e.g. two /sell modals both seeing the
# same pre-save snapshot, or two /confirm s racing).
_raid_locks: dict[int, asyncio.Lock] = {}


def get_raid_lock(thread_id: int) -> asyncio.Lock:
    lock = _raid_locks.get(thread_id)
    if lock is None:
        lock = asyncio.Lock()
        _raid_locks[thread_id] = lock
    return lock


def raid_path(thread_id: int) -> str:
    return os.path.join(DATA_DIR, f"{thread_id}.json")


def load_raid(thread_id: int):
    """Return the raid dict for this thread, None if there is no file, or
    raise RaidDataError if the file exists but can't be used. Every caller
    relies on this distinction -- never return a half-valid dict."""
    path = raid_path(thread_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            raid = json.load(f, parse_constant=_reject_constant)
    except (OSError, ValueError) as e:
        raise RaidDataError(
            f"this thread's raid file is unreadable ({type(e).__name__})"
        ) from e
    if not isinstance(raid, dict):
        raise RaidDataError("this thread's raid file is not a valid raid record")
    if _has_non_finite(raid):
        raise RaidDataError(
            "this thread's raid file contains a non-finite number (nan/inf)"
        )
    # Version is checked before the shape, so a future schema that renames
    # or drops "drops" is reported as too new rather than told to /raid cancel.
    version = raid.get("schema_version")
    if isinstance(version, int) and version > SCHEMA_VERSION:
        raise RaidDataError(
            "this raid was created by a newer version of the bot than I'm running"
        )
    if isinstance(version, int) and version < SCHEMA_VERSION:
        raise RaidDataError("this raid uses an older data format that I can no longer read")
    if "drops" not in raid:
        raise RaidDataError(
            "this raid was created by an older version of the bot and can no longer be "
            "read. Run `/raid cancel` in this thread to clear it, then start a new raid."
        )
    raid.setdefault("schema_version", SCHEMA_VERSION)  # persisted on the next save
    return raid


def save_raid(raid: dict):
    """Write atomically: full contents to a temp file in the same directory,
    fsync, then os.replace over the real file. A crash mid-write can never
    leave a truncated raid file behind."""
    path = raid_path(raid["thread_id"])
    fd, tmp_path = tempfile.mkstemp(dir=DATA_DIR, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(raid, f, indent=2, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def delete_raid(thread_id: int):
    path = raid_path(thread_id)
    if os.path.exists(path):
        os.remove(path)
    # Only drop the lock entry if nobody holds it. Evicting a lock the caller
    # is still inside (e.g. /raid cancel deleting under its own lock) would let
    # get_raid_lock hand out a *second* lock for the same thread, and the two
    # would no longer exclude each other.
    lock = _raid_locks.get(thread_id)
    if lock is not None and not lock.locked():
        _raid_locks.pop(thread_id, None)


# --------------------------------------------------------------------------
# Formatting / group helpers
# --------------------------------------------------------------------------


def fmt_gold(x: float) -> str:
    if float(x).is_integer():
        return f"{int(x):,}"
    return f"{x:,.2f}"


def now_ts() -> int:
    return int(datetime.datetime.now(datetime.timezone.utc).timestamp())


def compute_match_key(item_name: str, stamper_id) -> str:
    """Same matching algorithm /drop uses to group units into a stack.
    Shared here so /dropedit can recompute it consistently when the item
    name or stamper changes."""
    return f"{item_name.strip().lower()}|{stamper_id or 'none'}"


def drop_is_editable(drop: dict) -> bool:
    """A drop is editable (for /dropedit V1) only if none of its units
    have been sold yet."""
    return all(not u["sold"] for u in drop["units"])


def stamp_display_for(drop: dict) -> str:
    total_units = group_total_units(drop)
    if total_units == 1:
        return str(drop["units"][0]["stamp_qty"])
    return f"{group_total_stamp_qty(drop)} (across {total_units} units)"


def stamper_display_for(drop: dict) -> str:
    return f"<@{drop['stamper_id']}>" if drop["stamper_id"] else "None"


def group_remaining(group: dict) -> int:
    return sum(1 for u in group["units"] if not u["sold"])


def group_total_units(group: dict) -> int:
    return len(group["units"])


def group_total_stamp_qty(group: dict) -> int:
    return sum(u["stamp_qty"] for u in group["units"])


def group_status(group: dict) -> str:
    remaining = group_remaining(group)
    total = group_total_units(group)
    if remaining == total:
        return "AVAILABLE"
    if remaining == 0:
        return "SOLD"
    return "PARTIALLY SOLD"


def group_sold_total_price(raid: dict, group_id: str) -> float:
    return sum(s["price"] for s in raid.get("sales", []) if s["group_id"] == group_id)


def all_fully_sold(raid: dict) -> bool:
    drops = raid["drops"]
    if not drops:
        return False
    return all(group_remaining(g) == 0 for g in drops)


def compute_payout(raid: dict) -> dict:
    drops = raid["drops"]
    sales = raid.get("sales", [])
    gold = raid["gold"]
    stampprice = raid["stampprice"]
    num_players = len(raid["players"])

    total_sold = sum(s["price"] for s in sales)
    total_stamps = sum(group_total_stamp_qty(g) for g in drops)
    stamp_deduction = total_stamps * stampprice
    total_before_deduction = gold + total_sold
    net_pool = total_before_deduction - stamp_deduction
    base_share = net_pool / num_players if num_players else 0

    bonus_by_player = {}
    for g in drops:
        stamp_qty = group_total_stamp_qty(g)
        if g["stamper_id"] and stamp_qty > 0:
            bonus_by_player[g["stamper_id"]] = bonus_by_player.get(
                g["stamper_id"], 0
            ) + stamp_qty * stampprice

    return {
        "gold": gold,
        "total_sold": total_sold,
        "total_before_deduction": total_before_deduction,
        "total_stamps": total_stamps,
        "stamp_deduction": stamp_deduction,
        "net_pool": net_pool,
        "base_share": base_share,
        "bonus_by_player": bonus_by_player,
    }


def calculate_and_format(raid: dict) -> str:
    drops = raid["drops"]
    sales = raid.get("sales", [])
    players = raid["players"]
    num_players = len(players)
    payout = compute_payout(raid)

    # Aggregate sold amounts per item name for a clean payout summary line
    item_totals = {}
    item_order = []
    for g in drops:
        name = g["item_name"]
        if name not in item_totals:
            item_totals[name] = {"qty": 0, "price": 0.0}
            item_order.append(name)
        item_totals[name]["qty"] += group_total_units(g)
    for s in sales:
        name = s["item_name"]
        item_totals.setdefault(name, {"qty": 0, "price": 0.0})
        if name not in item_order:
            item_order.append(name)
        item_totals[name]["price"] += s["price"]

    lines = []
    lines.append(f"⚔️ **Raid Loot Share | {num_players} Players**")
    completed_ts = raid.get("completed_at_ts")
    if completed_ts:
        lines.append(f"🕒 Calculated: <t:{completed_ts}:F> (<t:{completed_ts}:R>)")
    lines.append("━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"💎 Gold  →  {fmt_gold(payout['gold'])}G")
    for name in item_order:
        t = item_totals[name]
        lines.append(f"💎 {name} (x{t['qty']})  →  {fmt_gold(t['price'])}G")
    lines.append(f"Total: {fmt_gold(payout['total_before_deduction'])}G")
    lines.append("━━━━━━━━━━━━━━━━━━━━━")
    lines.append(
        f"🔖 Stamp Deduction  →  −{fmt_gold(payout['stamp_deduction'])}G  "
        f"({payout['total_stamps']} stamps × {fmt_gold(raid['stampprice'])}G)"
    )
    lines.append(
        f"💰 Net Pool         →  {fmt_gold(payout['net_pool'])}G "
        f"({fmt_gold(payout['total_before_deduction'])}G - {fmt_gold(payout['stamp_deduction'])}G)"
    )
    lines.append(f"👥 Base Share       →  {fmt_gold(payout['base_share'])}G each "
        f"({fmt_gold(payout['net_pool'])}G ÷ {num_players} players)"
    )
    lines.append("━━━━━━━━━━━━━━━━━━━━━")
    lines.append("📋 Payout")
    for pid in raid["player_ids"]:
        bonus = payout["bonus_by_player"].get(pid, 0)
        if bonus:
            total = payout["base_share"] + bonus
            lines.append(
                f"🏅 <@{pid}>  →  {fmt_gold(total)}G  (+{fmt_gold(bonus)}G stamp bonus)"
            )
        else:
            lines.append(f"🏅 <@{pid}>  →  {fmt_gold(payout['base_share'])}G")
    lines.append("━━━━━━━━━━━━━━━━━━━━━")
    lines.append(
        "Want your salary sent via in-game mail? Please send your IGN (In-Game "
        "Name) in the Salary comment below. Make sure your IGN exactly matches "
        "your character name, including capitalization and special characters."
    )
    lines.append("")
    lines.append(
        "Once you've received your share, run `/confirm` in this thread. "
        "When everyone has confirmed, this thread will close automatically."
    )
    return "\n".join(lines)


def build_salary_dm_text(name: str, share: float) -> str:
    return (
        f"# 💰 Salary Ready - {name}\n\n"
        "Your payout has been calculated.\n\n"
        f"**Discord Name:** {name}\n"
        f"**Amount:** {fmt_gold(share)}G\n\n"
        "---\n\n"
        "**How you'll be paid — choose one:**\n\n"
        "- **In-Game Trade** — if an admin is online, you may ask to receive "
        "it directly via trade.\n"
        "- **In-Game Mail** — reply in the Salary Thread with your exact "
        "**IGN**.\n"
        "⚠️ Must match your in-game character name exactly (capitalization + "
        "special characters) — if mismatched, we are not responsible once "
        "the share has been sent.\n\n"
        "**Already received it?**\n"
        "Confirm with `/confirm` in the Salary Thread so we can close out "
        "your payout.\n\n"
        "---\n\n"
        "Thanks for running with us — see you at the next split. 🗡️\n\n"
        "👇 Open Salary Thread"
    )


async def send_salary_dms(client, raid: dict) -> list:
    """DM every player their individual share with a link back to the raid
    thread. Returns a list of player_ids that couldn't be reached."""
    payout = compute_payout(raid)
    thread_url = f"https://discord.com/channels/{raid['guild_id']}/{raid['thread_id']}"
    failed = []
    for pid in raid["player_ids"]:
        name = raid["players"].get(pid, pid)
        share = payout["base_share"] + payout["bonus_by_player"].get(pid, 0)
        text = build_salary_dm_text(name, share)
        view = discord.ui.View()
        view.add_item(
            discord.ui.Button(
                label="Open Salary Thread",
                url=thread_url,
                style=discord.ButtonStyle.link,
            )
        )
        try:
            user = client.get_user(int(pid)) or await client.fetch_user(int(pid))
            await user.send(content=text, view=view)
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            failed.append(pid)
    return failed


def build_stock_lines(raid: dict) -> list:
    lines = []
    for g in raid["drops"]:
        remaining = group_remaining(g)
        total = group_total_units(g)
        sold_count = total - remaining
        status = group_status(g)
        display_qty = 0 if status == "SOLD" else remaining
        stamp_qty = group_total_stamp_qty(g)
        if g["stamper_id"]:
            stamper_str = f"<@{g['stamper_id']}> {stamp_qty} stamps"
        else:
            stamper_str = "no stamper"
        extra = ""
        if sold_count > 0:
            sold_total = group_sold_total_price(raid, g["id"])
            extra = f" (sold {sold_count} for {fmt_gold(sold_total)}G so far)"
        lines.append(
            f"- {g['item_name']} x{display_qty} | {stamper_str} | {status}{extra}"
        )
    return lines


async def ack_and_announce(interaction: discord.Interaction, thread, content: str):
    """Post the real result as a normal thread message (this has no
    interaction-expiry risk at all), then best-effort acknowledge whatever
    state the interaction is in -- deferred (use followup) or not yet
    responded (use response.send_message directly). Any failure here is
    silently swallowed since the important content is already posted."""
    try:
        await thread.send(content)
    except discord.HTTPException:
        pass
    try:
        if interaction.response.is_done():
            await interaction.followup.send("✅ Done.", ephemeral=True)
        else:
            await interaction.response.send_message("✅ Done.", ephemeral=True)
    except discord.HTTPException:
        pass


# --------------------------------------------------------------------------
# Bot setup
# --------------------------------------------------------------------------

intents = discord.Intents.default()
# Item names and other user text are echoed back into bot messages, so never
# let a typed "@everyone"/"@role" actually ping. Individual <@id> mentions are
# still allowed -- the roster, /confirm receipts, and DM-failure notices need them.
bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True),
)


@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        print(f"Logged in as {bot.user}. Synced {len(synced)} command(s).")
    except Exception as e:
        print(f"Command sync failed: {e}")


async def _report_interaction_error(interaction: discord.Interaction, error, *, where: str):
    """Tell the user what went wrong on an ephemeral reply, and print a
    traceback for anything unexpected. Without this a failed handler leaves
    the interaction stuck on 'thinking...' forever, since load_raid raises
    RaidDataError instead of returning a junk dict."""
    if isinstance(error, RaidDataError):
        message = f"❌ {error}"
    else:
        traceback.print_exception(type(error), error, error.__traceback__)
        message = (
            f"❌ Something went wrong {where}. It may not have completed — "
            f"check the thread (e.g. `/stock`) before retrying."
        )
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        pass


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    # discord.py wraps whatever the command raised in CommandInvokeError.
    await _report_interaction_error(
        interaction, getattr(error, "original", error), where="handling this command"
    )


class RaidView(discord.ui.View):
    """View whose errors get reported to the user. discord.py's default
    View.on_error only logs, which would silently strand a menu."""

    async def on_error(self, interaction: discord.Interaction, error: Exception, item, /):
        await _report_interaction_error(interaction, error, where="in this menu")


class RaidModal(discord.ui.Modal):
    """Modal counterpart to RaidView."""

    async def on_error(self, interaction: discord.Interaction, error: Exception, /):
        await _report_interaction_error(interaction, error, where="in this form")


# --------------------------------------------------------------------------
# /raid new
# --------------------------------------------------------------------------

raid_group = app_commands.Group(name="raid", description="Raid loot management")


async def get_thread_intro_candidates(thread: discord.Thread, limit: int = 10):
    """Return candidate intro messages as (source, message) tuples, in priority
    order: the starter message (right-click -> Create Thread) first, then the
    earliest *real* user messages inside the thread. System messages (thread
    starter references, 'X added Y', etc.) are skipped."""
    candidates = []

    starter = thread.starter_message
    if starter is None and thread.parent is not None and hasattr(thread.parent, "fetch_message"):
        try:
            starter = await thread.parent.fetch_message(thread.id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            starter = None
    if starter is not None:
        candidates.append(("starter", starter))

    async for m in thread.history(limit=limit, oldest_first=True):
        if m.type not in (discord.MessageType.default, discord.MessageType.reply):
            continue
        if any(m.id == c.id for _, c in candidates):
            continue
        candidates.append(("history", m))

    return candidates


RAID_ALREADY_STARTED_MSG = (
    "❌ A raid has already been started in this thread. Use `/raid cancel` first "
    "if you need to restart it."
)


@raid_group.command(
    name="new",
    description="Start a new raid in this thread (players auto-detected from the thread's first message)",
)
@app_commands.describe(stampprice="Gold cost per stamp for this raid")
async def raid_new(interaction: discord.Interaction, stampprice: float):
    thread = interaction.channel
    if not isinstance(thread, discord.Thread):
        await interaction.response.send_message(
            "❌ `/raid new` must be run inside the thread you created for this raid "
            "(create a thread, mention all participants in its first message, then run "
            "this command there).",
            ephemeral=True,
        )
        return

    # Fast, friendly rejection before we read any history. The authoritative
    # check is re-done under the raid lock just before the save, so two
    # concurrent /raid new calls can't both create a raid for this thread.
    if load_raid(thread.id):
        await interaction.response.send_message(RAID_ALREADY_STARTED_MSG, ephemeral=True)
        return

    if not math.isfinite(stampprice):
        await interaction.response.send_message(
            "❌ Stamp price must be a finite number.", ephemeral=True
        )
        return

    if stampprice < 0:
        await interaction.response.send_message(
            "❌ Stamp price cannot be negative.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        candidates = await get_thread_intro_candidates(thread)
    except discord.Forbidden:
        await interaction.followup.send(
            "❌ I don't have permission to read this thread's message history "
            "(need the 'Read Message History' permission).",
            ephemeral=True,
        )
        return

    if not candidates:
        await interaction.followup.send(
            "❌ This thread doesn't have any messages yet. Post a message mentioning "
            "every raid participant first (e.g. `@Player1 @Player2 ...`), then run "
            "`/raid new` again.",
            ephemeral=True,
        )
        return

    player_members = []
    for source, msg in candidates:
        seen = set()
        found = []
        for m in msg.mentions:
            if m.bot or m.id in seen:
                continue
            seen.add(m.id)
            found.append(m)
        if found:
            player_members = found
            print(f"[raid new] using {source} message id={msg.id} -> {len(found)} player(s)")
            break

    if not player_members:
        # TEMP DEBUG: shows exactly what the bot saw
        print(f"[raid new] NO mentions. thread.id={thread.id} "
              f"parent={type(thread.parent).__name__ if thread.parent else None}")
        for source, msg in candidates:
            print(f"  {source}: id={msg.id} type={msg.type} author={msg.author} "
                  f"mentions={[x.id for x in msg.mentions]} "
                  f"role_mentions={[r.id for r in msg.role_mentions]} "
                  f"everyone={msg.mention_everyone}")
        await interaction.followup.send(
            "❌ No player mentions found in this thread's first messages. Make sure the "
            "first message in this thread @mentions every participant, then run "
            "`/raid new` again.",
            ephemeral=True,
        )
        return
       

    player_ids = [str(m.id) for m in player_members]
    players = {str(m.id): m.display_name for m in player_members}
    created_ts = now_ts()

    raid = {
        "schema_version": SCHEMA_VERSION,
        "guild_id": interaction.guild.id,
        "thread_id": thread.id,
        "created_by": interaction.user.id,
        "created_at_ts": created_ts,
        "player_ids": player_ids,
        "players": players,
        "stampprice": stampprice,
        "gold": 0.0,
        "drops": [],
        "sales": [],
        "confirmed": [],
        "status": "in_progress",
    }
    async with get_raid_lock(thread.id):
        # Re-check under the lock: two people can run /raid new concurrently,
        # and the fast check above happened before we read the history.
        if load_raid(thread.id):
            await interaction.followup.send(RAID_ALREADY_STARTED_MSG, ephemeral=True)
            return
        save_raid(raid)

    mentions_str = " ".join(f"<@{pid}>" for pid in player_ids)
    success_msg = (
        "**New Raid Created successfully!**\n\n"
        "Next action:\n"
        "- use `/drop` for listing current raid drop and stampers\n"
        "- use `/sell` for selling current drop\n"
        "- use `/gold` for list additional current raid gold\n"
        "- use `/stock` for checking current drop stock and the stock update\n\n"
        "if you need help use command `/help`\n\n"
        f"👥 Players detected: {mentions_str}\n"
        f"🔖 Stamp price: {fmt_gold(stampprice)}G/stamp\n\n"
        f"⚠️ **Please check the player list above. If your name is missing or incorrectly tagged, please let an admin know.**"
    )
    await thread.send(success_msg)
    await interaction.followup.send("✅ Raid created.", ephemeral=True)


# --------------------------------------------------------------------------
# /drop
# --------------------------------------------------------------------------


@bot.tree.command(name="drop", description="Record a loot drop for the current raid")
@app_commands.describe(
    item_name="Name of the item",
    stamp_qty="Number of stamps used on this unit (0 if none needed)",
    stamper="Player who stamped this item (required if stamp_qty > 0)",
)
async def drop_cmd(
    interaction: discord.Interaction,
    item_name: str,
    stamp_qty: int = 0,
    stamper: discord.Member = None,
):
    try:
        await interaction.response.defer(ephemeral=True)
    except discord.HTTPException:
        pass

    thread = interaction.channel
    if not isinstance(thread, discord.Thread):
        await interaction.followup.send(
            "This command must be used inside a raid thread.", ephemeral=True
        )
        return

    async with get_raid_lock(thread.id):
        raid = load_raid(thread.id)
        if not raid:
            await interaction.followup.send(
                "❌ No raid found in this thread. Run `/raid new` first.", ephemeral=True
            )
            return

        if raid["status"] != "in_progress":
            await interaction.followup.send(
                "❌ This raid's loot has already been calculated/closed — no more drops can "
                "be added.",
                ephemeral=True,
            )
            return

        item_name_clean = item_name.strip()
        if not item_name_clean:
            await interaction.followup.send(
                "❌ Item name cannot be empty.", ephemeral=True
            )
            return

        if stamp_qty < 0:
            await interaction.followup.send(
                "❌ Stamp qty cannot be negative.", ephemeral=True
            )
            return

        if stamp_qty > 0 and stamper is None:
            await interaction.followup.send(
                "❌ A stamper must be selected when `stamp_qty` is greater than 0.",
                ephemeral=True,
            )
            return

        if stamp_qty == 0:
            stamper = None

        if stamper is not None and str(stamper.id) not in raid["player_ids"]:
            await interaction.followup.send(
                f"❌ {stamper.display_name} isn't part of this raid's player list.",
                ephemeral=True,
            )
            return

        stamper_id = str(stamper.id) if stamper else None
        match_key = compute_match_key(item_name_clean, stamper_id)

        # Merge into an existing ACTIVE (not fully sold) group with the same
        # item name + stamper. If the only matching group is fully sold, start
        # a fresh one instead (that one is a closed transaction).
        entry = None
        for g in raid["drops"]:
            if g["match_key"] == match_key and group_remaining(g) > 0:
                entry = g
                break

        if entry:
            entry["units"].append({"stamp_qty": stamp_qty, "sold": False})
        else:
            entry = {
                "id": uuid.uuid4().hex,
                "match_key": match_key,
                "item_name": item_name_clean,
                "stamper_id": stamper_id,
                "units": [{"stamp_qty": stamp_qty, "sold": False}],
            }
            raid["drops"].append(entry)

        save_raid(raid)

    total = stamp_qty * raid["stampprice"]
    stamper_mention = f"<@{stamper_id}>" if stamper_id else "None"
    remaining_lines = build_stock_lines(raid)
    remaining_str = "\n".join(remaining_lines) if remaining_lines else "_(none)_"

    msg = (
        "**Drop recorded successfully!**\n\n"
        f"item name: {item_name_clean}\n"
        f"stamp: {stamp_qty} × {fmt_gold(raid['stampprice'])}G = {fmt_gold(total)}G\n"
        f"stampers: {stamper_mention}\n\n"
        f"**Remaining Stock:**\n{remaining_str}"
    )
    await ack_and_announce(interaction, thread, msg)


# --------------------------------------------------------------------------
# /gold
# --------------------------------------------------------------------------


@bot.tree.command(name="gold", description="Set the raid's additional gold (overwrites the previous value)")
@app_commands.describe(amount="Total additional gold for this raid")
async def gold_cmd(interaction: discord.Interaction, amount: float):
    try:
        await interaction.response.defer(ephemeral=True)
    except discord.HTTPException:
        pass

    thread = interaction.channel
    if not isinstance(thread, discord.Thread):
        await interaction.followup.send(
            "This command must be used inside a raid thread.", ephemeral=True
        )
        return

    async with get_raid_lock(thread.id):
        raid = load_raid(thread.id)
        if not raid:
            await interaction.followup.send(
                "❌ No raid found in this thread. Run `/raid new` first.", ephemeral=True
            )
            return

        if raid["status"] != "in_progress":
            await interaction.followup.send(
                "❌ This raid's loot has already been calculated/closed — gold can no longer "
                "be changed.",
                ephemeral=True,
            )
            return

        if not math.isfinite(amount):
            await interaction.followup.send(
                "❌ Gold amount must be a finite number.", ephemeral=True
            )
            return

        if amount < 0:
            await interaction.followup.send(
                "❌ Gold cannot be negative.", ephemeral=True
            )
            return

        raid["gold"] = amount
        save_raid(raid)

    await ack_and_announce(
        interaction, thread, f"**Raid Gold recorded successfully!**\n\nGold: {fmt_gold(amount)}G"
    )


# --------------------------------------------------------------------------
# /dropedit  (select drop -> edit menu -> modal/select -> confirm -> save)
# --------------------------------------------------------------------------

CANNOT_EDIT_SOLD_MSG = "❌ This drop is no longer editable because it has been sold."
DROP_GONE_MSG = "❌ Selected drop no longer exists."
RAID_GONE_MSG = "❌ This raid no longer exists."
DUPLICATE_MATCH_MSG = (
    "❌ Cannot update this drop.\n\n"
    "A drop with the same item and stamper already exists in this raid."
)
MULTI_UNIT_STAMP_MSG = (
    "❌ Stamp quantity cannot be edited for this drop.\n\n"
    "This drop contains multiple units. This will be supported in a future version."
)


def has_active_match_key_conflict(raid: dict, exclude_drop_id: str, match_key: str) -> bool:
    """True if another ACTIVE (not fully sold) drop in this raid already
    has this match_key. Mirrors /drop's own merge rule: a match_key that
    only belongs to an already-fully-sold drop is not a conflict, since
    /drop itself starts a fresh entry once an old one sells out."""
    return any(
        g["id"] != exclude_drop_id and g["match_key"] == match_key and group_remaining(g) > 0
        for g in raid["drops"]
    )


async def safe_edit_origin(origin_interaction: discord.Interaction, fallback_interaction: discord.Interaction, content: str, view=None):
    """Ephemeral interaction-response messages are not real channel
    messages, so Message.edit() can't touch them (404 Unknown Message).
    To edit one, you must use edit_original_response() on the SAME
    interaction whose response produced it. If that fails (e.g. an
    expired 15-minute token), fall back to a brand new ephemeral followup
    on fallback_interaction so the flow doesn't just silently vanish."""
    try:
        await origin_interaction.edit_original_response(content=content, view=view)
    except discord.HTTPException:
        try:
            followup_kwargs = {"content": content, "ephemeral": True}
            if view is not None:
                followup_kwargs["view"] = view
            await fallback_interaction.followup.send(**followup_kwargs)
        except discord.HTTPException:
            pass


class ConfirmEditView(RaidView):
    """Final Cancel/Confirm step shared by all three edit fields. `pending`
    carries everything needed to re-validate and apply the change:
    {thread_id, drop_id, field, new_value}."""

    def __init__(self, requester_id: int, pending: dict):
        super().__init__(timeout=300)
        self.requester_id = requester_id
        self.pending = pending

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Only the person who ran `/dropedit` can use this menu.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="❌")
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content="❌ Edit cancelled. No changes were made.", view=None
        )

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        thread_id = self.pending["thread_id"]
        drop_id = self.pending["drop_id"]
        field = self.pending["field"]

        async with get_raid_lock(thread_id):
            # Re-fetch fresh and re-validate everything before touching data --
            # concurrency protection in case something changed since this
            # confirmation screen was shown.
            raid = load_raid(thread_id)
            if not raid:
                await interaction.response.edit_message(content=RAID_GONE_MSG, view=None)
                return
            drop = next((g for g in raid["drops"] if g["id"] == drop_id), None)
            if not drop:
                await interaction.response.edit_message(content=DROP_GONE_MSG, view=None)
                return
            if not drop_is_editable(drop):
                await interaction.response.edit_message(content=CANNOT_EDIT_SOLD_MSG, view=None)
                return

            if field == "item_name":
                new_name = self.pending["new_value"]
                new_key = compute_match_key(new_name, drop["stamper_id"])
                if has_active_match_key_conflict(raid, drop_id, new_key):
                    await interaction.response.edit_message(content=DUPLICATE_MATCH_MSG, view=None)
                    return
                old_name = drop["item_name"]
                drop["item_name"] = new_name
                drop["match_key"] = new_key
                success = (
                    "✅ Drop updated successfully.\n\n"
                    f"{old_name}\n→ {new_name}\n\n"
                    f"Stamp: {stamp_display_for(drop)}\n"
                    f"Stamper: {stamper_display_for(drop)}"
                )

            elif field == "stamp_qty":
                if len(drop["units"]) != 1:
                    await interaction.response.edit_message(content=MULTI_UNIT_STAMP_MSG, view=None)
                    return
                old_qty = drop["units"][0]["stamp_qty"]
                new_qty = self.pending["new_value"]
                drop["units"][0]["stamp_qty"] = new_qty
                success = (
                    "✅ Drop updated successfully.\n\n"
                    f"{drop['item_name']}\n\n"
                    f"Stamp:\n{old_qty} → {new_qty}"
                )

            elif field == "stamper":
                new_stamper_id = self.pending["new_value"]
                if new_stamper_id not in raid["player_ids"]:
                    await interaction.response.edit_message(
                        content="❌ That player isn't part of this raid.", view=None
                    )
                    return
                new_key = compute_match_key(drop["item_name"], new_stamper_id)
                if has_active_match_key_conflict(raid, drop_id, new_key):
                    await interaction.response.edit_message(content=DUPLICATE_MATCH_MSG, view=None)
                    return
                old_display = stamper_display_for(drop)
                drop["stamper_id"] = new_stamper_id
                drop["match_key"] = new_key
                success = (
                    "✅ Drop updated successfully.\n\n"
                    f"{drop['item_name']}\n\n"
                    f"Stamper:\n{old_display} → <@{new_stamper_id}>"
                )
            else:
                await interaction.response.edit_message(content="❌ Unknown edit type.", view=None)
                return

            try:
                save_raid(raid)
            except Exception:
                await interaction.response.edit_message(
                    content="❌ Failed to save changes. Please try again.", view=None
                )
                return

            await interaction.response.edit_message(content=success, view=None)


async def finish_modal_edit(modal_interaction: discord.Interaction, origin_interaction: discord.Interaction, content: str, view=None):
    """Update the original edit-flow screen (via safe_edit_origin), then
    always close out the modal's own deferred interaction with a small
    ack so it never gets stuck showing 'thinking...'."""
    await safe_edit_origin(origin_interaction, modal_interaction, content, view)
    try:
        await modal_interaction.followup.send("✅ Done.", ephemeral=True)
    except discord.HTTPException:
        pass


class ItemNameEditModal(RaidModal, title="Edit Item Name"):
    def __init__(self, requester_id: int, thread_id: int, drop_id: str, current_name: str, origin_interaction: discord.Interaction):
        super().__init__()
        self.requester_id = requester_id
        self.thread_id = thread_id
        self.drop_id = drop_id
        self.origin_interaction = origin_interaction
        self.name_input = discord.ui.TextInput(
            label="New item name",
            style=discord.TextStyle.short,
            default=current_name,
            required=True,
            max_length=100,
        )
        self.add_item(self.name_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.HTTPException:
            pass

        new_name = str(self.name_input.value).strip()
        if not new_name:
            await interaction.followup.send("❌ Item name cannot be empty.", ephemeral=True)
            return

        raid = load_raid(self.thread_id)
        if not raid:
            await finish_modal_edit(interaction, self.origin_interaction, RAID_GONE_MSG, None)
            return

        drop = next((g for g in raid["drops"] if g["id"] == self.drop_id), None)
        if not drop:
            await finish_modal_edit(interaction, self.origin_interaction, DROP_GONE_MSG, None)
            return

        if not drop_is_editable(drop):
            await finish_modal_edit(interaction, self.origin_interaction, CANNOT_EDIT_SOLD_MSG, None)
            return

        new_key = compute_match_key(new_name, drop["stamper_id"])
        if has_active_match_key_conflict(raid, self.drop_id, new_key):
            await finish_modal_edit(interaction, self.origin_interaction, DUPLICATE_MATCH_MSG, None)
            return

        content = (
            "⚠️ **Confirm Change**\n\n"
            f"Item Name:\n{drop['item_name']}\n→ {new_name}\n\n"
            f"Stamp:\n{stamp_display_for(drop)}\n\n"
            f"Stamper:\n{stamper_display_for(drop)}"
        )
        pending = {
            "thread_id": self.thread_id,
            "drop_id": self.drop_id,
            "field": "item_name",
            "new_value": new_name,
        }
        confirm_view = ConfirmEditView(requester_id=self.requester_id, pending=pending)
        await finish_modal_edit(interaction, self.origin_interaction, content, confirm_view)


class StampQtyEditModal(RaidModal, title="Edit Stamp Quantity"):
    def __init__(self, requester_id: int, thread_id: int, drop_id: str, current_qty: int, origin_interaction: discord.Interaction):
        super().__init__()
        self.requester_id = requester_id
        self.thread_id = thread_id
        self.drop_id = drop_id
        self.origin_interaction = origin_interaction
        self.qty_input = discord.ui.TextInput(
            label="New stamp quantity",
            style=discord.TextStyle.short,
            default=str(current_qty),
            required=True,
            max_length=10,
        )
        self.add_item(self.qty_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.HTTPException:
            pass

        raw = str(self.qty_input.value).strip()
        try:
            new_qty = int(raw)
        except ValueError:
            await interaction.followup.send(
                f"❌ `{raw}` isn't a valid whole number for stamp quantity.", ephemeral=True
            )
            return

        if new_qty <= 0:
            await interaction.followup.send(
                "❌ Stamp quantity must be greater than 0.", ephemeral=True
            )
            return

        raid = load_raid(self.thread_id)
        if not raid:
            await finish_modal_edit(interaction, self.origin_interaction, RAID_GONE_MSG, None)
            return

        drop = next((g for g in raid["drops"] if g["id"] == self.drop_id), None)
        if not drop:
            await finish_modal_edit(interaction, self.origin_interaction, DROP_GONE_MSG, None)
            return

        if not drop_is_editable(drop):
            await finish_modal_edit(interaction, self.origin_interaction, CANNOT_EDIT_SOLD_MSG, None)
            return

        if len(drop["units"]) != 1:
            await finish_modal_edit(interaction, self.origin_interaction, MULTI_UNIT_STAMP_MSG, None)
            return

        old_qty = drop["units"][0]["stamp_qty"]
        content = (
            "⚠️ **Confirm Change**\n\n"
            f"{drop['item_name']}\n\n"
            f"Stamp:\n{old_qty} → {new_qty}\n\n"
            f"Stamper:\n{stamper_display_for(drop)}"
        )
        pending = {
            "thread_id": self.thread_id,
            "drop_id": self.drop_id,
            "field": "stamp_qty",
            "new_value": new_qty,
        }
        confirm_view = ConfirmEditView(requester_id=self.requester_id, pending=pending)
        await finish_modal_edit(interaction, self.origin_interaction, content, confirm_view)


class StamperSelect(discord.ui.Select):
    def __init__(self, options):
        super().__init__(placeholder="Choose new stamper...", options=options)

    async def callback(self, interaction: discord.Interaction):
        view: StamperSelectView = self.view
        thread_id = view.thread_id
        drop_id = view.drop_id
        new_stamper_id = self.values[0]

        raid = load_raid(thread_id)
        if not raid:
            await interaction.response.edit_message(content=RAID_GONE_MSG, view=None)
            return
        drop = next((g for g in raid["drops"] if g["id"] == drop_id), None)
        if not drop:
            await interaction.response.edit_message(content=DROP_GONE_MSG, view=None)
            return
        if not drop_is_editable(drop):
            await interaction.response.edit_message(content=CANNOT_EDIT_SOLD_MSG, view=None)
            return
        if new_stamper_id not in raid["player_ids"]:
            await interaction.response.edit_message(
                content="❌ That player isn't part of this raid.", view=None
            )
            return

        new_key = compute_match_key(drop["item_name"], new_stamper_id)
        if has_active_match_key_conflict(raid, drop_id, new_key):
            await interaction.response.edit_message(content=DUPLICATE_MATCH_MSG, view=None)
            return

        old_display = stamper_display_for(drop)
        content = (
            "⚠️ **Confirm Change**\n\n"
            f"{drop['item_name']}\n\n"
            f"Stamp:\n{stamp_display_for(drop)}\n\n"
            f"Stamper:\n{old_display} → <@{new_stamper_id}>"
        )
        pending = {
            "thread_id": thread_id,
            "drop_id": drop_id,
            "field": "stamper",
            "new_value": new_stamper_id,
        }
        confirm_view = ConfirmEditView(requester_id=view.requester_id, pending=pending)
        await interaction.response.edit_message(content=content, view=confirm_view)


class StamperSelectView(RaidView):
    def __init__(self, requester_id: int, thread_id: int, drop_id: str, options):
        super().__init__(timeout=300)
        self.requester_id = requester_id
        self.thread_id = thread_id
        self.drop_id = drop_id
        self.add_item(StamperSelect(options))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Only the person who ran `/dropedit` can use this menu.", ephemeral=True
            )
            return False
        return True


class EditMenuView(RaidView):
    def __init__(self, requester_id: int, thread_id: int, drop_id: str, origin_interaction: discord.Interaction):
        super().__init__(timeout=300)
        self.requester_id = requester_id
        self.thread_id = thread_id
        self.drop_id = drop_id
        self.origin_interaction = origin_interaction

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Only the person who ran `/dropedit` can use this menu.", ephemeral=True
            )
            return False
        return True

    def _load_current(self):
        raid = load_raid(self.thread_id)
        if not raid:
            return None, None
        drop = next((g for g in raid["drops"] if g["id"] == self.drop_id), None)
        return raid, drop

    @discord.ui.button(label="Item Name", emoji="📝", style=discord.ButtonStyle.primary)
    async def edit_item_name(self, interaction: discord.Interaction, button: discord.ui.Button):
        raid, drop = self._load_current()
        if not raid or not drop:
            await interaction.response.edit_message(content=DROP_GONE_MSG, view=None)
            return
        if not drop_is_editable(drop):
            await interaction.response.edit_message(content=CANNOT_EDIT_SOLD_MSG, view=None)
            return
        modal = ItemNameEditModal(
            requester_id=self.requester_id,
            thread_id=self.thread_id,
            drop_id=self.drop_id,
            current_name=drop["item_name"],
            origin_interaction=self.origin_interaction,
        )
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Stamp Quantity", emoji="🔢", style=discord.ButtonStyle.primary)
    async def edit_stamp_qty(self, interaction: discord.Interaction, button: discord.ui.Button):
        raid, drop = self._load_current()
        if not raid or not drop:
            await interaction.response.edit_message(content=DROP_GONE_MSG, view=None)
            return
        if not drop_is_editable(drop):
            await interaction.response.edit_message(content=CANNOT_EDIT_SOLD_MSG, view=None)
            return
        if len(drop["units"]) != 1:
            await interaction.response.send_message(MULTI_UNIT_STAMP_MSG, ephemeral=True)
            return
        modal = StampQtyEditModal(
            requester_id=self.requester_id,
            thread_id=self.thread_id,
            drop_id=self.drop_id,
            current_qty=drop["units"][0]["stamp_qty"],
            origin_interaction=self.origin_interaction,
        )
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Stamper", emoji="👤", style=discord.ButtonStyle.primary)
    async def edit_stamper(self, interaction: discord.Interaction, button: discord.ui.Button):
        raid, drop = self._load_current()
        if not raid or not drop:
            await interaction.response.edit_message(content=DROP_GONE_MSG, view=None)
            return
        if not drop_is_editable(drop):
            await interaction.response.edit_message(content=CANNOT_EDIT_SOLD_MSG, view=None)
            return

        options = []
        for pid in raid["player_ids"][:25]:
            name = raid["players"].get(pid, pid)
            options.append(
                discord.SelectOption(
                    label=name[:100], value=pid, default=(pid == drop["stamper_id"])
                )
            )
        if not options:
            await interaction.response.send_message(
                "❌ No participants found for this raid.", ephemeral=True
            )
            return

        content = (
            "👤 **Select Stamper**\n\n"
            f"Current stamper:\n{stamper_display_for(drop)}\n\n"
            "Select new stamper:"
        )
        stamper_view = StamperSelectView(
            requester_id=self.requester_id,
            thread_id=self.thread_id,
            drop_id=self.drop_id,
            options=options,
        )
        await interaction.response.edit_message(content=content, view=stamper_view)

    @discord.ui.button(label="Cancel", emoji="❌", style=discord.ButtonStyle.secondary)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content="❌ Edit cancelled. No changes were made.", view=None
        )


class DropSelect(discord.ui.Select):
    def __init__(self, options):
        super().__init__(placeholder="Select drop to edit...", options=options)

    async def callback(self, interaction: discord.Interaction):
        view: DropEditSelectView = self.view
        thread_id = view.thread_id
        drop_id = self.values[0]

        raid = load_raid(thread_id)
        if not raid:
            await interaction.response.edit_message(content=RAID_GONE_MSG, view=None)
            return
        drop = next((g for g in raid["drops"] if g["id"] == drop_id), None)
        if not drop:
            await interaction.response.edit_message(content=DROP_GONE_MSG, view=None)
            return
        if not drop_is_editable(drop):
            await interaction.response.edit_message(content=CANNOT_EDIT_SOLD_MSG, view=None)
            return

        content = (
            "✏️ **Edit Drop**\n\n"
            f"Item:\n{drop['item_name']}\n\n"
            f"Stamp:\n{stamp_display_for(drop)}\n\n"
            f"Stamper:\n{stamper_display_for(drop)}\n\n"
            "What would you like to edit?"
        )
        edit_view = EditMenuView(
            requester_id=view.requester_id, thread_id=thread_id, drop_id=drop_id, origin_interaction=interaction
        )
        await interaction.response.edit_message(content=content, view=edit_view)


class DropEditSelectView(RaidView):
    def __init__(self, requester_id: int, thread_id: int, options):
        super().__init__(timeout=300)
        self.requester_id = requester_id
        self.thread_id = thread_id
        self.add_item(DropSelect(options))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Only the person who ran `/dropedit` can use this menu.", ephemeral=True
            )
            return False
        return True


@bot.tree.command(
    name="dropedit", description="Edit an existing unsold raid drop (item name, stamp qty, or stamper)"
)
async def dropedit_cmd(interaction: discord.Interaction):
    try:
        await interaction.response.defer(ephemeral=True)
    except discord.HTTPException:
        pass

    thread = interaction.channel
    if not isinstance(thread, discord.Thread):
        await interaction.followup.send(
            "This command must be used inside a raid thread.", ephemeral=True
        )
        return

    raid = load_raid(thread.id)
    if not raid:
        await interaction.followup.send("❌ No active raid found.", ephemeral=True)
        return

    is_creator = interaction.user.id == raid["created_by"]
    is_admin = (
        isinstance(interaction.user, discord.Member)
        and interaction.user.guild_permissions.administrator
    )
    if not (is_creator or is_admin):
        await interaction.followup.send(
            "Only the raid creator or a server admin can use `/dropedit`.", ephemeral=True
        )
        return

    if raid["status"] != "in_progress":
        await interaction.followup.send(
            "🔒 This raid has already been finalized. Drops can no longer be edited.",
            ephemeral=True,
        )
        return

    if not raid["drops"]:
        await interaction.followup.send("❌ There are no drops to edit.", ephemeral=True)
        return

    editable = [g for g in raid["drops"] if drop_is_editable(g)]
    if not editable:
        await interaction.followup.send(
            "❌ There are no unsold drops available for editing.", ephemeral=True
        )
        return

    note = ""
    if len(editable) > 25:
        note = "\n⚠️ More than 25 editable drops exist — only the first 25 are shown."
        editable = editable[:25]

    options = []
    for idx, g in enumerate(editable, start=1):
        label = f"D{idx:02d} • {g['item_name']}"[:100]
        stamp_qty = group_total_stamp_qty(g)
        if g["stamper_id"]:
            stamper_name = raid["players"].get(g["stamper_id"], "?")
            desc = f"{stamp_qty} stamp{'s' if stamp_qty != 1 else ''} • {stamper_name}"
        else:
            desc = "No stamp"
        options.append(
            discord.SelectOption(label=label, description=desc[:100], value=g["id"])
        )

    view = DropEditSelectView(requester_id=interaction.user.id, thread_id=thread.id, options=options)
    await interaction.followup.send(
        f"✏️ **Select Drop to Edit**{note}", view=view, ephemeral=True
    )


# --------------------------------------------------------------------------
# /playeredit  (select player -> pick correct player (native member picker)
#               -> confirm -> save)
# --------------------------------------------------------------------------

PLAYER_RAID_GONE_MSG = "❌ This raid no longer exists."
PLAYER_GONE_MSG = "❌ Selected player is no longer on this raid's roster."
PLAYER_NOT_EDITABLE_MSG = (
    "❌ This player can no longer be edited — they're the stamper on an item that "
    "has already sold, so their stamp bonus is already locked into a completed sale."
)
PLAYER_ALREADY_ON_ROSTER_MSG = "❌ That player is already on this raid's roster."
PLAYER_NO_CHANGE_MSG = "That's already the selected player — no change made."
PLAYER_BOT_MSG = "❌ A bot can't be added as a raid player."
PLAYER_RAID_LOCKED_MSG = (
    "🔒 This raid has already been finalized. The player roster can no longer be edited."
)


def player_has_sold_stamp_bond(raid: dict, player_id: str) -> bool:
    """True if this player is the stamper on a drop that already has at
    least one sold unit -- their stamp bonus for that sale is already
    baked into a completed transaction, so swapping their identity out
    from under it would silently corrupt payout math that may already
    have been paid out/DMed."""
    return any(
        g["stamper_id"] == player_id and any(u["sold"] for u in g["units"])
        for g in raid["drops"]
    )


def find_player_swap_conflict(raid: dict, old_id: str, new_id: str):
    """If reassigning old_id's actively-stamped drops to new_id would
    collide with an item new_id is already actively stamping under the
    same name, return that item's name so the caller can reject the
    swap. Otherwise return None. (Only ACTIVE/not-fully-sold drops can
    collide, mirroring /drop and /dropedit's own merge rules.)"""
    for g in raid["drops"]:
        if g["stamper_id"] != old_id or group_remaining(g) == 0:
            continue
        new_key = compute_match_key(g["item_name"], new_id)
        collides = any(
            other["id"] != g["id"] and other["match_key"] == new_key and group_remaining(other) > 0
            for other in raid["drops"]
        )
        if collides:
            return g["item_name"]
    return None


def apply_player_swap(raid: dict, old_id: str, new_id: str, new_name: str):
    """Mutates raid in place: swaps old_id -> new_id everywhere it
    appears (roster, drop stamper references + their match_keys, and
    confirmation list), preserving order and any prior confirmation."""
    raid["player_ids"] = [new_id if pid == old_id else pid for pid in raid["player_ids"]]
    raid["players"] = {
        (new_id if pid == old_id else pid): (new_name if pid == old_id else name)
        for pid, name in raid["players"].items()
    }
    for g in raid["drops"]:
        if g["stamper_id"] == old_id:
            g["stamper_id"] = new_id
            g["match_key"] = compute_match_key(g["item_name"], new_id)
    confirmed = raid.get("confirmed", [])
    raid["confirmed"] = [new_id if pid == old_id else pid for pid in confirmed]


class PlayerEditConfirmView(RaidView):
    """Final Cancel/Confirm step. `pending` carries everything needed to
    re-validate and apply the swap: {thread_id, old_id, new_id, new_name}."""

    def __init__(self, requester_id: int, pending: dict):
        super().__init__(timeout=300)
        self.requester_id = requester_id
        self.pending = pending

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Only the person who ran `/playeredit` can use this menu.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="❌")
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content="❌ Edit cancelled. No changes were made.", view=None
        )

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        thread_id = self.pending["thread_id"]
        old_id = self.pending["old_id"]
        new_id = self.pending["new_id"]
        new_name = self.pending["new_name"]

        async with get_raid_lock(thread_id):
            # Re-fetch fresh and re-validate everything before touching data --
            # concurrency protection in case a sale went through, or the raid
            # changed some other way, while this confirmation screen was open.
            raid = load_raid(thread_id)
            if not raid:
                await interaction.response.edit_message(content=PLAYER_RAID_GONE_MSG, view=None)
                return
            if raid["status"] != "in_progress":
                await interaction.response.edit_message(content=PLAYER_RAID_LOCKED_MSG, view=None)
                return
            if old_id not in raid["player_ids"]:
                await interaction.response.edit_message(content=PLAYER_GONE_MSG, view=None)
                return
            if player_has_sold_stamp_bond(raid, old_id):
                await interaction.response.edit_message(content=PLAYER_NOT_EDITABLE_MSG, view=None)
                return
            if new_id in raid["player_ids"]:
                await interaction.response.edit_message(content=PLAYER_ALREADY_ON_ROSTER_MSG, view=None)
                return
            conflict_item = find_player_swap_conflict(raid, old_id, new_id)
            if conflict_item:
                await interaction.response.edit_message(
                    content=(
                        "❌ Cannot update this player.\n\n"
                        f"The new player already has another active stamped drop for "
                        f"**{conflict_item}** — that would collide with it."
                    ),
                    view=None,
                )
                return

            old_name = raid["players"].get(old_id, old_id)
            apply_player_swap(raid, old_id, new_id, new_name)

            try:
                save_raid(raid)
            except Exception:
                await interaction.response.edit_message(
                    content="❌ Failed to save changes. Please try again.", view=None
                )
                return

        # Public announcement so the rest of the raid sees the correction
        # and the up-to-date roster, not just the person who ran the
        # command. Best-effort: a failure here shouldn't undo the save or
        # block the ephemeral success reply below.
        roster_mentions = ", ".join(f"<@{pid}>" for pid in raid["player_ids"])
        public_announcement = (
            "🔧 **Player Roster Updated**\n"
            f"{old_name} (<@{old_id}>) → {new_name} (<@{new_id}>)\n"
            f"Updated by {interaction.user.mention}\n\n"
            f"**Current players ({len(raid['player_ids'])}):** {roster_mentions}"
        )
        try:
            await interaction.channel.send(public_announcement)
        except discord.HTTPException:
            pass

        success = (
            "✅ Player updated successfully. A public update was posted in the thread.\n\n"
            f"{old_name} (<@{old_id}>)\n→ {new_name} (<@{new_id}>)"
        )
        await interaction.response.edit_message(content=success, view=None)


class NewPlayerSelect(discord.ui.UserSelect):
    """Discord's native member picker -- same kind of no-typing, roster-
    safe input /drop uses for `stamper`, just for picking the corrected
    player instead."""

    def __init__(self):
        super().__init__(placeholder="Select the correct player...", min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        view: NewPlayerSelectView = self.view
        thread_id = view.thread_id
        old_id = view.old_id

        new_member = self.values[0]
        if new_member.bot:
            await interaction.response.edit_message(content=PLAYER_BOT_MSG, view=None)
            return
        new_id = str(new_member.id)

        raid = load_raid(thread_id)
        if not raid:
            await interaction.response.edit_message(content=PLAYER_RAID_GONE_MSG, view=None)
            return
        if raid["status"] != "in_progress":
            await interaction.response.edit_message(content=PLAYER_RAID_LOCKED_MSG, view=None)
            return
        if old_id not in raid["player_ids"]:
            await interaction.response.edit_message(content=PLAYER_GONE_MSG, view=None)
            return
        if player_has_sold_stamp_bond(raid, old_id):
            await interaction.response.edit_message(content=PLAYER_NOT_EDITABLE_MSG, view=None)
            return
        if new_id == old_id:
            await interaction.response.edit_message(content=PLAYER_NO_CHANGE_MSG, view=None)
            return
        if new_id in raid["player_ids"]:
            await interaction.response.edit_message(content=PLAYER_ALREADY_ON_ROSTER_MSG, view=None)
            return

        conflict_item = find_player_swap_conflict(raid, old_id, new_id)
        if conflict_item:
            await interaction.response.edit_message(
                content=(
                    "❌ Cannot update this player.\n\n"
                    f"The new player already has another active stamped drop for "
                    f"**{conflict_item}** — that would collide with it."
                ),
                view=None,
            )
            return

        old_name = raid["players"].get(old_id, old_id)
        new_name = getattr(new_member, "display_name", None) or new_member.name

        content = (
            "⚠️ **Confirm Change**\n\n"
            f"{old_name} (<@{old_id}>)\n→ {new_name} (<@{new_id}>)"
        )
        pending = {
            "thread_id": thread_id,
            "old_id": old_id,
            "new_id": new_id,
            "new_name": new_name,
        }
        confirm_view = PlayerEditConfirmView(requester_id=view.requester_id, pending=pending)
        await interaction.response.edit_message(content=content, view=confirm_view)


class NewPlayerSelectView(RaidView):
    def __init__(self, requester_id: int, thread_id: int, old_id: str):
        super().__init__(timeout=300)
        self.requester_id = requester_id
        self.thread_id = thread_id
        self.old_id = old_id
        self.add_item(NewPlayerSelect())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Only the person who ran `/playeredit` can use this menu.", ephemeral=True
            )
            return False
        return True


class PlayerSelect(discord.ui.Select):
    def __init__(self, options):
        super().__init__(placeholder="Select player to edit...", options=options)

    async def callback(self, interaction: discord.Interaction):
        view: PlayerEditSelectView = self.view
        thread_id = view.thread_id
        old_id = self.values[0]

        raid = load_raid(thread_id)
        if not raid:
            await interaction.response.edit_message(content=PLAYER_RAID_GONE_MSG, view=None)
            return
        if raid["status"] != "in_progress":
            await interaction.response.edit_message(content=PLAYER_RAID_LOCKED_MSG, view=None)
            return
        if old_id not in raid["player_ids"]:
            await interaction.response.edit_message(content=PLAYER_GONE_MSG, view=None)
            return
        if player_has_sold_stamp_bond(raid, old_id):
            await interaction.response.edit_message(content=PLAYER_NOT_EDITABLE_MSG, view=None)
            return

        old_name = raid["players"].get(old_id, old_id)
        content = (
            "👤 **Select Correct Player**\n\n"
            f"Currently: {old_name} (<@{old_id}>)\n\n"
            "Pick who this should actually be:"
        )
        new_view = NewPlayerSelectView(requester_id=view.requester_id, thread_id=thread_id, old_id=old_id)
        await interaction.response.edit_message(content=content, view=new_view)


class PlayerEditSelectView(RaidView):
    def __init__(self, requester_id: int, thread_id: int, options):
        super().__init__(timeout=300)
        self.requester_id = requester_id
        self.thread_id = thread_id
        self.add_item(PlayerSelect(options))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Only the person who ran `/playeredit` can use this menu.", ephemeral=True
            )
            return False
        return True


@bot.tree.command(
    name="playeredit", description="Fix a mistagged player on the raid roster (creator/admin only)"
)
async def playeredit_cmd(interaction: discord.Interaction):
    try:
        await interaction.response.defer(ephemeral=True)
    except discord.HTTPException:
        pass

    thread = interaction.channel
    if not isinstance(thread, discord.Thread):
        await interaction.followup.send(
            "This command must be used inside a raid thread.", ephemeral=True
        )
        return

    raid = load_raid(thread.id)
    if not raid:
        await interaction.followup.send("❌ No active raid found.", ephemeral=True)
        return

    is_creator = interaction.user.id == raid["created_by"]
    is_admin = (
        isinstance(interaction.user, discord.Member)
        and interaction.user.guild_permissions.administrator
    )
    if not (is_creator or is_admin):
        await interaction.followup.send(
            "Only the raid creator or a server admin can use `/playeredit`.", ephemeral=True
        )
        return

    if raid["status"] != "in_progress":
        await interaction.followup.send(PLAYER_RAID_LOCKED_MSG, ephemeral=True)
        return

    editable_ids = [
        pid for pid in raid["player_ids"] if not player_has_sold_stamp_bond(raid, pid)
    ]
    if not editable_ids:
        await interaction.followup.send(
            "❌ No players can be edited right now — everyone left on the roster is already "
            "the stamper on an item that's sold.",
            ephemeral=True,
        )
        return

    note = ""
    if len(editable_ids) > 25:
        note = "\n⚠️ More than 25 editable players exist — only the first 25 are shown."
        editable_ids = editable_ids[:25]

    options = [
        discord.SelectOption(label=raid["players"].get(pid, pid)[:100], value=pid)
        for pid in editable_ids
    ]

    view = PlayerEditSelectView(requester_id=interaction.user.id, thread_id=thread.id, options=options)
    await interaction.followup.send(
        f"👤 **Select Player to Edit**{note}", view=view, ephemeral=True
    )


# --------------------------------------------------------------------------
# /sell  (select menu -> modal with qty + price)
# --------------------------------------------------------------------------


class SellDetailsModal(RaidModal, title="Sell Item"):
    def __init__(self, group_id: str, remaining_at_open: int):
        super().__init__()
        self.group_id = group_id

        self.qty_input = discord.ui.TextInput(
            label="Quantity sold",
            style=discord.TextStyle.short,
            default=str(remaining_at_open),
            required=True,
            max_length=10,
        )
        self.price_input = discord.ui.TextInput(
            label="Sold price (total for this batch)",
            style=discord.TextStyle.short,
            required=True,
            max_length=20,
        )
        self.add_item(self.qty_input)
        self.add_item(self.price_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.HTTPException:
            pass

        raw_qty = str(self.qty_input.value).strip()
        raw_price = str(self.price_input.value).strip()

        try:
            qty_sold = int(raw_qty)
        except ValueError:
            await interaction.followup.send(
                f"❌ `{raw_qty}` isn't a valid whole number for quantity sold.",
                ephemeral=True,
            )
            return

        try:
            price = float(raw_price)
        except ValueError:
            await interaction.followup.send(
                f"❌ `{raw_price}` isn't a valid number for the sold price.",
                ephemeral=True,
            )
            return

        if price < 0:
            await interaction.followup.send(
                "❌ Price cannot be negative.", ephemeral=True
            )
            return

        if not math.isfinite(price):
            await interaction.followup.send(
                "❌ Sold price must be a finite number.", ephemeral=True
            )
            return

        thread = interaction.channel
        if not isinstance(thread, discord.Thread):
            await interaction.followup.send(
                "This command must be used inside a raid thread.", ephemeral=True
            )
            return

        # Everything from the re-read through the payout decision happens under
        # the raid lock: two open /sell modals used to validate against the same
        # pre-save snapshot, then both mark units sold and both post the payout
        # and DM every player their salary.
        async with get_raid_lock(thread.id):
            raid = load_raid(thread.id)
            if not raid:
                await interaction.followup.send(
                    "❌ No raid found in this thread (it may have been cancelled).",
                    ephemeral=True,
                )
                return

            entry = next((g for g in raid["drops"] if g["id"] == self.group_id), None)
            if not entry:
                await interaction.followup.send(
                    "❌ That stock line no longer exists.", ephemeral=True
                )
                return

            remaining = group_remaining(entry)
            if remaining == 0:
                await interaction.followup.send(
                    f"❌ `{entry['item_name']}` is already fully sold.", ephemeral=True
                )
                return

            if qty_sold < 1 or qty_sold > remaining:
                await interaction.followup.send(
                    f"❌ Quantity sold must be between 1 and {remaining} "
                    f"(the current remaining amount for `{entry['item_name']}`).",
                    ephemeral=True,
                )
                return

            # Mark the first `qty_sold` unsold units as sold
            marked = 0
            for u in entry["units"]:
                if marked >= qty_sold:
                    break
                if not u["sold"]:
                    u["sold"] = True
                    marked += 1

            raid.setdefault("sales", []).append(
                {
                    "group_id": entry["id"],
                    "item_name": entry["item_name"],
                    "stamper_id": entry["stamper_id"],
                    "qty": qty_sold,
                    "price": price,
                    "ts": now_ts(),
                }
            )
            save_raid(raid)

            new_status = group_status(entry)
            stock_lines = build_stock_lines(raid)
            stock_str = "\n".join(stock_lines) if stock_lines else "_(none)_"

            msg = (
                "**One or more item is SOLD!**\n\n"
                f"{entry['item_name']} x{qty_sold} sold at {fmt_gold(price)}G "
                f"(now {new_status})\n\n"
                f"**Remaining Stock:**\n{stock_str}"
            )
            await ack_and_announce(interaction, thread, msg)

            if all_fully_sold(raid):
                raid["completed_at_ts"] = now_ts()
                result_text = calculate_and_format(raid)
                raid["status"] = "completed"
                raid["confirmed"] = []
                save_raid(raid)
                await thread.send(result_text)

                failed_dms = await send_salary_dms(interaction.client, raid)
                if failed_dms:
                    mentions = " ".join(f"<@{pid}>" for pid in failed_dms)
                    await thread.send(
                        "⚠️ Couldn't DM the following players their salary summary "
                        f"(they may have DMs disabled) — please notify them manually: "
                        f"{mentions}"
                    )


class ItemSelect(discord.ui.Select):
    def __init__(self, options, remaining_by_value):
        super().__init__(placeholder="Choose the item that was sold...", options=options)
        self.remaining_by_value = remaining_by_value

    async def callback(self, interaction: discord.Interaction):
        group_id = self.values[0]
        remaining = self.remaining_by_value.get(group_id, 1)
        modal = SellDetailsModal(group_id, remaining)
        await interaction.response.send_modal(modal)


class SellSelectView(RaidView):
    def __init__(self, requester_id: int, options, remaining_by_value):
        super().__init__(timeout=300)
        self.requester_id = requester_id
        self.add_item(ItemSelect(options, remaining_by_value))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Only the person who ran `/sell` can use this menu.", ephemeral=True
            )
            return False
        return True


@bot.tree.command(name="sell", description="Mark units of a raid item as sold")
async def sell_cmd(interaction: discord.Interaction):
    thread = interaction.channel
    if not isinstance(thread, discord.Thread):
        await interaction.response.send_message(
            "This command must be used inside a raid thread.", ephemeral=True
        )
        return

    raid = load_raid(thread.id)
    if not raid:
        await interaction.response.send_message(
            "❌ No raid found in this thread. Run `/raid new` first.", ephemeral=True
        )
        return

    if raid["status"] != "in_progress":
        await interaction.response.send_message(
            "❌ This raid has already been calculated/closed.", ephemeral=True
        )
        return

    available = [g for g in raid["drops"] if group_remaining(g) > 0]
    if not available:
        await interaction.response.send_message(
            "❌ There are no unsold items right now. Use `/drop` to record one first.",
            ephemeral=True,
        )
        return

    note = ""
    if len(available) > 25:
        note = (
            "\n⚠️ More than 25 stock lines exist — only the first 25 are shown. "
            "Sell some of these first, then run `/sell` again for the rest."
        )
        available = available[:25]

    options = []
    remaining_by_value = {}
    for g in available:
        remaining = group_remaining(g)
        remaining_by_value[g["id"]] = remaining
        stamper_note = f" — {raid['players'].get(g['stamper_id'], '?')}" if g["stamper_id"] else ""
        options.append(
            discord.SelectOption(
                label=f"{g['item_name']} (remaining {remaining}){stamper_note}"[:100],
                value=g["id"],
            )
        )

    view = SellSelectView(
        requester_id=interaction.user.id, options=options, remaining_by_value=remaining_by_value
    )
    await interaction.response.send_message(
        f"Select the item that was sold:{note}", view=view, ephemeral=True
    )


# --------------------------------------------------------------------------
# /stock
# --------------------------------------------------------------------------


@bot.tree.command(name="stock", description="Show the current raid stock and gold")
async def stock_cmd(interaction: discord.Interaction):
    try:
        await interaction.response.defer(ephemeral=True)
    except discord.HTTPException:
        pass

    thread = interaction.channel
    if not isinstance(thread, discord.Thread):
        await interaction.followup.send(
            "This command must be used inside a raid thread.", ephemeral=True
        )
        return

    raid = load_raid(thread.id)
    if not raid:
        await interaction.followup.send(
            "❌ No raid found in this thread. Run `/raid new` first.", ephemeral=True
        )
        return

    lines = ["**Raid Stock**", ""]
    stock_lines = build_stock_lines(raid)
    if stock_lines:
        lines.extend(stock_lines)
    else:
        lines.append("_No items dropped yet._")
    lines.append("")
    lines.append(f"**Raid Gold:** {fmt_gold(raid['gold'])}G")
    await ack_and_announce(interaction, thread, "\n".join(lines))


# --------------------------------------------------------------------------
# /confirm, /raid forceconfirm, /raid status, /raid cancel
# --------------------------------------------------------------------------


async def finalize_if_all_confirmed(raid: dict, thread: discord.Thread) -> bool:
    """If every player has confirmed, close out the raid: mark it closed,
    post a summary, and archive the thread. Returns True if it closed.

    Must only be called while already holding this thread's raid lock --
    both callers (/confirm, /raid forceconfirm) do."""
    confirmed = raid.get("confirmed", [])
    total = len(raid["player_ids"])
    if len(confirmed) < total:
        return False

    closed_ts = now_ts()
    raid["status"] = "closed"
    raid["closed_at_ts"] = closed_ts
    save_raid(raid)
    await thread.send(
        f"🎉 **All players confirmed receipt! Closing this thread.** "
        f"(<t:{closed_ts}:F>)"
    )
    try:
        await thread.edit(archived=True, locked=False)
    except discord.Forbidden:
        await thread.send(
            "⚠️ I don't have permission to archive this thread automatically "
            "(need the 'Manage Threads' permission)."
        )
    return True


@bot.tree.command(name="confirm", description="Confirm you've received your share for this raid")
async def confirm(interaction: discord.Interaction):
    try:
        await interaction.response.defer(ephemeral=True)
    except discord.HTTPException:
        pass

    thread = interaction.channel
    if not isinstance(thread, discord.Thread):
        await interaction.followup.send(
            "This command must be used inside a raid thread.", ephemeral=True
        )
        return

    async with get_raid_lock(thread.id):
        raid = load_raid(thread.id)
        if not raid:
            await interaction.followup.send(
                "No raid data found for this thread.", ephemeral=True
            )
            return

        if raid["status"] == "in_progress":
            await interaction.followup.send(
                "The loot for this raid hasn't been calculated yet — wait until all items "
                "are marked sold first.",
                ephemeral=True,
            )
            return

        uid = str(interaction.user.id)
        if uid not in raid["player_ids"]:
            await interaction.followup.send(
                "You're not on the player roster for this raid, so this doesn't count as a "
                "confirmation.",
                ephemeral=True,
            )
            return

        if raid["status"] == "closed":
            await interaction.followup.send(
                "This raid is already fully confirmed and closed.", ephemeral=True
            )
            return

        confirmed = raid.setdefault("confirmed", [])
        if uid in confirmed:
            await interaction.followup.send(
                "You've already confirmed for this raid.", ephemeral=True
            )
            return

        confirmed.append(uid)
        save_raid(raid)

        total = len(raid["player_ids"])
        count = len(confirmed)
        await ack_and_announce(interaction, thread, f"✅ <@{uid}> confirmed receipt. ({count}/{total})")

        # Already holding this thread's lock -- finalize_if_all_confirmed must
        # not take it again (asyncio.Lock is not reentrant).
        await finalize_if_all_confirmed(raid, thread)


@raid_group.command(
    name="forceconfirm",
    description="Manually mark a player as confirmed, even if they haven't run /confirm (creator/admin only)",
)
@app_commands.describe(player="The player to mark as confirmed")
async def raid_forceconfirm(interaction: discord.Interaction, player: discord.Member):
    try:
        await interaction.response.defer(ephemeral=True)
    except discord.HTTPException:
        pass

    thread = interaction.channel
    if not isinstance(thread, discord.Thread):
        await interaction.followup.send(
            "This command must be used inside a raid thread.", ephemeral=True
        )
        return

    async with get_raid_lock(thread.id):
        raid = load_raid(thread.id)
        if not raid:
            await interaction.followup.send(
                "No raid data found for this thread.", ephemeral=True
            )
            return

        is_creator = interaction.user.id == raid["created_by"]
        is_admin = (
            isinstance(interaction.user, discord.Member)
            and interaction.user.guild_permissions.administrator
        )
        if not (is_creator or is_admin):
            await interaction.followup.send(
                "Only the raid creator or a server admin can force-confirm a player.",
                ephemeral=True,
            )
            return

        if raid["status"] == "in_progress":
            await interaction.followup.send(
                "The loot for this raid hasn't been calculated yet.", ephemeral=True
            )
            return

        uid = str(player.id)
        if uid not in raid["player_ids"]:
            await interaction.followup.send(
                f"{player.display_name} isn't on this raid's player roster.", ephemeral=True
            )
            return

        if raid["status"] == "closed":
            await interaction.followup.send(
                "This raid is already fully confirmed and closed.", ephemeral=True
            )
            return

        confirmed = raid.setdefault("confirmed", [])
        if uid in confirmed:
            await interaction.followup.send(
                f"{player.display_name} has already confirmed.", ephemeral=True
            )
            return

        confirmed.append(uid)
        save_raid(raid)

        total = len(raid["player_ids"])
        count = len(confirmed)
        await ack_and_announce(
            interaction,
            thread,
            f"✅ <@{uid}> marked as confirmed by {interaction.user.mention} (manual override). "
            f"({count}/{total})",
        )

        # Already holding this thread's lock -- see the note in `confirm`.
        await finalize_if_all_confirmed(raid, thread)


@raid_group.command(name="status", description="Show raid status and confirmation progress")
async def raid_status(interaction: discord.Interaction):
    thread = interaction.channel
    if not isinstance(thread, discord.Thread):
        await interaction.response.send_message(
            "This command must be used inside a raid thread.", ephemeral=True
        )
        return

    raid = load_raid(thread.id)
    if not raid:
        await interaction.response.send_message(
            "No raid data found for this thread.", ephemeral=True
        )
        return

    created_ts = raid.get("created_at_ts")
    lines = [f"**Raid status: {raid['status']}**"]
    if created_ts:
        lines.append(f"🕒 Started: <t:{created_ts}:F>")

    if raid["status"] in ("completed", "closed"):
        confirmed = raid.get("confirmed", [])
        total = len(raid["player_ids"])
        lines.append(f"\n**Confirmations: {len(confirmed)}/{total}**")
        for pid in raid["player_ids"]:
            mark = "✅" if pid in confirmed else "⏳"
            lines.append(f"{mark} {raid['players'].get(pid, pid)}")
    else:
        unsold = sum(1 for g in raid["drops"] if group_remaining(g) > 0)
        lines.append(f"\nUse `/stock` to see the full drop list. ({unsold} lines with stock remaining)")

    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@raid_group.command(name="cancel", description="Cancel/delete this raid (creator or admin only)")
async def raid_cancel(interaction: discord.Interaction):
    try:
        await interaction.response.defer(ephemeral=True)
    except discord.HTTPException:
        pass

    thread = interaction.channel
    if not isinstance(thread, discord.Thread):
        await interaction.followup.send(
            "This command must be used inside a raid thread.", ephemeral=True
        )
        return
    try:
        raid = load_raid(thread.id)
        creator_id = raid.get("created_by") if raid else None
    except RaidDataError:
        # The file exists but is unusable (corrupt, or written by an old
        # version). /raid cancel is the only escape hatch from that state, so
        # keep working: fall back to a best-effort raw read purely to identify
        # the creator. A file too broken to parse at all is admin-only.
        raid = None
        try:
            with open(raid_path(thread.id), "r", encoding="utf-8") as f:
                creator_id = json.load(f).get("created_by")
        except (OSError, ValueError, AttributeError):
            creator_id = None

    if raid is None and creator_id is None:
        await interaction.followup.send(
            "No raid data found for this thread.", ephemeral=True
        )
        return

    is_creator = creator_id is not None and interaction.user.id == creator_id
    is_admin = (
        isinstance(interaction.user, discord.Member)
        and interaction.user.guild_permissions.administrator
    )
    if not (is_creator or is_admin):
        await interaction.followup.send(
            "Only the raid creator or a server admin can cancel this raid.", ephemeral=True
        )
        return

    async with get_raid_lock(thread.id):
        delete_raid(thread.id)
    await ack_and_announce(interaction, thread, "🗑️ Raid data cancelled/deleted for this thread.")


bot.tree.add_command(raid_group)


# --------------------------------------------------------------------------
# /help  (lists every command with an elaboration; works anywhere)
# --------------------------------------------------------------------------

@bot.tree.command(name="help", description="List every raid command and what it does")
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📖 Raid Loot Share Bot — Commands",
        description=(
            "Run these inside the raid's thread, unless noted otherwise. "
            "🔒 = raid creator or a server admin only."
        ),
        color=discord.Color.gold(),
    )
    embed.add_field(
        name="/raid new `stampprice`",
        value=(
            "Starts tracking a raid in this thread. Reads player mentions from the "
            "thread's very first message to build the roster, and sets the "
            "gold-per-stamp price for the whole raid (set once here)."
        ),
        inline=False,
    )
    embed.add_field(
        name="/drop `item_name` `stamp_qty` `stamper`",
        value=(
            "Records one dropped unit. Run once per item that drops — running it "
            "again with the same item name + stamper merges into the same stack. "
            "`stamper` is required only if `stamp_qty` is greater than 0."
        ),
        inline=False,
    )
    embed.add_field(
        name="🔒 /dropedit",
        value=(
            "Fixes a mistake on an existing **unsold** drop — item name, stamp "
            "quantity, or stamper — through a private step-by-step menu, without "
            "cancelling the whole raid."
        ),
        inline=False,
    )
    embed.add_field(
        name="🔒 /playeredit",
        value=(
            "Fixes a mistagged player on the roster: pick the wrong player, then "
            "pick the correct one with a native member picker. Blocked for a "
            "player once they're the stamper on an item that's already sold."
        ),
        inline=False,
    )
    embed.add_field(
        name="/gold `amount`",
        value=(
            "Sets the raid's flat extra gold. **Overwrites** the previous value "
            "each time (not additive) — re-run it to correct a mistake."
        ),
        inline=False,
    )
    embed.add_field(
        name="/sell",
        value=(
            "Opens a private dropdown of stock that still has unsold units. Pick "
            "one, then enter the quantity sold and total price for that sale "
            "(supports partial sales). Once every unit is sold, the payout is "
            "calculated and posted automatically, and DMed to every player."
        ),
        inline=False,
    )
    embed.add_field(
        name="/stock",
        value=(
            "Posts the full current stock list publicly: item, stamper, "
            "remaining/sold quantities and status, plus the current raid gold."
        ),
        inline=False,
    )
    embed.add_field(
        name="/confirm",
        value=(
            "Run this once you've received your share, to acknowledge payment. "
            "Once everyone on the roster has confirmed, the thread auto-archives."
        ),
        inline=False,
    )
    embed.add_field(
        name="🔒 /raid forceconfirm `player`",
        value=(
            "Manually marks a non-responsive player as confirmed — same effect as "
            "them running `/confirm` themselves, including triggering auto-close."
        ),
        inline=False,
    )
    embed.add_field(
        name="/raid status",
        value=(
            "Shows the raid's current phase, when it started, and — once "
            "completed — who has and hasn't confirmed yet."
        ),
        inline=False,
    )
    embed.add_field(
        name="🔒 /raid cancel",
        value="Deletes this thread's raid data entirely, for starting over after a mistake.",
        inline=False,
    )
    embed.add_field(
        name="/help",
        value="Shows this list. Can be run anywhere, even outside a raid thread.",
        inline=False,
    )

    await interaction.response.send_message(embed=embed, ephemeral=True)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit(
            "DISCORD_BOT_TOKEN is not set. Create a .env file next to bot.py with a line "
            "like:\nDISCORD_BOT_TOKEN=your-token-here\n"
            "(or set it as an environment variable). See README.md for details."
        )
    bot.run(TOKEN)