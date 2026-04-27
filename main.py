"""
ProspecTA: Senales de Trading con analisis tecnico real.
Wyckoff + Ichimoku + Estocastico sobre acciones Colombia, EE.UU. y Cripto.
Interfaz de Telegram con botones inline. Freemium: 3 gratis/mes, $9.99/mes.

Autor: Ana Lorena Jimenez Preciado
XIX Congreso Internacional de Prospectiva (Cali, Colombia)
"""
import os, re, json, html, logging, tempfile, asyncio
from datetime import datetime, date
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()

import yfinance as yf
import pandas as pd
from google import genai
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("prospecta")

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
API_KEY = os.environ["API_KEY"]
FLASH_MODEL = os.environ.get("FLASH_MODEL", "gemini-2.5-flash")
TEMPLATE_PATH = Path(__file__).parent / "dashboard_template.html"
gemini = genai.Client(api_key=API_KEY)

FREE_MONTHLY_LIMIT = 3
SUB_DAILY_LIMIT = 3
SUB_PRICE = 9.99

# ---------------------------------------------------------------------------
# Control de uso (en memoria - en produccion usar BD)
# ---------------------------------------------------------------------------
user_usage: dict[int, dict] = {}


def check_usage(user_id: int) -> tuple[bool, str]:
    """Verifica si el usuario puede hacer una consulta."""
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

    # Demo mode: siempre permitir
    return True, ""

    # # Logica real (activar cuando pagos esten listos):
    # if u["sub"]:
    #     if u["daily"] >= SUB_DAILY_LIMIT:
    #         return False, f"Limite diario alcanzado ({SUB_DAILY_LIMIT}/dia). Vuelve manana."
    #     u["daily"] += 1
    #     return True, ""
    # if u["free"] < FREE_MONTHLY_LIMIT:
    #     u["free"] += 1
    #     remaining = FREE_MONTHLY_LIMIT - u["free"]
    #     return True, f"Consulta gratis {u['free']}/{FREE_MONTHLY_LIMIT} este mes."
    # return False, ("Has usado tus 3 analisis gratis este mes.\n"
    #                f"Suscribete por ${SUB_PRICE}/mes para 3 consultas diarias.")


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
    "col": {"name": "Colombia", "flag": "🇨🇴",
            "assets": {"EC": "Ecopetrol", "CIB": "Bancolombia", "AVAL": "Grupo Aval"}},
    "us": {"name": "EE.UU.", "flag": "🇺🇸",
           "assets": {"AAPL": "Apple", "MSFT": "Microsoft", "NVDA": "Nvidia"}},
    "crypto": {"name": "Cripto", "flag": "₿",
               "assets": {"BTC-USD": "Bitcoin", "ETH-USD": "Ethereum", "SOL-USD": "Solana"}},
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
        [InlineKeyboardButton("🇨🇴 Colombia", callback_data="mkt_col"),
         InlineKeyboardButton("🇺🇸 EE.UU.", callback_data="mkt_us")],
        [InlineKeyboardButton("₿ Cripto", callback_data="mkt_crypto")],
        [InlineKeyboardButton("📚 Como leer las senales", callback_data="learn")],
    ])


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    usage = get_usage_text(uid)
    await update.message.reply_text(
        "📊 *ProspecTA*\n"
        "_Senales de trading con IA y analisis tecnico real_\n\n"
        "• Wyckoff · Ichimoku · Estocastico\n"
        "• Precio objetivo y Stop Loss\n"
        "• Nivel de confiabilidad por senal\n\n"
        f"🎁 *3 analisis gratis al mes*\n"
        f"💎 Plan PRO: ${SUB_PRICE}/mes — 3 consultas diarias\n\n"
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


async def button_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "learn":
        await ctx.bot.send_message(chat_id=query.message.chat_id,
            text=LEARN_TEXT, parse_mode="Markdown")
        await ctx.bot.send_message(chat_id=query.message.chat_id,
            text="Analizar un mercado:", reply_markup=main_menu_keyboard())
        return

    if not data.startswith("mkt_"):
        return

    market_key = data.replace("mkt_", "")
    if market_key not in MARKETS:
        return

    uid = query.from_user.id
    allowed, msg = check_usage(uid)
    if not allowed:
        await query.edit_message_text(msg, reply_markup=main_menu_keyboard())
        return

    mkt = MARKETS[market_key]
    await query.edit_message_text(
        f"⏳ Analizando {mkt['flag']} {mkt['name']}...\n"
        "Calculando Ichimoku, Estocastico y Wyckoff...")

    results = await asyncio.to_thread(analyze_market, market_key)
    if not results:
        await ctx.bot.send_message(chat_id=query.message.chat_id,
            text="No se pudieron obtener datos. Intente mas tarde.",
            reply_markup=main_menu_keyboard())
        return

    # Resumen en Telegram (ordenado por fuerza)
    lines = [f"📊 *{mkt['flag']} {mkt['name']}* — Senales\n"]
    for r in results:
        emoji = {"COMPRA": "🟢", "VENTA": "🔴"}.get(r["signal"], "🟡")
        lines.append(
            f"{emoji} *{r['name']}* ({r['symbol']}) — {r['confidence']}%\n"
            f"   💰 ${r['price']:,.2f} → 🎯 ${r['target']:,.2f} ({r['target_pct']:+.1f}%)\n"
            f"   🛑 Stop: ${r['stop_loss']:,.2f} | Wyckoff: {r['wyckoff']}\n")
    usage = get_usage_text(uid)
    lines.append(f"_{usage}_\n_Generando dashboard..._")

    await ctx.bot.send_message(chat_id=query.message.chat_id,
        text="\n".join(lines), parse_mode="Markdown")

    try:
        commentary = await get_ai_commentary(results, mkt["name"])
        html_path = generate_dashboard(results, mkt, commentary)
        await ctx.bot.send_document(chat_id=query.message.chat_id,
            document=html_path.open("rb"),
            filename=f"ProspecTA_{market_key}_{datetime.now():%Y%m%d_%H%M}.html",
            caption="Abra en su navegador para ver graficas interactivas.")
        html_path.unlink(missing_ok=True)
    except Exception as e:
        logger.error("Error dashboard: %s", e, exc_info=True)

    await ctx.bot.send_message(chat_id=query.message.chat_id,
        text="Analizar otro mercado:", reply_markup=main_menu_keyboard())


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Selecciona un mercado:",
        reply_markup=main_menu_keyboard())


def main() -> None:
    logger.info("Iniciando ProspecTA")
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logger.info("Bot listo.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
