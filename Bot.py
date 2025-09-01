import requests
import random
import string
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from pymongo.mongo_client import MongoClient
from pymongo.server_api import ServerApi
import re

# -----------------------------
# تنظیمات
# -----------------------------
TOKEN = "7797893271:AAHJctebcYylKYYw26PVoAN7OCfN8JGZck4"
CHANNEL_ID = "@SIGLONA"

# MongoDB Atlas connection
MONGO_URI = "mongodb+srv://siglona:0929273826sS@cluster0.bdgw2km.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0"
client = MongoClient(MONGO_URI, server_api=ServerApi('1'))
db = client["Bot_User"]
users_collection = db["users"]

# تست اتصال
try:
    client.admin.command("ping")
    print("✅ Connected to MongoDB Atlas")
except Exception as e:
    print("❌ MongoDB Connection Error:", e)

# -----------------------------
# تابع escape برای MarkdownV2
# -----------------------------
def escape_md(text: str) -> str:
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

# -----------------------------
# تولید کد دعوت
# -----------------------------
def generate_invite_code():
    code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
    return f"Siglona_{code}"

# -----------------------------
# ذخیره کاربر جدید
# -----------------------------
def save_new_user(user_id: int, username: str):
    existing_user = users_collection.find_one({"user_id": user_id})
    if existing_user:
        return existing_user["invite_code"]

    invite_code = generate_invite_code()
    users_collection.insert_one({
        "user_id": user_id,
        "username": username,
        "invite_code": invite_code
    })
    return invite_code

# -----------------------------
# چک عضویت
# -----------------------------
async def check_membership(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        member = await context.bot.get_chat_member(CHANNEL_ID, user_id)
        return member.status in ["member", "administrator", "creator", "restricted"]
    except Exception:
        return False

# -----------------------------
# گرفتن قیمت از CoinMarketCap
# -----------------------------
API_KEY = "2972e723-e7c1-4a1b-9532-80d3f90b43c3"

def get_price(symbol: str):
    url = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest"
    headers = {
        "Accepts": "application/json",
        "X-CMC_PRO_API_KEY": API_KEY
    }
    symbol = symbol.upper().replace("USDT", "")

    params = {
        "symbol": symbol,
        "convert": "USD"
    }
    try:
        response = requests.get(url, headers=headers, params=params, timeout=5)
        data = response.json()
        if "data" in data and symbol in data["data"]:
            return float(data["data"][symbol]["quote"]["USD"]["price"])
    except Exception as e:
        print(f"Error fetching price: {e}")
    return None

# -----------------------------
# منوی اصلی قیمت ارزها
# -----------------------------
async def show_price_menu(update_or_query, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("💰 BTCUSDT", callback_data="BTCUSDT")],
        [InlineKeyboardButton("💰 ETHUSDT", callback_data="ETHUSDT")],
        [InlineKeyboardButton("💰 BNBUSDT", callback_data="BNBUSDT")],
        [InlineKeyboardButton("🔙 بازگشت به منو اصلی", callback_data="main_menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if isinstance(update_or_query, Update):
        await update_or_query.message.reply_text("یکی از ارزها را انتخاب کنید:", reply_markup=reply_markup)
    else:
        await update_or_query.edit_message_text("یکی از ارزها را انتخاب کنید:", reply_markup=reply_markup)

# -----------------------------
# منوی اولیه بعد از /start
# -----------------------------
async def show_start_menu(update_or_query, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("💰 مشاهده قیمت ارزها", callback_data="prices")],
        [InlineKeyboardButton("🎟️ دریافت کد دعوت", callback_data="invite")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if isinstance(update_or_query, Update):
        await update_or_query.message.reply_text("سلام! یکی از گزینه‌ها را انتخاب کن:", reply_markup=reply_markup)
    else:
        await update_or_query.edit_message_text("سلام! یکی از گزینه‌ها را انتخاب کن:", reply_markup=reply_markup)

# -----------------------------
# /start
# -----------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    username = user.username or f"user_{user_id}"

    if not await check_membership(user_id, context):
        keyboard = [
            [InlineKeyboardButton("📢 عضویت در کانال", url="https://t.me/SIGLONA")],
            [InlineKeyboardButton("✅ عضو شدم", callback_data="check_again")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "⚠️ لطفاً برای استفاده از ربات در کانال عضو شوید:",
            reply_markup=reply_markup,
        )
        return

    # ذخیره کاربر
    save_new_user(user_id, username)

    # نمایش منوی شروع
    await show_start_menu(update, context)

# -----------------------------
# هندلر کلیک روی دکمه‌ها
# -----------------------------
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id

    # چک دوباره عضویت
    if query.data == "check_again":
        if await check_membership(user_id, context):
            await show_start_menu(query, context)
        else:
            keyboard = [
                [InlineKeyboardButton("📢 عضویت در کانال", url="https://t.me/SIGLONA")],
                [InlineKeyboardButton("✅ عضو شدم", callback_data="check_again")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                "❌ هنوز عضو کانال نیستی!\nلطفاً عضو شوید:",
                reply_markup=reply_markup
            )
        return

    # برگشت به منوی اصلی
    if query.data == "main_menu":
        await show_start_menu(query, context)
        return

    # مشاهده قیمت ارزها
    if query.data == "prices":
        await show_price_menu(query, context)
        return

    # دریافت کد دعوت
    if query.data == "invite":
        user_data = users_collection.find_one({"user_id": user_id})
        invite_code = user_data["invite_code"] if user_data else "خطا!"
        await query.edit_message_text(
            escape_md(f"🎟️ کد دعوت اختصاصی شما:\n`{invite_code}`"),
            parse_mode="MarkdownV2"
        )
        return

    # دریافت قیمت یک ارز مشخص
    symbol = query.data
    price = get_price(symbol)
    if price:
        keyboard = [[InlineKeyboardButton("🔙 بازگشت به منو ارزها", callback_data="prices")]]
        await query.edit_message_text(f"💰 قیمت {symbol}: {price:.2f} USDT", reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await query.edit_message_text("❌ خطا در دریافت قیمت یا نماد نامعتبر است.")

# -----------------------------
# اجرای ربات
# -----------------------------
def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    print("🤖 Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
