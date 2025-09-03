import os
import re
import time
import random
import string
from datetime import datetime

import requests
from pymongo.mongo_client import MongoClient
from pymongo.server_api import ServerApi
from pymongo.errors import DuplicateKeyError

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# ===========================
# تنظیمات — این‌ها را پر کن
# ===========================
TOKEN = "7797893271:AAHJctebcYylKYYw26PVoAN7OCfN8JGZck4"
CHANNEL_ID = "@SIGLONA"
MONGO_URI = "mongodb+srv://siglona:0929273826sS@cluster0.bdgw2km.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0"

# ===========================
# اتصال به دیتابیس
# ===========================
client = MongoClient(MONGO_URI, server_api=ServerApi("1"))
db = client["Bot_User"]
users = db["users"]

# ایندکس‌ها (اولین بار ایجاد می‌شن)
users.create_index("user_id", unique=True)
users.create_index("invite_code", unique=True)

try:
    client.admin.command("ping")
    print("✅ Connected to MongoDB Atlas")
except Exception as e:
    print("❌ MongoDB Connection Error:", e)

# ===========================
# ابزارها و کمکی‌ها
# ===========================
def escape_md(text: str) -> str:
    # برای MarkdownV2
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

def generate_invite_code() -> str:
    code = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
    return f"Siglona_{code}"

def upsert_user(user_id: int, username: str) -> dict:
    """کاربر را اگر نباشد می‌سازد؛ اگر باشد همان را برمی‌گرداند."""
    doc = users.find_one({"user_id": user_id})
    if doc:
        return doc
    invite_code = generate_invite_code()
    new_doc = {
        "user_id": user_id,
        "username": username,
        "invite_code": invite_code,
        # برای رفرال
        "inviter_id": None,          # ID کسی که دعوت کرده (بعد از تایید عضویت ست می‌شود)
        "invites_count": 0,          # تعداد دعوت‌های موفق این کاربر
        "ref_applied": False,        # آیا اعتبار رفرالش اعمال شده؟
        "pending_ref_code": None,    # کد رفرال که با start آمده تا بعد از تایید عضویت اعمال شود
        # وضعیت عضویت کانال
        "is_member": False,          # آخرین وضعیت بررسی‌شده عضویت کانال
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
    }
    try:
        users.insert_one(new_doc)
        return new_doc
    except DuplicateKeyError:
        return users.find_one({"user_id": user_id})

async def check_membership(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """عضویت در کانال را بررسی می‌کند."""
    try:
        member = await context.bot.get_chat_member(CHANNEL_ID, user_id)
        # member.status یکی از این‌هاست: creator, administrator, member, restricted, left, kicked
        return member.status in ["creator", "administrator", "member", "restricted"]
    except Exception:
        return False

# ===========================
# قیمت رمزارز — CoinGecko
# ===========================
# نگاشت نمادهای مرسوم به ID های کوین‌گکو
COINGECKO_SYMBOL_MAP = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "BNB": "binancecoin",
}

def coingecko_get_price(symbol_or_pair: str) -> float | None:
    """
    symbols ورودی می‌تونه مثل "BTCUSDT" یا "BTC" باشه.
    خروجی قیمت به دلار آمریکا (USD).
    """
    s = symbol_or_pair.upper().strip()
    # اگر به صورت BTCUSDT بود، قسمت اول را بردار
    if s.endswith("USDT"):
        s = s[:-4]
    # مپ کردن به ID کوین‌گکو
    cg_id = COINGECKO_SYMBOL_MAP.get(s)
    if not cg_id:
        return None

    url = "https://api.coingecko.com/api/v3/simple/price"
    try:
        resp = requests.get(url, params={"ids": cg_id, "vs_currencies": "usd"}, timeout=10)
        data = resp.json()
        return float(data[cg_id]["usd"])
    except Exception:
        return None

# ===========================
# دکمه‌ها / منوها
# ===========================
def join_channel_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("📢 عضویت در کانال", url=f"https://t.me/{CHANNEL_ID.lstrip('@')}")],
        [InlineKeyboardButton("✅ عضو شدم", callback_data="check_again")],
    ]
    return InlineKeyboardMarkup(keyboard)

def main_menu_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("💰 مشاهده قیمت ارزها", callback_data="prices")],
        [InlineKeyboardButton("🎟️ لینک دعوت من", callback_data="invite_link")],
    ]
    return InlineKeyboardMarkup(keyboard)

def prices_menu_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("💰 BTCUSDT", callback_data="PRICE:BTCUSDT")],
        [InlineKeyboardButton("💰 ETHUSDT", callback_data="PRICE:ETHUSDT")],
        [InlineKeyboardButton("💰 BNBUSDT", callback_data="PRICE:BNBUSDT")],
        [InlineKeyboardButton("🔙 بازگشت", callback_data="main_menu")],
    ]
    return InlineKeyboardMarkup(keyboard)

# ===========================
# منطق رفرال
# ===========================
def parse_ref_from_args(args: list[str]) -> str | None:
    """
    انتظار: /start ref_Siglona_ABC123
    یا /start Siglona_ABC123
    """
    if not args:
        return None
    raw = args[0].strip()
    m = re.match(r"^ref_(Siglona_[A-Z0-9]{6})$", raw, re.IGNORECASE)
    if m:
        return m.group(1)
    m2 = re.match(r"^(Siglona_[A-Z0-9]{6})$", raw, re.IGNORECASE)
    if m2:
        return m2.group(1)
    return None

def set_pending_ref_if_valid(current_user_id: int, ref_code: str) -> None:
    """
    اگر کد معتبر بود و خودزنی نبود، کد را به عنوان pending نگه می‌داریم.
    اعمال امتیاز به بعد از تایید عضویت موکول می‌شود.
    """
    inviter = users.find_one({"invite_code": ref_code})
    if not inviter:
        return
    if inviter["user_id"] == current_user_id:
        return  # خودش خودش را دعوت نکند
    users.update_one(
        {"user_id": current_user_id},
        {"$set": {"pending_ref_code": ref_code, "updated_at": datetime.utcnow()}}
    )

def apply_referral_if_needed(current_user_id: int) -> None:
    """
    فقط وقتی صدا زده می‌شود که کاربر عضو کانال شده.
    اگر pending_ref_code داشت و ref_applied=False، امتیاز را اعمال می‌کنیم.
    """
    me = users.find_one({"user_id": current_user_id})
    if not me:
        return
    if me.get("ref_applied"):
        return
    ref_code = me.get("pending_ref_code")
    if not ref_code:
        return

    inviter = users.find_one({"invite_code": ref_code})
    if not inviter:
        # کد نامعتبر؛ پاکش کن
        users.update_one(
            {"user_id": current_user_id},
            {"$set": {"pending_ref_code": None, "updated_at": datetime.utcnow()}}
        )
        return

    # ثبت اینکه این کاربر توسط inviter دعوت شده
    users.update_one(
        {"user_id": current_user_id},
        {"$set": {
            "inviter_id": inviter["user_id"],
            "ref_applied": True,
            "updated_at": datetime.utcnow(),
        }}
    )
    # یکی به تعداد دعوت‌های موفق دعوت‌کننده اضافه کن
    users.update_one(
        {"user_id": inviter["user_id"]},
        {"$inc": {"invites_count": 1}, "$set": {"updated_at": datetime.utcnow()}}
    )
    # دیگه pending لازم نیست
    users.update_one(
        {"user_id": current_user_id},
        {"$set": {"pending_ref_code": None}}
    )

# ===========================
# منوها/اکشن‌ها
# ===========================
async def show_main_menu(update_or_query, context: ContextTypes.DEFAULT_TYPE):
    text = "سلام! یکی از گزینه‌ها را انتخاب کن:"
    if isinstance(update_or_query, Update):
        await update_or_query.message.reply_text(text, reply_markup=main_menu_keyboard())
    else:
        await update_or_query.edit_message_text(text, reply_markup=main_menu_keyboard())

async def show_prices_menu(update_or_query, context: ContextTypes.DEFAULT_TYPE):
    text = "یکی از ارزها را انتخاب کنید:"
    if isinstance(update_or_query, Update):
        await update_or_query.message.reply_text(text, reply_markup=prices_menu_keyboard())
    else:
        await update_or_query.edit_message_text(text, reply_markup=prices_menu_keyboard())

# ===========================
# /start — ورود و رفرال
# ===========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    username = user.username or f"user_{user_id}"

    # کاربر را ایجاد/آپدیت کن
    doc = upsert_user(user_id, username)

    # اگر کد رفرال در /start بود، فعلاً نگه می‌داریم تا بعد از عضویت اعمال بشه
    ref_code = parse_ref_from_args(context.args)
    if ref_code:
        set_pending_ref_if_valid(user_id, ref_code)

    # بررسی عضویت
    is_member = await check_membership(user_id, context)
    users.update_one(
        {"user_id": user_id},
        {"$set": {"is_member": is_member, "updated_at": datetime.utcnow()}}
    )

    if not is_member:
        await update.message.reply_text(
            "⚠️ لطفاً ابتدا در کانال عضو شوید، سپس روی «✅ عضو شدم» بزنید.",
            reply_markup=join_channel_keyboard(),
        )
        return

    # اگر تازه عضو شده و pending_ref_code داشت، حالا امتیاز رفرال را اعمال کن
    apply_referral_if_needed(user_id)

    # منوی اصلی
    await show_main_menu(update, context)

# ===========================
# هندلر دکمه‌ها
# ===========================
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    # کاربر باید در دیتابیس باشد
    doc = upsert_user(user_id, query.from_user.username or f"user_{user_id}")

    data = query.data or ""

    # چک عضویت مجدد
    if data == "check_again":
        is_member = await check_membership(user_id, context)
        users.update_one(
            {"user_id": user_id},
            {"$set": {"is_member": is_member, "updated_at": datetime.utcnow()}}
        )
        if not is_member:
            await query.edit_message_text(
                "❌ هنوز عضو کانال نیستی!\nلطفاً عضو شو و دوباره امتحان کن:",
                reply_markup=join_channel_keyboard(),
            )
            return
        # الان عضو است → اگر رفرال در انتظار داریم، اعمالش کن
        apply_referral_if_needed(user_id)
        await show_main_menu(query, context)
        return

    # برگشت به منوی اصلی
    if data == "main_menu":
        await show_main_menu(query, context)
        return

    # منوی قیمت‌ها
    if data == "prices":
        await show_prices_menu(query, context)
        return

    # لینک دعوت اختصاصی
    if data == "invite_link":
        me = users.find_one({"user_id": user_id})
        my_code = me.get("invite_code")
        # برای ساخت لینک، نیاز داریم یوزرنیم بات را از تلگرام بگیریم
        me_bot = await context.bot.get_me()
        bot_username = me_bot.username
        deep_link = f"https://t.me/{bot_username}?start=ref_{my_code}"

        invites_count = me.get("invites_count", 0)
        text = (
            "🎟️ لینک دعوت اختصاصی شما:\n"
            f"{escape_md(deep_link)}\n\n"
            f"👥 تعداد دعوت‌های موفق: {invites_count}"
        )
        await query.edit_message_text(text, parse_mode="MarkdownV2", reply_markup=main_menu_keyboard())
        return

    # قیمت — دکمه‌های قیمت به صورت PRICE:SYMBOL هستند
    if data.startswith("PRICE:"):
        symbol = data.split(":", 1)[1]
        price = coingecko_get_price(symbol)
        if price is None:
            await query.edit_message_text("❌ خطا در دریافت قیمت یا نماد نامعتبر است.", reply_markup=prices_menu_keyboard())
            return
        txt = f"💰 قیمت {symbol}: {price:,.2f} USD"
        await query.edit_message_text(txt, reply_markup=prices_menu_keyboard())
        return

# ===========================
# اجرا
# ===========================
def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    print("🤖 Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
