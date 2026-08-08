"""
Raid Loot Share Bot
--------------------
Slash-command Discord bot that:
  1. /raid start  -> select players via a real Discord user-picker, then a
     modal opens for gold/items/stamps/stamp price. Bot creates a thread
     and saves the raid.
  2. /sold        -> run inside the raid thread to mark an item as sold
     (item name + price). Once every item is sold, the bot automatically
     calculates and posts the payout, mentioning every player.
  3. /raid status -> shows current progress (sold/unsold items) in a thread.
  4. /raid cancel -> cancels/deletes the raid data for a thread (creator or
     server admin only).

Data is persisted as one JSON file per thread under DATA_DIR, so raids
survive a bot restart.

NOTE ON DESIGN: Discord Modal text fields cannot resolve @mentions (that
only works in real chat boxes or dedicated user-select components), so
players are selected via a discord.ui.UserSelect component, and the modal
only handles gold/items/stamps/stampprice -- stamps reference players by
plain display name instead of @mention.
"""

import os
import re
import json
import datetime
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# Load variables from a .env file in the same folder as this script (if present)
load_dotenv()

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "raids")
os.makedirs(DATA_DIR, exist_ok=True)

# Optional: if set, raid threads are always created in this channel, no
# matter which channel /raid start was run from (e.g. trigger in
# #bot-command, thread appears in #salary-loot). Leave unset to create the
# thread in whichever channel /raid start was run in.
RAID_THREAD_CHANNEL_ID = os.environ.get("RAID_THREAD_CHANNEL_ID")

ITEM_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")

TEMPLATE_EXAMPLE = (
    "gold: 0\n"
    "items: Item_Name x1, Another_Item x1\n"
    "stamps: PlayerName:1:Item_Name\n"
    "stampprice: 4"
)

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
# Parsing
# --------------------------------------------------------------------------


class RaidParseError(ValueError):
    """Raised when the pasted raid data is malformed."""


def parse_raid_details(raw: str, selected_members):
    """Parse the modal text (gold/items/stamps/stampprice) into structured
    data. `selected_members` is the list of discord.Member/User chosen via
    the UserSelect step -- used to resolve plain-text names in `stamps:`.
    """

    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    fields = {}
    for line in lines:
        if ":" not in line:
            raise RaidParseError(f"Invalid line (missing `:`): `{line}`")
        key, val = line.split(":", 1)
        fields[key.strip().lower()] = val.strip()

    required = ["gold", "items", "stampprice"]
    missing = [k for k in required if k not in fields]
    if missing:
        raise RaidParseError(f"Missing required field(s): {', '.join(missing)}")
    fields.setdefault("stamps", "")

    # --- gold ---
    try:
        gold = float(fields["gold"])
    except ValueError:
        raise RaidParseError(f"`gold:` must be a number, got `{fields['gold']}`.")
    if gold < 0:
        raise RaidParseError("`gold:` cannot be negative.")

    # --- stampprice ---
    try:
        stampprice = float(fields["stampprice"])
    except ValueError:
        raise RaidParseError(
            f"`stampprice:` must be a number, got `{fields['stampprice']}`."
        )
    if stampprice < 0:
        raise RaidParseError("`stampprice:` cannot be negative.")

    # --- items ---
    items = {}
    item_order = []
    items_raw = fields["items"].strip()
    if items_raw:
        for entry in items_raw.split(","):
            entry = entry.strip()
            if not entry:
                continue
            parts = entry.split()
            name = parts[0]
            qty_note = parts[1] if len(parts) > 1 else ""
            if not ITEM_NAME_RE.match(name):
                raise RaidParseError(
                    f"Invalid item name `{name}`. Item names must use underscores "
                    f"instead of spaces (e.g. `Gdn_ring`), letters/numbers/underscores only."
                )
            if name in items:
                raise RaidParseError(f"Duplicate item name `{name}` in `items:`.")
            items[name] = {"qty_note": qty_note, "sold": False, "price": None}
            item_order.append(name)

    # --- name lookup table for stamps ---
    name_lookup = {}
    ambiguous_names = set()
    for m in selected_members:
        for candidate in {m.display_name.lower(), m.name.lower()}:
            if candidate in name_lookup and name_lookup[candidate] != m.id:
                ambiguous_names.add(candidate)
            name_lookup[candidate] = m.id
    for a in ambiguous_names:
        name_lookup.pop(a, None)

    # --- stamps ---
    stamps = []
    stamps_raw = fields["stamps"].strip()
    if stamps_raw and stamps_raw.lower() != "none":
        for entry in stamps_raw.split(","):
            entry = entry.strip()
            if not entry:
                continue
            m = re.match(r"^@?([^:]+):(\d+):(\S+)$", entry)
            if not m:
                raise RaidParseError(
                    f"Invalid stamp entry `{entry}`. Expected format: "
                    f"`PlayerName:count:item_name` (e.g. `Nyan:3:Gdn_ring`)."
                )
            name_part, count, itemname = m.group(1).strip(), int(m.group(2)), m.group(3)
            pid = name_lookup.get(name_part.lower())
            if pid is None:
                available = ", ".join(sorted({mm.display_name for mm in selected_members}))
                raise RaidParseError(
                    f"Stamp entry references `{name_part}` who isn't one of the selected "
                    f"players (or the name is ambiguous). Selected players: {available}"
                )
            if itemname not in items:
                available_items = ", ".join(item_order) if item_order else "(none)"
                raise RaidParseError(
                    f"Stamp entry references item `{itemname}` which is not in `items:`. "
                    f"Available items: {available_items}"
                )
            if count <= 0:
                raise RaidParseError(
                    f"Stamp count for `{itemname}` must be greater than 0."
                )
            stamps.append({"player_id": str(pid), "count": count, "item": itemname})

    return {
        "gold": gold,
        "items": items,
        "item_order": item_order,
        "stamps": stamps,
        "stampprice": stampprice,
    }


# --------------------------------------------------------------------------
# Formatting helpers
# --------------------------------------------------------------------------


def fmt_gold(x: float) -> str:
    if float(x).is_integer():
        return f"{int(x):,}"
    return f"{x:,.2f}"


def calculate_and_format(raid: dict) -> str:
    items = raid["items"]
    gold = raid["gold"]
    stampprice = raid["stampprice"]
    stamps = raid["stamps"]
    players = raid["players"]  # {id: display_name}
    num_players = len(players)

    total_sold = sum(v["price"] for v in items.values() if v["sold"] and v["price"] is not None)
    total_stamps = sum(s["count"] for s in stamps)
    stamp_deduction = total_stamps * stampprice
    net_pool = gold + total_sold - stamp_deduction
    base_share = net_pool / num_players if num_players else 0

    bonus_by_player = {}
    for s in stamps:
        bonus_by_player[s["player_id"]] = bonus_by_player.get(s["player_id"], 0) + (
            s["count"] * stampprice
        )

    lines = []
    lines.append(f"⚔️ **Raid Loot Share | {num_players} Players**")
    completed_ts = raid.get("completed_at_ts")
    if completed_ts:
        lines.append(f"🕒 Calculated: <t:{completed_ts}:F> (<t:{completed_ts}:R>)")
    lines.append("━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"💎 Gold  →  {fmt_gold(gold)}G")
    for name, v in items.items():
        note = f" ({v['qty_note']})" if v.get("qty_note") else ""
        price = v["price"] if v["price"] is not None else 0
        lines.append(f"💎 {name}{note}  →  {fmt_gold(price)}G")
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
        name = players.get(pid, f"<@{pid}>")
        bonus = bonus_by_player.get(pid, 0)
        if bonus:
            total = base_share + bonus
            lines.append(
                f"🏅 <@{pid}>  →  {fmt_gold(total)}G  (+{fmt_gold(bonus)}G stamp bonus)"
            )
        else:
            lines.append(f"🏅 <@{pid}>  →  {fmt_gold(base_share)}G")
    lines.append("━━━━━━━━━━━━━━━━━━━━━")

    return "\n".join(lines)


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
# /raid start  (UserSelect -> Modal)
# --------------------------------------------------------------------------


class RaidDetailsModal(discord.ui.Modal, title="Raid Loot Details"):
    def __init__(self, selected_members, default_title: str):
        super().__init__()
        self.selected_members = selected_members
        self.default_title = default_title

        self.title_input = discord.ui.TextInput(
            label="Thread title",
            style=discord.TextStyle.short,
            default=default_title,
            required=True,
            max_length=100,
        )
        self.data_input = discord.ui.TextInput(
            label="Gold / items / stamps / stamp price",
            style=discord.TextStyle.paragraph,
            default=TEMPLATE_EXAMPLE,
            required=True,
            max_length=4000,
        )
        self.add_item(self.title_input)
        self.add_item(self.data_input)

    async def on_submit(self, interaction: discord.Interaction):
        raw = str(self.data_input.value)
        try:
            parsed = parse_raid_details(raw, self.selected_members)
        except RaidParseError as e:
            names = ", ".join(m.display_name for m in self.selected_members)
            await interaction.response.send_message(
                f"❌ **Could not read your raid data:**\n{e}\n\n"
                f"Selected players: {names}\n\n"
                f"**Template example:**\n```\n{TEMPLATE_EXAMPLE}\n```",
                ephemeral=True,
            )
            return

        source_channel = interaction.channel
        if isinstance(source_channel, discord.Thread):
            await interaction.response.send_message(
                "❌ `/raid start` must be run in a regular text channel, not inside a thread.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        # Resolve which channel the thread should be created in.
        if RAID_THREAD_CHANNEL_ID:
            channel = interaction.client.get_channel(int(RAID_THREAD_CHANNEL_ID))
            if channel is None:
                try:
                    channel = await interaction.client.fetch_channel(
                        int(RAID_THREAD_CHANNEL_ID)
                    )
                except (discord.NotFound, discord.Forbidden):
                    channel = None
            if channel is None or not isinstance(channel, discord.TextChannel):
                await interaction.followup.send(
                    "❌ RAID_THREAD_CHANNEL_ID is set but I can't find/access that channel "
                    "as a text channel. Ask an admin to check the bot's config and permissions.",
                    ephemeral=True,
                )
                return
        else:
            channel = source_channel

        player_ids = [str(m.id) for m in self.selected_members]
        players = {str(m.id): m.display_name for m in self.selected_members}

        now = datetime.datetime.now(datetime.timezone.utc)
        created_ts = int(now.timestamp())

        thread_title = str(self.title_input.value).strip()
        if not thread_title:
            thread_title = self.default_title
        thread_name = thread_title[:100]

        try:
            thread = await channel.create_thread(
                name=thread_name,
                type=discord.ChannelType.public_thread,
                auto_archive_duration=1440,
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ I don't have permission to create threads in this channel. "
                "Please grant me 'Create Public Threads' and 'Send Messages in Threads'.",
                ephemeral=True,
            )
            return

        raid = {
            "guild_id": interaction.guild.id,
            "channel_id": channel.id,
            "thread_id": thread.id,
            "created_by": interaction.user.id,
            "created_at_ts": created_ts,
            "player_ids": player_ids,
            "players": players,
            "gold": parsed["gold"],
            "stampprice": parsed["stampprice"],
            "items": parsed["items"],
            "item_order": parsed["item_order"],
            "stamps": parsed["stamps"],
            "status": "in_progress",
        }
        save_raid(raid)

        mentions = " ".join(f"<@{pid}>" for pid in player_ids)
        item_list = (
            "\n".join(
                f"• `{name}`" + (f" ({v['qty_note']})" if v["qty_note"] else "")
                for name, v in parsed["items"].items()
            )
            if parsed["items"]
            else "_(no sellable items — gold only)_"
        )
        intro = (
            f"🧵 **Raid loot tracking started!**\n"
            f"🕒 Started: <t:{created_ts}:F> (<t:{created_ts}:R>)\n"
            f"Players: {mentions}\n"
            f"Gold: {fmt_gold(parsed['gold'])}G | Stamp price: {fmt_gold(parsed['stampprice'])}G/stamp\n\n"
            f"**Items to sell:**\n{item_list}\n\n"
            f"When an item sells, run `/sold item:<item_name> price:<amount>` in this thread. "
            f"Once every item is marked sold, I'll calculate and post the payout automatically."
        )
        await thread.send(intro)

        if not parsed["items"]:
            raid["completed_at_ts"] = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
            result = calculate_and_format(raid)
            raid["status"] = "completed"
            raid["confirmed"] = []
            save_raid(raid)
            await thread.send(result)
            await thread.send(
                "Once you've received your share, run `/confirm` in this thread. "
                "When everyone has confirmed, this thread will close automatically."
            )

        note = (
            f" (in {channel.mention})" if channel.id != source_channel.id else ""
        )
        await interaction.followup.send(
            f"✅ Raid thread created: {thread.mention}{note}", ephemeral=True
        )


class PlayerSelectView(discord.ui.View):
    def __init__(self, requester_id: int):
        super().__init__(timeout=300)
        self.requester_id = requester_id

    @discord.ui.select(
        cls=discord.ui.UserSelect,
        placeholder="Select all players in this raid...",
        min_values=1,
        max_values=25,
    )
    async def select_players(
        self, interaction: discord.Interaction, select: discord.ui.UserSelect
    ):
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Only the person who ran `/raid start` can select players here.",
                ephemeral=True,
            )
            return
        selected_members = list(select.values)
        now = datetime.datetime.now(datetime.timezone.utc)
        default_title = f"Raid Loot - {now.strftime('%Y-%m-%d %H:%M UTC')}"
        modal = RaidDetailsModal(selected_members, default_title)
        await interaction.response.send_modal(modal)


raid_group = app_commands.Group(name="raid", description="Raid loot management")


@raid_group.command(name="start", description="Start tracking a new raid's loot")
async def raid_start(interaction: discord.Interaction):
    view = PlayerSelectView(requester_id=interaction.user.id)
    await interaction.response.send_message(
        "**Step 1/2:** Select all players who were in this raid, then fill in the loot details.",
        view=view,
        ephemeral=True,
    )


@raid_group.command(name="status", description="Show current progress of the raid in this thread")
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

    lines = [f"**Raid status: {raid['status']}**"]
    created_ts = raid.get("created_at_ts")
    if created_ts:
        lines.append(f"🕒 Started: <t:{created_ts}:F>")
    if raid["items"]:
        for name, v in raid["items"].items():
            if v["sold"]:
                lines.append(f"✅ `{name}` — sold for {fmt_gold(v['price'])}G")
            else:
                lines.append(f"⏳ `{name}` — not sold yet")
    else:
        lines.append("_No sellable items in this raid._")

    if raid["status"] in ("completed", "closed"):
        confirmed = raid.get("confirmed", [])
        total = len(raid["player_ids"])
        lines.append(f"\n**Confirmations: {len(confirmed)}/{total}**")
        for pid in raid["player_ids"]:
            mark = "✅" if pid in confirmed else "⏳"
            lines.append(f"{mark} {raid['players'].get(pid, pid)}")

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


bot.tree.add_command(raid_group)


# --------------------------------------------------------------------------
# /sold
# --------------------------------------------------------------------------


@bot.tree.command(name="sold", description="Mark a raid item as sold")
@app_commands.describe(
    item="Item name using underscores, e.g. Gdn_ring",
    price="Sold price in gold",
)
async def sold(interaction: discord.Interaction, item: str, price: int):
    thread = interaction.channel
    if not isinstance(thread, discord.Thread):
        await interaction.response.send_message(
            "This command must be used inside a raid thread.", ephemeral=True
        )
        return

    raid = load_raid(thread.id)
    if not raid:
        await interaction.response.send_message(
            "No active raid found for this thread.", ephemeral=True
        )
        return

    if raid["status"] == "completed":
        await interaction.response.send_message(
            "This raid has already been calculated and completed.", ephemeral=True
        )
        return

    item_key = item.strip()
    if item_key not in raid["items"]:
        available = [k for k, v in raid["items"].items() if not v["sold"]]
        available_str = ", ".join(f"`{a}`" for a in available) if available else "(none left)"
        await interaction.response.send_message(
            f"❌ Item `{item_key}` not found in this raid.\n"
            f"Remaining unsold items: {available_str}\n\n"
            f"Make sure the name matches exactly (underscores instead of spaces), "
            f"e.g. `/sold item:Gdn_ring price:1000`.",
            ephemeral=True,
        )
        return

    if raid["items"][item_key]["sold"]:
        await interaction.response.send_message(
            f"`{item_key}` was already marked as sold for "
            f"{fmt_gold(raid['items'][item_key]['price'])}G.",
            ephemeral=True,
        )
        return

    if price < 0:
        await interaction.response.send_message(
            "Price must be zero or greater.", ephemeral=True
        )
        return

    raid["items"][item_key]["sold"] = True
    raid["items"][item_key]["price"] = price
    save_raid(raid)

    remaining = [k for k, v in raid["items"].items() if not v["sold"]]
    if remaining:
        remaining_str = ", ".join(f"`{r}`" for r in remaining)
        await interaction.response.send_message(
            f"✅ Marked `{item_key}` as sold for **{fmt_gold(price)}G**.\n"
            f"Remaining items to sell: {remaining_str}"
        )
    else:
        await interaction.response.send_message(
            f"✅ Marked `{item_key}` as sold for **{fmt_gold(price)}G**.\n"
            f"All items sold! Calculating shares..."
        )
        raid["completed_at_ts"] = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
        result_text = calculate_and_format(raid)
        raid["status"] = "completed"
        raid["confirmed"] = []
        save_raid(raid)
        await thread.send(result_text)
        await thread.send(
            "Once you've received your share, run `/confirm` in this thread. "
            "When everyone has confirmed, this thread will close automatically."
        )


# --------------------------------------------------------------------------
# /confirm
# --------------------------------------------------------------------------


async def finalize_if_all_confirmed(raid: dict, thread: discord.Thread) -> bool:
    """If every player has confirmed, close out the raid: mark it closed,
    post a summary, and archive the thread. Returns True if it closed."""
    confirmed = raid.get("confirmed", [])
    total = len(raid["player_ids"])
    if len(confirmed) < total:
        return False

    closed_ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
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
