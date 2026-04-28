"""
ProspecTA: Senales de Trading con analisis tecnico real.
Wyckoff + Ichimoku + Estocastico sobre acciones Colombia, EE.UU. y Cripto.
Interfaz de Telegram con botones inline. Freemium: 3 gratis/mes, $9.99/mes.

Autor: Ana Lorena Jimenez Preciado
XIX Congreso Internacional de Prospectiva (Cali, Colombia)
"""
import os, re, json, html, logging, tempfile, asyncio, traceback
from datetime import datetime, date
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()

import yfinance as yf
import pandas as pd
from google import genai
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, LabeledPrice
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, PreCheckoutQueryHandler, ContextTypes, filters,
)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("prospecta")

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
API_KEY = os.environ["API_KEY"]
FLASH_MODEL = os.environ.get("FLASH_MODEL", "gemini-2.5-flash")
TEMPLATE_PATH = Path(__file__).parent / "dashboard_template.html"
gemini = genai.Client(api_key=API_KEY)

FREE_MONTHLY_LIMIT = 3  # 3 gratis, luego cobra
SUB_DAILY_LIMIT = 3
SUB_PRICE = 9.99
STARS_PRICE = 250  # ~$9.99 en Telegram Stars

# ---------------------------------------------------------------------------
# Control de uso (en memoria - en produccion usar BD)
# ---------------------------------------------------------------------------
user_usage: dict[int, dict] = {}


def reset_user(user_id: int) -> None:
    """Reinicia los intentos gratuitos de un usuario."""
    today = date.today().isoformat()
    user_usage[user_id] = {"month": today[:7], "free": 0, "sub": False,
                            "day": today, "daily": 0}
    logger.info("Intentos reiniciados para usuario %s", user_id)


def check_usage(user_id: int) -> tuple[bool, str]:
    """Verifica si el usuario puede hacer una consulta (NO incrementa)."""
    today = date.today().isoformat()
    month = today[:7]

    if user_id not in user_usage:
        user_usage[user_id] = {"month": month, "free": 0, "sub": False,
                                "day": today, "daily": 0}
    u = user_usage[user_id]

    # Reset mensual
    if u["month"] != month:
        u["month"] = month
        u["free"] = 0

    # Reset diario
    if u["day"] != today:
        u["day"] = today
        u["daily"] = 0

    # Suscriptor PRO
    if u.get("sub"):
        if u["daily"] >= SUB_DAILY_LIMIT:
            return False, "Limite diario alcanzado."
        return True, ""

    # Plan gratuito
    if u["free"] < FREE_MONTHLY_LIMIT:
        return True, ""

    return False, ""


def record_usage(user_id: int) -> None:
    """Incrementa el contador SOLO tras analisis exitoso."""
    u = user_usage.get(user_id)
    if not u:
        return
    if u.get("sub"):
        u["daily"] += 1
    else:
        u["free"] += 1


def get_usage_text(user_id: int) -> str:
    u = user_usage.get(user_id, {})
    free = u.get("free", 0)
    if u.get("sub"):
        return f"Suscriptor PRO | {u.get('daily', 0)}/{SUB_DAILY_LIMIT} hoy"
    return f"Plan gratuito: {free}/{FREE_MONTHLY_LIMIT} usados este mes"


# ---------------------------------------------------------------------------
# Activos por mercado
# ---------------------------------------------------------------------------
MARKETS = {
    # --- Colombia (ADRs en NYSE) ---
    "col": {"name": "Colombia", "flag": "\ud83c\udde8\ud83c\uddf4", "assets": {
        "EC": "Ecopetrol", "CIB": "Bancolombia", "AVAL": "Grupo Aval",
        "ICE": "Interconexion Electrica", "BHP": "BHP (Minera)",
    }},
    # --- EE.UU. por sector ---
    "us_tech": {"name": "EE.UU. Tech", "flag": "\ud83c\uddfa\ud83c\uddf8\ud83d\udcbb", "assets": {
        "AAPL": "Apple", "MSFT": "Microsoft", "NVDA": "Nvidia",
        "GOOGL": "Google", "META": "Meta", "AMZN": "Amazon",
    }},
    "us_finance": {"name": "EE.UU. Finanzas", "flag": "\ud83c\uddfa\ud83c\uddf8\ud83c\udfe6", "assets": {
        "JPM": "JP Morgan", "BAC": "Bank of America", "GS": "Goldman Sachs",
        "V": "Visa", "MA": "Mastercard", "BLK": "BlackRock",
    }},
    "us_health": {"name": "EE.UU. Salud", "flag": "\ud83c\uddfa\ud83c\uddf8\ud83d\udc8a", "assets": {
        "JNJ": "Johnson & Johnson", "UNH": "UnitedHealth", "PFE": "Pfizer",
        "LLY": "Eli Lilly", "ABBV": "AbbVie", "MRK": "Merck",
    }},
    "us_energy": {"name": "EE.UU. Energia", "flag": "\ud83c\uddfa\ud83c\uddf8\u26a1", "assets": {
        "XOM": "Exxon Mobil", "CVX": "Chevron", "COP": "ConocoPhillips",
        "TSLA": "Tesla", "NEE": "NextEra Energy", "ENPH": "Enphase",
    }},
    "us_consumer": {"name": "EE.UU. Consumo", "flag": "\ud83c\uddfa\ud83c\uddf8\ud83d\udecd\ufe0f", "assets": {
        "WMT": "Walmart", "KO": "Coca-Cola", "MCD": "McDonald's",
        "NKE": "Nike", "DIS": "Disney", "PEP": "PepsiCo",
    }},
    # --- Cripto ---
    "crypto_top": {"name": "Cripto Top", "flag": "\u20bf", "assets": {
        "BTC-USD": "Bitcoin", "ETH-USD": "Ethereum", "SOL-USD": "Solana",
        "BNB-USD": "BNB", "XRP-USD": "XRP",
    }},
    "crypto_alt": {"name": "Cripto Alt", "flag": "\ud83e\udea8", "assets": {
        "ADA-USD": "Cardano", "AVAX-USD": "Avalanche", "DOT-USD": "Polkadot",
        "LINK-USD": "Chainlink", "DOGE-USD": "Dogecoin",
    }},
}

# ---------------------------------------------------------------------------
# Analisis tecnico
# ---------------------------------------------------------------------------

def fetch_and_analyze(symbol: str) -> dict | None:
    """Calcula Ichimoku, Estocastico y senal Wyckoff."""
    try:
        df = yf.Ticker(symbol).history(period="6mo", interval="1d")
        if df.empty or len(df) < 52:
            logger.warning("%s: solo %d filas", symbol, len(df))
            return None
        h, l, c, v = df["High"], df["Low"], df["Close"], df["Volume"]
        price = float(c.iloc[-1])

        # Ichimoku
        tenkan = float((h.rolling(9, min_periods=9).max() +
                        l.rolling(9, min_periods=9).min()).iloc[-1] / 2)
        kijun = float((h.rolling(26, min_periods=26).max() +
                       l.rolling(26, min_periods=26).min()).iloc[-1] / 2)
        span_a = (tenkan + kijun) / 2
        span_b = float((h.rolling(52, min_periods=52).max() +
                        l.rolling(52, min_periods=52).min()).iloc[-1] / 2)
        cloud_top, cloud_bot = max(span_a, span_b), min(span_a, span_b)
        ichi_signal = "ALCISTA" if price > cloud_top else ("BAJISTA" if price < cloud_bot else "NEUTRAL")
        ichi_cross = "COMPRA" if tenkan > kijun else "VENTA"

        # Estocastico (14 periodos)
        low14, high14 = l.rolling(14, min_periods=14).min(), h.rolling(14, min_periods=14).max()
        denom = high14.iloc[-1] - low14.iloc[-1]
        k_val = float(((c.iloc[-1] - low14.iloc[-1]) / denom) * 100) if denom != 0 else 50
        k_series = ((c - low14) / (high14 - low14)) * 100
        d_val = float(k_series.rolling(3, min_periods=3).mean().iloc[-1])
        stoch_signal = "SOBREVENTA" if k_val < 20 else ("SOBRECOMPRA" if k_val > 80 else "NEUTRAL")

        # Wyckoff simplificado
        vol_sma20 = float(v.rolling(20, min_periods=20).mean().iloc[-1])
        vol_now, vol_ratio = float(v.iloc[-1]), float(v.iloc[-1]) / vol_sma20 if vol_sma20 else 1
        chg20 = float((c.iloc[-1] - c.iloc[-20]) / c.iloc[-20] * 100)
        if chg20 < -5 and vol_ratio > 1.3: wyckoff = "CAPITULACION"
        elif chg20 < -2 and vol_ratio < 0.8: wyckoff = "ACUMULACION"
        elif chg20 > 5 and vol_ratio > 1.3: wyckoff = "MARKUP"
        elif chg20 > 2 and vol_ratio < 0.8: wyckoff = "DISTRIBUCION"
        else: wyckoff = "RANGO"

        # ATR (14)
        tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
        atr = float(tr.rolling(14, min_periods=14).mean().iloc[-1])

        # Senal compuesta
        bull = sum([ichi_signal == "ALCISTA", ichi_cross == "COMPRA",
                    stoch_signal == "SOBREVENTA" or k_val < 40,
                    wyckoff in ("ACUMULACION", "MARKUP")])
        if bull >= 3: signal, conf = "COMPRA", min(60 + bull * 10, 95)
        elif bull <= 1: signal, conf = "VENTA", min(60 + (4 - bull) * 10, 95)
        else: signal, conf = "NEUTRAL", 50

        target = round(price + atr * 2, 2) if signal == "COMPRA" else round(price - atr * 2, 2)
        stop = round(price - atr * 1.2, 2) if signal == "COMPRA" else round(price + atr * 1.2, 2)

        # Ultimos 30 precios para sparkline
        prices_30d = [round(float(x), 2) for x in c.iloc[-30:].values]

        return {
            "price": round(price, 2), "atr": round(atr, 2),
            "signal": signal, "confidence": conf,
            "target": target, "stop_loss": stop,
            "target_pct": round((target - price) / price * 100, 1),
            "stop_pct": round((stop - price) / price * 100, 1),
            "ichimoku": {"signal": ichi_signal, "cross": ichi_cross,
                         "tenkan": round(tenkan, 2), "kijun": round(kijun, 2)},
            "stochastic": {"k": round(k_val, 1), "d": round(d_val, 1), "signal": stoch_signal},
            "wyckoff": wyckoff, "vol_ratio": round(vol_ratio, 2), "change_20d": round(chg20, 1),
            "prices_30d": prices_30d,
        }
    except Exception as e:
        logger.error("Error analizando %s: %s", symbol, e, exc_info=True)
        return None


def analyze_market(market_key: str) -> list[dict]:
    mkt = MARKETS[market_key]
    results = []
    for symbol, name in mkt["assets"].items():
        logger.info("Analizando %s (%s)...", name, symbol)
        data = fetch_and_analyze(symbol)
        if data:
            data["symbol"] = symbol
            data["name"] = name
            results.append(data)
    # Ordenar: COMPRA primero, luego por confianza desc
    order = {"COMPRA": 0, "NEUTRAL": 1, "VENTA": 2}
    results.sort(key=lambda r: (order.get(r["signal"], 1), -r["confidence"]))
    return results


# ---------------------------------------------------------------------------
# IA con Flash
# ---------------------------------------------------------------------------
SIGNAL_SYSTEM = (
    "Eres un analista tecnico profesional. Recibes datos de indicadores tecnicos "
    "reales (Ichimoku, Estocastico, Wyckoff) de 3 activos. "
    "Da un resumen de 3-4 lineas por activo. Se directo: 'Comprar', 'Vender' o "
    "'Esperar'. Menciona niveles clave de soporte/resistencia. "
    "Responde en espanol. NO inventes datos."
)


async def get_ai_commentary(results: list[dict], market_name: str) -> str:
    prompt = f"Mercado: {market_name}\n{json.dumps(results, indent=2, ensure_ascii=False)}"
    resp = await gemini.aio.models.generate_content(
        model=FLASH_MODEL, contents=f"{SIGNAL_SYSTEM}\n\n{prompt}")
    return resp.text


# ---------------------------------------------------------------------------
# Dashboard HTML
# ---------------------------------------------------------------------------
def _md_to_html(text: str) -> str:
    text = html.escape(text)
    text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
    text = re.sub(r'\*(.+?)\*', r'<em>\1</em>', text)
    return ''.join(f'<p>{p.strip()}</p>' for p in text.split('\n\n') if p.strip())


def generate_dashboard(results: list[dict], market: dict, commentary: str) -> Path:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    today = datetime.now().strftime("%d %b %Y %H:%M")

    cards_html = ""
    all_charts = {}
    for i, r in enumerate(results):
        sig_class = {"COMPRA": "buy", "VENTA": "sell"}.get(r["signal"], "hold")
        sig_emoji = {"COMPRA": "🟢", "VENTA": "🔴"}.get(r["signal"], "🟡")
        chart_id = f"chart_{i}"
        all_charts[chart_id] = r.get("prices_30d", [])
        cards_html += f"""
        <div class="signal-card {sig_class}">
          <div class="signal-header">
            <span class="signal-badge {sig_class}">{sig_emoji} {r['signal']}</span>
            <span class="confidence">{r['confidence']}%</span>
          </div>
          <h3>{r['name']} <span class="symbol">({r['symbol']})</span></h3>
          <div class="chart-mini"><canvas id="{chart_id}" height="60"></canvas></div>
          <div class="price-row">
            <div class="price-item"><span class="label">Precio</span><span class="val">${r['price']:,.2f}</span></div>
            <div class="price-item"><span class="label">Objetivo</span><span class="val target">${r['target']:,.2f} ({r['target_pct']:+.1f}%)</span></div>
            <div class="price-item"><span class="label">Stop Loss</span><span class="val stop">${r['stop_loss']:,.2f} ({r['stop_pct']:+.1f}%)</span></div>
          </div>
          <div class="indicators">
            <div class="ind"><span class="ind-name">Ichimoku</span><span class="ind-val">{r['ichimoku']['signal']} / {r['ichimoku']['cross']}</span></div>
            <div class="ind"><span class="ind-name">Estocastico</span><span class="ind-val">K={r['stochastic']['k']} D={r['stochastic']['d']} ({r['stochastic']['signal']})</span></div>
            <div class="ind"><span class="ind-name">Wyckoff</span><span class="ind-val">{r['wyckoff']} (Vol {r['vol_ratio']}x)</span></div>
          </div>
        </div>"""

    replacements = {
        "{{DATE}}": today,
        "{{MARKET_NAME}}": f"{market['flag']} {market['name']}",
        "{{SIGNAL_CARDS}}": cards_html,
        "{{AI_COMMENTARY}}": _md_to_html(commentary),
        "{{CHART_DATA_JSON}}": json.dumps(all_charts, ensure_ascii=False),
    }
    for ph, val in replacements.items():
        template = template.replace(ph, val)

    out = Path(tempfile.mktemp(suffix=".html", prefix="scout_"))
    out.write_text(template, encoding="utf-8")
    return out


# ---------------------------------------------------------------------------
# Telegram Handlers
# ---------------------------------------------------------------------------
def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🇨🇴 Colombia", callback_data="mkt_col")],
        [InlineKeyboardButton("🇺🇸 Tech", callback_data="mkt_us_tech"),
         InlineKeyboardButton("🇺🇸 Finanzas", callback_data="mkt_us_finance")],
        [InlineKeyboardButton("🇺🇸 Salud", callback_data="mkt_us_health"),
         InlineKeyboardButton("🇺🇸 Energia", callback_data="mkt_us_energy")],
        [InlineKeyboardButton("🇺🇸 Consumo", callback_data="mkt_us_consumer")],
        [InlineKeyboardButton("₿ Cripto Top 5", callback_data="mkt_crypto_top"),
         InlineKeyboardButton("🪨 Altcoins", callback_data="mkt_crypto_alt")],
        [InlineKeyboardButton("📚 Como leer las senales", callback_data="learn")],
    ])


def us_submenu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💻 Tech", callback_data="mkt_us_tech"),
         InlineKeyboardButton("🏦 Finanzas", callback_data="mkt_us_finance")],
        [InlineKeyboardButton("💊 Salud", callback_data="mkt_us_health"),
         InlineKeyboardButton("⚡ Energia", callback_data="mkt_us_energy")],
        [InlineKeyboardButton("🛍️ Consumo", callback_data="mkt_us_consumer")],
        [InlineKeyboardButton("◀️ Volver", callback_data="back")],
    ])


def crypto_submenu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("₿ Top 5", callback_data="mkt_crypto_top"),
         InlineKeyboardButton("🪨 Altcoins", callback_data="mkt_crypto_alt")],
        [InlineKeyboardButton("◀️ Volver", callback_data="back")],
    ])


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    usage = get_usage_text(uid)
    await update.message.reply_text(
        "\U0001f4ca *ProspecTA*\n"
        "_Senales de trading con IA y analisis tecnico real_\n\n"
        "\u2022 Wyckoff \u00b7 Ichimoku \u00b7 Estocastico\n"
        "\u2022 Precio objetivo y Stop Loss\n"
        "\u2022 Nivel de confiabilidad por senal\n\n"
        f"\U0001f381 *{FREE_MONTHLY_LIMIT} analisis gratis*\n"
        f"\U0001f48e Plan PRO: ${SUB_PRICE}/mes \u2014 3 consultas diarias\n\n"
        f"_{usage}_\n\n"
        "Selecciona un mercado:",
        reply_markup=main_menu_keyboard(),
        parse_mode="Markdown",
    )


LEARN_TEXT = (
    "📚 *Como leer las senales de ProspecTA*\n\n"
    "ProspecTA combina 3 metodos de analisis tecnico profesional:\n\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "☁️ *ICHIMOKU KINKO HYO*\n"
    "_\"Equilibrio de un vistazo\"_\n\n"
    "Imagina una nube en el grafico:\n"
    "• Precio *arriba* de la nube → tendencia ALCISTA 🟢\n"
    "• Precio *abajo* de la nube → tendencia BAJISTA 🔴\n"
    "• Precio *dentro* de la nube → sin tendencia clara 🟡\n\n"
    "Tambien mide dos lineas (Tenkan y Kijun):\n"
    "• Tenkan cruza *arriba* de Kijun → senal de COMPRA\n"
    "• Tenkan cruza *abajo* de Kijun → senal de VENTA\n\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "📊 *ESTOCASTICO*\n"
    "_Mide si el precio esta \"caro\" o \"barato\"_\n\n"
    "Usa una escala de 0 a 100:\n"
    "• K < 20 → *SOBREVENTA* (puede subir pronto) 🟢\n"
    "• K > 80 → *SOBRECOMPRA* (puede bajar pronto) 🔴\n"
    "• 20-80 → zona neutral 🟡\n\n"
    "Es como un termometro del precio.\n\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "🏗 *WYCKOFF*\n"
    "_Analiza que hacen los \"grandes jugadores\"_\n\n"
    "Fases del mercado:\n"
    "• *ACUMULACION* → los grandes compran callados (oportunidad) 🟢\n"
    "• *MARKUP* → el precio sube con fuerza 📈\n"
    "• *DISTRIBUCION* → los grandes venden callados (peligro) 🔴\n"
    "• *CAPITULACION* → venta masiva con panico 📉\n"
    "• *RANGO* → mercado sin direccion clara\n\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "🎯 *CONFIABILIDAD*\n\n"
    "• *85-95%* → Los 3 indicadores coinciden → senal fuerte\n"
    "• *60-75%* → 2 de 3 coinciden → senal moderada\n"
    "• *50%* → indicadores mixtos → mejor esperar\n"
)


async def _send(bot, chat_id, text, reply_markup=None, retries=3):
    """Enviar mensaje con reintentos para NetworkError de Railway."""
    for attempt in range(retries):
        try:
            return await bot.send_message(chat_id=chat_id, text=text,
                                          reply_markup=reply_markup,
                                          read_timeout=30, write_timeout=30, connect_timeout=30)
        except Exception as e:
            if attempt < retries - 1:
                logger.warning("send_message intento %d fallo: %s", attempt + 1, e)
                await asyncio.sleep(2)
            else:
                logger.error("send_message fallo tras %d intentos: %s", retries, e)
                raise


async def button_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data
    chat_id = query.message.chat_id
    logger.info("Callback recibido: data=%s chat_id=%s user=%s", data, chat_id, query.from_user.id)

    # Responder callback inmediatamente para quitar el "relojito"
    try:
        await query.answer()
    except Exception as e:
        logger.warning("Error answering callback: %s", e)

    # Navegacion de sub-menus
    if data == "back":
        await _send(ctx.bot, chat_id, "Selecciona un mercado:",
                    reply_markup=main_menu_keyboard())
        return
    if data == "sub_us":
        await _send(ctx.bot, chat_id,
            "\U0001f1fa\U0001f1f8 EE.UU. \u2014 Selecciona un sector:",
            reply_markup=us_submenu())
        return
    if data == "sub_crypto":
        await _send(ctx.bot, chat_id,
            "\u20bf Cripto \u2014 Selecciona una categoria:",
            reply_markup=crypto_submenu())
        return
    if data == "subscribe":
        await send_subscription_invoice(chat_id, ctx)
        return
    if data == "learn":
        try:
            await _send(ctx.bot, chat_id, LEARN_TEXT)
        except Exception:
            pass
        await _send(ctx.bot, chat_id, "Analizar un mercado:",
                    reply_markup=main_menu_keyboard())
        return

    if not data.startswith("mkt_"):
        return

    market_key = data.replace("mkt_", "")
    if market_key not in MARKETS:
        logger.warning("Market key no encontrado: %s", market_key)
        return

    uid = query.from_user.id
    allowed, msg = check_usage(uid)
    if not allowed:
        await _send(ctx.bot, chat_id,
            f"\U0001f6ab Limite alcanzado\n\n"
            f"Has usado tus {FREE_MONTHLY_LIMIT} analisis gratis.\n\n"
            f"\U0001f48e Plan PRO \u2014 {STARS_PRICE} Stars (~${SUB_PRICE})\n"
            "\u2022 3 consultas diarias\n"
            "\u2022 Todos los mercados\n"
            "\u2022 Dashboard con IA",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("\U0001f48e Suscribirse PRO", callback_data="subscribe")],
                [InlineKeyboardButton("\u25c0\ufe0f Volver", callback_data="back")],
            ]),
        )
        return

    mkt = MARKETS[market_key]
    logger.info("Iniciando analisis de mercado: %s", market_key)

    try:
        # Mensaje de espera
        await _send(ctx.bot, chat_id,
            f"\u23f3 Analizando {mkt['flag']} {mkt['name']}...\n"
            "Calculando Ichimoku, Estocastico y Wyckoff...\n"
            "Esto puede tardar 10-30 segundos."
        )

        # Analisis con timeout de 90 segundos
        try:
            results = await asyncio.wait_for(
                asyncio.to_thread(analyze_market, market_key),
                timeout=90
            )
        except asyncio.TimeoutError:
            logger.error("Timeout analizando mercado %s", market_key)
            await _send(ctx.bot, chat_id,
                "\u23f0 El analisis tardo demasiado. Intente de nuevo.",
                reply_markup=main_menu_keyboard())
            return
        except Exception as e:
            logger.error("Error en analyze_market: %s", e, exc_info=True)
            await _send(ctx.bot, chat_id,
                f"\u274c Error obteniendo datos: {type(e).__name__}",
                reply_markup=main_menu_keyboard())
            return

        if not results:
            await _send(ctx.bot, chat_id,
                "No se pudieron obtener datos. Intente mas tarde.",
                reply_markup=main_menu_keyboard())
            return

        logger.info("Analisis completado: %d activos", len(results))
        record_usage(uid)  # Solo cuenta si el analisis funciono

        # Resumen en Telegram
        lines = [f"\U0001f4ca {mkt['flag']} {mkt['name']} \u2014 Senales\n"]
        for r in results:
            emoji = {"COMPRA": "\U0001f7e2", "VENTA": "\U0001f534"}.get(r["signal"], "\U0001f7e1")
            lines.append(
                f"{emoji} {r['name']} ({r['symbol']}) \u2014 {r['signal']} {r['confidence']}%\n"
                f"   \U0001f4b0 ${r['price']:,.2f} \u2192 \U0001f3af ${r['target']:,.2f} ({r['target_pct']:+.1f}%)\n"
                f"   \U0001f6d1 Stop: ${r['stop_loss']:,.2f} | Wyckoff: {r['wyckoff']}\n")
        usage = get_usage_text(uid)
        lines.append(f"\n{usage}\nGenerando dashboard con IA...")

        await _send(ctx.bot, chat_id, "\n".join(lines))

        # Dashboard
        try:
            commentary = await get_ai_commentary(results, mkt["name"])
            html_path = generate_dashboard(results, mkt, commentary)
            for attempt in range(3):
                try:
                    await ctx.bot.send_document(chat_id=chat_id,
                        document=html_path.open("rb"),
                        filename=f"ProspecTA_{market_key}_{datetime.now():%Y%m%d_%H%M}.html",
                        caption="Abra en su navegador para ver graficas interactivas.",
                        read_timeout=60, write_timeout=60, connect_timeout=60)
                    break
                except Exception:
                    if attempt < 2:
                        await asyncio.sleep(2)
            html_path.unlink(missing_ok=True)
        except Exception as e:
            logger.error("Error dashboard: %s", e, exc_info=True)
            await _send(ctx.bot, chat_id,
                "\u26a0\ufe0f Dashboard no disponible, pero las senales arriba son validas.")

        await _send(ctx.bot, chat_id, "Analizar otro mercado:",
                    reply_markup=main_menu_keyboard())

    except Exception as e:
        logger.error("Error CRITICO en button_handler: %s", traceback.format_exc())
        try:
            await _send(ctx.bot, chat_id,
                f"\u274c Error inesperado: {type(e).__name__} - {str(e)[:50]}.\nIntente /start de nuevo.",
                reply_markup=main_menu_keyboard())
        except Exception:
            pass


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Selecciona un mercado:",
        reply_markup=main_menu_keyboard())


# ---------------------------------------------------------------------------
# Pagos con Telegram Stars
# ---------------------------------------------------------------------------
async def send_subscription_invoice(chat_id: int, ctx: ContextTypes.DEFAULT_TYPE):
    """Envia factura de suscripcion via Telegram Stars."""
    await ctx.bot.send_invoice(
        chat_id=chat_id,
        title="ProspecTA PRO - Suscripcion Mensual",
        description=(
            "3 analisis diarios de mercado con IA.\n"
            "Colombia, EE.UU. (5 sectores) y Cripto.\n"
            "Wyckoff + Ichimoku + Estocastico."
        ),
        payload="prospecta_pro_monthly",
        provider_token="",  # Telegram Stars
        currency="XTR",
        prices=[LabeledPrice(label="ProspecTA PRO", amount=STARS_PRICE)],
    )


async def pre_checkout_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Aprueba el pago."""
    await update.pre_checkout_query.answer(ok=True)


async def successful_payment_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Activa la suscripcion tras pago exitoso."""
    uid = update.effective_user.id
    today = date.today().isoformat()
    if uid not in user_usage:
        user_usage[uid] = {"month": today[:7], "free": 0, "sub": True,
                           "day": today, "daily": 0}
    else:
        user_usage[uid]["sub"] = True
        user_usage[uid]["daily"] = 0
    logger.info("Pago exitoso de usuario %s", uid)
    await update.message.reply_text(
        "\u2705 *Suscripcion PRO activada!*\n\n"
        "Ya puedes hacer hasta 3 analisis diarios.\n"
        "Selecciona un mercado:",
        reply_markup=main_menu_keyboard(),
        parse_mode="Markdown",
    )


async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Reinicia los intentos del usuario que envia el comando."""
    uid = update.effective_user.id
    reset_user(uid)
    usage = get_usage_text(uid)
    await update.message.reply_text(
        "✅ Tus intentos han sido reiniciados.\n\n"
        f"{usage}\n\n"
        "Selecciona un mercado:",
        reply_markup=main_menu_keyboard(),
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Diagnostico: prueba yfinance y Gemini."""
    uid = update.effective_user.id
    lines = ["\U0001f527 Diagnostico ProspecTA\n"]

    # Test yfinance
    try:
        import yfinance as yf
        df = yf.Ticker("AAPL").history(period="5d", interval="1d")
        lines.append(f"\u2705 Yahoo Finance: {len(df)} filas")
    except Exception as e:
        lines.append(f"\u274c Yahoo Finance: {e}")

    # Test Gemini
    try:
        resp = await gemini.aio.models.generate_content(
            model=FLASH_MODEL, contents="Responde solo: OK")
        lines.append(f"\u2705 Gemini: {resp.text.strip()[:50]}")
    except Exception as e:
        lines.append(f"\u274c Gemini: {e}")

    # Usage
    lines.append(f"\n{get_usage_text(uid)}")
    lines.append(f"Usuarios en memoria: {len(user_usage)}")

    await update.message.reply_text("\n".join(lines))


def main() -> None:
    logger.info("Iniciando ProspecTA")
    app = (Application.builder()
           .token(TELEGRAM_TOKEN)
           .read_timeout(30)
           .write_timeout(30)
           .connect_timeout(30)
           .pool_timeout(30)
           .build())
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(PreCheckoutQueryHandler(pre_checkout_handler))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logger.info("Bot listo.")
    app.run_polling(allowed_updates=Update.ALL_TYPES,
                    drop_pending_updates=True)

if __name__ == "__main__":
    main()
