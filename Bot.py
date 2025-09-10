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
# دکمه‌ها
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
        [InlineKeyboardButton("🏆 نفرات برتر", callback_data="top_inviters")],
        [InlineKeyboardButton("📰 اخبار ارز دیجیتال", callback_data="crypto_news")],
    ]
    return InlineKeyboardMarkup(keyboard)

async def show_top_inviters(update_or_query, context: ContextTypes.DEFAULT_TYPE):
    top_users = users.find().sort("invites_count", -1).limit(3)
    text = "🏆 نفرات برتر :\n\n"
    for i, u in enumerate(top_users, 1):
        username = u.get("username") or f"user_{u.get('user_id')}"
        invites = u.get("invites_count", 0)
        text += f"{i}. {username} - {invites} دعوت موفق\n"
    keyboard = [[InlineKeyboardButton("🔙 بازگشت", callback_data="main_menu")]]
    markup = InlineKeyboardMarkup(keyboard)
    if isinstance(update_or_query, Update):
        await update_or_query.message.reply_text(text, reply_markup=markup)
    else:
        await update_or_query.edit_message_text(text, reply_markup=markup)

def prices_menu_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton("₿ BTC", callback_data="PRICE:BTC"),
        ],
        [
            InlineKeyboardButton("🟡 BNB", callback_data="PRICE:BNB"),
            InlineKeyboardButton("🔥 SOL", callback_data="PRICE:SOL"),
        ],

        # سایر گزینه‌ها
        [InlineKeyboardButton("🔍 جستجوی ارز", callback_data="search_coin")],
        [InlineKeyboardButton("🔙 بازگشت", callback_data="main_menu")],
    ]
    return InlineKeyboardMarkup(keyboard)

# ===========================
# منوها
# ===========================
async def show_main_menu(update_or_query, context: ContextTypes.DEFAULT_TYPE):
    text = "سلام! یکی از گزینه‌ها را انتخاب کن:"
    if isinstance(update_or_query, Update):
        await update_or_query.message.reply_text(text, reply_markup=main_menu_keyboard())
    else:
        await update_or_query.edit_message_text(text, reply_markup=main_menu_keyboard())

async def show_prices_menu(update_or_query, context: ContextTypes.DEFAULT_TYPE):
    text = "یکی از گزینه‌ها را انتخاب کنید:"
    if isinstance(update_or_query, Update):
        await update_or_query.message.reply_text(text, reply_markup=prices_menu_keyboard())
    else:
        await update_or_query.edit_message_text(text, reply_markup=prices_menu_keyboard())

# ===========================
# /start
# ===========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    username = user.username or f"user_{user_id}"
    doc = upsert_user(user_id, username)
    is_member = await check_membership(user_id, context)
    users.update_one({"user_id": user_id}, {"$set": {"is_member": is_member, "updated_at": datetime.utcnow()}})
    if not is_member:
        await update.message.reply_text("⚠️ لطفاً ابتدا در کانال عضو شوید، سپس روی «✅ عضو شدم» بزنید.", reply_markup=join_channel_keyboard())
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

    if data == "check_again":
        is_member = await check_membership(user_id, context)
        users.update_one({"user_id": user_id}, {"$set": {"is_member": is_member, "updated_at": datetime.utcnow()}})
        if not is_member:
            await query.edit_message_text("❌ هنوز عضو کانال نیستی!", reply_markup=join_channel_keyboard())
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

    if data == "invite_link":
        me = users.find_one({"user_id": user_id})
        my_code = me.get("invite_code")
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

    if data == "crypto_news":
        news_items = fetch_crypto_news(limit=5)
        if not news_items:
            await query.edit_message_text("❌ خطا در دریافت اخبار!", reply_markup=main_menu_keyboard())
            return

        text = "📰 آخرین اخبار ارز دیجیتال:\n\n"
        for n in news_items:
            title = n.get("title", "بدون عنوان")
            url = n.get("url", "#")
            source = n.get("source", {}).get("name", "نامشخص")
            text += f"• {title} ({source})\n[مشاهده خبر]({url})\n\n"

        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="main_menu")]]),
            parse_mode="Markdown"
        )
        return

    if data.startswith("PRICE:"):
        symbol = data.split(":", 1)[1]
        coin = ALL_COINS.get(symbol)
        cg_id = coin["id"] if coin else None
        if not cg_id:
            await query.edit_message_text("❌ نماد نامعتبر است.", reply_markup=prices_menu_keyboard())
            return
        price = coingecko_get_price(cg_id)
        if not price:
            await query.edit_message_text("❌ خطا در دریافت قیمت!", reply_markup=prices_menu_keyboard())
            return

        # --- بخش جدید: تحلیل روند با 30 کندل و RSI
        analysis = analyze_trend_with_rsi(cg_id)
        if analysis.get("error"):
            analysis_text = f"⚠️ خطا در تحلیل: {analysis.get('error')}"
        else:
            combined = analysis.get("combined")
            overall = analysis.get("overall_trend")
            rsi = analysis.get("rsi")
            ma10 = analysis.get("ma10")
            ma30 = analysis.get("ma30")

            rsi_str = "—"
            if rsi is None:
                rsi_str = "❌ نامشخص"
            else:
                rsi_str = f"{rsi:.2f}"
            ma10_str = f"{ma10:.4f}" if ma10 is not None else "—"
            ma30_str = f"{ma30:.4f}" if ma30 is not None else "—"

            # توضیحات بیشتر برای RSI (اشباع خرید/فروش)
            if rsi is None:
                rsi_note = ""
            elif rsi > 70:
                rsi_note = " (اشباع خرید)"
            elif rsi < 30:
                rsi_note = " (اشباع فروش)"
            else:
                rsi_note = ""

            analysis_text = (
                f"📊 وضعیت تحلیل ۳۰ روزه:\n"
                f"• روند کلی (اولین ↔ آخرین): {overall}\n"
                f"• نتیجه ترکیبی (MA10 vs MA30 & RSI): {combined}\n"
                f"• RSI(14): {rsi_str}{rsi_note}\n"
                f"• MA10: {ma10_str}  |  MA30: {ma30_str}"
            )

        txt = f"💰 قیمت {symbol}: {str(price)} USD\n\n{analysis_text}\n\n📊 مایلید چارت این ارز رو هم ببینید؟"
        keyboard = [
            [InlineKeyboardButton("📈 بله", url=f"https://www.tradingview.com/chart/?symbol={symbol}USDT")],
            [InlineKeyboardButton("🔙 بازگشت", callback_data="main_menu")],
        ]
        await query.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if data == "search_coin":
        SEARCH_STATE[user_id] = True
        await query.edit_message_text("🔍 لطفاً نماد یا نام ارز را ارسال کنید (حداقل 3 حرف).", reply_markup=prices_menu_keyboard())
        return

# ===========================
# هندلر جستجو
# ===========================
async def search_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not SEARCH_STATE.get(user_id):
        return
    query_text = update.message.text.strip().upper()
    query_prefix = query_text[:3]
    results = []

    # اولویت: ارزهای معروف
    for sym in POPULAR_COINS:
        if sym in ALL_COINS and sym.startswith(query_prefix):
            results.append((sym, ALL_COINS[sym]["name"], ALL_COINS[sym]["id"]))

    # بقیه ارزها
    for sym, info in ALL_COINS.items():
        if len(sym) >= 3 and sym.startswith(query_prefix) and (sym, info["name"], info["id"]) not in results:
            results.append((sym, info["name"], info["id"]))
        elif len(info["name"]) >= 3 and info["name"].upper().startswith(query_prefix):
            results.append((sym, info["name"], info["id"]))
        if len(results) >= 10:
            break

    if not results:
        await update.message.reply_text("❌ هیچ ارزی یافت نشد.", reply_markup=prices_menu_keyboard())
        return

    keyboard = []
    for sym, name, _ in results[:10]:
        keyboard.append([InlineKeyboardButton(f"💰 {sym} ({name})", callback_data=f"PRICE:{sym}")])
    keyboard.append([InlineKeyboardButton("🔙 بازگشت", callback_data="prices")])
    await update.message.reply_text("نتایج جستجو:", reply_markup=InlineKeyboardMarkup(keyboard))
    SEARCH_STATE[user_id] = False

# ===========================
# اجرا
# ===========================
def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, search_handler))
    print("🤖 Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
