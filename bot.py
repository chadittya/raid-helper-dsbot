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
import uuid
import datetime
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

# --------------------------------------------------------------------------
# Storage helpers
# --------------------------------------------------------------------------


def raid_path(thread_id: int) -> str:
    return os.path.join(DATA_DIR, f"{thread_id}.json")


def load_raid(thread_id: int):
    path = raid_path(thread_id)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_raid(raid: dict):
    path = raid_path(raid["thread_id"])
    with open(path, "w", encoding="utf-8") as f:
        json.dump(raid, f, indent=2)


def delete_raid(thread_id: int):
    path = raid_path(thread_id)
    if os.path.exists(path):
        os.remove(path)


# --------------------------------------------------------------------------
# Formatting / group helpers
# --------------------------------------------------------------------------


def fmt_gold(x: float) -> str:
    if float(x).is_integer():
        return f"{int(x):,}"
    return f"{x:,.2f}"


def now_ts() -> int:
    return int(datetime.datetime.now(datetime.timezone.utc).timestamp())


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


def calculate_and_format(raid: dict) -> str:
    drops = raid["drops"]
    sales = raid.get("sales", [])
    gold = raid["gold"]
    stampprice = raid["stampprice"]
    players = raid["players"]
    num_players = len(players)

    total_sold = sum(s["price"] for s in sales)
    total_stamps = sum(group_total_stamp_qty(g) for g in drops)
    stamp_deduction = total_stamps * stampprice
    net_pool = gold + total_sold - stamp_deduction
    base_share = net_pool / num_players if num_players else 0

    bonus_by_player = {}
    for g in drops:
        stamp_qty = group_total_stamp_qty(g)
        if g["stamper_id"] and stamp_qty > 0:
            bonus_by_player[g["stamper_id"]] = bonus_by_player.get(
                g["stamper_id"], 0
            ) + stamp_qty * stampprice

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
    lines.append(f"💎 Gold  →  {fmt_gold(gold)}G")
    for name in item_order:
        t = item_totals[name]
        lines.append(f"💎 {name} (x{t['qty']})  →  {fmt_gold(t['price'])}G")
    lines.append("━━━━━━━━━━━━━━━━━━━━━")
    lines.append(
        f"🔖 Stamp Deduction  →  −{fmt_gold(stamp_deduction)}G  "
        f"({total_stamps} stamps × {fmt_gold(stampprice)}G)"
    )
    lines.append(f"💰 Net Pool         →  {fmt_gold(net_pool)}G")
    lines.append(f"👥 Base Share       →  {fmt_gold(base_share)}G each")
    lines.append("━━━━━━━━━━━━━━━━━━━━━")
    lines.append("📋 Payout")
    for pid in raid["player_ids"]:
        bonus = bonus_by_player.get(pid, 0)
        if bonus:
            total = base_share + bonus
            lines.append(
                f"🏅 <@{pid}>  →  {fmt_gold(total)}G  (+{fmt_gold(bonus)}G stamp bonus)"
            )
        else:
            lines.append(f"🏅 <@{pid}>  →  {fmt_gold(base_share)}G")
    lines.append("━━━━━━━━━━━━━━━━━━━━━")
    lines.append(
        "Once you've received your share, run `/confirm` in this thread. "
        "When everyone has confirmed, this thread will close automatically."
    )
    return "\n".join(lines)


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


# --------------------------------------------------------------------------
# Bot setup
# --------------------------------------------------------------------------

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        print(f"Logged in as {bot.user}. Synced {len(synced)} command(s).")
    except Exception as e:
        print(f"Command sync failed: {e}")


# --------------------------------------------------------------------------
# /raid new
# --------------------------------------------------------------------------

raid_group = app_commands.Group(name="raid", description="Raid loot management")


async def get_thread_intro_message(thread: discord.Thread):
    """Find the message containing player mentions for this raid thread.

    Threads created via "right-click a message -> Create Thread" have a
    special *starter message* that is NOT returned by thread.history() --
    it has to be fetched from the parent channel using the thread's own ID
    (Discord assigns the starter message the same ID as the thread).
    Threads created via the plain "+ Threads -> Create" flow have no
    starter message at all, so we fall back to the first message actually
    sent inside the thread.
    """
    starter = thread.starter_message
    if starter is None and thread.parent is not None and hasattr(thread.parent, "fetch_message"):
        try:
            starter = await thread.parent.fetch_message(thread.id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            starter = None
    if starter is not None:
        return starter

    async for m in thread.history(limit=1, oldest_first=True):
        return m
    return None


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

    if load_raid(thread.id):
        await interaction.response.send_message(
            "❌ A raid has already been started in this thread. Use `/raid cancel` first "
            "if you need to restart it.",
            ephemeral=True,
        )
        return

    if stampprice < 0:
        await interaction.response.send_message(
            "❌ Stamp price cannot be negative.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    try:
        first_msg = await get_thread_intro_message(thread)
    except discord.Forbidden:
        await interaction.followup.send(
            "❌ I don't have permission to read this thread's message history "
            "(need the 'Read Message History' permission).",
            ephemeral=True,
        )
        return

    if first_msg is None:
        await interaction.followup.send(
            "❌ This thread doesn't have any messages yet. Post a message mentioning "
            "every raid participant first (e.g. `@Player1 @Player2 ...`), then run "
            "`/raid new` again.",
            ephemeral=True,
        )
        return

    seen = set()
    player_members = []
    for m in first_msg.mentions:
        if m.bot or m.id in seen:
            continue
        seen.add(m.id)
        player_members.append(m)

    if not player_members:
        await interaction.followup.send(
            "❌ No player mentions found in this thread's first message. Make sure the "
            "very first message in this thread @mentions every participant, then run "
            "`/raid new` again.",
            ephemeral=True,
        )
        return

    player_ids = [str(m.id) for m in player_members]
    players = {str(m.id): m.display_name for m in player_members}
    created_ts = now_ts()

    raid = {
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
    save_raid(raid)

    mentions_str = " ".join(f"<@{pid}>" for pid in player_ids)
    success_msg = (
        "**New Raid Created successfully!**\n\n"
        "use `/drop` for listing current raid drop and stampers\n"
        "use `/sell` for selling current drop\n"
        "use `/gold` for list additional current raid gold\n"
        "use `/stock` for checking current drop stock and the stock update\n\n"
        f"👥 Players detected: {mentions_str}\n"
        f"🔖 Stamp price: {fmt_gold(stampprice)}G/stamp"
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
            "❌ This raid's loot has already been calculated/closed — no more drops can "
            "be added.",
            ephemeral=True,
        )
        return

    item_name_clean = item_name.strip()
    if not item_name_clean:
        await interaction.response.send_message(
            "❌ Item name cannot be empty.", ephemeral=True
        )
        return

    if stamp_qty < 0:
        await interaction.response.send_message(
            "❌ Stamp qty cannot be negative.", ephemeral=True
        )
        return

    if stamp_qty > 0 and stamper is None:
        await interaction.response.send_message(
            "❌ A stamper must be selected when `stamp_qty` is greater than 0.",
            ephemeral=True,
        )
        return

    if stamp_qty == 0:
        stamper = None

    if stamper is not None and str(stamper.id) not in raid["player_ids"]:
        await interaction.response.send_message(
            f"❌ {stamper.display_name} isn't part of this raid's player list.",
            ephemeral=True,
        )
        return

    stamper_id = str(stamper.id) if stamper else None
    match_key = f"{item_name_clean.lower()}|{stamper_id or 'none'}"

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
    await interaction.response.send_message(msg)


# --------------------------------------------------------------------------
# /gold
# --------------------------------------------------------------------------


@bot.tree.command(name="gold", description="Set the raid's additional gold (overwrites the previous value)")
@app_commands.describe(amount="Total additional gold for this raid")
async def gold_cmd(interaction: discord.Interaction, amount: float):
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
            "❌ This raid's loot has already been calculated/closed — gold can no longer "
            "be changed.",
            ephemeral=True,
        )
        return

    if amount < 0:
        await interaction.response.send_message(
            "❌ Gold cannot be negative.", ephemeral=True
        )
        return

    raid["gold"] = amount
    save_raid(raid)
    await interaction.response.send_message(
        f"**Raid Gold recorded successfully!**\n\nGold: {fmt_gold(amount)}G"
    )


# --------------------------------------------------------------------------
# /sell  (select menu -> modal with qty + price)
# --------------------------------------------------------------------------


class SellDetailsModal(discord.ui.Modal, title="Sell Item"):
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
        raw_qty = str(self.qty_input.value).strip()
        raw_price = str(self.price_input.value).strip()

        try:
            qty_sold = int(raw_qty)
        except ValueError:
            await interaction.response.send_message(
                f"❌ `{raw_qty}` isn't a valid whole number for quantity sold.",
                ephemeral=True,
            )
            return

        try:
            price = float(raw_price)
        except ValueError:
            await interaction.response.send_message(
                f"❌ `{raw_price}` isn't a valid number for the sold price.",
                ephemeral=True,
            )
            return

        if price < 0:
            await interaction.response.send_message(
                "❌ Price cannot be negative.", ephemeral=True
            )
            return

        thread = interaction.channel
        if not isinstance(thread, discord.Thread):
            await interaction.response.send_message(
                "This command must be used inside a raid thread.", ephemeral=True
            )
            return

        raid = load_raid(thread.id)
        if not raid:
            await interaction.response.send_message(
                "❌ No raid found in this thread (it may have been cancelled).",
                ephemeral=True,
            )
            return

        entry = next((g for g in raid["drops"] if g["id"] == self.group_id), None)
        if not entry:
            await interaction.response.send_message(
                "❌ That stock line no longer exists.", ephemeral=True
            )
            return

        remaining = group_remaining(entry)
        if remaining == 0:
            await interaction.response.send_message(
                f"❌ `{entry['item_name']}` is already fully sold.", ephemeral=True
            )
            return

        if qty_sold < 1 or qty_sold > remaining:
            await interaction.response.send_message(
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
            f"**remaining stock:**\n{stock_str}"
        )
        await interaction.response.send_message(msg)

        if all_fully_sold(raid):
            raid["completed_at_ts"] = now_ts()
            result_text = calculate_and_format(raid)
            raid["status"] = "completed"
            raid["confirmed"] = []
            save_raid(raid)
            await thread.send(result_text)


class ItemSelect(discord.ui.Select):
    def __init__(self, options, remaining_by_value):
        super().__init__(placeholder="Choose the item that was sold...", options=options)
        self.remaining_by_value = remaining_by_value

    async def callback(self, interaction: discord.Interaction):
        group_id = self.values[0]
        remaining = self.remaining_by_value.get(group_id, 1)
        modal = SellDetailsModal(group_id, remaining)
        await interaction.response.send_modal(modal)


class SellSelectView(discord.ui.View):
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

    lines = ["**Raid Stock**", ""]
    stock_lines = build_stock_lines(raid)
    if stock_lines:
        lines.extend(stock_lines)
    else:
        lines.append("_No items dropped yet._")
    lines.append("")
    lines.append(f"**Raid Gold:** {fmt_gold(raid['gold'])}G")
    await interaction.response.send_message("\n".join(lines))


# --------------------------------------------------------------------------
# /confirm, /raid forceconfirm, /raid status, /raid cancel
# --------------------------------------------------------------------------


async def finalize_if_all_confirmed(raid: dict, thread: discord.Thread) -> bool:
    """If every player has confirmed, close out the raid: mark it closed,
    post a summary, and archive the thread. Returns True if it closed."""
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

    if raid["status"] == "in_progress":
        await interaction.response.send_message(
            "The loot for this raid hasn't been calculated yet — wait until all items "
            "are marked sold first.",
            ephemeral=True,
        )
        return

    uid = str(interaction.user.id)
    if uid not in raid["player_ids"]:
        await interaction.response.send_message(
            "You're not on the player roster for this raid, so this doesn't count as a "
            "confirmation.",
            ephemeral=True,
        )
        return

    if raid["status"] == "closed":
        await interaction.response.send_message(
            "This raid is already fully confirmed and closed.", ephemeral=True
        )
        return

    confirmed = raid.setdefault("confirmed", [])
    if uid in confirmed:
        await interaction.response.send_message(
            "You've already confirmed for this raid.", ephemeral=True
        )
        return

    confirmed.append(uid)
    save_raid(raid)

    total = len(raid["player_ids"])
    count = len(confirmed)
    await interaction.response.send_message(
        f"✅ <@{uid}> confirmed receipt. ({count}/{total})"
    )

    await finalize_if_all_confirmed(raid, thread)


@raid_group.command(
    name="forceconfirm",
    description="Manually mark a player as confirmed, even if they haven't run /confirm (creator/admin only)",
)
@app_commands.describe(player="The player to mark as confirmed")
async def raid_forceconfirm(interaction: discord.Interaction, player: discord.Member):
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

    is_creator = interaction.user.id == raid["created_by"]
    is_admin = (
        isinstance(interaction.user, discord.Member)
        and interaction.user.guild_permissions.administrator
    )
    if not (is_creator or is_admin):
        await interaction.response.send_message(
            "Only the raid creator or a server admin can force-confirm a player.",
            ephemeral=True,
        )
        return

    if raid["status"] == "in_progress":
        await interaction.response.send_message(
            "The loot for this raid hasn't been calculated yet.", ephemeral=True
        )
        return

    uid = str(player.id)
    if uid not in raid["player_ids"]:
        await interaction.response.send_message(
            f"{player.display_name} isn't on this raid's player roster.", ephemeral=True
        )
        return

    if raid["status"] == "closed":
        await interaction.response.send_message(
            "This raid is already fully confirmed and closed.", ephemeral=True
        )
        return

    confirmed = raid.setdefault("confirmed", [])
    if uid in confirmed:
        await interaction.response.send_message(
            f"{player.display_name} has already confirmed.", ephemeral=True
        )
        return

    confirmed.append(uid)
    save_raid(raid)

    total = len(raid["player_ids"])
    count = len(confirmed)
    await interaction.response.send_message(
        f"✅ <@{uid}> marked as confirmed by {interaction.user.mention} (manual override). "
        f"({count}/{total})"
    )

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

    is_creator = interaction.user.id == raid["created_by"]
    is_admin = (
        isinstance(interaction.user, discord.Member)
        and interaction.user.guild_permissions.administrator
    )
    if not (is_creator or is_admin):
        await interaction.response.send_message(
            "Only the raid creator or a server admin can cancel this raid.", ephemeral=True
        )
        return

    delete_raid(thread.id)
    await interaction.response.send_message("🗑️ Raid data cancelled/deleted for this thread.")


bot.tree.add_command(raid_group)


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