import os
import re
import random
import string
from datetime import datetime

import requests
from pymongo.mongo_client import MongoClient
from pymongo.server_api import ServerApi
from pymongo.errors import DuplicateKeyError
from dotenv import load_dotenv

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# ===========================
# تنظیمات
# ===========================
# بارگذاری متغیرهای env
load_dotenv()

TOKEN = os.getenv("TELEGRAM_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
CHANNEL_ID = os.getenv("CHANNEL_ID")
NEWS_API_KEY = os.getenv("NEWS_API_KEY")

# ===========================
# اتصال به دیتابیس
# ===========================
client = MongoClient(MONGO_URI, server_api=ServerApi("1"))
db = client["Bot_User"]
users = db["users"]

users.create_index("user_id", unique=True)
users.create_index("invite_code", unique=True)

try:
    client.admin.command("ping")
    print("✅ Connected to MongoDB Atlas")
except Exception as e:
    print("❌ MongoDB Connection Error:", e)

# ===========================
# ابزارها
# ===========================
def escape_md(text: str) -> str:
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

def generate_invite_code() -> str:
    code = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
    return f"Siglona_{code}"

def upsert_user(user_id: int, username: str) -> dict:
    doc = users.find_one({"user_id": user_id})
    if doc:
        return doc
    invite_code = generate_invite_code()
    new_doc = {
        "user_id": user_id,
        "username": username,
        "invite_code": invite_code,
        "inviter_id": None,
        "invites_count": 0,
        "ref_applied": False,
        "pending_ref_code": None,
        "is_member": False,
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
    }
    try:
        users.insert_one(new_doc)
        return new_doc
    except DuplicateKeyError:
        return users.find_one({"user_id": user_id})

async def check_membership(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        member = await context.bot.get_chat_member(CHANNEL_ID, user_id)
        return member.status in ["creator", "administrator", "member", "restricted"]
    except Exception:
        return False

# ===========================
# گرفتن لیست ارزها
# ===========================
def get_all_coins() -> dict[str, dict]:
    url = "https://api.coingecko.com/api/v3/coins/list"
    try:
        resp = requests.get(url, timeout=15)
        coins = resp.json()
        mapping = {}
        for c in coins:
            symbol = c["symbol"].upper()
            mapping[symbol] = {"id": c["id"], "name": c["name"]}
        return mapping
    except Exception as e:
        print("Coin list fetch error:", e)
        return {}

ALL_COINS = get_all_coins()
print(f"✅ Loaded {len(ALL_COINS)} coins from CoinGecko")

POPULAR_COINS = ["BTC", "ETH", "BNB", "USDT", "USDC", "XRP", "DOGE", "SOL", "TON", "TRX"]

def coingecko_get_price(cg_id: str) -> float | None:
    url = "https://api.coingecko.com/api/v3/simple/price"
    try:
        resp = requests.get(url, params={"ids": cg_id, "vs_currencies": "usd"}, timeout=10)
        data = resp.json()
        return float(data[cg_id]["usd"])
    except Exception:
        return None

# ===========================
# --- بخش جدید: کندل، RSI و میانگین های متحرک (بدون کتابخانه اضافی)
# ===========================
def fetch_ohlc_cg(cg_id: str, days: int = 30) -> list:
    """
    دریافت کندل‌های روزانه از CoinGecko (ohlc).
    خروجی: لیست کندل‌ها به صورت [timestamp, open, high, low, close]
    """
    url = f"https://api.coingecko.com/api/v3/coins/{cg_id}/ohlc"
    try:
        resp = requests.get(url, params={"vs_currency": "usd", "days": days}, timeout=15)
        data = resp.json()
        # API ممکن است خطا یا داده کم برگرداند؛ بررسی می‌کنیم
        if not isinstance(data, list):
            return []
        return data
    except Exception as e:
        print("fetch_ohlc_cg error:", e)
        return []

def simple_sma(values: list[float], window: int) -> float | None:
    """محاسبه SMA ساده برای لیست قیمت‌ها (آخرین مقدار نافذ)."""
    if not values or len(values) < window:
        return None
    last_window = values[-window:]
    return sum(last_window) / len(last_window)

def calculate_rsi(closes: list[float], period: int = 14) -> float | None:
    """
    محاسبه RSI به صورت دستی (فرمول استاندارد با میانگین‌گیری نمایی ساده از gains/losses).
    خروجی: مقدار RSI آخرین کندل.
    """
    if len(closes) < period + 1:
        return None

    # محاسبه تغییرات
    deltas = []
    for i in range(1, len(closes)):
        deltas.append(closes[i] - closes[i-1])

    # اول seed
    seed = deltas[:period]
    up = sum([d for d in seed if d > 0]) / period
    down = -sum([d for d in seed if d < 0]) / period
    if down == 0:
        rs = float('inf')  # خیلی بزرگ
    else:
        rs = up / down
    rsi = 100 - (100 / (1 + rs))

    # ادامه محاسبه به روش Wilder smoothing (EMA-like)
    avg_up = up
    avg_down = down
    for delta in deltas[period:]:
        up_val = max(delta, 0)
        down_val = -min(delta, 0)
        avg_up = (avg_up * (period - 1) + up_val) / period
        avg_down = (avg_down * (period - 1) + down_val) / period
        if avg_down == 0:
            rs = float('inf')
        else:
            rs = avg_up / avg_down
        rsi = 100 - (100 / (1 + rs))

    # اگر rs بی‌نهایت باشه، rsi نزدیک به 100 خواهد شد
    if rsi != rsi:  # NaN check
        return None
    return float(rsi)

def fetch_crypto_news(limit: int = 5) -> list[dict]:
    url = "https://newsapi.org/v2/everything"
    params = {
        "q": "cryptocurrency OR bitcoin OR ethereum",  # کلیدواژه‌ها
        "language": "fa",  # یا "en" برای انگلیسی
        "sortBy": "publishedAt",
        "pageSize": limit,
        "apiKey": NEWS_API_KEY
    }
    try:
        resp = requests.get(url, params=params, timeout=10)
        data = resp.json()
        if data.get("status") != "ok":
            return []
        return data.get("articles", [])
    except Exception as e:
        print("fetch_crypto_news error:", e)
        return []


def analyze_trend_with_rsi(cg_id: str) -> dict:
    """
    تحلیل روند با استفاده از:
      -کندل‌های 30 روزه (Close)
      -میانگین متحرک 10 و 30
      -RSI(14)
    خروجی: دیکشنری شامل وضعیت، مقادیر rsi, ma10, ma30 و پیام خطا در صورت وجود
    """
    try:
        ohlc = fetch_ohlc_cg(cg_id, days=30)
        if not ohlc or len(ohlc) < 10:
            return {"error": "داده کافی برای تحلیل وجود ندارد."}

        closes = [c[4] for c in ohlc if len(c) >= 5]
        if len(closes) < 10:
            return {"error": "داده قیمت کافی ثبت نشده."}

        # محاسبه MA10 و MA30 (SMA)
        ma10 = simple_sma(closes, 10)
        ma30 = simple_sma(closes, 30)

        # روند کلی مقایسه اولین و آخرین کندل 30 روز
        overall_trend = None
        if len(closes) >= 2:
            if closes[-1] > closes[0]:
                overall_trend = "صعودی"
            elif closes[-1] < closes[0]:
                overall_trend = "نزولی"
            else:
                overall_trend = "خنثی"

        # محاسبه RSI
        rsi = calculate_rsi(closes, period=14)

        # تصمیم‌گیری ترکیبی برای وضعیت نهایی
        # قواعد پیشنهادی:
        # - اگر MA10 > MA30 و RSI > 50 => صعودی
        # - اگر MA10 < MA30 و RSI < 50 => نزولی
        # - در غیر اینصورت خنثی
        combined = "خنثی"
        if ma10 is not None and ma30 is not None and rsi is not None:
            if ma10 > ma30 and rsi > 50:
                combined = "صعودی"
            elif ma10 < ma30 and rsi < 50:
                combined = "نزولی"
            else:
                # اگر روند کلی 30 روزه هم صعودی/نزولی قوی باشد، آنرا لحاظ کن
                if overall_trend == "صعودی" and rsi > 45:
                    combined = "صعودی"
                elif overall_trend == "نزولی" and rsi < 55:
                    combined = "نزولی"
                else:
                    combined = "خنثی"
        else:
            # اگر یکی از مقادیر موجود نیست، سعی کن با اطلاعات موجود نتیجه بده
            if rsi is not None:
                if rsi > 70:
                    combined = "احتمال اصلاح (شاخص RSI بالا)"
                elif rsi < 30:
                    combined = "احتمال برگشت/صعود (شاخص RSI پایین)"
                else:
                    combined = "خنثی"

        return {
            "combined": combined,
            "overall_trend": overall_trend,
            "rsi": rsi,
            "ma10": ma10,
            "ma30": ma30,
            "error": None
        }

    except Exception as e:
        print("analyze_trend_with_rsi error:", e)
        return {"error": f"خطا در تحلیل: {e}"}

# ===========================
# دکمه‌ها با طراحی شیشه‌ای و حرفه‌ای
# ===========================
def join_channel_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("✨ عضویت در کانال ✨", url=f"https://t.me/{CHANNEL_ID.lstrip('@')}")],
        [InlineKeyboardButton("✅ تأیید عضویت", callback_data="check_again")],
    ]
    return InlineKeyboardMarkup(keyboard)

def main_menu_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("💰 قیمت ارزها", callback_data="prices")],
        [InlineKeyboardButton("🎟️ لینک دعوت", callback_data="invite_link")],
        [InlineKeyboardButton("🏆 جدول برترین‌ها", callback_data="top_inviters")],
        [InlineKeyboardButton("📰 اخبار ارزها", callback_data="crypto_news")],
        [InlineKeyboardButton("👨‍💻 پشتیبانی", callback_data="support")],
        [InlineKeyboardButton("ℹ️ راهنما", callback_data="help")],
    ]
    return InlineKeyboardMarkup(keyboard)

def prices_menu_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton("₿ بیت‌کوین", callback_data="PRICE:BTC"),
            InlineKeyboardButton("🔶 اتریوم", callback_data="PRICE:ETH"),
        ],
        [
            InlineKeyboardButton("💎 بایننس", callback_data="PRICE:BNB"),
            InlineKeyboardButton("🔥 سولانا", callback_data="PRICE:SOL"),
        ],
        [
            InlineKeyboardButton("🌀 تتر", callback_data="PRICE:USDT"),
            InlineKeyboardButton("🐕 دوج‌کوین", callback_data="PRICE:DOGE"),
        ],
        [
            InlineKeyboardButton("🔍 جستجوی ارز", callback_data="search_coin"),
            InlineKeyboardButton("📊 تحلیل بازار", callback_data="market_analysis"),
        ],
        [InlineKeyboardButton("🔙 بازگشت به منوی اصلی", callback_data="main_menu")],
    ]
    return InlineKeyboardMarkup(keyboard)

def back_to_main_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("🔙 بازگشت به منوی اصلی", callback_data="main_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)

def back_to_prices_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("🔙 بازگشت به بخش قیمت‌ها", callback_data="prices")]
    ]
    return InlineKeyboardMarkup(keyboard)

# ===========================
# منوها با طراحی حرفه‌ای
# ===========================
async def show_main_menu(update_or_query, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = """
    🌟 *به ربات تحلیل ارزهای دیجیتال خوش آمدید* 🌟

    💎 *امکانات ربات:*
    • مشاهده قیمت لحظه‌ای ارزها
    • تحلیل تکنیکال پیشرفته
    • اخبار روز ارزهای دیجیتال
    • سیستم دعوت دوستان و دریافت پاداش

    لطفاً یکی از گزینه‌های زیر را انتخاب کنید:
    """

    if isinstance(update_or_query, Update):
        await update_or_query.message.reply_text(welcome_text, parse_mode="HTML", reply_markup=main_menu_keyboard())
    else:
        await update_or_query.edit_message_text(welcome_text, parse_mode="HTML", reply_markup=main_menu_keyboard())

async def show_prices_menu(update_or_query, context: ContextTypes.DEFAULT_TYPE):
    text = """
    💰 *بخش قیمت‌های ارز دیجیتال*

    🔸 می‌توانید از میان ارزهای پرطرفدار انتخاب کنید
    🔸 یا با استفاده از دکمه جستجو، ارز مورد نظر خود را پیدا کنید

    لطفاً یکی از گزینه‌ها را انتخاب کنید:
    """

    if isinstance(update_or_query, Update):
        await update_or_query.message.reply_text(text, parse_mode="Markdown", reply_markup=prices_menu_keyboard())
    else:
        await update_or_query.edit_message_text(text, parse_mode="Markdown", reply_markup=prices_menu_keyboard())

async def show_top_inviters(update_or_query, context: ContextTypes.DEFAULT_TYPE):
    top_users = users.find().sort("invites_count", -1).limit(5)
    
    text = "🏆 *برترین دعوت‌کنندگان* 🏆\n\n"
    
    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
    
    for i, u in enumerate(top_users):
        if i >= len(medals):
            break
            
        username = u.get("username") or f"user_{u.get('user_id')}"
        invites = u.get("invites_count", 0)
        text += f"{medals[i]} {escape_md(username)} - *{invites} دعوت*\n"
    
    text += "\nبرای افزایش رتبه خود، دوستان بیشتری دعوت کنید!"
    
    keyboard = [[InlineKeyboardButton("🔙 بازگشت به منوی اصلی", callback_data="main_menu")]]
    markup = InlineKeyboardMarkup(keyboard)
    
    if isinstance(update_or_query, Update):
        await update_or_query.message.reply_text(text, parse_mode="Markdown", reply_markup=markup)
    else:
        await update_or_query.edit_message_text(text, parse_mode="Markdown", reply_markup=markup)

# ===========================
# /start
# ===========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    username = user.username or f"user_{user_id}"
    
    # بررسی ارجاع
    if context.args and context.args[0].startswith('ref_'):
        ref_code = context.args[0][4:]
        referrer = users.find_one({"invite_code": ref_code})
        if referrer and referrer["user_id"] != user_id:
            users.update_one(
                {"user_id": referrer["user_id"]},
                {"$inc": {"invites_count": 1}}
            )
    
    doc = upsert_user(user_id, username)
    is_member = await check_membership(user_id, context)
    users.update_one({"user_id": user_id}, {"$set": {"is_member": is_member, "updated_at": datetime.utcnow()}})
    
    if not is_member:
        welcome_text = """
        🌟 *به ربات تحلیل ارزهای دیجیتال خوش آمدید* 🌟

        برای استفاده از تمامی امکانات ربات، لطفاً در کانال ما عضو شوید و سپس روی دکمه «تأیید عضویت» کلیک کنید.
        """
        await update.message.reply_text(welcome_text, parse_mode="Markdown", reply_markup=join_channel_keyboard())
        return
    
    await show_main_menu(update, context)

# ===========================
# هندلر دکمه‌ها
# ===========================
SEARCH_STATE = {}

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    doc = upsert_user(user_id, query.from_user.username or f"user_{user_id}")
    data = query.data or ""

    if data == "support":
        support_text = """
        👨‍💻 *پشتیبانی آنلاین*

        🌟 برای دریافت راهنمایی و پاسخ به سوالات خود، می‌توانید با پشتیبان ما در ارتباط باشید:

        🔹 *آیدی پشتیبان:* @SIGLONA_TRADER
        🔹 *ساعات پاسخگویی:* ۹ صبح تا ۱۲ شب
        🔹 *پاسخگویی:* حداکثر ۲ ساعت

        💡 *قبل از تماس:*
        • سوال خود را به صورت واضح بیان کنید
        • در صورت امکان اسکرین‌شот ارسال کنید
        • شماره کاربری خود را ذکر کنید

        📞 برای ارتباط مستقیم روی دکمه زیر کلیک کنید:
        """

        keyboard = [
            [InlineKeyboardButton("📞 تماس با پشتیبان", url="https://t.me/SIGLONA_TRADER")],
            [InlineKeyboardButton("🔙 بازگشت به منوی اصلی", callback_data="main_menu")]
        ]
        
        await query.edit_message_text(
            support_text, 
            parse_mode="HTML", 
            reply_markup=InlineKeyboardMarkup(keyboard),
            disable_web_page_preview=True
        )
        return
    
    if data == "check_again":
        is_member = await check_membership(user_id, context)
        users.update_one({"user_id": user_id}, {"$set": {"is_member": is_member, "updated_at": datetime.utcnow()}})
        if not is_member:
            await query.edit_message_text("❌ هنوز عضو کانال نیستید! لطفاً ابتدا در کانال عضو شوید.", reply_markup=join_channel_keyboard())
            return
        await show_main_menu(query, context)
        return

    if data == "main_menu":
        await show_main_menu(query, context)
        return

    if data == "top_inviters":
        await show_top_inviters(query, context)
        return

    if data == "prices":
        await show_prices_menu(query, context)
        return

    if data == "market_analysis":
        # تحلیل کلی بازار - می‌توانید این بخش را توسعه دهید
        text = """
        📊 *تحلیل کلی بازار*

        🔸 شاخص ترس و طمع: 45 (خنثی)
        🔸 حجم معاملات 24h: 85.4B
        🔸 دامیننس بیت‌کوین: 48.3%

        💡 *پیشنهاد ما:*
        در شرایط کنونی بازار، بهترین استراتژی، تنوع بخشیدن به سبد سرمایه‌گذاری و مدیریت ریسک است.
        """
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=back_to_prices_keyboard())
        return

    if data == "invite_link":
        me = users.find_one({"user_id": user_id})
        my_code = me.get("invite_code")
        me_bot = await context.bot.get_me()
        bot_username = me_bot.username
        deep_link = f"https://t.me/{bot_username}?start=ref_{my_code}"
        invites_count = me.get("invites_count", 0)
        
        text = f"""
        🎟️ *لینک دعوت اختصاصی شما*

        🔗 {escape_md(deep_link)}

        👥 *تعداد دعوت‌های موفق:* {invites_count}

        💎 *پاداش‌های سیستم دعوت:*
        • 10 دعوت: دسترسی به ویژگی‌های پیشرفته
        • 25 دعوت: مشاوره رایگان تحلیل بازار
        • 50 دعوت: عضویت ویژه در کانال VIP

        از لینک بالا برای دعوت دوستان خود استفاده کنید!
        """
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=main_menu_keyboard())
        return

    if data == "crypto_news":
        news_items = fetch_crypto_news(limit=5)
        if not news_items:
            await query.edit_message_text("❌ خطا در دریافت اخبار" , reply_markup=main_menu_keyboard())
            return

        text = "📰 *آخرین اخبار ارز دیجیتال*\n\n"
        for i, n in enumerate(news_items, 1):
            title = n.get("title", "بدون عنوان")
            url = n.get("url", "#")
            source = n.get("source", {}).get("name", "نامشخص")
            text += f"{i}. {title}\n   *منبع:* {source}\n   [مشاهده خبر]({url})\n\n"

        await query.edit_message_text(
            text,
            reply_markup=back_to_main_keyboard(),
            parse_mode="Markdown",
            disable_web_page_preview=True
        )
        return

    if data == "help":
        help_text = """
        ℹ️ *راهنمای استفاده از ربات*

        💰 *بخش قیمت‌ها:*
        - مشاهده قیمت لحظه‌ای ارزهای دیجیتال
        - دریافت تحلیل تکنیکال (RSI، میانگین متحرک)
        - مشاهده چارت قیمت در TradingView

        🎟️ *سیستم دعوت:*
        - دریافت لینک دعوت اختصاصی
        - دعوت دوستان و دریافت پاداش
        - مشاهده رتبه در جدول برترین‌ها

        📰 *اخبار:*
        - دریافت آخرین اخبار بازار ارزهای دیجیتال
        - منابع معتبر فارسی و انگلیسی

        برای شروع، از منوی اصلی گزینه مورد نظر را انتخاب کنید.
        """
        await query.edit_message_text(help_text, parse_mode="Markdown", reply_markup=back_to_main_keyboard())
        return

    if data.startswith("PRICE:"):
        symbol = data.split(":", 1)[1]
        coin = ALL_COINS.get(symbol)
        cg_id = coin["id"] if coin else None
        if not cg_id:
            await query.edit_message_text("❌ نماد ارز نامعتبر است.", reply_markup=prices_menu_keyboard())
            return
        
        price = coingecko_get_price(cg_id)
        if not price:
            await query.edit_message_text("❌ خطا در دریافت قیمت! لطفاً稍后再试.", reply_markup=prices_menu_keyboard())
            return

        # تحلیل روند با 30 کندل و RSI
        analysis = analyze_trend_with_rsi(cg_id)
        
        # ایجاد متن قیمت با فرمت زیبا
        price_formatted = f"{price:,.2f}" if price >= 1 else f"{price:.6f}"
        
        if analysis.get("error"):
            analysis_text = f"⚠️ *خطا در تحلیل:* {analysis.get('error')}"
        else:
            combined = analysis.get("combined")
            rsi = analysis.get("rsi")
            ma10 = analysis.get("ma10")
            ma30 = analysis.get("ma30")

            rsi_str = f"{rsi:.2f}" if rsi is not None else "نامشخص"
            ma10_str = f"{ma10:.4f}" if ma10 is not None else "—"
            ma30_str = f"{ma30:.4f}" if ma30 is not None else "—"

            # تعیین ایموجی بر اساس وضعیت
            if combined == "صعودی":
                trend_emoji = "📈"
            elif combined == "نزولی":
                trend_emoji = "📉"
            else:
                trend_emoji = "➡️"

            # تعیین وضعیت RSI
            rsi_status = ""
            if rsi is not None:
                if rsi > 70:
                    rsi_status = " (اشباع خرید 🔴)"
                elif rsi < 30:
                    rsi_status = " (اشباع فروش 🟢)"
                else:
                    rsi_status = " (عادی 🟡)"

            analysis_text = f"""
            📊 *تحلیل تکنیکال {symbol}*

            • وضعیت: {trend_emoji} *{combined}*
            • RSI(14): {rsi_str}{rsi_status}
            • میانگین متحرک 10 روزه: {ma10_str}
            • میانگین متحرک 30 روزه: {ma30_str}

            💡 *تفسیر تحلیل:*
            """

            if combined == "صعودی":
                analysis_text += "روند صعودی است. احتمال افزایش قیمت وجود دارد."
            elif combined == "نزولی":
                analysis_text += "روند نزولی است. مراقب کاهش قیمت باشید."
            else:
                analysis_text += "روند خنثی است. منتظر سیگنال واضح‌تر بمانید."

        # ایجاد دکمه‌های مربوط به این ارز
        keyboard = [
            [InlineKeyboardButton("📈 مشاهده چارت", url=f"https://www.tradingview.com/chart/?symbol={symbol}USDT")],
            [InlineKeyboardButton("🔄 بروزرسانی قیمت", callback_data=f"PRICE:{symbol}")],
            [InlineKeyboardButton("🔙 بازگشت به قیمت‌ها", callback_data="prices")],
        ]
        
        text = f"""
        💎 *قیمت {symbol}*

        💰 قیمت فعلی: *{price_formatted}* دلار

        {analysis_text}
        """
        
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if data == "search_coin":
        SEARCH_STATE[user_id] = True
        await query.edit_message_text("🔍 لطفاً نماد یا نام ارز را ارسال کنید (حداقل 3 حرف).", reply_markup=back_to_prices_keyboard())
        return

# ===========================
# هندلر جستجو
# ===========================
async def search_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not SEARCH_STATE.get(user_id):
        return
        
    query_text = update.message.text.strip().upper()
    if len(query_text) < 3:
        await update.message.reply_text("❌ لطفاً حداقل 3 حرف وارد کنید.", reply_markup=back_to_prices_keyboard())
        return
        
    query_prefix = query_text[:3]
    results = []

    # جستجو در ارزهای معروف اولویت دارند
    for sym in POPULAR_COINS:
        if sym in ALL_COINS and (sym.startswith(query_prefix) or ALL_COINS[sym]["name"].upper().startswith(query_prefix)):
            results.append((sym, ALL_COINS[sym]["name"], ALL_COINS[sym]["id"]))

    # جستجو در سایر ارزها
    for sym, info in ALL_COINS.items():
        if (sym.startswith(query_prefix) or info["name"].upper().startswith(query_prefix)) and (sym, info["name"], info["id"]) not in results:
            results.append((sym, info["name"], info["id"]))
        if len(results) >= 15:  # محدودیت نتایج
            break

    if not results:
        await update.message.reply_text("❌ هیچ ارزی یافت نشد. لطفاً نام کامل‌تر یا نماد دیگری را امتحان کنید.", reply_markup=back_to_prices_keyboard())
        SEARCH_STATE[user_id] = False
        return

    keyboard = []
    for sym, name, _ in results[:10]:
        # کوتاه کردن نام اگر طولانی باشد
        display_name = name if len(name) < 20 else name[:17] + "..."
        keyboard.append([InlineKeyboardButton(f"💰 {sym} ({display_name})", callback_data=f"PRICE:{sym}")])
    
    keyboard.append([InlineKeyboardButton("🔙 بازگشت", callback_data="prices")])
    
    await update.message.reply_text(
        f"🔍 *نتایج جستجو برای '{query_text}':*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    SEARCH_STATE[user_id] = False

# ===========================
# اجرا
# ===========================
def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, search_handler))
    print("🤖 Bot running")
    app.run_polling()

if __name__ == "__main__":
    main()