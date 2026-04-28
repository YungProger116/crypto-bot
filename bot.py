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
import random
import math
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
BYBIT_API_URL = "https://api.bybit.com"  # ДОБАВЛЕНО
COINGLASS_WEBHOOK_URL = os.getenv("COINGLASS_WEBHOOK_URL")

if not TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN не найден в .env файле")

# Кэш доступных фьючерсных пар (ИЗМЕНЕНО для поддержки бирж)
AVAILABLE_FUTURES_PAIRS = {'Binance': set(), 'Bybit': set()}
LAST_FUTURES_UPDATE = 0
FUTURES_UPDATE_INTERVAL = 3600  # 1 час


# Создание сессии с повторными попытками
def create_session():
    session = requests.Session()
    retry_strategy = Retry(
        total=5,
        backoff_factor=0.3,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=['GET']
    )
    adapter = HTTPAdapter(
        max_retries=retry_strategy,
        pool_connections=100,
        pool_maxsize=100
    )
    session.mount('https://', adapter)
    session.mount('http://', adapter)
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    })
    return session


# Глобальная сессия для всех запросов
REQUEST_SESSION = create_session()

bot = telebot.TeleBot(TOKEN)

user_states = {}
user_settings = defaultdict(lambda: {
    'enable_price': True,
    'enable_volume': True,
    'enable_oi': True,
    'enable_liq': True,
    'price_threshold': 1.5,
    'volume_threshold': 50.0,
    'oi_threshold': 5.0,
    'liq_threshold': 1000000,
    'interval': "15",
    'condition_mode': 'ANY'  # ДОБАВЛЕНО: 'ANY' (Любое) или 'ALL' (Все)
})

signal_counters = {}
lock = threading.Lock()
# ИСПРАВЛЕНИЕ: отдельный лок для кэша данных
cache_lock = threading.Lock()
# ИСПРАВЛЕНИЕ: отдельный лок для OI данных
oi_lock = threading.Lock()
active_monitors = {}
data_cache = {}
cache_expiry = 2  # Кэширование данных на 2 секунды
daily_counters_reset = defaultdict(float)
monitoring_progress = defaultdict(dict)
prev_oi_data = defaultdict(dict)
last_analysis_time = defaultdict(dict)

# Поддерживаемые интервалы Binance
SUPPORTED_INTERVALS = {
    "1": "1m",
    "3": "3m",
    "5": "5m",
    "15": "15m",
    "30": "30m",
    "60": "1h",
    "120": "2h",
    "240": "4h",
    "360": "6h",
    "480": "8h",
    "720": "12h",
    "1440": "1d",
    "10080": "1w"
}

# Читаемые названия интервалов
INTERVAL_READABLE = {
    "1m": "1 минута",
    "3m": "3 минуты",
    "5m": "5 минут",
    "15m": "15 минут",
    "30m": "30 минут",
    "1h": "1 час",
    "2h": "2 часа",
    "4h": "4 часа",
    "6h": "6 часов",
    "8h": "8 часов",
    "12h": "12 часов",
    "1d": "1 день",
    "1w": "1 неделя"
}


def get_available_futures_pairs(force_update=False):
    """ИЗМЕНЕНО: Получает и кэширует список пар для Binance и Bybit"""
    global AVAILABLE_FUTURES_PAIRS, LAST_FUTURES_UPDATE

    current_time = time.time()

    if not force_update and current_time - LAST_FUTURES_UPDATE < FUTURES_UPDATE_INTERVAL and (
            AVAILABLE_FUTURES_PAIRS['Binance'] or AVAILABLE_FUTURES_PAIRS['Bybit']):
        return AVAILABLE_FUTURES_PAIRS

    # --- Binance ---
    try:
        url = f"{BINANCE_API_URL}/fapi/v1/exchangeInfo"
        response = REQUEST_SESSION.get(url, timeout=15, verify=True)
        data = response.json()
        AVAILABLE_FUTURES_PAIRS['Binance'] = {
            s['symbol'] for s in data['symbols']
            if s['status'] == 'TRADING' and s['contractType'] == 'PERPETUAL' and s['quoteAsset'] in ['USDT', 'USDC']
        }
    except Exception as e:
        logger.error(f"Ошибка Binance exchangeInfo: {e}")

    # --- Bybit ---
    try:
        url = f"{BYBIT_API_URL}/v5/market/instruments-info?category=linear"
        response = REQUEST_SESSION.get(url, timeout=15, verify=True)
        data = response.json()
        AVAILABLE_FUTURES_PAIRS['Bybit'] = {
            s['symbol'] for s in data['result']['list']
            if s['status'] == 'Trading' and s['quoteCoin'] in ['USDT', 'USDC']
        }
    except Exception as e:
        logger.error(f"Ошибка Bybit exchangeInfo: {e}")

    LAST_FUTURES_UPDATE = current_time
    return AVAILABLE_FUTURES_PAIRS


def is_futures_pair_available(symbol, exchange='Binance'):
    """Проверяет доступность пары на конкретной бирже"""
    available = get_available_futures_pairs()
    return symbol in available.get(exchange, set())


def send_coin_glass_signal(signal_type: str, symbol: str, price: float, volume: float = None,
                           price_change: float = None, oi_change: float = None):
    if not COINGLASS_WEBHOOK_URL:
        logger.warning("COINGLASS_WEBHOOK_URL не настроен в .env файле")
        return

    payload = {
        "type": signal_type,
        "symbol": symbol,
        "price": price,
        "timestamp": int(time.time())
    }

    if volume:
        payload["volume"] = volume
    if price_change is not None:
        payload["price_change"] = price_change
    if oi_change is not None:
        payload["oi_change"] = oi_change

    try:
        response = REQUEST_SESSION.post(
            COINGLASS_WEBHOOK_URL,
            json=payload,
            timeout=5,
            verify=True
        )
        if response.status_code == 200:
            logger.info(f"✅ Сигнал {signal_type} для {symbol} отправлен в CoinGlass!")
        else:
            logger.warning(f"❌ Ошибка отправки в CoinGlass: {response.status_code} {response.text}")
    except Exception as e:
        logger.error(f"⚠️ Ошибка при отправке в CoinGlass: {e}")


def get_binance_kline(symbol="BTCUSDT", interval="15", limit=2):
    """Получаем свечные данные с Binance API"""
    cache_key = f"kline_{symbol}_{interval}"
    current_time = time.time()

    with cache_lock:
        if cache_key in data_cache:
            cached_data, timestamp = data_cache[cache_key]
            if current_time - timestamp < cache_expiry:
                return cached_data

    try:
        if not is_futures_pair_available(symbol, 'Binance'):
            return None

        binance_interval = SUPPORTED_INTERVALS.get(interval, "15m")
        url = f"{BINANCE_API_URL}/fapi/v1/klines"
        params = {'symbol': symbol, 'interval': binance_interval, 'limit': limit}
        response = REQUEST_SESSION.get(url, params=params, timeout=10, verify=True)
        data = response.json()

        with cache_lock:
            data_cache[cache_key] = (data, current_time)
        return data
    except Exception as e:
        logger.error(f"Binance kline error for {symbol}: {e}")
        return None


def get_binance_ticker(symbol="BTCUSDT"):
    """Получаем полные данные с Binance API"""
    cache_key = f"ticker_{symbol}"
    current_time = time.time()

    with cache_lock:
        if cache_key in data_cache:
            cached_data, timestamp = data_cache[cache_key]
            if current_time - timestamp < cache_expiry:
                return cached_data

    try:
        if not is_futures_pair_available(symbol, 'Binance'):
            return None

        ticker_response = REQUEST_SESSION.get(f"{BINANCE_API_URL}/fapi/v1/ticker/24hr?symbol={symbol}",
                                              timeout=10).json()
        oi_response = REQUEST_SESSION.get(f"{BINANCE_API_URL}/fapi/v1/openInterest?symbol={symbol}", timeout=10).json()

        result = {
            'symbol': symbol,
            'price': float(ticker_response['lastPrice']),
            'price_change': float(ticker_response['priceChangePercent']),
            'volume': float(ticker_response['volume']),
            'quote_volume': float(ticker_response['quoteVolume']),
            'oi': float(oi_response['openInterest']),
            'timestamp': ticker_response['closeTime']
        }

        with cache_lock:
            data_cache[cache_key] = (result, current_time)
        return result
    except Exception as e:
        logger.error(f"Binance ticker error for {symbol}: {str(e)}")
        return None


def get_bybit_market_data(symbol, interval="15"):
    """ДОБАВЛЕНО: Получение данных с Bybit V5"""
    try:
        # Тикер и OI
        t_res = REQUEST_SESSION.get(f"{BYBIT_API_URL}/v5/market/tickers?category=linear&symbol={symbol}",
                                    timeout=5).json()
        t_data = t_res['result']['list'][0]

        # Клайны для объема
        bybit_interval = interval if interval != "1440" else "D"
        k_res = REQUEST_SESSION.get(
            f"{BYBIT_API_URL}/v5/market/kline?category=linear&symbol={symbol}&interval={bybit_interval}&limit=2",
            timeout=5).json()
        k_list = k_res['result']['list']

        curr_k = k_list[0]
        prev_k = k_list[1]

        p_now = float(t_data['lastPrice'])
        p_open = float(curr_k[1])
        v_now = float(curr_k[5])
        v_prev = float(prev_k[5])

        return {
            'symbol': symbol,
            'price': p_now,
            'price_change': ((p_now - p_open) / p_open * 100) if p_open else 0,
            'volume': v_now,
            'volume_change': ((v_now - v_prev) / v_prev * 100) if v_prev else 0,
            'oi': float(t_data.get('openInterest', 0)),
            'total_liq': 0.0,  # Ликвидации по API Bybit требуют WebSocket для всех пар
            'timestamp': int(time.time() * 1000),
            'exchange': 'Bybit'
        }
    except Exception as e:
        logger.error(f"Bybit data error for {symbol}: {e}")
        return None


def get_binance_liquidations(symbol="BTCUSDT", interval_min="15"):
    """ИСПРАВЛЕНО: Считаем ликвидации суммарно за выбранный интервал"""
    try:
        if not is_futures_pair_available(symbol, 'Binance'):
            return 0.0, 0.0, 0.0

        response = REQUEST_SESSION.get(
            f"{BINANCE_API_URL}/fapi/v1/allForceOrders",
            params={'symbol': symbol, 'limit': 1000},
            timeout=5
        )
        liq_data = response.json()

        cutoff_time = int(time.time() * 1000) - (int(interval_min) * 60 * 1000)
        long_liq, short_liq = 0.0, 0.0

        if isinstance(liq_data, list):
            for order in liq_data:
                order_time = int(order.get('time', 0))
                if order_time >= cutoff_time:
                    value = float(order['executedQty']) * float(order['avgPrice'])
                    if order['side'] == 'SELL':
                        long_liq += value
                    else:
                        short_liq += value

        return (long_liq + short_liq), long_liq, short_liq
    except Exception as e:
        logger.debug(f"Ошибка ликвидаций для {symbol}: {str(e)}")
        return 0.0, 0.0, 0.0


def analyze_market(exchange, symbol, interval="15", enable_liq=False):
    """ИЗМЕНЕНО: Поддержка бирж"""
    if exchange == 'Bybit':
        return get_bybit_market_data(symbol, interval)

    try:
        binance_data = get_binance_ticker(symbol)
        if not binance_data: return None

        kline = get_binance_kline(symbol, interval, limit=2)
        if not kline or len(kline) < 2: return None

        latest_candle = kline[-1]
        open_price = float(latest_candle[1])
        current_price = binance_data['price']
        price_change = ((current_price - open_price) / open_price * 100) if open_price else 0

        latest_volume = float(latest_candle[5])
        prev_volume = float(kline[-2][5])
        volume_change = ((latest_volume - prev_volume) / prev_volume * 100) if prev_volume else 0

        total_liq, long_liq, short_liq = 0.0, 0.0, 0.0
        if enable_liq:
            total_liq, long_liq, short_liq = get_binance_liquidations(symbol, interval)

        return {
            'symbol': symbol, 'price': current_price, 'price_change': price_change,
            'volume': latest_volume, 'volume_change': volume_change,
            'oi': binance_data['oi'], 'total_liq': total_liq,
            'long_liq': long_liq, 'short_liq': short_liq,
            'timestamp': binance_data['timestamp'], 'exchange': 'Binance'
        }
    except Exception as e:
        logger.error(f"Analysis error for {symbol}: {str(e)}")
        return None


def get_all_futures_symbols():
    """Получаем все пары с обеих бирж"""
    available = get_available_futures_pairs()
    targets = []
    for s in available['Binance']: targets.append(('Binance', s))
    for s in available['Bybit']: targets.append(('Bybit', s))
    return targets


def get_main_keyboard():
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    buttons = ["📈 Цена BTC", "🔍 Анализ BTC", "🧠 Автосигналы", "📡 Статус мониторинга", "⚙ Настроить сигналы",
               "ℹ️ Помощь"]
    keyboard.add(*buttons)
    return keyboard


def get_settings_keyboard(chat_id):
    """ИЗМЕНЕНО: Добавлена кнопка AND/OR"""
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    s = user_settings[chat_id]
    mode_text = "🔄 Режим: ВСЕ (AND) ✅" if s.get('condition_mode') == 'ALL' else "🔄 Режим: ЛЮБОЕ (OR) 🔀"

    buttons = [
        "💰 Порог цены", "📊 Порог объема", "📈 Порог OI", "⏱ Интервал анализа", "💧 Порог ликвидаций",
        f"🔔 Цена: {'✅' if s.get('enable_price') else '❌'}",
        f"🔔 Объем: {'✅' if s.get('enable_volume') else '❌'}",
        f"🔔 OI: {'✅' if s.get('enable_oi') else '❌'}",
        f"🔔 Ликв: {'✅' if s.get('enable_liq') else '❌'}",
        mode_text, "✅ Сохранить настройки", "↩️ Назад"
    ]
    keyboard.add(*buttons)
    return keyboard


@bot.message_handler(commands=['start'])
def send_welcome(message):
    chat_id = message.chat.id
    welcome_msg = (
        "🤖 Добро пожаловать в <b>Crypto Market Scanner Pro</b>!\n\n"
        "📊 <b>Возможности бота:</b>\n"
        "• Мониторинг Binance + Bybit\n"
        "• Настраиваемая логика AND/OR\n"
        "• Точный расчет ликвидаций за интервал\n\n"
        "⚡️ <b>Быстрый старт:</b>\n"
        "1. Нажмите ⚙ Настроить сигналы\n"
        "2. Установите пороги и выберите режим (ЛЮБОЕ или ВСЕ)\n"
        "3. Запустите 🧠 Автосигналы"
    )
    bot.send_message(chat_id, welcome_msg, parse_mode='HTML', reply_markup=get_main_keyboard())


@bot.message_handler(func=lambda msg: "🔄 Режим:" in msg.text)
def toggle_condition_mode(message):
    chat_id = message.chat.id
    current = user_settings[chat_id].get('condition_mode', 'ANY')
    user_settings[chat_id]['condition_mode'] = 'ALL' if current == 'ANY' else 'ANY'
    bot.send_message(chat_id, f"✅ Режим изменен на: {user_settings[chat_id]['condition_mode']}",
                     reply_markup=get_settings_keyboard(chat_id))


@bot.message_handler(func=lambda msg: msg.text.startswith("🔔"))
def toggle_setting(message):
    chat_id = message.chat.id
    txt = message.text
    if "Цена" in txt:
        user_settings[chat_id]['enable_price'] = not user_settings[chat_id].get('enable_price', True)
    elif "Объем" in txt:
        user_settings[chat_id]['enable_volume'] = not user_settings[chat_id].get('enable_volume', True)
    elif "OI" in txt:
        user_settings[chat_id]['enable_oi'] = not user_settings[chat_id].get('enable_oi', True)
    elif "Ликв" in txt:
        user_settings[chat_id]['enable_liq'] = not user_settings[chat_id].get('enable_liq', True)
    bot.send_message(chat_id, "⚙️ Обновлено", reply_markup=get_settings_keyboard(chat_id))


@bot.message_handler(func=lambda msg: msg.text == "📈 Цена BTC")
def price_btn(message):
    data = get_binance_ticker("BTCUSDT")
    if data:
        update_time = datetime.fromtimestamp(data['timestamp'] / 1000).strftime('%H:%M:%S')
        msg = (
            f"💰 <b>BTC/USDT (Binance)</b>\n\n"
            f"• Цена: <b>{data['price']}$</b>\n"
            f"• Изменение 24ч: <b>{data['price_change']:+.2f}%</b>\n"
            f"🕒 Обновлено: {update_time} UTC"
        )
        bot.send_message(message.chat.id, msg, parse_mode='HTML')


@bot.message_handler(func=lambda msg: msg.text == "🔍 Анализ BTC")
def scan_btn(message):
    chat_id = message.chat.id
    s = user_settings[chat_id]
    status_msg = bot.send_message(chat_id, "🔄 Анализирую BTC/USDT...")
    analysis = analyze_market('Binance', "BTCUSDT", interval=s['interval'], enable_liq=s['enable_liq'])

    if analysis:
        msg = (
            f"🔍 <b>BTC/USDT Анализ</b>\n"
            f"💰 Цена: <b>{analysis['price']:.2f}$</b> ({analysis['price_change']:+.2f}%)\n"
            f"📊 Объем: {analysis['volume_change']:+.2f}%\n"
            f"💧 Ликв: {analysis['total_liq']:,.0f}$\n"
            f"⏱ Интервал: {s['interval']}м"
        )
        bot.edit_message_text(msg, chat_id, status_msg.message_id, parse_mode='HTML')


@bot.message_handler(func=lambda msg: msg.text == "⚙ Настроить сигналы")
def setup_signals(message):
    chat_id = message.chat.id
    bot.send_message(chat_id, "⚙️ Настройки параметров:", reply_markup=get_settings_keyboard(chat_id))


@bot.message_handler(func=lambda msg: msg.text in ["💰 Порог цены", "📊 Порог объема", "📈 Порог OI", "⏱ Интервал анализа",
                                                   "💧 Порог ликвидаций"])
def setting_selection(message):
    chat_id = message.chat.id
    text = message.text
    params = {
        "💰 Порог цены": "price_threshold", "📊 Порог объема": "volume_threshold",
        "📈 Порог OI": "oi_threshold", "⏱ Интервал анализа": "interval",
        "💧 Порог ликвидаций": "liq_threshold"
    }
    user_states[chat_id] = {'setting': params[text]}
    bot.send_message(chat_id, f"✍️ Введите новое значение для: {text}", reply_markup=types.ReplyKeyboardRemove())


@bot.message_handler(func=lambda msg: msg.chat.id in user_states and 'setting' in user_states[msg.chat.id])
def handle_setting_input(message):
    chat_id = message.chat.id
    setting = user_states[chat_id]['setting']
    try:
        val = message.text.replace(',', '').replace(' ', '')
        user_settings[chat_id][setting] = val if setting == 'interval' else float(val)
        del user_states[chat_id]
        bot.send_message(chat_id, "✅ Сохранено", reply_markup=get_settings_keyboard(chat_id))
    except:
        bot.send_message(chat_id, "❌ Введите число")


@bot.message_handler(func=lambda msg: msg.text == "✅ Сохранить настройки")
def save_settings(message):
    bot.send_message(message.chat.id, "🎉 Настройки применены!", reply_markup=get_main_keyboard())


@bot.message_handler(func=lambda msg: msg.text == "↩️ Назад")
def go_back(message):
    bot.send_message(message.chat.id, "Главное меню", reply_markup=get_main_keyboard())


def check_signal_conditions(analysis, settings, chat_id, symbol):
    """ИСПРАВЛЕНО: Логика ANY/ALL"""
    res = {
        'price_match': False, 'volume_match': False, 'oi_match': False, 'liq_match': False,
        'oi_change_pct': 0.0, 'price_change': analysis['price_change'],
        'volume_change': analysis['volume_change'], 'total_liq': analysis['total_liq']
    }

    # OI Change
    prev_oi_key = f"{chat_id}_{symbol}"
    with oi_lock:
        current_oi = analysis['oi']
        if prev_oi_key not in prev_oi_data:
            prev_oi_data[prev_oi_key] = {'value': current_oi, 'first_measurement': True}
        else:
            prev_val = prev_oi_data[prev_oi_key]['value']
            res['oi_change_pct'] = ((current_oi - prev_val) / prev_val * 100) if prev_val else 0
            prev_oi_data[prev_oi_key]['value'] = current_oi

    active_filters = []
    matches = []

    if settings.get('enable_price'):
        active_filters.append('price')
        p_t = settings.get('price_threshold', 1.5)
        m = (analysis['price_change'] >= p_t) if p_t >= 0 else (analysis['price_change'] <= p_t)
        if m: matches.append('price')
        res['price_match'] = m

    if settings.get('enable_volume'):
        active_filters.append('volume')
        v_t = settings.get('volume_threshold', 50.0)
        m = analysis['volume_change'] >= v_t
        if m: matches.append('volume')
        res['volume_match'] = m

    if settings.get('enable_oi'):
        active_filters.append('oi')
        oi_t = settings.get('oi_threshold', 5.0)
        m = (res['oi_change_pct'] >= oi_t) if oi_t >= 0 else (res['oi_change_pct'] <= oi_t)
        if m: matches.append('oi')
        res['oi_match'] = m

    if settings.get('enable_liq'):
        active_filters.append('liq')
        m = analysis['total_liq'] >= settings.get('liq_threshold', 1000000)
        if m: matches.append('liq')
        res['liq_match'] = m

    # РЕЖИМ AND/OR
    if settings.get('condition_mode') == 'ALL':
        res['triggered'] = (len(matches) == len(active_filters)) and len(active_filters) > 0
    else:
        res['triggered'] = len(matches) > 0

    return res


@bot.message_handler(func=lambda msg: msg.text == "🧠 Автосигналы")
def start_auto_signals(message):
    chat_id = message.chat.id
    settings = user_settings[chat_id]

    if chat_id in active_monitors and active_monitors[chat_id]:
        bot.send_message(chat_id, "⚠️ Мониторинг уже запущен.")
        return

    all_targets = get_all_futures_symbols()
    active_monitors[chat_id] = True
    monitoring_progress[chat_id] = {'status': 'active', 'start_time': time.time(), 'scanned_coins': 0,
                                    'total_coins': len(all_targets)}

    bot.send_message(chat_id, f"🚀 Мониторинг {len(all_targets)} пар (Binance + Bybit) запущен!")

    def monitor():
        last_signal_time = {}
        while active_monitors.get(chat_id):
            try:
                monitoring_progress[chat_id]['scanned_coins'] = 0
                with ThreadPoolExecutor(max_workers=30) as executor:
                    future_to_target = {
                        executor.submit(analyze_market, t[0], t[1], settings['interval'], settings['enable_liq']): t for
                        t in all_targets}
                    for future in as_completed(future_to_target):
                        if not active_monitors.get(chat_id): break
                        data = future.result()
                        monitoring_progress[chat_id]['scanned_coins'] += 1
                        if not data: continue

                        cond = check_signal_conditions(data, settings, chat_id, data['symbol'])
                        if cond['triggered']:
                            key = f"{data['exchange']}_{data['symbol']}"
                            if time.time() - last_signal_time.get(key, 0) > 300:
                                last_signal_time[key] = time.time()
                                msg = (
                                    f"🚨 <b>СИГНАЛ: {data['exchange']}</b>\n"
                                    f"🪙 Пара: <code>{data['symbol']}</code>\n"
                                    f"💰 Цена: {data['price']:.4f} ({data['price_change']:+.2f}%)\n"
                                    f"📊 Объем: {data['volume_change']:+.2f}%\n"
                                    f"📈 OI: {cond['oi_change_pct']:+.2f}%\n"
                                    f"💧 Ликв: {data['total_liq']:,.0f}$\n"
                                    f"⏱ Интравал: {settings['interval']}м"
                                )
                                bot.send_message(chat_id, msg, parse_mode='HTML')
                time.sleep(60)
            except Exception as e:
                logger.error(f"Error in monitor: {e}")
                time.sleep(10)

    threading.Thread(target=monitor, daemon=True).start()


@bot.message_handler(func=lambda msg: msg.text == "📡 Статус мониторинга")
def status_btn(message):
    cid = message.chat.id
    if cid in monitoring_progress:
        p = monitoring_progress[cid]
        bot.send_message(cid, f"📡 <b>Статус:</b>\nОбработано: {p['scanned_coins']}/{p['total_coins']}",
                         parse_mode='HTML')
    else:
        bot.send_message(cid, "❌ Не запущен")


@bot.message_handler(commands=['stop'])
def stop_command(message):
    active_monitors[message.chat.id] = False
    bot.send_message(message.chat.id, "🛑 Мониторинг остановлен")


@bot.message_handler(commands=['debug'])
def debug_command(message):
    chat_id = message.chat.id
    msg = f"🔧 Дебаг:\nАктивных пар: {len(get_all_futures_symbols())}\nТвой режим: {user_settings[chat_id].get('condition_mode', 'ANY')}"
    bot.send_message(chat_id, msg)


if __name__ == "__main__":
    logger.info("🚀 Бот запускается...")
    get_available_futures_pairs(force_update=True)
    bot.infinity_polling()
