import os
import asyncio
import logging
import time
import json
from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
from aiohttp import web

# Logging Setup
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Environment Variables
API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")
SESSION_STRING = os.environ.get("SESSION_STRING")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID"))

COOLDOWN_TIME = 3600  # Manual post cooldown (1 hour)
auto_post_interval = 3600  # Default: 1 hour (in seconds)
last_post_time = 0

waiting_users = set()
waiting_autopost_users = set()
bot_users = set()

# System State Flags
auto_delete_enabled = True   # Auto delete feature toggle
is_auto_post_running = False # Track if Auto-Post is active
auto_post_task = None
auto_post_message = None     # Saved message object
last_sent_messages = {}      # {group_id: message_id} for deletion tracking

# Selected Groups Storage
GROUPS_FILE = "selected_groups.json"

def load_selected_groups():
    if os.path.exists(GROUPS_FILE):
        try:
            with open(GROUPS_FILE, "r") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()

def save_selected_groups(groups_set):
    try:
        with open(GROUPS_FILE, "w") as f:
            json.dump(list(groups_set), f)
    except Exception as e:
        logger.error(f"Failed to save groups: {e}")

selected_groups = load_selected_groups()

# Telethon Clients
userbot = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
bot = TelegramClient('bot_session', API_ID, API_HASH)

# Render Port Server Keep-Alive
async def handle(request):
    return web.Response(text="JisanV1 Engine is Alive!")

async def start_dummy_server():
    app = web.Application()
    app.router.add_get('/', handle)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logger.info(f"Dummy Web Server running on port {port}")

# Background Auto-Post Engine
async def auto_post_loop():
    global last_post_time, is_auto_post_running
    while is_auto_post_running:
        await asyncio.sleep(auto_post_interval)
        if not is_auto_post_running:
            break

        if auto_post_message and selected_groups:
            logger.info("⚡ Executing Scheduled Auto-Post...")
            for group_id in list(selected_groups):
                try:
                    # ১. আগের পোস্ট থাকলে অটো ডিলিট করা
                    if auto_delete_enabled and group_id in last_sent_messages:
                        try:
                            await userbot.delete_messages(group_id, last_sent_messages[group_id])
                        except Exception:
                            pass

                    # ২. নতুন পোস্ট দেওয়া
                    sent_msg = await userbot.send_message(group_id, auto_post_message)
                    last_sent_messages[group_id] = sent_msg.id
                    await asyncio.sleep(2)
                except Exception as e:
                    logger.error(f"Auto post error in {group_id}: {str(e)}")
            last_post_time = time.time()

@bot.on(events.NewMessage(pattern=r'^/start'))
async def start_handler(event):
    bot_users.add(event.sender_id)
    await event.reply(
        "🏴‍☠️ **Welcome to JisanV1_Bot!** 🩸\n\n"
        "👑 *The official View & Engagement automation bot by Jisan Brand.*\n\n"
        "⚡ **Available Commands:**\n"
        "• `/post` — Broadcast message to your target groups (1 hour cooldown)\n"
        "• `/setautopost` — অটো পোস্টের কন্টেন্ট সেভ করুন\n"
        "• `/autopost start` — অটো পোস্ট চালু করুন (ডিফল্ট ১ ঘণ্টা)\n"
        "• `/autopost stop` — অটো পোস্ট বন্ধ করুন\n"
        "• `/autopost time <hours>` — সময় পরিবর্তন করুন (যেমন: `/autopost time 2`)\n"
        "• `/autodelete on` / `off` — আগের পোস্ট অটো ডিলিট সেটিং\n"
        "• `/groups` — Select/Manage target groups (Admin only)\n"
        "• `/stats` — View total bot active users\n"
        "• `/help` — How to use this bot\n\n"
        "🎯 Send `/groups` first to select your target groups!"
    )

@bot.on(events.NewMessage(pattern=r'^/help'))
async def help_handler(event):
    bot_users.add(event.sender_id)
    await event.reply(
        "📚 **Help & Instructions** ⚔️\n\n"
        "1. First, send `/groups` to select which groups to target (Admin Only).\n"
        "2. Click on the group buttons to turn them **✅ ON** or **❌ OFF**.\n"
        "3. Send `/setautopost` to set content, then `/autopost start` to begin (1 hour interval by default).\n"
        "4. Change interval anytime using `/autopost time 2` (for 2 hours).\n"
        "5. Send `/post` command to initiate manual broadcast (works when auto-post is STOPPED)."
    )

# --- Admin Only Group Manager ---
@bot.on(events.NewMessage(pattern=r'^/groups'))
async def groups_manager(event):
    if event.sender_id != ADMIN_ID:
        await event.reply("❌ **Unauthorized Access! Only Admin can manage target groups.**")
        return

    msg = await event.reply("⏳ **Fetching joined groups from Userbot...**")
    try:
        dialogs = await userbot.get_dialogs()
        group_dialogs = [d for d in dialogs if d.is_group]

        if not group_dialogs:
            await msg.edit("❌ **No groups found in your Userbot account!**")
            return

        buttons = []
        for g in group_dialogs:
            is_active = g.id in selected_groups
            status_symbol = "✅" if is_active else "❌"
            button_text = f"{status_symbol} {g.title[:25]}"
            buttons.append([Button.inline(button_text, data=f"toggle_{g.id}")])

        buttons.append([Button.inline("💾 Save & Close", data="close_groups")])

        await msg.edit(
            "⚙️ **Target Group Selection** ⚔️\n\n"
            "Click a button to toggle target status:\n"
            "• **✅** = Post will be sent here\n"
            "• **❌** = Post will NOT be sent here",
            buttons=buttons
        )
    except Exception as e:
        logger.error(f"Error fetching groups: {str(e)}")
        await msg.edit(f"❌ **Error fetching groups:** `{str(e)[:100]}`")

@bot.on(events.CallbackQuery(pattern=r'^toggle_(-?\d+)'))
async def toggle_group_callback(event):
    if event.sender_id != ADMIN_ID:
        await event.answer("❌ Admin action only!", alert=True)
        return

    group_id = int(event.data_match.group(1))

    if group_id in selected_groups:
        selected_groups.remove(group_id)
    else:
        selected_groups.add(group_id)

    save_selected_groups(selected_groups)

    try:
        dialogs = await userbot.get_dialogs()
        group_dialogs = [d for d in dialogs if d.is_group]

        buttons = []
        for g in group_dialogs:
            is_active = g.id in selected_groups
            status_symbol = "✅" if is_active else "❌"
            button_text = f"{status_symbol} {g.title[:25]}"
            buttons.append([Button.inline(button_text, data=f"toggle_{g.id}")])

        buttons.append([Button.inline("💾 Save & Close", data="close_groups")])

        await event.edit("⚙️ **Target Group Selection** ⚔️\n\nClick a button to toggle target status:", buttons=buttons)
        await event.answer("Updated!")
    except Exception as e:
        logger.error(f"Failed to update keyboard: {e}")

@bot.on(events.CallbackQuery(pattern=r'^close_groups$'))
async def close_groups_callback(event):
    if event.sender_id != ADMIN_ID:
        await event.answer("❌ Admin action only!", alert=True)
        return
    await event.edit(f"✅ **Target Groups Saved!**\n\n🎯 Total selected groups: `{len(selected_groups)}`\n\nYou can now run `/post` to broadcast.")

@bot.on(events.NewMessage(pattern=r'^/stats'))
async def stats_handler(event):
    current_hours = int(auto_post_interval / 3600)
    await event.reply(
        f"📊 **JisanV1 Engine Statistics** 💀\n\n"
        f"👥 Total unique bot users: `{len(bot_users)}`\n"
        f"🎯 Active target groups set: `{len(selected_groups)}`\n"
        f"⏱️ Auto-Post Interval: `{current_hours} Hour(s)`\n"
        f"🔄 Auto-Post Status: `{'🟢 ACTIVE' if is_auto_post_running else '🔴 OFF'}`"
    )

# --- Auto Delete Toggle ---
@bot.on(events.NewMessage(pattern=r'^/autodelete\s+(on|off)$'))
async def autodelete_toggle(event):
    global auto_delete_enabled
    mode = event.pattern_match.group(1).lower()
    if mode == "on":
        auto_delete_enabled = True
        await event.reply("🗑️ **Auto Delete Enabled!**\nনতুন পোস্ট করার সময় আগের পোস্ট অটো-ডিলিট হয়ে যাবে।")
    else:
        auto_delete_enabled = False
        await event.reply("🚫 **Auto Delete Disabled!**\nআগের পোস্ট মুছে ফেলা হবে না।")

# --- Auto Post Management Commands ---
@bot.on(events.NewMessage(pattern=r'^/setautopost$'))
async def set_autopost_prompt(event):
    waiting_autopost_users.add(event.sender_id)
    await event.reply("📝 **Auto-Post কন্টেন্ট সেটিং:**\nঅটো-পোস্টের জন্য যে মেসেজটি (Text/Photo/Video) পাঠাতে চান তা এখন সেন্ড করুন:")

@bot.on(events.NewMessage(pattern=r'^/autopost(?:\s+(start|stop|time(?:\s+\d+)?))?$'))
async def autopost_control(event):
    global is_auto_post_running, auto_post_task, auto_post_interval

    args = event.text.split()
    action = args[1].lower() if len(args) > 1 else ""

    if action == "start":
        if not auto_post_message:
            await event.reply("❌ আগে `/setautopost` দিয়ে অটো-পোস্টের কন্টেন্ট সেভ করুন!")
            return
        if is_auto_post_running:
            await event.reply("⚠️ Auto-Post সার্ভিস আগে থেকেই চালু আছে!")
            return

        is_auto_post_running = True
        auto_post_task = asyncio.create_task(auto_post_loop())
        current_hours = int(auto_post_interval / 3600)
        await event.reply(f"🚀 **Auto-Post Engine Activated!**\nপ্রতি `{current_hours}` ঘণ্টা পর পর অটোমেটিক পোস্ট হতে থাকবে।")

    elif action == "stop":
        if is_auto_post_running:
            is_auto_post_running = False
            if auto_post_task and not auto_post_task.done():
                auto_post_task.cancel()
            await event.reply("🛑 **Auto-Post Engine Stopped!**\nএখন ম্যানুয়াল `/post` ব্রডকাস্ট করার জন্য বোট প্রস্তুত।")
        else:
            await event.reply("⚠️ Auto-Post বর্তমানে বন্ধই আছে।")

    elif action == "time":
        if len(args) > 2 and args[2].isdigit():
            hours = int(args[2])
            if hours < 1:
                await event.reply("❌ সময় অন্তত ১ ঘণ্টা বা তার বেশি হতে হবে!")
                return
            auto_post_interval = hours * 3600
            await event.reply(f"⏱️ **Auto-Post Time Interval Updated!**\nএখন থেকে প্রতি `{hours}` ঘণ্টা পর পর অটো-পোস্ট হবে।")
        else:
            await event.reply("⚠️ নিয়ম: `/autopost time <ঘণ্টা>` (উদাহরণ: ১ ঘণ্টা পর পর চালুর জন্য `/autopost time 1`, ২ ঘণ্টার জন্য `/autopost time 2`)")
    else:
        status = "🟢 ACTIVE" if is_auto_post_running else "🔴 OFF"
        current_hours = int(auto_post_interval / 3600)
        await event.reply(
            f"ℹ️ **Auto-Post Status:** `{status}`\n"
            f"⏱️ **Current Interval:** `{current_hours} Hour(s)`\n\n"
            f"• `/autopost start` — চালু করতে\n"
            f"• `/autopost stop` — বন্ধ করতে\n"
            f"• `/autopost time 2` — সময় পরিবর্তন করতে (ঘণ্টা হিসেবে)"
        )

# --- Manual Post Command (Blocked when Auto-Post is ON) ---
@bot.on(events.NewMessage(pattern=r'^/post$'))
async def post_handler(event):
    global last_post_time
    user_id = event.sender_id
    bot_users.add(user_id)

    # Auto Post চালু থাকলে /post ব্লক থাকবে
    if is_auto_post_running:
        await event.reply(
            "⚠️ **Manual `/post` Disabled!**\n\n"
            "বর্তমানে **Auto-Post System** চালু আছে। ম্যানুয়ালি ব্রডকাস্ট করতে চাইলে আগে `/autopost stop` দিয়ে অটো-পোস্ট বন্ধ করুন।"
        )
        return

    if not selected_groups:
        await event.reply("❌ **No target groups selected!**\n\nPlease run `/groups` first to select target groups.")
        return

    current_time = time.time()
    elapsed = current_time - last_post_time

    if elapsed < COOLDOWN_TIME:
        remaining = int(COOLDOWN_TIME - elapsed)
        mins, secs = remaining // 60, remaining % 60
        await event.reply(f"⏳ **Cooldown Active!** ☠️\n\nPlease wait **{mins}m {secs}s** before next broadcast.")
        return

    waiting_users.add(user_id)
    await event.reply(f"📨 **Ready for Broadcast Execution** ⚔️\n\n🎯 Target Groups: **{len(selected_groups)}**\nSend/forward your message (Text, Photo, or Video) now:")

@bot.on(events.NewMessage)
async def content_handler(event):
    global last_post_time, auto_post_message
    user_id = event.sender_id

    # ১. Auto-Post কন্টেন্ট সেভ
    if user_id in waiting_autopost_users:
        if event.text and event.text.startswith('/'):
            return
        waiting_autopost_users.remove(user_id)
        auto_post_message = event.message
        await event.reply("✅ **Auto-Post কন্টেন্ট সফলভাবে সেভ হয়েছে!**\nচালু করতে লিখুন: `/autopost start`")
        return

    # ২. ম্যানুয়াল পোস্ট সেন্ড
    if user_id in waiting_users:
        if event.text and event.text.startswith('/'):
            return

        waiting_users.remove(user_id)
        processing_msg = await event.reply("⏳ **Processing... Initiating targeted broadcast...** 💣")

        success_count = 0
        failed_count = 0

        for group_id in list(selected_groups):
            try:
                if auto_delete_enabled and group_id in last_sent_messages:
                    try:
                        await userbot.delete_messages(group_id, last_sent_messages[group_id])
                    except Exception:
                        pass

                sent_msg = await userbot.send_message(group_id, event.message)
                last_sent_messages[group_id] = sent_msg.id
                success_count += 1
                await asyncio.sleep(2)
            except Exception as e:
                failed_count += 1
                logger.error(f"Failed to post to group {group_id}: {str(e)}")
                await asyncio.sleep(1)

        last_post_time = time.time()

        await processing_msg.edit(
            f"✅ **Targeted Broadcast Complete** GF\n\n"
            f"📊 **Summary:**\n"
            f"• **Successful**: {success_count} groups ⚡\n"
            f"• **Failed**: {failed_count} groups ❌\n\n"
            f"⏱️ Next broadcast system reset in 1 hour."
        )

async def main():
    logger.info("Starting JisanV1 Engine (Telegram Userbot + Controller Bot)...")
    await start_dummy_server()
    await userbot.start()
    await bot.start(bot_token=BOT_TOKEN)
    logger.info("🚀 JisanV1 Engine is Online!")
    await asyncio.gather(
        userbot.run_until_disconnected(),
        bot.run_until_disconnected()
    )

if __name__ == '__main__':
    asyncio.run(main())
