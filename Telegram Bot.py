import ccxt
import pandas as pd
import ta
import os
import numpy as np
from datetime import datetime, timedelta
import pytz
import mplfinance as mpf
import matplotlib.pyplot as plt
import matplotlib.patches as patches
from telegram import Update, Bot
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    JobQueue,
    filters
)

# تنظیمات اصلی
TELEGRAM_TOKEN = "8143402560:AAHjpGS9l9zgnSx9FD2cL7FvgW9IAkMWKhI"
CHAT_ID = "173894121"
ADMIN_USER_ID = "173894121"
exchanges = {
    'mexc': ccxt.mexc(),
    'bybit': ccxt.bybit(),
    'okx': ccxt.okx()
}
symbols_limit = 200
timeframe = '15m'
rsi_period = 14
candles_to_check = 100
timezone = pytz.timezone('Asia/Tehran')
shadow_to_body_ratio_threshold = 2.0

# متغیرهای جهانی
is_bot_active = True
sent_signals = set()
processed_symbols = set()  # برای جلوگیری از تکرار ارزها

async def send_telegram_message(message: str, image_path: str = None):
    bot = Bot(token=TELEGRAM_TOKEN)
    try:
        if image_path:
            with open(image_path, 'rb') as photo:
                await bot.send_photo(chat_id=CHAT_ID, photo=photo, caption=message)
            os.remove(image_path)
        else:
            await bot.send_message(chat_id=CHAT_ID, text=message)
    except Exception as e:
        print(f"خطا در ارسال پیام: {e}")

def fetch_top_200_symbols():
    all_symbols = []
    try:
        for exchange_name, exchange in exchanges.items():
            markets = exchange.load_markets()
            tickers = exchange.fetch_tickers()
            usdt_pairs = [s for s in markets if s.endswith('/USDT') and markets[s].get('active', False)]
            volumes = [(s, tickers[s].get('quoteVolume', 0), exchange_name) for s in usdt_pairs if s in tickers]
            all_symbols.extend(volumes)
        
        # حذف تکراری‌ها و مرتب‌سازی بر اساس حجم
        unique_symbols = {(symbol, ex_name): vol for symbol, vol, ex_name in all_symbols}
        sorted_symbols = sorted(unique_symbols.items(), key=lambda x: x[1], reverse=True)[:symbols_limit]
        return [(symbol, volume, ex_name) for (symbol, ex_name), volume in sorted_symbols]
    except Exception as e:
        print(f"خطا در دریافت نمادها: {e}")
        return []

def calculate_shadow_to_body_ratio(row):
    body = abs(row['open'] - row['close'])
    shadow_upper = row['high'] - max(row['open'], row['close'])
    shadow_lower = min(row['open'], row['close']) - row['low']
    total_shadow = shadow_upper + shadow_lower
    return float('inf') if body == 0 else total_shadow / body

def detect_rsi_extremes(symbol, exchange):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=candles_to_check)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['rsi'] = ta.momentum.RSIIndicator(df['close'], rsi_period).rsi()
        df['shadow_to_body_ratio'] = df.apply(calculate_shadow_to_body_ratio, axis=1)

        now = datetime.now(pytz.UTC)
        cutoff_time = now - timedelta(hours=5)

        events = []
        for i in range(rsi_period, len(df) - 1):
            rsi = df['rsi'].iloc[i]
            ratio = df['shadow_to_body_ratio'].iloc[i]
            ts_utc = pd.to_datetime(df['timestamp'].iloc[i], unit='ms').tz_localize('UTC')
            if ts_utc < cutoff_time:
                continue
            ts_local = ts_utc.astimezone(timezone)
            if (rsi > 70 or rsi < 30) and ratio > shadow_to_body_ratio_threshold:
                event_type = 'اشباع خرید' if rsi > 70 else 'اشباع فروش'
                events.append((ts_local, event_type, rsi, i, ratio))
        return events
    except Exception as e:
        print(f"خطا در تشخیص RSI برای {symbol}: {e}")
        return []

def save_candle_image(df, signal_index, symbol, event_type, exchange_name):
    try:
        start = max(0, len(df) - 96)
        df_window = df.iloc[start:]
        signal_index_in_window = signal_index - start

        if signal_index_in_window < 0 or signal_index_in_window >= len(df_window):
            return None

        df_window = df_window.set_index(pd.to_datetime(df_window['timestamp'], unit='ms'))
        df_candles = df_window[['open', 'high', 'low', 'close', 'rsi']]

        # تنظیمات رنگ و استایل
        candle_color = '#FFD700' if event_type == 'اشباع خرید' else '#00CED1'
        mc = mpf.make_marketcolors(
            up='#00FF00',
            down='#FF0000',
            edge='black',
            wick='black',
            volume='in',
            ohlc='i'
        )
        s = mpf.make_mpf_style(
            marketcolors=mc,
            gridstyle='--',
            gridcolor='gray',
            figcolor='#F5F5F5',
            facecolor='#F5F5F5'
        )

        # تنظیمات RSI
        apds = [
            mpf.make_addplot(df_candles['rsi'], panel=1, color='#800080', ylabel='RSI', width=1.5),
            mpf.make_addplot([70]*len(df_candles), panel=1, color='#FF0000', linestyle='--', alpha=0.5),
            mpf.make_addplot([30]*len(df_candles), panel=1, color='#008000', linestyle='--', alpha=0.5)
        ]

        # ایجاد فایل
        os.makedirs("tmp_candle", exist_ok=True)
        filename = f"{symbol.replace('/', '_')}_{df_window.index[signal_index_in_window].strftime('%Y%m%d_%H%M')}.png"
        fullpath = os.path.join("tmp_candle", filename)

        # رسم نمودار
        fig, axes = mpf.plot(
            df_candles,
            type='candle',
            style=s,
            addplot=apds,
            figsize=(14, 10),
            panel_ratios=(6, 3),
            returnfig=True,
            volume=False,
            title=f"{symbol} ({exchange_name}) - {event_type}",
            tight_layout=True
        )

        # تغییر رنگ کندل سیگنال
        candle_ax = axes[0]
        candle_rect = plt.Rectangle(
            (signal_index_in_window - 0.4, df_candles['low'].iloc[signal_index_in_window]),
            0.8,
            df_candles['high'].iloc[signal_index_in_window] - df_candles['low'].iloc[signal_index_in_window],
            fill=True,
            color=candle_color,
            alpha=0.6
        )
        candle_ax.add_patch(candle_rect)

        # تنظیمات اضافی
        candle_ax.grid(True, linestyle='--', alpha=0.5)
        axes[1].grid(True, linestyle='--', alpha=0.5)
        fig.suptitle(f"{symbol} - {event_type}", fontsize=16, fontweight='bold', y=0.95)

        fig.savefig(fullpath, bbox_inches='tight', dpi=150)
        plt.close(fig)
        return fullpath
    except Exception as e:
        print(f"خطا در تولید تصویر: {e}")
        return None

async def check_all(context: ContextTypes.DEFAULT_TYPE):
    global is_bot_active, sent_signals, processed_symbols
    if not is_bot_active:
        return

    symbols = fetch_top_200_symbols()
    if not symbols:
        await send_telegram_message("⚠️ خطا در دریافت لیست ارزها")
        return

    found_signals = False
    processed_symbols.clear()  # ریست کردن لیست ارزهای پردازش‌شده
    for idx, (symbol, volume, exchange_name) in enumerate(symbols[:30]):
        if symbol in processed_symbols:
            continue  # رد کردن ارزهای تکراری
        processed_symbols.add(symbol)
        exchange = exchanges[exchange_name]
        events = detect_rsi_extremes(symbol, exchange)
        if events:
            for event in events:
                ts_local, event_type, rsi, i, ratio = event
                signal_key = (symbol, ts_local.strftime('%Y-%m-%d %H:%M'), event_type)
                if signal_key not in sent_signals:
                    found_signals = True
                    sent_signals.add(signal_key)
                    df = pd.DataFrame(exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=candles_to_check),
                                    columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    df['rsi'] = ta.momentum.RSIIndicator(df['close'], rsi_period).rsi()

                    img_path = save_candle_image(df, i, symbol, event_type, exchange_name)
                    if img_path:
                        message = (
                            f"📊 نماد: {symbol}\n"
                            f"🏦 صرافی: {exchange_name.upper()}\n"
                            f"🕒 زمان: {ts_local.strftime('%Y-%m-%d %H:%M')}\n"
                            f"🔔 نوع سیگنال: {event_type}\n"
                            f"📈 RSI: {round(rsi, 2)}\n"
                            f"💰 حجم 24h: {round(volume, 2)}\n"
                            f"📏 نسبت سایه به بدنه: {round(ratio, 2)}\n"
                            f"🎨 رنگ سری: {'طلایی' if event_type == 'اشباع خرید' else 'فیروزه‌ای'}"
                        )
                        await send_telegram_message(message, img_path)

    if not found_signals:
        await send_telegram_message("⚠️ هیچ سیگنالی در 5 ساعت اخیر یافت نشد")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global is_bot_active
    if str(update.effective_user.id) == ADMIN_USER_ID:
        is_bot_active = True
        await update.message.reply_text("✅ ربات فعال شد")
        await check_all(context)

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global is_bot_active
    if str(update.effective_user.id) == ADMIN_USER_ID:
        is_bot_active = False
        await update.message.reply_text("⛔ ربات غیرفعال شد")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_text = "فعال ✅" if is_bot_active else "غیرفعال ⛔"
    await update.message.reply_text(f"وضعیت ربات: {status_text}")

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start, filters.User(user_id=ADMIN_USER_ID)))
    app.add_handler(CommandHandler("stop", stop, filters.User(user_id=ADMIN_USER_ID)))
    app.add_handler(CommandHandler("status", status, filters.User(user_id=ADMIN_USER_ID)))
    
    job_queue = app.job_queue
    job_queue.run_repeating(check_all, interval=1800, first=10)
    
    app.run_polling()

if __name__ == "__main__":
    main()