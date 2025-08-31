import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

TOKEN = "7797893271:AAHJctebcYylKYYw26PVoAN7OCfN8JGZck4"
CHANNEL_ID = "@SIGLONA"

# -----------------------------
# تابع چک عضویت
# -----------------------------
async def check_membership(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        member = await context.bot.get_chat_member(CHANNEL_ID, user_id)
        return member.status in ["member", "administrator", "creator"]
    except Exception:
        return False

# -----------------------------
# گرفتن قیمت از Binance
# -----------------------------
API_KEY = "2972e723-e7c1-4a1b-9532-80d3f90b43c3"

def get_price(symbol: str):
    url = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest"
    headers = {
        "Accepts": "application/json",
        "X-CMC_PRO_API_KEY": API_KEY
    }
    # فقط نماد اولی (مثلاً BTCUSDT → BTC)
    symbol = symbol.upper().replace("USDT", "")  

    params = {
        "symbol": symbol,
        "convert": "USD"
    }
    try:
        response = requests.get(url, headers=headers, params=params, timeout=5)
        data = response.json()
        print(data)  # برای دیباگ
        if "data" in data and symbol in data["data"]:
            return float(data["data"][symbol]["quote"]["USD"]["price"])
    except Exception as e:
        print(f"Error fetching price: {e}")
    return None


# -----------------------------
# منوی اصلی (نمایش دکمه‌های ارز)
# -----------------------------
async def show_main_menu(update_or_query, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("💰 قیمت BTCUSDT", callback_data="BTCUSDT")],
        [InlineKeyboardButton("💰 قیمت ETHUSDT", callback_data="ETHUSDT")],
        [InlineKeyboardButton("💰 قیمت BNBUSDT", callback_data="BNBUSDT")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if isinstance(update_or_query, Update):
        await update_or_query.message.reply_text("یکی از ارزها رو انتخاب کن 👇", reply_markup=reply_markup)
    else:
        await update_or_query.edit_message_text("یکی از ارزها رو انتخاب کن 👇", reply_markup=reply_markup)

# -----------------------------
# /start
# -----------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await check_membership(user_id, context):
        keyboard = [
            [InlineKeyboardButton("📢 عضویت در کانال", url="https://t.me/SIGLONA")],
            [InlineKeyboardButton("✅ عضو شدم", callback_data="check_again")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "⚠️ لطفاً برای استفاده از ربات در کانال‌های زیر عضو شوید:",
            reply_markup=reply_markup,
        )
        return

    await show_main_menu(update, context)

# -----------------------------
# هندلر کلیک روی دکمه‌ها
# -----------------------------
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    # وقتی روی "عضو شدم" کلیک می‌کنه
    if query.data == "check_again":
        user_id = query.from_user.id
        if await check_membership(user_id, context):
            await show_main_menu(query, context)
        else:
            keyboard = [
                [InlineKeyboardButton("📢 عضویت در کانال", url="https://t.me/SIGLONA")],
                [InlineKeyboardButton("✅ عضو شدم", callback_data="check_again")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                "❌ هنوز عضو کانال نیستی!\n\n⚠️ لطفاً برای استفاده از ربات در کانال‌های زیر عضو شوید:",
                reply_markup=reply_markup
            )
        return

    # وقتی یکی از ارزها رو انتخاب می‌کنه
    symbol = query.data
    price = get_price(symbol)
    if price:
        keyboard = [[InlineKeyboardButton("🔙 بازگشت به منو", callback_data="menu")]]
        await query.edit_message_text(f"💰 قیمت {symbol}: {price:.2f} USDT", reply_markup=InlineKeyboardMarkup(keyboard))
    elif query.data == "menu":
        await show_main_menu(query, context)
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
