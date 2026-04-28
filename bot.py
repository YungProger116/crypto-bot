import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import os
import logging
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dotenv import load_dotenv
import telebot
from telebot import types
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib3

# Отключение предупреждений о небезопасных запросах
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Загрузка .env
load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
BINANCE_API_URL = "https://fapi.binance.com"
BYBIT_API_URL = "https://api.bybit.com"
COINGLASS_WEBHOOK_URL = os.getenv("COINGLASS_WEBHOOK_URL")

if not TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN не найден в .env файле")

# Кэш доступных фьючерсных пар
AVAILABLE_PAIRS = {'Binance': set(), 'Bybit': set()}
LAST_FUTURES_UPDATE = 0
FUTURES_UPDATE_INTERVAL = 3600  # 1 час


def create_session():
    session = requests.Session()
    retry_strategy = Retry(
        total=5,
        backoff_factor=0.3,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=['GET']
    )
    adapter = HTTPAdapter(max_retries=retry_strategy, pool_connections=100, pool_maxsize=100)
    session.mount('https://', adapter)
    session.mount('http://', adapter)
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
    })
    return session


REQUEST_SESSION = create_session()
bot = telebot.TeleBot(TOKEN)

user_states = {}
user_settings = defaultdict(dict)
signal_counters = {}
lock = threading.Lock()
cache_lock = threading.Lock()
oi_lock = threading.Lock()
active_monitors = {}
data_cache = {}
cache_expiry = 2
daily_counters_reset = defaultdict(float)
monitoring_progress = defaultdict(dict)
prev_oi_data = defaultdict(dict)

SUPPORTED_INTERVALS = {
    "1": "1m", "3": "3m", "5": "5m", "15": "15m", "30": "30m",
    "60": "1h", "120": "2h", "240": "4h", "360": "6h", "480": "8h",
    "720": "12h", "1440": "1d", "10080": "1w"
}

BYBIT_INTERVALS = {
    "1": "1", "3": "3", "5": "5", "15": "15", "30": "30",
    "60": "60", "120": "120", "240": "240", "360": "360",
    "720": "720", "1440": "D", "10080": "W"
}

INTERVAL_READABLE = {
    "1m": "1 минута", "3m": "3 минуты", "5m": "5 минут", "15m": "15 минут",
    "30m": "30 минут", "1h": "1 час", "2h": "2 часа", "4h": "4 часа",
    "6h": "6 часов", "8h": "8 часов", "12h": "12 часов", "1d": "1 день", "1w": "1 неделя"
}


def update_available_pairs(force_update=False):
    global AVAILABLE_PAIRS, LAST_FUTURES_UPDATE
    current_time = time.time()

    if not force_update and current_time - LAST_FUTURES_UPDATE < FUTURES_UPDATE_INTERVAL:
        return AVAILABLE_PAIRS

    # Binance
    try:
        res = REQUEST_SESSION.get(f"{BINANCE_API_URL}/fapi/v1/exchangeInfo", timeout=10)
        res.raise_for_status()
        binance_pairs = set(s['symbol'] for s in res.json()['symbols'] if
                            s['status'] == 'TRADING' and s['quoteAsset'] in ['USDT', 'USDC'])
        AVAILABLE_PAIRS['Binance'] = binance_pairs
    except Exception as e:
        logger.error(f"Binance pairs error: {e}")

    # Bybit
    try:
        res = REQUEST_SESSION.get(f"{BYBIT_API_URL}/v5/market/instruments-info?category=linear", timeout=10)
        res.raise_for_status()
        bybit_pairs = set(s['symbol'] for s in res.json()['result']['list'] if
                          s['status'] == 'Trading' and s['quoteCoin'] in ['USDT', 'USDC'])
        AVAILABLE_PAIRS['Bybit'] = bybit_pairs
    except Exception as e:
        logger.error(f"Bybit pairs error: {e}")

    LAST_FUTURES_UPDATE = current_time
    logger.info(f"Обновлены списки: {len(AVAILABLE_PAIRS['Binance'])} Binance, {len(AVAILABLE_PAIRS['Bybit'])} Bybit")
    return AVAILABLE_PAIRS


def get_binance_data(symbol, interval_val, enable_liq):
    cache_key = f"binance_data_{symbol}_{interval_val}"
    current_time = time.time()

    with cache_lock:
        if cache_key in data_cache and current_time - data_cache[cache_key][1] < cache_expiry:
            return data_cache[cache_key][0]

    try:
        # Ticker & OI
        ticker_res = REQUEST_SESSION.get(f"{BINANCE_API_URL}/fapi/v1/ticker/24hr?symbol={symbol}", timeout=5).json()
        oi_res = REQUEST_SESSION.get(f"{BINANCE_API_URL}/fapi/v1/openInterest?symbol={symbol}", timeout=5).json()

        # Klines
        binance_interval = SUPPORTED_INTERVALS.get(interval_val, "15m")
        kline = REQUEST_SESSION.get(f"{BINANCE_API_URL}/fapi/v1/klines",
                                    params={'symbol': symbol, 'interval': binance_interval, 'limit': 2},
                                    timeout=5).json()

        if len(kline) < 2: return None

        latest_candle, prev_candle = kline[-1], kline[-2]
        open_price, current_price = float(latest_candle[1]), float(ticker_res['lastPrice'])
        price_change = ((current_price - open_price) / open_price * 100) if open_price > 0 else 0.0

        latest_vol, prev_vol = float(latest_candle[5]), float(prev_candle[5])
        volume_change = ((latest_vol - prev_vol) / prev_vol * 100) if prev_vol > 0 else 0.0

        # Ликвидации за интервал
        total_liq = 0.0
        if enable_liq:
            try:
                liq_res = REQUEST_SESSION.get(f"{BINANCE_API_URL}/fapi/v1/allForceOrders",
                                              params={'symbol': symbol, 'limit': 1000}, timeout=5)
                if liq_res.status_code == 200:
                    cutoff_time = int(time.time() * 1000) - (int(interval_val) * 60 * 1000)
                    for order in liq_res.json():
                        if order.get('time', 0) >= cutoff_time:
                            total_liq += float(order['executedQty']) * float(order['avgPrice'])
            except:
                pass

        result = {
            'exchange': 'Binance', 'symbol': symbol, 'price': current_price, 'price_change': price_change,
            'volume': latest_vol, 'volume_change': volume_change, 'oi': float(oi_res.get('openInterest', 0)),
            'total_liq': total_liq, 'timestamp': ticker_res['closeTime']
        }
        with cache_lock:
            data_cache[cache_key] = (result, current_time)
        return result
    except Exception as e:
        return None


def get_bybit_data(symbol, interval_val, enable_liq):
    try:
        # Ticker (включает OI и 24h vol)
        ticker_res = REQUEST_SESSION.get(f"{BYBIT_API_URL}/v5/market/tickers?category=linear&symbol={symbol}",
                                         timeout=5).json()
        if not ticker_res['result']['list']: return None
        t_data = ticker_res['result']['list'][0]

        current_price = float(t_data['lastPrice'])
        oi = float(t_data.get('openInterest', 0))

        # Klines
        bybit_interval = BYBIT_INTERVALS.get(interval_val, "15")
        kline_res = REQUEST_SESSION.get(
            f"{BYBIT_API_URL}/v5/market/kline?category=linear&symbol={symbol}&interval={bybit_interval}&limit=2",
            timeout=5).json()
        k_list = kline_res['result']['list']
        if len(k_list) < 2: return None

        # Bybit отдает новые свечи первыми! k_list[0] - текущая, k_list[1] - предыдущая
        latest_candle, prev_candle = k_list[0], k_list[1]

        open_price = float(latest_candle[1])
        price_change = ((current_price - open_price) / open_price * 100) if open_price > 0 else 0.0

        latest_vol, prev_vol = float(latest_candle[5]), float(prev_candle[5])
        volume_change = ((latest_vol - prev_vol) / prev_vol * 100) if prev_vol > 0 else 0.0

        result = {
            'exchange': 'Bybit', 'symbol': symbol, 'price': current_price, 'price_change': price_change,
            'volume': latest_vol, 'volume_change': volume_change, 'oi': oi,
            'total_liq': 0.0,  # У Bybit нет публичного REST для ликвидаций
            'timestamp': int(time.time() * 1000)
        }
        return result
    except Exception as e:
        return None


def analyze_market(exchange, symbol, interval="15", enable_liq=False):
    if exchange == 'Binance':
        return get_binance_data(symbol, interval, enable_liq)
    elif exchange == 'Bybit':
        return get_bybit_data(symbol, interval, enable_liq)
    return None


def get_all_markets():
    pairs = update_available_pairs()
    markets = []
    for exch, symbols in pairs.items():
        for sym in symbols:
            markets.append({'exchange': exch, 'symbol': sym})
    return markets


def get_main_keyboard():
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    keyboard.add("📈 Цена BTC", "🔍 Анализ BTC", "🧠 Автосигналы", "📡 Статус мониторинга", "⚙ Настроить сигналы",
                 "ℹ️ Помощь")
    return keyboard


def get_settings_keyboard():
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    keyboard.add(
        "💰 Порог цены", "📊 Порог объема", "📈 Порог OI", "💧 Порог ликвидаций",
        "🔔 Вкл/Выкл Цену", "🔔 Вкл/Выкл Объем", "🔔 Вкл/Выкл OI", "🔔 Вкл/Выкл Ликвидации",
        "🔄 Режим: ЛЮБОЕ / ВСЕ", "⏱ Интервал анализа", "✅ Сохранить настройки", "↩️ Назад"
    )
    return keyboard


@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.send_message(message.chat.id,
                     "🤖 Добро пожаловать в <b>Crypto Market Scanner Pro</b>!\nМониторинг Binance и Bybit.",
                     parse_mode='HTML', reply_markup=get_main_keyboard())


@bot.message_handler(func=lambda msg: msg.text == "↩️ Назад")
def back_to_main(message):
    bot.send_message(message.chat.id, "Главное меню:", reply_markup=get_main_keyboard())


@bot.message_handler(func=lambda msg: msg.text == "📈 Цена BTC")
def price_btn(message):
    data = get_binance_data("BTCUSDT", "15", False)
    if data:
        bot.send_message(message.chat.id, f"💰 <b>BTC/USDT (Binance)</b>\nЦена: {data['price']}$", parse_mode='HTML')
    else:
        bot.send_message(message.chat.id, "❌ Ошибка получения данных")


@bot.message_handler(func=lambda msg: msg.text == "⚙ Настроить сигналы")
def setup_signals(message):
    chat_id = message.chat.id
    if chat_id not in user_settings:
        user_settings[chat_id] = {
            'enable_price': True, 'enable_volume': True, 'enable_oi': True, 'enable_liq': True,
            'price_threshold': 1.5, 'volume_threshold': 50.0, 'oi_threshold': 5.0, 'liq_threshold': 1000000,
            'interval': "15", 'condition_mode': 'ANY'
        }
    bot.send_message(chat_id, "⚙️ <b>Настройки:</b>\nВыберите параметр для изменения:", parse_mode='HTML',
                     reply_markup=get_settings_keyboard())


@bot.message_handler(func=lambda msg: msg.text == "🔄 Режим: ЛЮБОЕ / ВСЕ")
def toggle_mode(message):
    chat_id = message.chat.id
    current = user_settings.get(chat_id, {}).get('condition_mode', 'ANY')
    new_mode = 'ALL' if current == 'ANY' else 'ANY'
    user_settings[chat_id]['condition_mode'] = new_mode
    mode_text = "ВСЕ (AND)" if new_mode == 'ALL' else "ЛЮБОЕ (OR)"
    bot.send_message(chat_id,
                     f"✅ Режим изменен. Теперь для сигнала требуется срабатывание: <b>{mode_text}</b> включенных условий.",
                     parse_mode='HTML')


@bot.message_handler(
    func=lambda msg: msg.text in ["🔔 Вкл/Выкл Цену", "🔔 Вкл/Выкл Объем", "🔔 Вкл/Выкл OI", "🔔 Вкл/Выкл Ликвидации"])
def toggle_setting(message):
    chat_id = message.chat.id
    key_map = {"Цену": "enable_price", "Объем": "enable_volume", "OI": "enable_oi", "Ликвидации": "enable_liq"}
    key = key_map[message.text.split()[-1]]
    user_settings[chat_id][key] = not user_settings.get(chat_id, {}).get(key, True)
    status = "✅ ВКЛЮЧЕНО" if user_settings[chat_id][key] else "❌ ВЫКЛЮЧЕНО"
    bot.send_message(chat_id, f"✅ Параметр теперь {status}")


@bot.message_handler(func=lambda msg: msg.text in ["💰 Порог цены", "📊 Порог объема", "📈 Порог OI", "⏱ Интервал анализа",
                                                   "💧 Порог ликвидаций"])
def setting_selection(message):
    chat_id = message.chat.id
    settings_map = {
        "💰 Порог цены": "price_threshold", "📊 Порог объема": "volume_threshold",
        "📈 Порог OI": "oi_threshold", "⏱ Интервал анализа": "interval", "💧 Порог ликвидаций": "liq_threshold"
    }
    user_states[chat_id] = {'setting': settings_map[message.text]}
    bot.send_message(chat_id, f"✍️ Введите новое значение для {message.text}:",
                     reply_markup=types.ReplyKeyboardRemove())


@bot.message_handler(func=lambda msg: msg.chat.id in user_states and 'setting' in user_states[msg.chat.id])
def handle_setting_input(message):
    chat_id = message.chat.id
    setting = user_states[chat_id]['setting']
    try:
        val = message.text if setting == 'interval' else float(message.text.replace(',', ''))
        user_settings[chat_id][setting] = val
        del user_states[chat_id]
        bot.send_message(chat_id, "✅ Значение сохранено!", reply_markup=get_settings_keyboard())
    except ValueError:
        bot.send_message(chat_id, "❌ Неверный формат.")


@bot.message_handler(func=lambda msg: msg.text == "✅ Сохранить настройки")
def save_settings(message):
    bot.send_message(message.chat.id, "🎉 Настройки сохранены!", reply_markup=get_main_keyboard())


def check_signal_conditions(analysis, settings, chat_id, exchange, symbol):
    price_change = analysis['price_change']
    volume_change = analysis['volume_change']
    oi = analysis['oi']
    total_liq = analysis['total_liq']

    oi_key = f"{chat_id}_{exchange}_{symbol}"
    with oi_lock:
        if oi_key not in prev_oi_data:
            prev_oi_data[oi_key] = {'value': oi, 'first': True}
            oi_change_pct = 0.0
        else:
            prev_val = prev_oi_data[oi_key]['value']
            oi_change_pct = ((oi - prev_val) / prev_val * 100) if prev_val > 0 else 0.0
            prev_oi_data[oi_key] = {'value': oi, 'first': False}

    active_conditions = []
    matched_conditions = []
    triggers = []

    if settings.get('enable_price', True):
        active_conditions.append('price')
        t = settings.get('price_threshold', 1.5)
        if (t >= 0 and price_change >= t) or (t < 0 and price_change <= t):
            matched_conditions.append('price')
            triggers.append(f"💰 Цена: {price_change:+.2f}%")

    if settings.get('enable_volume', True):
        active_conditions.append('volume')
        t = settings.get('volume_threshold', 50.0)
        if (t >= 0 and volume_change >= t) or (t < 0 and volume_change <= t):
            matched_conditions.append('volume')
            triggers.append(f"📊 Объем: {volume_change:+.2f}%")

    if settings.get('enable_oi', True):
        active_conditions.append('oi')
        t = settings.get('oi_threshold', 5.0)
        if (t >= 0 and oi_change_pct >= t) or (t < 0 and oi_change_pct <= t):
            matched_conditions.append('oi')
            triggers.append(f"📈 OI: {oi_change_pct:+.2f}%")

    if settings.get('enable_liq', True) and exchange == 'Binance':
        active_conditions.append('liq')
        if total_liq >= settings.get('liq_threshold', 1000000):
            matched_conditions.append('liq')
            triggers.append(f"💧 Ликв: {total_liq:,.0f}$")

    mode = settings.get('condition_mode', 'ANY')
    is_triggered = False

    if mode == 'ALL' and active_conditions:
        is_triggered = len(matched_conditions) == len(active_conditions)
    elif mode == 'ANY':
        is_triggered = len(matched_conditions) > 0

    return {'is_triggered': is_triggered, 'triggers': triggers, 'oi_change_pct': oi_change_pct}


@bot.message_handler(func=lambda msg: msg.text == "🧠 Автосигналы")
def start_auto_signals(message):
    chat_id = message.chat.id
    settings = user_settings.get(chat_id, {})
    if chat_id in active_monitors and active_monitors[chat_id]:
        bot.send_message(chat_id, "Мониторинг уже запущен!")
        return

    markets = get_all_markets()
    active_monitors[chat_id] = True
    bot.send_message(chat_id, f"🚀 Запущен мониторинг {len(markets)} пар (Binance + Bybit)!")

    def monitor():
        last_signal_time = {}
        while active_monitors.get(chat_id, False):
            interval_val = settings.get('interval', "15")
            enable_liq = settings.get('enable_liq', True)

            with ThreadPoolExecutor(max_workers=15) as executor:
                futures = {executor.submit(analyze_market, m['exchange'], m['symbol'], interval_val, enable_liq): m for
                           m in markets}
                for future in as_completed(futures):
                    if not active_monitors.get(chat_id, False): break
                    m = futures[future]
                    analysis = future.result()
                    if not analysis: continue

                    cond = check_signal_conditions(analysis, settings, chat_id, m['exchange'], m['symbol'])
                    if cond['is_triggered']:
                        sym_key = f"{chat_id}_{m['exchange']}_{m['symbol']}"
                        if time.time() - last_signal_time.get(sym_key, 0) < 300: continue
                        last_signal_time[sym_key] = time.time()

                        msg = (
                            f"🚨 <b>Сигнал #{m['exchange']} {m['symbol']}</b>\n\n"
                            f"✅ <b>Сработало:</b> {', '.join(cond['triggers'])}\n\n"
                            f"💰 Цена: {analysis['price']:.6f}$ ({analysis['price_change']:+.2f}%)\n"
                            f"📊 Объем: {analysis['volume']:,.0f}$ ({analysis['volume_change']:+.2f}%)\n"
                            f"📈 OI: {analysis['oi']:,.0f}$ ({cond['oi_change_pct']:+.2f}%)\n"
                        )
                        if analysis['total_liq'] > 0:
                            msg += f"💧 Ликв: {analysis['total_liq']:,.0f}$\n"
                        bot.send_message(chat_id, msg, parse_mode='HTML')
            time.sleep(60)

    threading.Thread(target=monitor, daemon=True).start()


@bot.message_handler(commands=['stop'])
def stop_command(message):
    active_monitors[message.chat.id] = False
    bot.send_message(message.chat.id, "🛑 Мониторинг остановлен.", reply_markup=get_main_keyboard())


if __name__ == "__main__":
    update_available_pairs(force_update=True)
    bot.infinity_polling()
