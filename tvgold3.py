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
ADMIN_USER_ID = "173894121"  # باید عددی باشد
exchange = ccxt.mexc()
symbols_limit = 200
timeframe = '15m'
rsi_period = 14
candles_to_check = 100
timezone = pytz.timezone('Asia/Tehran')

# متغیر全局 وضعیت
is_bot_active = True

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
    try:
        markets = exchange.load_markets()
        tickers = exchange.fetch_tickers()
        usdt_pairs = [s for s in markets if s.endswith('/USDT') and markets[s].get('active', False)]
        volumes = [(s, tickers[s].get('quoteVolume', 0)) for s in usdt_pairs if s in tickers]
        return sorted(volumes, key=lambda x: x[1], reverse=True)[:symbols_limit]
    except Exception as e:
        print(f"خطا در دریافت نمادها: {e}")
        return []

def detect_rsi_extremes(symbol):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=candles_to_check)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['rsi'] = ta.momentum.RSIIndicator(df['close'], rsi_period).rsi()

        now = datetime.now(pytz.UTC)
        cutoff_time = now - timedelta(hours=5)

        events = []
        for i in range(rsi_period, len(df) - 1):
            rsi = df['rsi'].iloc[i]
            ts_utc = pd.to_datetime(df['timestamp'].iloc[i], unit='ms').tz_localize('UTC')
            if ts_utc < cutoff_time:
                continue
            ts_local = ts_utc.astimezone(timezone)
            if rsi > 70 or rsi < 30:
                event_type = 'اشباع خرید' if rsi > 70 else 'اشباع فروش'
                events.append((ts_local, event_type, rsi, i))
        return events
    except Exception as e:
        print(f"خطا در تشخیص RSI: {e}")
        return []

def save_candle_image(df, signal_index, symbol, event_type):
    try:
        start = max(0, len(df) - 96)
        df_window = df.iloc[start:]
        signal_index_in_window = signal_index - start
        
        if signal_index_in_window < 0 or signal_index_in_window >= len(df_window):
            return None

        df_window = df_window.set_index(pd.to_datetime(df_window['timestamp'], unit='ms'))
        df_candles = df_window[['open', 'high', 'low', 'close', 'rsi']]

        # تنظیمات رنگ
        candle_color = 'gold' if event_type == 'اشباع خرید' else 'cyan'
        mc = mpf.make_marketcolors(
            up='green',
            down='red',
            edge='inherit',
            wick='inherit',
            volume='in',
            ohlc='i'
        )
        s = mpf.make_mpf_style(marketcolors=mc)

        # تنظیمات پنل RSI
        apds = [
            mpf.make_addplot(df_candles['rsi'], panel=1, color='purple', ylabel='RSI'),
            mpf.make_addplot([70]*len(df_candles), panel=1, color='red', linestyle='--'),
            mpf.make_addplot([30]*len(df_candles), panel=1, color='green', linestyle='--')
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
            figsize=(12, 8),
            panel_ratios=(6, 3),
            returnfig=True,
            volume=False
        )

        # تغییر رنگ کندل سیگنال
        candle_ax = axes[0]
        candle_rect = plt.Rectangle(
            (signal_index_in_window - 0.4, df_candles['low'].iloc[signal_index_in_window]),
            0.8,
            df_candles['high'].iloc[signal_index_in_window] - df_candles['low'].iloc[signal_index_in_window],
            fill=True,
            color=candle_color,
            alpha=0.5
        )
        candle_ax.add_patch(candle_rect)

        fig.savefig(fullpath, bbox_inches='tight')
        plt.close(fig)
        return fullpath
    except Exception as e:
        print(f"خطا در تولید تصویر: {e}")
        return None

async def check_all(context: ContextTypes.DEFAULT_TYPE):
    global is_bot_active
    if not is_bot_active:
        return

    symbols = fetch_top_200_symbols()
    if not symbols:
        await send_telegram_message("⚠️ خطا در دریافت لیست ارزها")
        return

    found_signals = False
    for idx, (symbol, volume) in enumerate(symbols[:30]):
        events = detect_rsi_extremes(symbol)
        if events:
            found_signals = True
            last_event = max(events, key=lambda e: e[0])
            df = pd.DataFrame(exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=candles_to_check),
                            columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['rsi'] = ta.momentum.RSIIndicator(df['close'], rsi_period).rsi()
            
            img_path = save_candle_image(df, last_event[3], symbol, last_event[1])
            if img_path:
                message = (
                    f"📊 نماد: {symbol}\n"
                    f"🕒 زمان: {last_event[0].strftime('%Y-%m-%d %H:%M')}\n"
                    f"🔔 نوع سیگنال: {last_event[1]}\n"
                    f"📈 RSI: {round(last_event[2], 2)}\n"
                    f"💰 حجم 24h: {round(volume, 2)}\n"
                    f"🎨 رنگ کندل: {'طلایی' if last_event[1] == 'اشباع خرید' else 'فیروزه‌ای'}"
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