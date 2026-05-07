from dotenv import load_dotenv
load_dotenv()
import discord
from discord.ext import commands, tasks
from discord import app_commands
import pymongo
import datetime
import requests
import asyncio
import os

# ===== CONFIG =====
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
MONGO_URI     = os.environ["MONGO_URI"]
DATABASE_NAME = os.environ.get("DATABASE_NAME", "auramc")
GROQ_API_KEY  = os.environ["GROQ_API_KEY"]

# ===== SETTINGS =====
AI_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"
AI_URL = "https://api.groq.com/openai/v1/chat/completions"
IGNORE_CHANNELS = ["bot-spam", "bot-commands"]
HISTORY_LIMIT = 25
BOT_COLOR = discord.Color.from_str("#5865F2")
DEVELOPER_ID = 864213870494220341  # Bot developer — can use !restart from any server

# ===== CONVERSATION MEMORY =====
conversation_history: dict = {}
MAX_HISTORY = 10

# ===== MONGODB SETUP =====
mongo_client = pymongo.MongoClient(MONGO_URI)
db = mongo_client[DATABASE_NAME]
state_col    = db["server_state"]
messages_col = db["messages"]
members_col  = db["members"]
roles_col    = db["roles"]
channels_col = db["channels"]
staff_col    = db["server_staff"]   # stores founder/owner/admin/senior-admin
config_col   = db["bot_config"]     # stores global bot settings like developer name

# ===== BOT CONFIG HELPERS =====
def _get_bot_config(key: str, default: str = "") -> str:
    doc = config_col.find_one({"key": key})
    return doc["value"] if doc else default

def _set_bot_config(key: str, value: str):
    config_col.update_one(
        {"key": key},
        {"$set": {"key": key, "value": value,
                  "updated_at": datetime.datetime.now(datetime.UTC)}},
        upsert=True
    )

async def get_bot_config(key: str, default: str = "") -> str:
    return await asyncio.to_thread(_get_bot_config, key, default)

async def set_bot_config(key: str, value: str):
    await asyncio.to_thread(_set_bot_config, key, value)

# ===== PLACEHOLDERS CONFIG =====
# These are all the keys users can edit with !edit or !rem set
PLACEHOLDERS = {
    "server_ip":      "The Minecraft server IP address",
    "version":        "The server Minecraft version (e.g. 1.21.11)",
    "discord_link":   "The Discord invite link",
    "rules_channel":  "Channel ID or name for server rules",
    "store_link":     "Link to the server store",
    "update":         "Latest update/patch notes",
    "current":        "Current server status or announcement",
    "owner":          "Server owner name or @mention",
    "admins":         "List of admins",
    "support":        "Support channel or contact info",
    "server_name":    "The name of the server",
    "discord_invite": "Discord invite link (alias for discord_link)",
    "store":          "Store link (alias for store_link)",
}

# ===== IN-MEMORY PRESENCE CACHE =====
presence_cache: dict = {}

# ==========================================================================
# STAFF HELPERS (NEW)
# ==========================================================================

def _get_staff(guild_id: str) -> dict:
    """Returns dict like { 'founder': '123456789', 'owner': '987654321', ... }"""
    doc = staff_col.find_one({"guild_id": guild_id})
    if doc:
        doc.pop("_id", None)
        doc.pop("guild_id", None)
        return doc
    return {}

def _set_staff(guild_id: str, role: str, user_id: str, display_name: str):
    """Save a staff role (founder/owner/admin/senior-admin) to MongoDB."""
    staff_col.update_one(
        {"guild_id": guild_id},
        {"$set": {
            role: user_id,
            f"{role}_name": display_name,
            "updated_at": datetime.datetime.now(datetime.UTC)
        }},
        upsert=True
    )

async def get_staff(guild_id: str) -> dict:
    return await asyncio.to_thread(_get_staff, guild_id)

async def set_staff(guild_id: str, role: str, user_id: str, display_name: str):
    await asyncio.to_thread(_set_staff, guild_id, role, user_id, display_name)

# ==========================================================================
# SYNC HELPERS
# ==========================================================================

def _get_state(guild_id: str) -> dict:
    return {doc["key"]: doc["value"] for doc in state_col.find({"guild_id": guild_id})}

def _set_state(guild_id: str, key: str, value):
    state_col.update_one(
        {"guild_id": guild_id, "key": key},
        {"$set": {"guild_id": guild_id, "key": key, "value": value,
                  "updated_at": datetime.datetime.now(datetime.UTC)}},
        upsert=True
    )

def _seed_guild_defaults(guild_id: str, guild_name: str):
    defaults = {
        "server_name":    guild_name,
        "server_ip":      "N/A",
        "version":        "N/A",
        "discord_link":   "N/A",
        "discord_invite": "N/A",
        "rules_channel":  "N/A",
        "store_link":     "N/A",
        "store":          "N/A",
        "update":         "N/A",
        "current":        "N/A",
        "owner":          "N/A",
        "admins":         "N/A",
        "support":        "N/A",
    }
    for key, value in defaults.items():
        if not state_col.find_one({"guild_id": guild_id, "key": key}):
            state_col.insert_one({"guild_id": guild_id, "key": key, "value": value})
    print(f"✅ Defaults seeded for guild: {guild_name} ({guild_id})")

def _save_message(message):
    try:
        if not message.content or not message.guild:
            return
        messages_col.update_one(
            {"message_id": str(message.id)},
            {"$set": {
                "message_id": str(message.id),
                "guild_id": str(message.guild.id),
                "channel": message.channel.name,
                "channel_id": str(message.channel.id),
                "author": message.author.display_name,
                "author_id": str(message.author.id),
                "content": message.content,
                "timestamp": message.created_at,
            }},
            upsert=True
        )
        all_ids = [d["_id"] for d in messages_col.find(
            {"channel_id": str(message.channel.id)}
        ).sort("timestamp", pymongo.DESCENDING)]
        if len(all_ids) > 500:
            messages_col.delete_many({"_id": {"$in": all_ids[500:]}})
    except Exception as e:
        print(f"_save_message error: {e}")

def _build_system_prompt(guild_id: str) -> str:
    state = _get_state(guild_id)
    staff = _get_staff(guild_id)   # NEW: load staff from MongoDB
    guild_filter = {"guild_id": guild_id}

    recent_msgs = list(messages_col.find(guild_filter).sort("timestamp", pymongo.DESCENDING).limit(30))

    channel_last = {}
    for m in recent_msgs:
        ch = m["channel"]
        if ch not in channel_last:
            channel_last[ch] = m

    recent_text = "\n".join(
        f"  #{ch}: [{m['author']}] {m['content'][:120]}"
        for ch, m in channel_last.items()
    ) or "  None yet."

    full_recent = "\n".join(
        f"  [{m['channel']}] {m['author']}: {m['content'][:100]}"
        for m in reversed(recent_msgs[:15])
    ) or "  None."

    roles = list(roles_col.find(guild_filter).sort("member_count", pymongo.DESCENDING))
    roles_text = "\n".join(
        f"  {r['name']} ({r['member_count']} members): {', '.join(r['members'][:10]) or 'none'}"
        for r in roles[:15]
    ) or "  None."

    channels = list(channels_col.find({**guild_filter, "type": "text"}))
    channels_text = ", ".join(
        f"#{c['name']} (<#{c['channel_id']}>)" for c in channels[:20]
    ) or "None"

    server_name = state.get("server_name", "this server")

    # Presence
    statuses = presence_cache.get(guild_id, {})
    online = [n for n, s in statuses.items() if s == "online"]
    idle   = [n for n, s in statuses.items() if s == "idle"]
    dnd    = [n for n, s in statuses.items() if s == "dnd"]
    active_count = len(online) + len(idle) + len(dnd)

    # NEW: Build staff section
    def staff_mention(role_key):
        uid = staff.get(role_key)
        name = staff.get(f"{role_key}_name", "Not set")
        if uid and uid != "Not set":
            return f"<@{uid}> ({name})"
        return "Not set"

    return f"""You are Rem, the official AI assistant for the {server_name} Discord server.
You are helpful, friendly, and always up to date with real-time server data.
Your name is Rem. Never refer to yourself by any other name.
When mentioning channels, always use their Discord mention format like <#channel_id>.
Keep answers concise. Use bullet points • for lists. No filler phrases.
You have memory of the current conversation — stay on topic and refer back to what was said earlier when relevant.
IMPORTANT: If anyone asks about your commands, tell them to use `!rem help` or `!rem list all` to see the real command list. Never make up or guess commands.
You were developed by {_get_bot_config('developer_name', 'Sam (Sam | Dev | Sleepwalker)')}. If anyone asks who made you, who developed you, or who is your creator, always say their name.

=== SERVER INFO ===
- Server Name: {server_name}
- Server IP: {state.get('server_ip', 'N/A')}
- Version: {state.get('version', 'N/A')}
- Discord Link: {state.get('discord_link', state.get('discord_invite', 'N/A'))}
- Rules Channel: {state.get('rules_channel', 'N/A')}
- Store: {state.get('store_link', state.get('store', 'N/A'))}
- Latest Update: {state.get('update', 'N/A')}
- Current Status: {state.get('current', 'N/A')}
- Owner: {state.get('owner', 'N/A')}
- Admins: {state.get('admins', 'N/A')}
- Support: {state.get('support', 'N/A')}
- Total Members: {state.get('total_members', '?')}
- Online Right Now: {active_count} active ({len(online)} online, {len(idle)} idle, {len(dnd)} do not disturb)
- Total Channels: {state.get('total_channels', '?')}
- Total Roles: {state.get('total_roles', '?')}

=== SERVER STAFF ===
- Founder: {staff_mention('founder')}
- Owner: {staff_mention('owner')}
- Admin: {staff_mention('admin')}
- Senior Admin: {staff_mention('senior_admin')}

=== WHO IS ONLINE RIGHT NOW ===
- Online: {', '.join(online[:20]) or 'none'}
- Idle: {', '.join(idle[:20]) or 'none'}
- Do Not Disturb: {', '.join(dnd[:20]) or 'none'}

=== TEXT CHANNELS (use these IDs for mentions) ===
{channels_text}

=== ROLES & WHO HAS THEM ===
{roles_text}

=== LAST MESSAGE IN EACH CHANNEL ===
{recent_text}

=== RECENT ACTIVITY (last 15 messages) ===
{full_recent}

Always answer using this real-time data. Be concise and friendly."""

def _ask_groq(prompt: str, guild_id: str, history: list) -> str:
    try:
        resp = requests.post(AI_URL, json={
            "model": AI_MODEL,
            "messages": [
                {"role": "system", "content": _build_system_prompt(guild_id)},
                *history,
                {"role": "user", "content": prompt}
            ],
            "max_tokens": 1024,
            "temperature": 0.7,
        }, headers={
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json"
        }, timeout=30)
        if resp.status_code != 200:
            return f"⚠️ API Error {resp.status_code}: {resp.text[:200]}"
        return resp.json()["choices"][0]["message"]["content"]
    except Exception as e:
        return f"⚠️ Error: {e}"

def _sync_server_data(guild):
    try:
        guild_id = str(guild.id)

        members_col.delete_many({"guild_id": guild_id})
        member_docs = [{
            "guild_id": guild_id,
            "user_id": str(m.id),
            "name": m.display_name,
            "username": str(m.name),
            "roles": [r.name for r in m.roles if r.name != "@everyone"],
            "joined_at": m.joined_at,
            "bot": m.bot,
        } for m in guild.members]
        if member_docs:
            members_col.insert_many(member_docs)

        roles_col.delete_many({"guild_id": guild_id})
        role_docs = []
        for r in guild.roles:
            if r.name == "@everyone":
                continue
            role_members = [m.display_name for m in r.members if not m.bot]
            role_docs.append({
                "guild_id": guild_id,
                "name": r.name,
                "member_count": len(role_members),
                "members": role_members[:50],
            })
        if role_docs:
            roles_col.insert_many(role_docs)

        channels_col.delete_many({"guild_id": guild_id})
        channel_docs = [{
            "guild_id": guild_id,
            "channel_id": str(c.id),
            "name": c.name,
            "type": str(c.type),
            "category": c.category.name if hasattr(c, "category") and c.category else "None"
        } for c in guild.channels]
        if channel_docs:
            channels_col.insert_many(channel_docs)

        _set_state(guild_id, "total_members", str(guild.member_count))
        _set_state(guild_id, "total_channels", str(len(guild.channels)))
        _set_state(guild_id, "total_roles", str(len(guild.roles) - 1))
        _set_state(guild_id, "server_name", guild.name)
        print(f"✅ [{guild.name}] Synced {guild.member_count} members, {len(guild.roles)} roles, {len(guild.channels)} channels")
    except Exception as e:
        print(f"_sync_server_data error for {guild.name}: {e}")

# ==========================================================================
# ASYNC WRAPPERS
# ==========================================================================

async def save_message(message):
    await asyncio.to_thread(_save_message, message)

async def get_state(guild_id: str) -> dict:
    return await asyncio.to_thread(_get_state, guild_id)

async def set_state(guild_id: str, key: str, value):
    await asyncio.to_thread(_set_state, guild_id, key, value)

async def seed_guild_defaults(guild_id: str, guild_name: str):
    await asyncio.to_thread(_seed_guild_defaults, guild_id, guild_name)

async def sync_server_data(guild):
    await asyncio.to_thread(_sync_server_data, guild)

async def ask_groq(prompt: str, guild_id: str, history: list) -> str:
    return await asyncio.to_thread(_ask_groq, prompt, guild_id, history)

async def fetch_channel_history(guild):
    print(f"📖 Fetching message history for {guild.name}...")
    for channel in guild.text_channels:
        if channel.name in IGNORE_CHANNELS:
            continue
        try:
            count = 0
            async for message in channel.history(limit=HISTORY_LIMIT, oldest_first=False):
                if not message.author.bot and message.content:
                    await save_message(message)
                    count += 1
            print(f"  ✅ #{channel.name}: {count} messages saved")
        except discord.Forbidden:
            print(f"  ⚠️ No access to #{channel.name}")
        except Exception as e:
            print(f"  ❌ #{channel.name} error: {e}")

# ==========================================================================
# BOT SETUP
# ==========================================================================

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)

# ==========================================================================
# AI REPLY HANDLER
# ==========================================================================

async def handle_ai_message(message, question: str):
    if not message.guild:
        return
    if not question.strip():
        await message.reply("Hey! Ask me something 😊 e.g. `!rem whats the last announcement?`")
        return

    channel_id = str(message.channel.id)
    guild_id = str(message.guild.id)

    # NEW: Check if this is a set staff command e.g. !rem set founder @user
    parts = question.strip().split()
    if len(parts) >= 3 and parts[0].lower() == "set":
        role_key = parts[1].lower().replace("-", "_")  # senior-admin -> senior_admin
        valid_roles = ["founder", "owner", "admin", "senior_admin"]
        if role_key in valid_roles and message.mentions:
            member = message.mentions[0]
            await set_staff(guild_id, role_key, str(member.id), member.display_name)
            role_display = role_key.replace("_", " ").title()
            await message.reply(f"✅ **{role_display}** has been set to {member.mention} and saved to database!")
            return

    if channel_id not in conversation_history:
        conversation_history[channel_id] = []

    history = conversation_history[channel_id]
    context = f"[Asked by: {message.author.display_name} in #{message.channel.name}]\n{question}"

    async with message.channel.typing():
        reply = await ask_groq(context, guild_id, history)

        history.append({"role": "user", "content": context})
        history.append({"role": "assistant", "content": reply})

        if len(history) > MAX_HISTORY * 2:
            conversation_history[channel_id] = history[-(MAX_HISTORY * 2):]

        embed = discord.Embed(description=reply[:4000], color=BOT_COLOR)
        embed.set_author(name="Rem", icon_url=bot.user.display_avatar.url if bot.user.display_avatar else None)
        embed.set_footer(text=f"Asked by {message.author.display_name}", icon_url=message.author.display_avatar.url)
        await message.reply(embed=embed)

# ==========================================================================
# EVENTS
# ==========================================================================

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    raise error

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} ({bot.user.id})")
    print(f"✅ Connected to {len(bot.guilds)} guild(s)")

    try:
        synced = await bot.tree.sync()
        print(f"✅ Synced {len(synced)} slash commands globally")
    except Exception as e:
        print(f"❌ Command sync failed: {e}")

    for guild in bot.guilds:
        guild_id = str(guild.id)
        await seed_guild_defaults(guild_id, guild.name)
        await sync_server_data(guild)
        await fetch_channel_history(guild)

        for member in guild.members:
            if not member.bot:
                if guild_id not in presence_cache:
                    presence_cache[guild_id] = {}
                presence_cache[guild_id][member.display_name] = str(member.status)

        online_count = sum(1 for s in presence_cache.get(guild_id, {}).values() if s == "online")
        print(f"✅ [{guild.name}] Presence seeded — {online_count} online")

    if not full_sync.is_running():
        full_sync.start()

    # ── Send restart complete message if bot was restarted ─────────────────
    restart_channel_id = await get_bot_config("restart_channel_id")
    restart_guild_id   = await get_bot_config("restart_guild_id")
    if restart_channel_id and restart_guild_id:
        try:
            guild = bot.get_guild(int(restart_guild_id))
            if guild:
                channel = guild.get_channel(int(restart_channel_id))
                if channel:
                    embed = discord.Embed(
                        description="✅ **Restart complete!** I'm back online and fully synced.",
                        color=discord.Color.green()
                    )
                    await channel.send(embed=embed)
        except Exception as e:
            print(f"❌ Could not send restart message: {e}")
        # Clear so it doesn't send again on next normal startup
        await set_bot_config("restart_channel_id", "")
        await set_bot_config("restart_guild_id", "")


@bot.event
async def on_message(message):
    if message.author.bot or not message.guild:
        return

    if message.channel.name not in IGNORE_CHANNELS and message.content:
        await save_message(message)

    content = message.content.strip()
    content_lower = content.lower()

    # ── !sync (developer only) ─────────────────────────────────────────────
    if content_lower == "!sync":
        if message.author.id != DEVELOPER_ID:
            await message.reply("❌ You don't have permission to use this command.")
            return
        guild_id = str(message.guild.id)
        embed = discord.Embed(description="🔄 Syncing members, roles & channels...", color=BOT_COLOR)
        msg = await message.reply(embed=embed)
        try:
            # Step 1 — sync members/roles/channels
            await sync_server_data(message.guild)

            # Step 2 — refresh presence cache
            for member in message.guild.members:
                if not member.bot:
                    if guild_id not in presence_cache:
                        presence_cache[guild_id] = {}
                    presence_cache[guild_id][member.display_name] = str(member.status)

            online = sum(1 for s in presence_cache.get(guild_id, {}).values() if s == "online")

            # Step 3 — update message to show done
            embed = discord.Embed(title="✅ Sync Complete!", color=discord.Color.green())
            embed.add_field(name="👥 Members",  value=str(message.guild.member_count), inline=True)
            embed.add_field(name="🏷️ Roles",   value=str(len(message.guild.roles) - 1), inline=True)
            embed.add_field(name="💬 Channels", value=str(len(message.guild.channels)), inline=True)
            embed.add_field(name="🟢 Online",   value=str(online), inline=True)
            embed.set_footer(text=f"Synced by {message.author.display_name}")
            await msg.edit(embed=embed)

            # Step 4 — fetch message history in background (non-blocking)
            asyncio.create_task(fetch_channel_history(message.guild))

        except Exception as e:
            embed = discord.Embed(description=f"❌ Sync failed: {e}", color=discord.Color.red())
            await msg.edit(embed=embed)
        return

    # ── !rem setdev <name> (developer only) ───────────────────────────────
    if content_lower.startswith("!rem setdev "):
        if message.author.id != DEVELOPER_ID:
            await message.reply("❌ Only the bot developer can use this command.")
            return
        new_name = content[12:].strip()
        if not new_name:
            await message.reply("❌ Usage: `!rem setdev <your name>`")
            return
        await set_bot_config("developer_name", new_name)
        embed = discord.Embed(
            description=f"✅ Developer name updated to: **{new_name}**\nRem will now say she was developed by **{new_name}**.",
            color=discord.Color.green()
        )
        await message.reply(embed=embed)
        return

    # ── !placeholders ──────────────────────────────────────────────────────
    if content_lower == "!placeholders":
        lines = "\n".join(f"• `{k}` — {v}" for k, v in PLACEHOLDERS.items())
        embed = discord.Embed(
            title="📋 Available Config Placeholders",
            description=f"You can edit these using `!edit <key> <value>`\n\n{lines}",
            color=BOT_COLOR
        )
        embed.set_footer(text=f"Requested by {message.author.display_name}")
        await message.reply(embed=embed)
        return

    # ── !edit <key> <value> ────────────────────────────────────────────────
    if content_lower.startswith("!edit "):
        parts = content[6:].strip().split(" ", 1)
        if len(parts) < 2:
            await message.reply("❌ Usage: `!edit <key> <value>`\nRun `!placeholders` to see available keys.")
            return
        key, value = parts[0].lower(), parts[1]
        if key not in PLACEHOLDERS:
            await message.reply(f"❌ Unknown key `{key}`. Run `!placeholders` to see valid keys.")
            return
        guild_id = str(message.guild.id)
        await set_state(guild_id, key, value)
        embed = discord.Embed(
            description=f"✅ Updated **{key}** to: `{value}`",
            color=discord.Color.green()
        )
        await message.reply(embed=embed)
        return

    # ── !rem help / !rem list / !rem list all ─────────────────────────────
    if content_lower in ("!rem help", "!rem list", "!rem list all", "!rem commands"):
        embed = discord.Embed(
            title="📋 Rem — All Commands",
            color=BOT_COLOR
        )
        embed.add_field(name="🤖 AI Commands", value=(
            "`!rem <question>` — Ask Rem anything about the server\n"
            "`@Rem <question>` — Mention Rem to ask a question"
        ), inline=False)
        embed.add_field(name="⚙️ Config Commands", value=(
            "`!edit <key> <value>` — Edit a server info field\n"
            "`!placeholders` — Show all editable fields"
        ), inline=False)
        embed.add_field(name="👥 Staff Commands", value=(
            "`!rem set founder @user` — Set founder\n"
            "`!rem set owner @user` — Set owner\n"
            "`!rem set admin @user` — Set admin\n"
            "`!rem set senior_admin @user` — Set senior admin"
        ), inline=False)
        embed.add_field(name="📊 Slash Commands", value=(
            "`/rem` — Ask Rem a question\n"
            "`/serverinfo` — Show live server info\n"
            "`/staff` — Show current staff list\n"
            "`/roles` — Show all roles & members\n"
            "`/members` — Show recent members\n"
            "`/activity` — Show recent chat activity\n"
            "`/editinfo` — Edit server info *(admin only)*\n"
            "`/setstaff` — Set staff roles *(admin only)*"
        ), inline=False)
        if message.author.id == DEVELOPER_ID:
            embed.add_field(name="🔧 Developer Only", value=(
                "`!sync` — Force sync all members, roles & channels\n"
                "`!rem setdev <name>` — Change the developer name"
            ), inline=False)
        embed.set_footer(text=f"Requested by {message.author.display_name}")
        await message.reply(embed=embed)
        return

    # ── @Rem mention ───────────────────────────────────────────────────────
    if bot.user in message.mentions:
        question = message.content.replace(f"<@{bot.user.id}>", "").replace(f"<@!{bot.user.id}>", "").strip()
        await handle_ai_message(message, question)
        return

    # ── !rem ───────────────────────────────────────────────────────────────
    if content_lower.startswith("!rem"):
        question = content[4:].strip()
        await handle_ai_message(message, question)
        return

    await bot.process_commands(message)


@bot.event
async def on_guild_join(guild):
    print(f"🎉 Joined new guild: {guild.name} ({guild.id})")
    guild_id = str(guild.id)
    await seed_guild_defaults(guild_id, guild.name)
    await sync_server_data(guild)
    await fetch_channel_history(guild)


@bot.event
async def on_member_join(member):
    guild_id = str(member.guild.id)
    await asyncio.to_thread(members_col.update_one,
        {"user_id": str(member.id), "guild_id": guild_id},
        {"$set": {"name": member.display_name, "username": str(member.name),
                  "roles": [], "joined_at": member.joined_at, "bot": member.bot,
                  "guild_id": guild_id, "user_id": str(member.id)}},
        True
    )
    await set_state(guild_id, "total_members", str(member.guild.member_count))


@bot.event
async def on_member_remove(member):
    guild_id = str(member.guild.id)
    await asyncio.to_thread(members_col.delete_one, {"user_id": str(member.id), "guild_id": guild_id})
    await set_state(guild_id, "total_members", str(member.guild.member_count))


@bot.event
async def on_member_update(before, after):
    guild_id = str(after.guild.id)
    await asyncio.to_thread(members_col.update_one,
        {"user_id": str(after.id), "guild_id": guild_id},
        {"$set": {
            "roles": [r.name for r in after.roles if r.name != "@everyone"],
            "name": after.display_name
        }},
        True
    )
    for role in after.guild.roles:
        if role.name == "@everyone":
            continue
        role_members = [m.display_name for m in role.members if not m.bot]
        await asyncio.to_thread(roles_col.update_one,
            {"name": role.name, "guild_id": guild_id},
            {"$set": {"members": role_members[:50], "member_count": len(role_members)}},
            True
        )


@bot.event
async def on_message_edit(before, after):
    if after.author.bot or not after.content:
        return
    await asyncio.to_thread(messages_col.update_one,
        {"message_id": str(after.id)},
        {"$set": {"content": after.content, "edited": True}}
    )


@bot.event
async def on_message_delete(message):
    await asyncio.to_thread(messages_col.delete_one, {"message_id": str(message.id)})


@bot.event
async def on_guild_channel_create(channel):
    guild_id = str(channel.guild.id)
    await asyncio.to_thread(channels_col.update_one,
        {"channel_id": str(channel.id)},
        {"$set": {"channel_id": str(channel.id), "name": channel.name,
                  "type": str(channel.type), "guild_id": guild_id}},
        True
    )
    await set_state(guild_id, "total_channels", str(len(channel.guild.channels)))


@bot.event
async def on_guild_channel_delete(channel):
    guild_id = str(channel.guild.id)
    await asyncio.to_thread(channels_col.delete_one, {"channel_id": str(channel.id)})
    await set_state(guild_id, "total_channels", str(len(channel.guild.channels)))


@bot.event
async def on_presence_update(before, after):
    if after.bot or not after.guild:
        return
    guild_id = str(after.guild.id)
    if guild_id not in presence_cache:
        presence_cache[guild_id] = {}
    presence_cache[guild_id][after.display_name] = str(after.status)


@bot.event
async def on_guild_role_create(role):
    """Sync roles when a new role is created."""
    if role.name == "@everyone":
        return
    guild_id = str(role.guild.id)
    role_members = [m.display_name for m in role.members if not m.bot]
    await asyncio.to_thread(roles_col.update_one,
        {"name": role.name, "guild_id": guild_id},
        {"$set": {"name": role.name, "guild_id": guild_id,
                  "member_count": len(role_members), "members": role_members[:50]}},
        True
    )
    await set_state(guild_id, "total_roles", str(len(role.guild.roles) - 1))
    print(f"✅ Role created & saved: {role.name}")


@bot.event
async def on_guild_role_delete(role):
    """Remove role from DB when deleted."""
    guild_id = str(role.guild.id)
    await asyncio.to_thread(roles_col.delete_one, {"name": role.name, "guild_id": guild_id})
    await set_state(guild_id, "total_roles", str(len(role.guild.roles) - 1))
    print(f"🗑️ Role deleted & removed: {role.name}")


@bot.event
async def on_guild_role_update(before, after):
    """Sync role when it's renamed or members change."""
    guild_id = str(after.guild.id)
    role_members = [m.display_name for m in after.members if not m.bot]
    # If renamed, delete old entry
    if before.name != after.name:
        await asyncio.to_thread(roles_col.delete_one, {"name": before.name, "guild_id": guild_id})
    await asyncio.to_thread(roles_col.update_one,
        {"name": after.name, "guild_id": guild_id},
        {"$set": {"name": after.name, "guild_id": guild_id,
                  "member_count": len(role_members), "members": role_members[:50]}},
        True
    )


# ==========================================================================
# BACKGROUND TASK
# ==========================================================================

@tasks.loop(minutes=10)
async def full_sync():
    print("🔄 Running full server sync...")
    for guild in bot.guilds:
        await sync_server_data(guild)
    print("✅ Full sync complete.")

# ==========================================================================
# SLASH COMMANDS
# ==========================================================================

@bot.tree.command(name="rem", description="Ask Rem anything about this server")
@app_commands.describe(question="What do you want to ask Rem?")
async def rem_cmd(interaction: discord.Interaction, question: str):
    await interaction.response.defer()

    if not interaction.guild:
        await interaction.followup.send(
            "⚠️ I can only answer questions from inside a server I've been added to.",
            ephemeral=True
        )
        return

    guild_id = str(interaction.guild.id)
    channel_id = str(interaction.channel_id)
    await seed_guild_defaults(guild_id, interaction.guild.name)

    if channel_id not in conversation_history:
        conversation_history[channel_id] = []

    history = conversation_history[channel_id]
    context = f"[Asked by: {interaction.user.display_name}]\n{question}"
    reply = await ask_groq(context, guild_id, history)

    history.append({"role": "user", "content": context})
    history.append({"role": "assistant", "content": reply})

    if len(history) > MAX_HISTORY * 2:
        conversation_history[channel_id] = history[-(MAX_HISTORY * 2):]

    embed = discord.Embed(description=reply[:4000], color=BOT_COLOR)
    embed.set_author(name="Rem", icon_url=bot.user.display_avatar.url if bot.user.display_avatar else None)
    embed.set_footer(text=f"Asked by {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="setstaff", description="Set server staff roles (Admin only)")
@app_commands.describe(
    role="Role to set: founder, owner, admin, senior_admin",
    member="The member to assign to this role"
)
@app_commands.checks.has_permissions(manage_guild=True)
async def setstaff(interaction: discord.Interaction, role: str, member: discord.Member):
    """Slash command to set staff roles — saves permanently to MongoDB."""
    if not interaction.guild:
        await interaction.response.send_message("⚠️ This command only works inside a server.", ephemeral=True)
        return

    role_key = role.lower().replace("-", "_")
    valid_roles = ["founder", "owner", "admin", "senior_admin"]
    if role_key not in valid_roles:
        await interaction.response.send_message(
            f"❌ Invalid role. Choose from: `founder`, `owner`, `admin`, `senior_admin`",
            ephemeral=True
        )
        return

    await set_staff(str(interaction.guild.id), role_key, str(member.id), member.display_name)
    role_display = role_key.replace("_", " ").title()
    await interaction.response.send_message(f"✅ **{role_display}** set to {member.mention} and saved!", ephemeral=False)


@bot.tree.command(name="staff", description="Show current server staff")
async def staff_cmd(interaction: discord.Interaction):
    """Show who is set as founder/owner/admin/senior-admin."""
    if not interaction.guild:
        await interaction.response.send_message("⚠️ This command only works inside a server.", ephemeral=True)
        return

    guild_id = str(interaction.guild.id)
    staff = await get_staff(guild_id)

    embed = discord.Embed(title=f"👥 {interaction.guild.name} — Staff", color=BOT_COLOR)

    def fmt(role_key):
        uid = staff.get(role_key)
        name = staff.get(f"{role_key}_name", "Not set")
        if uid:
            return f"<@{uid}> ({name})"
        return "Not set"

    embed.add_field(name="👑 Founder",      value=fmt("founder"),      inline=False)
    embed.add_field(name="🛡️ Owner",        value=fmt("owner"),        inline=False)
    embed.add_field(name="⚙️ Admin",        value=fmt("admin"),        inline=False)
    embed.add_field(name="🔰 Senior Admin", value=fmt("senior_admin"), inline=False)
    embed.set_footer(text=f"Use /setstaff or !rem set founder @user to update")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="serverinfo", description="Show live server info")
async def serverinfo(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("⚠️ This command only works inside a server.", ephemeral=True)
        return

    guild_id = str(interaction.guild.id)
    await seed_guild_defaults(guild_id, interaction.guild.name)
    state = await get_state(guild_id)
    recent = await asyncio.to_thread(
        lambda: list(messages_col.find({"guild_id": guild_id}).sort("timestamp", pymongo.DESCENDING).limit(3))
    )

    embed = discord.Embed(title=f"🌐 {state.get('server_name', interaction.guild.name)} — Live Info", color=BOT_COLOR)
    embed.add_field(name="📡 Server IP",  value=f"`{state.get('server_ip', 'N/A')}`", inline=True)
    embed.add_field(name="📦 Version",    value=state.get("version", "N/A"), inline=True)
    embed.add_field(name="👥 Members",    value=state.get("total_members", "N/A"), inline=True)
    embed.add_field(name="💬 Channels",   value=state.get("total_channels", "N/A"), inline=True)
    embed.add_field(name="🏷️ Roles",      value=state.get("total_roles", "N/A"), inline=True)
    embed.add_field(name="🛒 Store",      value=state.get("store", "N/A"), inline=True)
    if state.get("discord_invite", "N/A") != "N/A":
        embed.add_field(name="🔗 Invite", value=state["discord_invite"], inline=True)

    if recent:
        activity = "\n".join(
            f"• #{m['channel']} **{m['author']}**: {m['content'][:60]}"
            for m in recent
        )
        embed.add_field(name="📋 Recent Activity", value=activity, inline=False)

    embed.set_footer(text=f"Live data • {datetime.datetime.now(datetime.UTC).strftime('%Y-%m-%d %H:%M UTC')}")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="roles", description="Show server roles and their members")
async def roles_cmd(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("⚠️ This command only works inside a server.", ephemeral=True)
        return

    guild_id = str(interaction.guild.id)
    roles = await asyncio.to_thread(
        lambda: list(roles_col.find({"guild_id": guild_id}).sort("member_count", pymongo.DESCENDING))
    )
    if not roles:
        await interaction.response.send_message("❌ No role data yet — try again in a minute.", ephemeral=True)
        return

    embed = discord.Embed(title=f"🏷️ {interaction.guild.name} Roles", color=BOT_COLOR)
    for r in roles[:15]:
        names = ", ".join(r["members"][:8]) or "No members"
        if r["member_count"] > 8:
            names += f" +{r['member_count'] - 8} more"
        embed.add_field(name=f"{r['name']} ({r['member_count']})", value=names, inline=False)
    embed.set_footer(text=f"Live data • {datetime.datetime.now(datetime.UTC).strftime('%Y-%m-%d %H:%M UTC')}")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="members", description="Show recent server members and their roles")
async def members_cmd(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("⚠️ This command only works inside a server.", ephemeral=True)
        return

    guild_id = str(interaction.guild.id)
    members = await asyncio.to_thread(
        lambda: list(members_col.find({"guild_id": guild_id, "bot": False}).sort("joined_at", pymongo.DESCENDING).limit(10))
    )
    if not members:
        await interaction.response.send_message("❌ No member data yet — try again after the bot has synced.", ephemeral=True)
        return

    embed = discord.Embed(title=f"👥 Recent Members — {interaction.guild.name}", color=BOT_COLOR)
    lines = []
    for m in members:
        roles_str = ", ".join(m["roles"]) if m.get("roles") else "No roles"
        joined = m["joined_at"].strftime("%b %d") if m.get("joined_at") else "?"
        lines.append(f"**{m['name']}** — {roles_str} *(joined {joined})*")
    embed.description = "\n".join(lines)
    embed.set_footer(text=f"Live data • {datetime.datetime.now(datetime.UTC).strftime('%Y-%m-%d %H:%M UTC')}")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="activity", description="Show recent chat activity")
async def activity_cmd(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("⚠️ This command only works inside a server.", ephemeral=True)
        return

    guild_id = str(interaction.guild.id)
    recent = await asyncio.to_thread(
        lambda: list(messages_col.find({"guild_id": guild_id}).sort("timestamp", pymongo.DESCENDING).limit(10))
    )
    if not recent:
        await interaction.response.send_message("❌ No message history yet — try again after the bot has been running.", ephemeral=True)
        return

    embed = discord.Embed(title=f"📋 Recent Activity — {interaction.guild.name}", color=BOT_COLOR)
    lines = [f"**#{m['channel']}** {m['author']}: {m['content'][:80]}" for m in reversed(recent)]
    embed.description = "\n".join(lines)
    embed.set_footer(text=f"Live data • {datetime.datetime.now(datetime.UTC).strftime('%Y-%m-%d %H:%M UTC')}")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="editinfo", description="Update server info (Admin only)")
@app_commands.describe(key="Field to update e.g. server_ip, version, store, discord_invite", value="New value")
@app_commands.checks.has_permissions(manage_guild=True)
async def editinfo(interaction: discord.Interaction, key: str, value: str):
    if not interaction.guild:
        await interaction.response.send_message("⚠️ This command only works inside a server.", ephemeral=True)
        return
    await set_state(str(interaction.guild.id), key.lower().replace(" ", "_"), value)
    await interaction.response.send_message(f"✅ Updated **{key}** → `{value}`", ephemeral=True)


# ==========================================================================
# RUN
# ==========================================================================
bot.run(DISCORD_TOKEN)