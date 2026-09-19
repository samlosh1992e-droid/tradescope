import os
import sqlite3
import time
import uuid
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

import yfinance as yf
from flask import (Flask, render_template, request, redirect, url_for,
                   flash, session, send_file, abort, jsonify)


def _load_env_file():
    """Charge .env s'il existe (sans ecraser les variables deja definies)."""
    path = os.path.join(os.path.dirname(__file__), ".env")
    if not os.path.exists(path):
        return
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except Exception as e:
        print(f"[env] chargement .env impossible: {e}")


_load_env_file()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "tradescope-dev-secret")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "tradescope2026")
PAY_LINK = os.environ.get("PAY_LINK", "")
FREE_CREDITS = int(os.environ.get("FREE_CREDITS", "3"))
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".webp"}
PRICE = os.environ.get("PRICE", "4 900 XPF/mois")
PRICE_XPF = int(os.environ.get("PRICE_XPF", "4900"))

# ==== Paiement par BITCOIN (adresse de reception du portefeuille du proprio) ====
BTC_ADDRESS = os.environ.get("BTC_ADDRESS", "").strip()
BTC_MIN_CONF = int(os.environ.get("BTC_MIN_CONF", "1"))

# ==== Stripe (actif quand les cles sont renseignees) ====
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "").strip()
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
STRIPE_PRICE_ID = os.environ.get("STRIPE_PRICE_ID", "").strip()

def _norm(email):
    """Minuscules, strip ; gmail/googlemail : points et +tag ignores."""
    email = (email or "").strip().lower()
    if "@" not in email:
        return email
    local, _, domain = email.partition("@")
    domain = domain.lower()
    if domain == "gmail.com":
        local = local.split("+")[0].replace(".", "")
    elif domain in ("googlemail.com",):
        local = local.split("+")[0].replace(".", "")
        domain = "gmail.com"
    return local + "@" + domain


# Emails avec pass illimite (admin). separes par virgules.
ADMIN_EMAILS = {_norm(e) for e in
                os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}
# SMTP optionnel : si vide, les mails sont logges dans mails.log
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
MAIL_FROM = os.environ.get("MAIL_FROM", "tradescope@votredomaine.com")
MAIL_TO_ADMIN = os.environ.get("MAIL_TO_ADMIN", "")  # ou sont notifies paiements/annulations

DB_PATH = os.path.join(os.path.dirname(__file__), "data.db")
MAIL_LOG = os.path.join(os.path.dirname(__file__), "mails.log")

_IMAGES = {}
_QUOTES_CACHE = {"t": 0.0, "data": []}
TICKER_SYMBOLS = ["GC=F"]


def _store_images(chart_bytes, overlay_bytes):
    uid = uuid.uuid4().hex
    _IMAGES[uid] = {"chart": chart_bytes, "overlay": overlay_bytes}
    while len(_IMAGES) > 24:
        _IMAGES.pop(next(iter(_IMAGES)))
    return uid

SYMBOLS = [
    ("XAU-USD", "Or spot CFD — XAU/USD (MT4/MT5)"),
]

TIMEFRAMES = {
    "1m":  {"interval": "1m",  "period": "5d", "label": "M1"},
    "5m":  {"interval": "5m",  "period": "5d", "label": "M5"},
    "15m": {"interval": "15m", "period": "1mo", "label": "M15"},
    "30m": {"interval": "30m", "period": "1mo", "label": "M30"},
    "1h":  {"interval": "1h",  "period": "3mo", "label": "H1"},
    "2h":  {"interval": "1h",  "period": "1y", "label": "H2"},
    "4h":  {"interval": "1h",  "period": "1y", "label": "H4"},
}

DEFAULT_TF = "1h"

from engine import analyse, render_chart_png, overlay_screenshot
from vision import analyse_screenshot as _analyse_screenshot


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def normalize_email(email):
    """Rend deux adresses equivalentes identiques (gmail: points et +tag ignorent)."""
    return _norm(email)


def init_db():
    conn = get_db()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS users ("
        " email TEXT PRIMARY KEY,"
        " credits INTEGER NOT NULL,"
        " created_at TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS analyses ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " email TEXT, symbol TEXT, timeframe TEXT, bias TEXT,"
        " created_at TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS visits ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " ts TEXT NOT NULL, ip TEXT, page TEXT, event TEXT, email TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS subscriptions ("
        " email TEXT PRIMARY KEY,"
        " status TEXT NOT NULL DEFAULT 'none',"
        " plan TEXT NOT NULL DEFAULT 'pro',"
        " started_at TEXT, cancelled_at TEXT, updated_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS payments ("
        " id TEXT PRIMARY KEY,"
        " email TEXT NOT NULL,"
        " method TEXT NOT NULL,"
        " btc_address TEXT, btc_sats INTEGER, fiat_xpf INTEGER,"
        " status TEXT NOT NULL DEFAULT 'pending',"
        " txid TEXT, created_at TEXT, updated_at TEXT)"
    )
    conn.commit()
    conn.close()


def _track(event, page):
    """Enregistre un evenement de parcours (visite/analyse/email/upgrade)."""
    try:
        ip = (request.headers.get("X-Forwarded-For") or request.remote_addr or "").split(",")[0].strip()
        conn = get_db()
        conn.execute(
            "INSERT INTO visits(ts, ip, page, event, email) VALUES (?,?,?,?,?)",
            (datetime.now().isoformat(), ip, page, event, get_email() or ""),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def get_email():
    return normalize_email(session.get("email")).strip()


def require_email():
    email = get_email()
    if not email:
        return None
    if email in ADMIN_EMAILS:
        return -1  # illimite
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    conn.close()
    if row is None:
        conn = get_db()
        conn.execute(
            "INSERT INTO users (email, credits, created_at) VALUES (?,?,?)",
            (email, FREE_CREDITS, datetime.now().isoformat()),
        )
        conn.commit()
        conn.close()
        return FREE_CREDITS
    return row["credits"]


def get_subscription(email):
    conn = get_db()
    row = conn.execute("SELECT * FROM subscriptions WHERE email = ?", (email,)).fetchone()
    conn.close()
    return row


def set_subscription(email, status, plan="pro"):
    now = datetime.now().isoformat()
    conn = get_db()
    conn.execute(
        "INSERT INTO subscriptions(email, status, plan, started_at, updated_at) "
        "VALUES (?,?,?,?,?) ON CONFLICT(email) DO UPDATE SET "
        "status=?, updated_at=?",
        (email, status, plan, now, now, status, now),
    )
    conn.commit()
    conn.close()


def is_unlimited(email, credits):
    """... admin, credits=-1, OU abonnement actif (payeur)."""
    if email in ADMIN_EMAILS:
        return True
    if credits is not None and credits == -1:
        return True
    sub = get_subscription(email)
    return bool(sub and sub["status"] == "active")


def _download(symbol, interval, period, tries=3):
    import time as _time
    last_err = None
    for attempt in range(tries):
        try:
            df = yf.download(symbol, period=period, interval=interval,
                             progress=False, auto_adjust=False,
                             group_by="ticker", threads=False)
            if df is not None and not df.empty and len(df) > 10:
                if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
                    df.columns = df.columns.get_level_values(-1)
                if "Close" in df.columns:
                    return df[["Open", "High", "Low", "Close", "Volume"]].dropna()
        except Exception as e:
            last_err = e
        _time.sleep(1.5 if attempt < tries - 1 else 0)
    if last_err is not None:
        print(f"[fetch] {symbol} {interval}: {type(last_err).__name__}: {last_err}")
    return None


def fetch_candles(symbol, tf_key):
    cfg = TIMEFRAMES[tf_key]
    fetch_sym = data_symbol(symbol)
    if tf_key in ("2h", "4h"):
        raw = _download(fetch_sym, "1h", cfg["period"])
        if raw is None:
            return None
        raw.index = raw.index.tz_localize(None) if raw.index.tz is not None else raw.index
        df = raw.resample(tf_key).agg({
            "Open": "first", "High": "max", "Low": "min",
            "Close": "last", "Volume": "sum",
        }).dropna(subset=["Open", "Close"])
    else:
        df = _download(fetch_sym, cfg["interval"], cfg["period"])
    if df is None or len(df) < 10:
        return None
    candles = []
    for idx, row in df.iterrows():
        candles.append({
            "close": float(row["Close"]),
            "high": float(row["High"]),
            "low": float(row["Low"]),
            "open": float(row["Open"]),
            "time": str(idx)[:16],
        })
    # CFD or/argent (MT4/MT5) : les bougies viennent du future equivalent,
    # mais l'utilisateur voit du SPOT. On RECALE les bougies sur le prix
    # spot reel (les decalages futures/spot sont quasi constants en intraday).
    if symbol in SPOT_CFD:
        spot = get_spot_price(symbol)
        if spot and spot > 0 and candles:
            shift = spot - candles[-1]["close"]
            if abs(shift) < spot * 0.03:  # garde : ecart raisonnable
                for c in candles:
                    c["close"] += shift
                    c["high"] += shift
                    c["low"] += shift
                    c["open"] += shift
    return candles


@app.template_filter("fmt")
def fmt_filter(value):
    if value is None:
        return "—"
    if abs(value) >= 10000:
        return f"{value:,.1f}".replace(",", " ")
    if abs(value) >= 100:
        return f"{value:,.2f}".replace(",", " ")
    return f"{value:,.4f}".replace(",", " ")


@app.route("/")
def index():
    _track("visit", "/")
    email = get_email()
    credits = require_email() if email else None
    return render_template(
        "index.html",
        symbols=SYMBOLS,
        timeframes=TIMEFRAMES,
        credits=credits,
        email=email,
        pay_link=PAY_LINK,
        price=PRICE,
        unlimited=is_unlimited(email, credits),
    )


@app.route("/set-email", methods=["POST"])
def set_email():
    email = normalize_email(request.form.get("email"))
    _track("email_submit", "/set-email")
    if "@" not in email or len(email) < 5:
        flash("Adresse email invalide.", "error")
        return redirect(url_for("index"))
    session["email"] = email
    credits = require_email()
    if credits is None:
        flash("Erreur lors de l'enregistrement, reessayez.", "error")
        return redirect(url_for("index"))
    if is_unlimited(email, credits):
        flash("Pass illimite active. Bon voyage.", "success")
        return redirect(url_for("index"))
    if credits <= 0:
        flash("Cette adresse a deja utilise ses 3 analyses gratuites. "
              "Passez a l'abonnement pour continuer.", "error")
        return redirect(url_for("upgrade"))
    flash(f"Email enregistre. Il vous reste {credits} analyse"
          f"{'s' if credits > 1 else ''} gratuite{'s' if credits > 1 else ''}.", "success")
    return redirect(url_for("index"))


_DEB_COST = 1

CONF_RANK = {"elevee": 3, "moyenne": 2, "minimum": 1, "basse": 0}


def _analysis_rank(plan):
    return (
        CONF_RANK.get(plan.get("confidence", ""), 0),
        plan.get("score", 0) or 0,
    )


_LIVE_CACHE = {"t": 0.0, "prices": {}, "old": {}}

# Symboles spot CFD (MT4/MT5) : bougies issues du future equivalent,
# prix live issu d'une API spot dediee.
SPOT_CFD = {
    "XAU-USD": {"futures": "GC=F", "spot_api": "XAU"},
    "XAG-USD": {"futures": "SI=F", "spot_api": "XAG"},
}


def data_symbol(symbol):
    """Le symbole effectivement telecharge pour les bougies."""
    return SPOT_CFD.get(symbol, {}).get("futures", symbol)


def get_spot_price(symbol):
    """Prix spot (metaux precieux) via api.gold-api.com, cache 20 s."""
    spot_key = SPOT_CFD.get(symbol, {}).get("spot_api")
    if not spot_key:
        return None
    now = time.time()
    if now - _LIVE_CACHE["t"] < 20 and symbol in _LIVE_CACHE["prices"]:
        return _LIVE_CACHE["prices"][symbol]
    try:
        # demande minima via urllib pour eviter une dependance
        import json as _json
        import urllib.request as _ur
        req = _ur.Request(
            f"https://api.gold-api.com/price/{spot_key}",
            headers={"User-Agent": "TradeScope/1.0"},
        )
        with _ur.urlopen(req, timeout=15) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
        price = float(data.get("price", 0))
        if price > 0:
            _LIVE_CACHE["prices"][symbol] = price
            _LIVE_CACHE["t"] = now
            return price
    except Exception as e:
        print(f"[spot] {symbol}: {type(e).__name__}: {e}")
    # Secours : dernier prix spot connu (evite de retomber sur les futures)
    if symbol in _LIVE_CACHE["prices"]:
        return _LIVE_CACHE["prices"][symbol]
    return None


def get_live_price(symbol):
    """Prix de marche actuel : spot pour les CFD or/argent, sinon yfinance."""
    spot = get_spot_price(symbol)
    if spot:
        return spot
    data_sym = data_symbol(symbol)
    now = time.time()
    if now - _LIVE_CACHE["t"] < 20 and data_sym in _LIVE_CACHE["prices"]:
        return _LIVE_CACHE["prices"][data_sym]
    try:
        fi = yf.Ticker(data_sym).fast_info
        price = fi.get("lastPrice") or fi.get("regular_market_price") or fi.get("previous_close")
    except Exception as e:
        print(f"[live] {data_sym}: {type(e).__name__}: {e}")
        price = None
    if price and price > 0:
        _LIVE_CACHE["prices"][data_sym] = float(price)
        _LIVE_CACHE["t"] = now
        return float(price)
    return None


def reanchor_to_live(plan, live):
    """Recale l'ordre en entree/attente sur le prix live en gardant la
    distance de risque (ATR) et les ratios R1/R2/R3 du plan."""
    d = plan.get("direct") or {}
    old_entry = d.get("entry")
    if not old_entry:
        return plan
    sl_dist = abs(d.get("sl", old_entry) - old_entry)
    r1 = d.get("r1", 1.0)
    r2 = d.get("r2", 2.0)
    r3 = d.get("r3", 3.0)
    bias = plan.get("bias")
    sign = -1.0 if bias == "short" else 1.0
    d["entry"] = live
    d["sl"] = live - sign * sl_dist
    d["tp1"] = live + sign * sl_dist * r1
    d["tp2"] = live + sign * sl_dist * r2
    d["tp3"] = live + sign * sl_dist * r3
    d["sl_pct"] = round(sl_dist / (abs(live) + 1e-9) * 100, 2)
    plan["entry"] = live
    plan["last"] = live
    plan["current_price"] = live
    plan["sl"] = d["sl"]
    plan["tp1"] = d["tp1"]
    plan["tp2"] = d["tp2"]
    plan["tp3"] = d["tp3"]
    plan["r"] = d["sl_pct"]
    if plan.get("alt"):
        alt = plan["alt"]
        old_a = alt.get("entry")
        if old_a:
            alt_dist = abs(alt.get("sl", old_a) - old_a)
            alt["entry"] = live - sign * sl_dist * 0.45
            alt["sl"] = alt["entry"] - sign * alt_dist
            alt["tp1"] = alt["entry"] + sign * sl_dist * r1
            alt["tp2"] = alt["entry"] + sign * sl_dist * r2
            alt["tp3"] = alt["entry"] + sign * sl_dist * r3
    return plan


def _analyse_tf(symbol, tf_key):
    try:
        candles = fetch_candles(symbol, tf_key)
        if candles is None:
            return tf_key, None, None
        plan = analyse(candles)
        plan["source_tf"] = tf_key
        return tf_key, plan, candles
    except Exception as e:
        print(f"[multi] {symbol} {tf_key}: {type(e).__name__}: {e}")
        return tf_key, None, None


def _analyse_image_mode(email, credits, unlimited, symbol, tf):
    """Mode « Analyse de mon image » : on lit vraiment le screenshot et on ne
    propose une position QUE si l'image le permet (biais net + bougies + force)."""
    uploaded = request.files.get("screenshot")
    if not uploaded or not uploaded.filename:
        flash("Mode « Analyse de mon image » : envoyez d'abord le screenshot de votre graphique.", "error")
        return redirect(url_for("index") + "#start")
    ext = os.path.splitext(uploaded.filename)[1].lower()
    raw = uploaded.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES or ext not in ALLOWED_EXT:
        flash("Image trop lourde (> 8 Mo) ou format non supporté.", "error")
        return redirect(url_for("index") + "#start")
    try:
        vision = _analyse_screenshot(raw, method="auto")
    except Exception as e:
        print(f"[vision] erreur: {e}")
        vision = None
    if not vision or not vision.get("ok"):
        flash("Impossible de lire cette image. Envoyez un screenshot net de votre graphique (bougies visibles).", "error")
        return redirect(url_for("index") + "#start")

    tf_key = tf if tf in TIMEFRAMES else "5m"
    live_price = get_live_price(symbol)

    # Position = UNIQUEMENT si l'image est exploitable :
    # biais net + assez de bougies + force suffisante + graduation lisible.
    # Les niveaux (entree/SL/TP) sont calcules 100 % depuis le screenshot :
    # l'OCR lit l'axe des prix de l'image, l'ATR visuelle est mesuree sur les
    # bougies du screenshot. AUCUN recalcul par le marche reel.
    plan_img = None
    if (vision.get("bias") in ("long", "short")
            and (vision.get("bars") or 0) >= 10
            and (vision.get("confidence") or 0) >= 45):
        levels = vision.get("levels")
        # Garde-fou « vrai analyste » : le prix lu sur l'image (cloture de la
        # derniere bougie) ne doit pas etre trop loin du cours reel du symbole.
        # Si l'ecart depasse 1.5%, le screenshot est trop ancien ou l'echelle
        # mal lue : on REFUSE la position au lieu de sortir un prix decale.
        if levels and symbol in SPOT_CFD:
            spot = get_spot_price(symbol)
            if spot and spot > 0:
                diff_pct = abs(levels["entry"] - spot) / spot * 100
                if diff_pct > 1.5:
                    vision["guard"] = {
                        "entry": levels["entry"],
                        "live": spot,
                        "diff_pct": round(diff_pct, 2),
                    }
                    levels = None
        if levels:
            bias = vision["bias"]
            d = {
                "entry": levels["entry"],
                "sl": levels["sl"],
                "tp1": levels["tp1"],
                "tp2": levels["tp2"],
                "tp3": levels["tp3"],
                "r1": round(abs(levels["tp1"] - levels["entry"]) / (abs(levels["sl"] - levels["entry"]) + 1e-9), 2),
                "r2": round(abs(levels["tp2"] - levels["entry"]) / (abs(levels["sl"] - levels["entry"]) + 1e-9), 2),
                "r3": round(abs(levels["tp3"] - levels["entry"]) / (abs(levels["sl"] - levels["entry"]) + 1e-9), 2),
                "sl_pct": levels.get("sl_pct", 0),
            }
            plan_img = {
                "bias": bias,
                "bias_label": "ACHAT (LONG)" if bias == "long" else "VENTE (SHORT)",
                "trend": (vision.get("recent") or "").replace("hausse", "hausse").replace("baisse", "baisse") or "neutre",
                "confidence": vision.get("confidence") or 50,
                "confidence_pct": int(vision.get("confidence") or 50),
                "score": 0,
                "entry": levels["entry"],
                "last": levels["entry"],
                "current_price": levels["entry"],
                "atr": levels.get("atr", 0) or 0,
                "rr": levels.get("rr", 3.0),
                "mode": "direct",
                "direct": d,
                "alt": None,
                "reasons": list(vision.get("notes") or []),
                "rsi": None,
                "stoch": None,
                "pivots": {},
                "symbol": symbol,
                "source_tf": tf_key,
                "sl": d["sl"], "tp1": d["tp1"],
                "tp2": d["tp2"], "tp3": d["tp3"],
                "r": d["sl_pct"],
                "from_image": True,
                "snapped": bool(levels.get("snapped")),
                "axis_min": levels.get("axis_min"),
                "axis_max": levels.get("axis_max"),
                "axis_step": levels.get("axis_step"),
                "image_axis": levels.get("axis", ""),
                "live_ref": live_price if (live_price and live_price > 0) else None,
            }

    # Facturation : même règle que pour l'analyse marché.
    remaining = 0
    if not unlimited:
        conn = get_db()
        conn.execute(
            "INSERT OR IGNORE INTO users (email, credits, created_at)"
            " VALUES (?,?,?)", (email, FREE_CREDITS, datetime.now().isoformat()))
        conn.execute("UPDATE users SET credits = credits - ? WHERE email = ?",
                     (_DEB_COST, email))
        conn.execute(
            "INSERT INTO analyses (email, symbol, timeframe, bias, created_at) "
            "VALUES (?,?,?,?,?)",
            (email, symbol, tf_key, vision.get("bias") or "neutre",
             datetime.now().isoformat()))
        conn.commit()
        remaining = conn.execute(
            "SELECT credits FROM users WHERE email = ?", (email,)).fetchone()["credits"]
        conn.close()
    else:
        remaining = -1

    _track("analyse_image", "/analyser")

    return render_template(
        "result_image.html",
        symbol=symbol,
        tf_key=tf_key,
        email=email,
        remaining=remaining,
        unlimited=unlimited,
        live_price=live_price,
        vision=vision,
        plan=plan_img,
        pay_link=PAY_LINK,
        price=PRICE,
        TIMEFRAMES=TIMEFRAMES,
    )


@app.route("/analyser", methods=["POST"])
def analyser():
    email = get_email()
    if not email:
        flash("Entrez d'abord votre email pour activer vos analyses gratuites.", "error")
        return redirect(url_for("index") + "#start")

    credits = require_email()
    if credits is None:
        flash("Email introuvable, reessayez.", "error")
        return redirect(url_for("index"))
    if credits == 0:
        return redirect(url_for("upgrade"))
    unlimited = is_unlimited(email, credits)

    symbol = request.form.get("symbol") or "BTC-USD"
    tf = request.form.get("timeframe") or "Tous"
    mode = request.form.get("mode") or "market"

    # ============ MODE "image" : on analyse le screenshot, pas le marche ============
    if mode == "image":
        return _analyse_image_mode(email, credits, unlimited, symbol, tf)

    if tf in TIMEFRAMES:
        tf_keys = [tf]
        scope = tf
    else:
        tf_keys = list(TIMEFRAMES.keys())
        scope = "Tous"

    # Analyse des TF en parallele (un seul si l'utilisateur a choisi).
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(_analyse_tf, symbol, k): k for k in tf_keys}
        plans = {}
        candle_map = {}
        for fut in futures:
            k, p, c = fut.result()
            if p is not None:
                plans[k] = p
                candle_map[k] = c

    if not plans:
        flash("Impossible de recuperer les donnees du marche. Reessayez.", "error")
        return redirect(url_for("index") + "#start")

    # Position du moment : meilleur signal DIRECTEUR (confidence la plus haute).
    # Jamais de repli : si aucun plan "direct" fiable, on n'affiche PAS de
    # position au marche (evite les fausses entrees qui se font stopper).
    direct_candidates = [p for p in plans.values() if p.get("mode") == "direct"]
    best_direct = max(direct_candidates, key=_analysis_rank) if direct_candidates else None

    # Ordre limite : meilleur plan avec entree pullback (attente_suggeree).
    limit_candidates = [p for p in plans.values() if p.get("alt")]
    if limit_candidates:
        best_limit = max(limit_candidates, key=_analysis_rank)
    else:
        best_limit = None

    # Prix live du marche : la Position du moment est ancree dessus,
    # c'est le VRAI prix auquel l'utilisateur peut entrer.
    live_price = get_live_price(symbol)
    if best_direct and live_price:
        reanchor_to_live(best_direct, live_price)
    if best_limit and live_price:
        reanchor_to_live(best_limit, live_price)

    # Prix/dernier + narrative sur le meilleur direct (ou le meilleur dispo).
    plan_out = best_direct or best_limit or next(iter(plans.values()))
    if best_direct:
        last_candles = candle_map.get(best_direct["source_tf"])
    else:
        last_candles = None

    # ---- Analyse reelle de l'image envoyee (screenshot de la plateforme) ----
    vision = None
    uploaded = request.files.get("screenshot")
    if uploaded and uploaded.filename:
        ext = os.path.splitext(uploaded.filename)[1].lower()
        raw = uploaded.read(MAX_UPLOAD_BYTES + 1)
        if len(raw) <= MAX_UPLOAD_BYTES and ext in ALLOWED_EXT:
            try:
                vision = _analyse_screenshot(raw, method="auto")
            except Exception as e:
                print(f"[vision] erreur analyse image: {e}")
                vision = None

    # Verdict croise : l'image dit quoi vs les donnees reelles disent quoi ?
    vision_verdict = None
    if vision and vision.get("ok"):
        v_bias = vision.get("bias")
        if v_bias in ("long", "short"):
            if plan_out.get("bias") == v_bias:
                vision_verdict = {
                    "level": "converge",
                    "label": "L'image CONFIRME l'analyse des données réelles",
                    "cls": "pos",
                    "detail": (
                        f"Le screenshot montre une tendance {v_bias} et les données "
                        "réelles du marché sont alignées."
                    ),
                }
            else:
                vision_verdict = {
                    "level": "conflit",
                    "label": "Attention : l'image CONTREDIT les données réelles",
                    "cls": "neg",
                    "detail": (
                        f"Le screenshot semble montrer une tendance {v_bias} alors "
                        "que les données actuelles indiquent plutôt "
                        f"{'hausse' if plan_out.get('bias') == 'long' else 'baisse'}. "
                        "Privilégiez la prudence ou re-vérifiez le marché choisi."
                    ),
                }
        elif v_bias == "neutre":
            vision_verdict = {
                "level": "neutre",
                "label": "L'image ne montre pas de direction claire",
                "cls": "",
                "detail": "Aucun biais fiable sur ce screenshot, on s'appuie sur les données réelles.",
            }

    # Prix/dernier + narrative sur le meilleur direct (ou le meilleur dispo).
    narrative = build_narrative(plan_out, symbol)

    if not unlimited:
        conn = get_db()
        conn.execute(
            "INSERT OR IGNORE INTO users (email, credits, created_at)"
            " VALUES (?,?,?)",
            (email, FREE_CREDITS, datetime.now().isoformat()),
        )
        conn.execute("UPDATE users SET credits = credits - ? WHERE email = ?",
                     (_DEB_COST, email))
        conn.execute(
            "INSERT INTO analyses (email, symbol, timeframe, bias, created_at) "
            "VALUES (?,?,?,?,?)",
            (email, symbol, plan_out["source_tf"], plan_out["bias"],
             datetime.now().isoformat()),
        )
        conn.commit()
        remaining = conn.execute(
            "SELECT credits FROM users WHERE email = ?", (email,)).fetchone()["credits"]
        conn.close()
    else:
        remaining = -1

    _track("analyse", "/analyser")

    rows = []
    for k in tf_keys:
        p = plans.get(k)
        d = (p.get("direct") or p.get("alt") or {}) if p else {}
        rows.append({
            "tf": TIMEFRAMES[k]["label"],
            "tf_key": k,
            "plan": p,
            "bias": p["bias_label"] if p else None,
            "conf": p["confidence"] if p else None,
            "mode": p.get("mode") if p else None,
            "entry": d.get("entry") if p else None,
            "sl": d.get("sl") if p else None,
            "tp1": d.get("tp1") if p else None,
            "tp2": d.get("tp2") if p else None,
            "tp3": d.get("tp3") if p else None,
            "rr": p.get("rr") if p else None,
            "is_direct": bool(p and p.get("mode") == "direct"),
            "is_best_direct": p is best_direct if p else False,
            "is_best_limit": (p is best_limit) if (p and best_limit) else False,
        })

    return render_template(
        "result.html",
        plan=plan_out,
        best_direct=best_direct,
        has_direct=bool(best_direct),
        best_limit=best_limit,
        rows=rows,
        symbol=symbol,
        scope=scope,
        email=email,
        remaining=remaining,
        unlimited=unlimited,
        pay_link=PAY_LINK,
        price=PRICE,
        narrative=narrative,
        TIMEFRAMES=TIMEFRAMES,
        live_price=live_price,
        vision=vision,
        vision_verdict=vision_verdict,
    )


@app.route("/chart.png")
def chart_png():
    uid = session.get("last_uid")
    item = _IMAGES.get(uid) if uid else None
    if not item:
        abort(404)
    return send_file(_bytes_io(item["chart"]), mimetype="image/png")


@app.route("/overlay.png")
def overlay_png():
    uid = session.get("last_uid")
    item = _IMAGES.get(uid) if uid else None
    if not item or item["overlay"] is None:
        abort(404)
    return send_file(_bytes_io(item["overlay"]), mimetype="image/png")


def _bytes_io(data):
    import io
    return io.BytesIO(data)


def build_narrative(plan, symbol):
    """Texte d'analyste qui explique POURQUOI prendre ces positions."""
    d = plan.get("direct") or plan.get("alt") or {}
    p = plan.get("pivots") or {}
    sections = []
    mode = plan.get("mode")

    # 0. Pas de trade : expliquer pourquoi on n'entre pas.
    if mode in ("no_trade",) and not d:
        return [
            ("Pourquoi pas de trade maintenant",
             "Le marché est <b>trop neutre</b> pour l'instant : les indicateurs ne dégagent "
             "aucun biais clair sur cette unité de temps. Entrer maintenant reviendrait à "
             "parler pile ou face avec un stop subi immédiatement. "
             "TradeScope attend un vrai signal, comme un trader discipliné, plutôt que de "
             "forcer une position. <b>Action recommandée : ne rien faire</b>, ou choisir une "
             "autre unité de temps / un autre marché et relancer l'analyse."),
        ]

    # 1. Direction
    trend_txt = {
        "hausse": "Le prix évolue au-dessus des EMA21 et EMA50, une tendance haussière durable.",
        "baisse": "Le prix évolue sous les EMA21 et EMA50, une tendance baissière engagée.",
        "neutre": "Le prix évolue dans une zone sans tendance claire, on préfère un déclenchement.",
    }.get(plan.get("trend"), "")
    rsi_txt = "élevé (pression acheteuse)" if (plan.get("rsi") or 50) >= 55 \
        else "faible (pression vendeuse)" if (plan.get("rsi") or 50) <= 45 \
        else "neutre (pas d'extrême)"
    direction = (
        f"<b>{plan['bias_label']} — {plan['confidence'].upper()}</b> : {trend_txt} "
        f"Le RSI(14) est {rsi_txt}, ce qui confirme le biais "
        f"{'acheteur' if plan['bias'] == 'long' else 'vendeur'}."
    )
    sections.append(("Direction", direction))

    # 2. Pourquoi cette entrée
    entry_w = d.get("entry") or plan["last"]
    if mode == "attente_suggeree":
        entry_txt = (
            f"L'entrée cible à <b>{entry_w:.4f}</b> est passée en <b>ordre en attente</b> "
            f"(pullback vers un niveau d'intérêt) : on laisse d'abord le marché venir à nous "
            f"pour obtenir une meilleure zone de prix et un stop plus serré."
        )
    else:
        entry_txt = (
            f"L'entrée à <b>{entry_w:.4f}</b> correspond au prix du marché actuel de {symbol}, "
            f"récupéré en direct à l'instant de l'analyse. On entre au marché pour ne pas rater "
            f"le mouvement, avec un stop immédiatement derrière un niveau significatif."
        )
    sections.append(("Pourquoi cette entrée", entry_txt))

    # 3. Stop loss
    if "sl" in d:
        atr = plan.get("atr", 0)
        sl = d["sl"]
        sl_dist = abs(sl - d["entry"])
        sl_txt = (
            f"Le stop est placé à <b>{sl:.4f}</b>, soit <b>{plan['r'] if 'r' in plan else sl_dist/(entry_w or 1)*100:.2f} %</b> "
            f"du prix. La distance s'appuie sur la volatilité récente : "
            f"on laisse au marché assez d'air pour respirer sans laisser la perte exploser. "
            f"Règle de gestion : si TP1 est touché, on remonte le stop au point d'entrée — le trade devient sans risque."
        )
        sections.append(("Le stop loss protecteur", sl_txt))

    # 4. Objectifs TP
    if "tp1" in d:
        tp_txt = (
            f"Trois cibles sont proposées, espacées d'une unité de risque chacune : "
            f"<b>TP1 à {d['tp1']:.4f}</b> (rapide, probabilité plus élevée), "
            f"<b>TP2 à {d['tp2']:.4f}</b> (cible intermédiaire) et "
            f"<b>TP3 à {d['tp3']:.4f}</b> (plein potentiel, ~{d.get('r3', 3):.1f}R). "
            f"Chaque niveau se prend en tranches (par exemple 1/2, 1/3, 1/6 du capital) pour "
            f"verrouiller des gains tout en laissant courir le papier gagnant."
        )
        sections.append(("Les 3 objectifs de gain", tp_txt))

    # 5. Structure (niveaux cles sans chiffres : on protege la methode)
    sp = p.get("s1")
    rp = p.get("r1")
    struct_parts = []
    if sp:
        struct_parts.append("un niveau de support majeur")
    if rp:
        struct_parts.append("un niveau de résistance majeur")
    if plan.get("pivots") and struct_parts:
        near = ""
        atr = plan.get("atr", 0)
        dist_s1 = abs(p.get("s1", 0) - entry_w) / atr if atr else 99
        dist_r1 = abs(p.get("r1", 0) - entry_w) / atr if atr else 99
        if dist_s1 < 1.0:
            near = "Le prix est proche d'un support : il peut servir de rebond (ou de zone de stop si cassé)."
        elif dist_r1 < 1.0:
            near = "Le prix bute sur une résistance : d'où l'intérêt d'attendre une réaction avant de s'exposer."
        struct_txt = f"Le contexte de structure s'appuie sur {', '.join(struct_parts)}. {near}"
        sections.append(("Le contexte de structure", struct_txt))

    # 6. Confiance / mode
    mode_txt = (
        f"Confiance : <b>{plan['confidence']}</b>."
    )
    if plan.get("mode") == "attente_suggeree" and plan.get("alt"):
        alt = plan["alt"]
        mode_txt += (
            f"Signal présent mais pas idéal : un <b>ordre en attente</b> est proposé "
            f"(entrée pullback <b>{alt.get('entry', 0):.4f}</b>) pour une meilleure zone de prix. "
            f"Le déclencheur est : <i>{alt.get('trigger', '')}</i>. "
            f"On n'exécute qu'au contact de la zone, jamais au marché."
        )
    elif plan.get("mode") == "direct":
        mode_txt += "Configuration directe : exécution au marché, sans attendre de rebond."
    else:
        mode_txt += "Aucune position à prendre maintenant : le risque n'est pas maîtrisé."
    sections.append(("Niveau de certitude", mode_txt))

    return sections


def send_mail(to, subject, text_html, categories=""):
    """Envoie un email si SMTP configure, sinon le journalise dans mails.log."""
    entry = (
        "=" * 60 + "\n"
        f"TIME   : {datetime.now().isoformat()}\n"
        f"TO     : {to}\n"
        f"SUBJECT: {subject}\n"
        f"CATEG  : {categories}\n"
        f"BODY   : {text_html}\n"
    )
    print(f"[mail][{categories or 'mail'}] -> {to}: {subject}")
    try:
        with open(MAIL_LOG, "a", encoding="utf-8") as f:
            f.write(entry)
    except Exception:
        pass
    if not SMTP_HOST:
        return False
    try:
        import smtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = MAIL_FROM
        msg["To"] = to
        msg.attach(MIMEText(text_html, "html", "utf-8"))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as srv:
            srv.starttls()
            srv.login(SMTP_USER, SMTP_PASS)
            srv.sendmail(MAIL_FROM, [to], msg.as_string())
        return True
    except Exception as e:
        print(f"[mail] SMTP erreur: {type(e).__name__}: {e}")
        return False


def notify_payment_ok(email, plan="pro"):
    set_subscription(email, "active", plan)
    # Un payeur devient ILLIMITE (plus de compte de credits).
    conn = get_db()
    conn.execute("UPDATE users SET credits=-1 WHERE email=?", (email,))
    conn.commit()
    conn.close()
    html = (
        f"<h2>Paiement accepte ✅</h2>"
        f"<p>Bonjour {email},</p>"
        f"<p>Votre abonnement TradeScope <b>{plan}</b> est <b>actif</b>.</p>"
        f"<ul><li>Analyses <b>illimitees</b> sur tous les timeframes</li>"
        f"<li>Position du moment + ordres limites</li></ul>"
        f"<p>Bonne analyse.</p>"
    )
    send_mail(email, "TradeScope : paiement accepte, abonnement actif", html, "payment_ok")
    if MAIL_TO_ADMIN:
        send_mail(MAIL_TO_ADMIN,
                  f"[Admin] Paiement accepte: {email}",
                  f"<p>{email} vient de payer l'abonnement {plan}.</p>",
                  "admin_payment")
    return True


def notify_cancel(email):
    conn = get_db()
    conn.execute(
        "UPDATE subscriptions SET status='cancelled', cancelled_at=?, updated_at=? "
        "WHERE email=?",
        (datetime.now().isoformat(), datetime.now().isoformat(), email),
    )
    # Resiliation IMMEDIATE : plus d'acces illimite des maintenant.
    conn.execute("UPDATE users SET credits=0 WHERE email=?", (email,))
    conn.commit()
    conn.close()
    html = (
        f"<h2>Abonnement resilie</h2>"
        f"<p>Bonjour {email},</p>"
        f"<p>Votre abonnement TradeScope a bien ete <b>resilie</b>. "
        f"Accessible jusqu'a la fin de la periode payee.</p>"
        f"<p>A bientot !</p>"
    )
    send_mail(email, "TradeScope : abonnement resilie", html, "cancel_user")
    if MAIL_TO_ADMIN:
        send_mail(MAIL_TO_ADMIN,
                  f"[Admin] Abonnement resilie: {email}",
                  f"<p>{email} a resilie son abonnement.</p>",
                  "admin_cancel")
    return True


@app.route("/upgrade")
def upgrade():
    _track("visit", "/upgrade")
    email = get_email()
    sub = get_subscription(email) if email else None
    return render_template("upgrade.html", pay_link=PAY_LINK, email=email,
                           price=PRICE, sub=sub, btc_enabled=bool(BTC_ADDRESS))


@app.route("/account")
def account():
    email = get_email()
    if not email:
        flash("Enregistrez votre email d'abord.", "error")
        return redirect(url_for("index"))
    sub = get_subscription(email)
    credits = require_email()
    return render_template("account.html", email=email, sub=sub,
                           credits=credits, price=PRICE,
                           unlimited=is_unlimited(email, credits))


@app.route("/account/cancel", methods=["POST"])
def account_cancel():
    email = get_email()
    if not email:
        flash("Enregistrez votre email d'abord.", "error")
        return redirect(url_for("index"))
    notify_cancel(email)
    flash("Votre abonnement a ete resilie. Nous vous attendons au prochain trade ;)",
          "success")
    return redirect(url_for("account"))


@app.route("/webhook/paypal", methods=["POST"])
def webhook_paypal():
    """Webhook style PayPal : activer al'arrivee du paiement, resilier au cancel."""
    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        data = {}
    _track("webhook", "/webhook/paypal")
    event_type = data.get("event_type", "").upper()
    resource = data.get("resource") or {}
    email = (resource.get("email") or resource.get("payer_email") or "").strip()
    sub_id = resource.get("id") or ""
    if event_type in ("PAYMENT.SALE.COMPLETED", "BILLING.SUBSCRIPTION.ACTIVATED",
                      "CHECKOUT.ORDER.APPROVED"):
        if not email:
            email = (data.get("payer") or {}).get("email_address", "")
        if email and normalize_email(email):
            notify_payment_ok(normalize_email(email))
            return jsonify({"ok": True}), 200
    if event_type in ("BILLING.SUBSCRIPTION.CANCELLED",):
        if email and normalize_email(email):
            notify_cancel(normalize_email(email))
            return jsonify({"ok": True}), 200
    print(f"[webhook] event={event_type} sub={sub_id} email={email}")
    return jsonify({"received": True}), 200


# ============================ PAIEMENT BITCOIN ============================
# Aucun compte tiers : le proprio fournit une adresse BTC de reception,
# le site affiche un QR + montant exact, puis VERIFIE la blockchain
# (mempool.space, API publique) pour activer l'abonnement des qu'il recoit
# le paiement. Robuste meme derriere un tunnel (polling sortant).
_BTC_RATE_CACHE = {"t": 0.0, "sats_per_xpf": 0.0}


def _http_json(url, timeout=12):
    import json as _json
    import urllib.request as _ur
    req = _ur.Request(url, headers={"User-Agent": "TradeScope/1.0"})
    with _ur.urlopen(req, timeout=timeout) as resp:
        return _json.loads(resp.read().decode("utf-8"))


def _btc_sats_per_xpf():
    """Satoshi par franc CFP, cache 5 min (CoinGecko + Frankfurter, sans cle)."""
    now = time.time()
    if now - _BTC_RATE_CACHE["t"] < 300 and _BTC_RATE_CACHE["sats_per_xpf"]:
        return _BTC_RATE_CACHE["sats_per_xpf"]
    try:
        btc_usd = float(_http_json(
            "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"
        )["bitcoin"]["usd"])
        eur_per_usd = float(_http_json(
            "https://api.frankfurter.app/latest?base=USD&symbols=EUR"
        )["rates"]["EUR"])
        # 1 XPF = 1/119.33 EUR (parite fixe) ; 1 USD = eur_per_usd EUR
        usd_per_xpf = (1.0 / 119.33) / eur_per_usd
        sats = usd_per_xpf / btc_usd * 1e8
        if sats > 0:
            _BTC_RATE_CACHE.update({"t": now, "sats_per_xpf": sats})
            return sats
    except Exception as e:
        print(f"[btc] taux: {type(e).__name__}: {e}")
    return _BTC_RATE_CACHE["sats_per_xpf"] or None


def _create_btc_order(email):
    if not BTC_ADDRESS:
        return None, "Le paiement Bitcoin n'est pas encore activé (aucune adresse de réception configurée)."
    sats_per_xpf = _btc_sats_per_xpf()
    if not sats_per_xpf:
        return None, "Taux BTC temporairement indisponible, réessayez dans un instant."
    import random
    # montant exact LEGEREMENT differendi entre clients (pas d'adresse unique)
    sats = int(round(PRICE_XPF * sats_per_xpf)) + random.randint(0, 1999)
    oid = uuid.uuid4().hex
    now = datetime.now().isoformat()
    conn = get_db()
    conn.execute(
        "INSERT INTO payments(id, email, method, btc_address, btc_sats, fiat_xpf,"
        " status, txid, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (oid, email, "btc", BTC_ADDRESS, sats, PRICE_XPF,
         "pending", "", now, now),
    )
    conn.commit()
    conn.close()
    return oid, None


@app.route("/pay/btc", methods=["POST"])
def pay_btc():
    email = get_email()
    if not email:
        flash("Enregistrez d'abord votre email.", "error")
        return redirect(url_for("index") + "#start")
    oid, err = _create_btc_order(email)
    if err:
        flash(err, "error")
        return redirect(url_for("upgrade"))
    _track("pay_btc_start", "/pay/btc")
    return redirect(url_for("pay_btc_page", oid=oid))


def _row_or_404(q, args):
    conn = get_db()
    row = conn.execute(q, args).fetchone()
    conn.close()
    if row is None:
        abort(404)
    return row


@app.route("/pay/btc/<oid>")
def pay_btc_page(oid):
    row = _row_or_404("SELECT * FROM payments WHERE id=?", (oid,))
    email = get_email()
    if email and email != row["email"]:
        abort(404)  # order prive
    amount = row["btc_sats"] / 1e8
    bip21 = f"bitcoin:{row['btc_address']}?amount={amount:.8f}"
    qr = None
    try:
        import base64 as _b64
        import io as _io
        import qrcode
        from qrcode.constants import ERROR_CORRECT_M
        img = qrcode.QRCode(version=None, error_correction=ERROR_CORRECT_M, box_size=9, border=2)
        img.add_data(bip21)
        img.make(fit=True)
        buf = _io.BytesIO()
        img.make_image(fill_color="black", back_color="white").save(buf, format="PNG")
        qr = "data:image/png;base64," + _b64.b64encode(buf.getvalue()).decode()
    except Exception:
        qr = None
    return render_template(
        "payout.html", pay=row, amount_btc=amount, bip21=bip21, qr=qr,
        email=email, price=PRICE, sub=get_subscription(row["email"]),
    )


@app.route("/pay/btc/status/<oid>")
def pay_btc_status(oid):
    row = _row_or_404("SELECT status FROM payments WHERE id=?", (oid,))
    return jsonify(status=row["status"])


def _btc_address_txs(addr):
    try:
        data = _http_json(
            f"https://mempool.space/api/address/{addr}/txs", timeout=15)
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"[btc] mempool: {type(e).__name__}: {e}")
        return []


def btc_scanner_loop():
    """Polling blockchain : active l'abonnement quand le BTC est recu."""
    while True:
        try:
            conn = get_db()
            orders = conn.execute(
                "SELECT * FROM payments WHERE method='btc' AND status='pending'"
            ).fetchall()
            for o in orders:
                try:
                    txs = _btc_address_txs(o["btc_address"])
                    for tx in txs:
                        st = tx.get("status") or {}
                        if not st.get("confirmed"):
                            continue
                        sats = sum(v.get("value", 0) for v in tx.get("vout", [])
                                   if v.get("scriptpubkey_address") == o["btc_address"])
                        if sats >= (o["btc_sats"] or 0) - 500:
                            conn.execute(
                                "UPDATE payments SET status='paid', txid=?, updated_at=? "
                                "WHERE id=? AND status='pending'",
                                (tx.get("txid", ""), datetime.now().isoformat(), o["id"]),
                            )
                            conn.commit()
                            notify_payment_ok(o["email"])
                            _track("pay_btc_paid", "/pay/btc/status")
                            break
                except Exception as e:
                    print(f"[btc] ordre {o.get('id')}: {type(e).__name__}: {e}")
            conn.close()
        except Exception as e:
            print(f"[btc] scan: {type(e).__name__}: {e}")
        time.sleep(60)


_BTC_SCANNER_STARTED = False


def start_btc_scanner():
    global _BTC_SCANNER_STARTED
    if _BTC_SCANNER_STARTED or not BTC_ADDRESS:
        return
    _BTC_SCANNER_STARTED = True
    import threading as _th
    _th.Thread(target=btc_scanner_loop, daemon=True, name="btc-scanner").start()


# ============================ WEBHOOK STRIPE ============================
@app.route("/webhook/stripe", methods=["POST"])
def webhook_stripe():
    """Active uniquement quand STRIPE_WEBHOOK_SECRET est renseigne (compte Stripe)."""
    if not STRIPE_WEBHOOK_SECRET:
        return jsonify({"error": "stripe not configured"}), 400
    payload = request.get_data(as_text=True)
    sig = request.headers.get("Stripe-Signature", "")
    try:
        import stripe as _stripe
        from stripe import Webhook as _Wh
        event = _Wh.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        print(f"[stripe] signature: {type(e).__name__}: {e}")
        return jsonify({"error": "bad signature"}), 400
    etype = event["type"]
    obj = event["data"]["object"]
    email = ""
    if etype == "checkout.session.completed":
        email = (obj.get("customer_details") or {}).get("email", "")
        if email and normalize_email(email):
            notify_payment_ok(normalize_email(email))
    elif etype in ("customer.subscription.deleted",):
        email = (obj.get("metadata") or {}).get("email", "")
        if email and normalize_email(email):
            notify_cancel(normalize_email(email))
    print(f"[stripe] event={etype} email={email}")
    return jsonify({"received": True})


@app.route("/admin-stats")
def admin_stats():
    """Stats JSON temps reel (polling par le tableau de bord)."""
    if request.args.get("t") != ADMIN_TOKEN:
        return jsonify({"error": "bad token"}), 401
    conn = get_db()

    def one(q):
        return conn.execute(q).fetchone()[0]

    data = {
        "total_visits": one("SELECT COUNT(*) FROM visits"),
        "unique_visitors": one("SELECT COUNT(DISTINCT ip) FROM visits WHERE ip != ''"),
        "nb_emails": one("SELECT COUNT(*) FROM visits WHERE event='email_submit'"),
        "nb_users": one("SELECT COUNT(*) FROM users"),
        "nb_analyses": one("SELECT COUNT(*) FROM analyses"),
        "nb_upgrade_views": one("SELECT COUNT(*) FROM visits WHERE page='/upgrade'"),
        "active_subs": one("SELECT COUNT(*) FROM subscriptions WHERE status='active'"),
        "last_visits": [
            dict(r) for r in conn.execute(
                "SELECT ts, ip, page, event, email FROM visits ORDER BY id DESC LIMIT 15"
            ).fetchall()
        ],
    }
    conn.close()
    return jsonify(data)


@app.route("/admin", methods=["GET", "POST"])
def admin():
    token = request.args.get("t") or (request.form.get("t") if request.method == "POST" else None)
    if token != ADMIN_TOKEN:
        if request.method == "POST":
            flash("Code incorrect.", "error")
        return render_template("admin_login.html"), 401 if request.method == "POST" else 200

    conn = get_db()
    action = request.args.get("action")
    if action == "add":
        email = request.args.get("email", "").strip().lower()
        try:
            credits = int(request.args.get("credits", "10"))
        except ValueError:
            credits = 10
        if email:
            conn.execute(
                "INSERT INTO users (email, credits, created_at) VALUES (?,?,?) "
                "ON CONFLICT(email) DO UPDATE SET credits = credits + ?",
                (email, credits, datetime.now().isoformat(), credits),
            )
            conn.commit()
            flash("Credits ajoutes a " + email, "success")
    elif action == "unlimited":
        email = request.args.get("email", "").strip().lower()
        if email:
            conn.execute(
                "INSERT INTO users (email, credits, created_at) VALUES (?,?,?) "
                "ON CONFLICT(email) DO UPDATE SET credits = -1",
                (email, -1, datetime.now().isoformat()),
            )
            conn.commit()
            flash("Pass illimite accorde a " + email, "success")
    elif action == "paid":
        email = request.args.get("email", "").strip().lower()
        if email:
            notify_payment_ok(email)
            flash("Abonnement actif (paiement valide manuellement) : " + email, "success")
    elif action == "cancel":
        email = request.args.get("email", "").strip().lower()
        if email:
            notify_cancel(email)
            flash("Abonnement resilie : " + email, "success")

    # --- Stats dashboards ---
    def one(q):
        return conn.execute(q).fetchone()[0]

    total_visits = one("SELECT COUNT(*) FROM visits")
    unique_visitors = one("SELECT COUNT(DISTINCT ip) FROM visits WHERE ip != ''")
    visit_pages = one("SELECT COUNT(*) FROM visits WHERE event='visit'")
    nb_analyses = one("SELECT COUNT(*) FROM analyses")
    nb_users = one("SELECT COUNT(*) FROM users")
    nb_emails = one("SELECT COUNT(*) FROM visits WHERE event='email_submit'")
    nb_upgrade_views = one("SELECT COUNT(*) FROM visits WHERE page='/upgrade'")
    nb_webhooks = one("SELECT COUNT(*) FROM visits WHERE event='webhook'")
    active_subs = one("SELECT COUNT(*) FROM subscriptions WHERE status='active'")

    recent_visits = conn.execute(
        "SELECT * FROM visits ORDER BY id DESC LIMIT 40").fetchall()
    recent_emails = conn.execute(
        "SELECT DISTINCT email FROM visits WHERE email != '' ORDER BY id DESC LIMIT 15").fetchall()
    subs = conn.execute(
        "SELECT s.*, u.credits FROM subscriptions s LEFT JOIN users u "
        "ON u.email = s.email ORDER BY s.updated_at DESC").fetchall()

    users = conn.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()
    analyses = conn.execute("SELECT * FROM analyses ORDER BY id DESC LIMIT 40").fetchall()
    conn.close()
    return render_template(
        "admin.html",
        users=users,
        analyses=analyses,
        token=ADMIN_TOKEN,
        stats={
            "total_visits": total_visits,
            "unique_visitors": unique_visitors,
            "visit_pages": visit_pages,
            "nb_analyses": nb_analyses,
            "nb_users": nb_users,
            "nb_emails": nb_emails,
            "nb_upgrade_views": nb_upgrade_views,
            "nb_webhooks": nb_webhooks,
            "active_subs": active_subs,
        },
        recent_visits=recent_visits,
        recent_emails=recent_emails,
        subs=subs,
        email=get_email(),
    )


@app.after_request
def _no_store(resp):
    # la page et le resultat doivent toujours etre a jour
    # (surtout apres chaque correction, pas de vieux cache navigateur)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/health")
def health():
    return {"status": "ok"}


@app.route("/api/quotes")
def api_quotes():
    now = time.time()
    if now - _QUOTES_CACHE["t"] < 60 and _QUOTES_CACHE["data"]:
        return jsonify(_QUOTES_CACHE["data"])
    rows = []
    try:
        df = yf.download(TICKER_SYMBOLS, period="5d", interval="1h",
                         progress=False, auto_adjust=False,
                         group_by="ticker", threads=True)
        for sym in TICKER_SYMBOLS:
            try:
                sub = df[sym]
                cc = sub["Close"].dropna()
                if len(cc) < 2:
                    continue
                last = float(cc.iloc[-1])
                prev = float(cc.iloc[-2])
                if sym == "GC=F":
                    spot = get_spot_price("XAU-USD")
                    if spot:
                        last = spot
                    rows.append({"symbol": "Or (XAU/USD)", "price": last,
                                 "chg": round(((last - prev) / prev * 100.0) if prev else 0.0, 2)})
                else:
                    chg = ((last - prev) / prev * 100.0) if prev else 0.0
                    rows.append({"symbol": sym, "price": last, "chg": round(chg, 2)})
            except Exception:
                continue
    except Exception:
        pass
    _QUOTES_CACHE.update({"t": now, "data": rows})
    return jsonify(rows)


# Tables creees des l'import (tout point d'entree : serve_prod, wsgi, test)
init_db()


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)