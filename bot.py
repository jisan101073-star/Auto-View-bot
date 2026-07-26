import os
import random
import logging
import asyncio
from threading import Thread
from flask import Flask
from telegram.ext import Application, MessageHandler, filters
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)

# বটের ঘুম আটকানোর জন্য Flask সার্ভার
app_flask = Flask(name)
@app_flask.route('/')
def home():
    return "Bot is running perfectly! 🚀"

def run_server():
    app_flask.run(host='0.0.0.0', port=int(os.environ.get("PORT", 8080)))

def get_tokens():
    tokens = []
    for i in range(1, 50):
        token = os.getenv(f"BOT_TOKEN_{i}")
        if token:
            tokens.append(token)
    return tokens

async def handle_incoming(update, context):
    target = update.channel_post or update.message
    if not target: return
    
    # প্রিমিয়াম এবং ইউনিক রিঅ্যাকশন লিস্ট
    emojis = [
        "❤️", "🔥", "🥰", "🎉", "🤩", "💯", "👌", "❤️‍🔥", "🫡", "😎", "🤗", "✨", "🙌", "🎈", "🌟",
        "⚡", "🔥", "🔥", "😻", "🌭", "😈", "💔", "❤️‍🔥", "💋", "💎", "🥰", "🤩", "👍", "🔥", "⚡",
        "🧡", "🦄", "🌈", "🎈", "🎉", "🔥", "🔥", "💯", "💖", "⚡", "🦋", "🍄", "🌷", "🍕", "🎸",
        "🚀", "🥂", "🏆", "🌟", "😎", "👾", "🔥", "🔥", "❤️"
    ]
    
    # সংঘর্ষ এড়ানোর জন্য খুবই সামান্য র‍্যান্ডম ডিলে
    await asyncio.sleep(random.uniform(0.1, 1.0)) 
    
    # ৫ বার রিট্রাই লজিক - কোনোভাবেই মিস করা যাবে না!
    for attempt in range(5):
        try:
            await context.bot.set_message_reaction(
                chat_id=target.chat_id,
                message_id=target.message_id,
                reaction=random.choice(emojis),
                is_big=True
            )
            return # সফল হলে কাজ শেষ
        except Exception as e:
            await asyncio.sleep(0.5)

async def main():
    # Flask চালু
    Thread(target=run_server, daemon=True).start()
    
    tokens = get_tokens()
    if not tokens: return

    for token in tokens:
        app = Application.builder().token(token).concurrent_updates(True).build()
        app.add_handler(MessageHandler(filters.ChatType.CHANNEL | filters.ChatType.GROUPS, handle_incoming))
        
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
    
    await asyncio.Event().wait()

if name == "main":
    asyncio.run(main())
