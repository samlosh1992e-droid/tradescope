import math

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter


def _sma(values, n):
    return [sum(values[i - n:i]) / n if i >= n else None for i in range(1, len(values) + 1)]


def _ema(values, n):
    k = 2.0 / (n + 1)
    out = []
    prev = None
    for v in values:
        if prev is None:
            prev = v
        else:
            prev = v * k + prev * (1 - k)
        out.append(prev)
    return out


def _rsi(values, n=14):
    r = [None]
    avg_gain = avg_loss = 0.0
    for i in range(1, len(values)):
        diff = values[i] - values[i - 1]
        gain = max(diff, 0.0)
        loss = max(-diff, 0.0)
        if i <= n:
            avg_gain += gain
            avg_loss += loss
            if i == n:
                avg_gain /= n
                avg_loss /= n
        else:
            avg_gain = (avg_gain * (n - 1) + gain) / n
            avg_loss = (avg_loss * (n - 1) + loss) / n
        if avg_loss == 0:
            r.append(100.0)
        else:
            rs = avg_gain / avg_loss
            r.append(100.0 - 100.0 / (1.0 + rs))
    return r


def _macd(values, fast=12, slow=26, signal=9):
    ef = _ema(values, fast)
    es = _ema(values, slow)
    dif = [a - b for a, b in zip(ef, es)]
    valid = [d for d in dif if d is not None]
    hist = []
    dea = []
    if len(valid) >= signal:
        dea_full = _ema(valid, signal)
        offset = len(dif) - len(dea_full)
        dea = [None] * offset + list(dea_full)
        hist = []
        for i, d in enumerate(dif):
            if dea[i] is not None:
                hist.append(d - dea[i])
    return dif, dea, hist


def _stoch(high, low, close, n=14, k_smooth=3):
    out = []
    for i in range(len(close)):
        if i < n - 1:
            out.append(None)
            continue
        hh = max(high[i - n + 1:i + 1])
        ll = min(low[i - n + 1:i + 1])
        rng = hh - ll
        out.append(50.0 if rng == 0 else (close[i] - ll) / rng * 100.0)
    return out


def _atr(high, low, close, n=14):
    trs = [high[0] - low[0]]
    for i in range(1, len(close)):
        trs.append(max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1])))
    return _sma(trs, n)


def _pivots(high, low, close):
    if len(high) < 1:
        return {}
    last_h = high[-10:] if len(high) >= 10 else high
    last_l = low[-10:] if len(low) >= 10 else low
    last_c = close[-10:] if len(close) >= 10 else close
    H = max(last_h)
    L = min(last_l)
    C = last_c[-1]
    P = (H + L + C) / 3.0
    return {
        "pivot": P,
        "r1": 2 * P - L,
        "s1": 2 * P - H,
        "r2": P + (H - L),
        "s2": P - (H - L),
        "r3": H + 2 * (P - L),
        "s3": L - 2 * (H - P),
    }


def _conviction(score, bp):
    s = abs(score)
    if s >= 6.5:
        return "elevee", score, bp
    if s >= 5:
        return "moyenne", score, bp
    if s >= 3.5:
        return "minimum", score, bp
    return "basse", score, bp


def analyse(candles):
    c = [x["close"] for x in candles]
    h = [x["high"] for x in candles]
    l = [x["low"] for x in candles]
    if len(c) < 30:
        return None

    ema9 = _ema(c, 9)
    ema21 = _ema(c, 21)
    ema50 = _ema(c, 50)
    rsi = _rsi(c, 14)
    stoch = _stoch(h, l, c, 14, 3)
    atr = _atr(h, l, c, 14)
    dif, dea, hist = _macd(c)

    last = c[-1]
    ema9_v = ema9[-1]
    ema21_v = ema21[-1]
    ema50_v = ema50[-1]
    rsi_v = rsi[-1]
    stoch_v = stoch[-1] if len(stoch) > 1 else 50.0
    atr_v = float(atr[-1] if atr[-1] is not None and atr[-1] > 0 else (max(h) - min(l)) * 0.005 or 1.0)
    dif_v = dif[-1]
    hist_v = hist[-1] if hist else 0.0
    pvs = _pivots(h, l, c)

    # Flux shorter atr for intraday
    atr_recent = atr[-8:]
    atr_recent = [a for a in atr_recent if a is not None]
    if atr_recent:
        atr_v = float(atr_recent[-1])

    score = 0.0
    bp = 0.0

    trend = "neutre"
    if last > ema21_v > ema50_v:
        trend = "hausse"
    elif last < ema21_v < ema50_v:
        trend = "baisse"

    # MACD
    if dif_v > 0 and hist_v > 0:
        score += 1.75
    elif dif_v < 0 and hist_v < 0:
        score -= 1.75
    elif dif_v > 0:
        score += 1.0
    elif dif_v < 0:
        score -= 1.0

    # RSI
    if rsi_v >= 55:
        score += 1.0
        bp += 1
    elif rsi_v <= 45:
        score -= 1.0
        bp += 1

    # EMA align
    if ema9_v > ema21_v > ema50_v:
        score += 1.5
        bp += 1
    elif ema9_v < ema21_v < ema50_v:
        score -= 1.5
        bp += 1

    # Price vs EMA21
    if last > ema21_v:
        score += 0.75
    else:
        score -= 0.75

    # Stoch
    if stoch_v > 80:
        score -= 0.5
        bp += 1
    elif stoch_v < 20:
        score += 0.5
        bp += 1

    # Pivots dist
    dist_r1 = abs(pvs["r1"] - last) / atr_v if atr_v else 5
    dist_s1 = abs(pvs["s1"] - last) / atr_v if atr_v else 5
    if dist_r1 < 1.0:
        score -= 0.75
        bp += 1
    elif dist_s1 < 1.0:
        score += 0.75
        bp += 1

    total = 10.0
    bias = "long" if score >= 0 else "short"
    conf, s, bp = _conviction(score, int(bp))
    conviction_pct = min(98, 55 + abs(score) * 6)

    entry = last if bias == "long" else last
    entry_direct = last
    sl_dist = atr_v * 1.1
    if bias == "long":
        entry = round(entry_direct, 6)
        sl = entry - sl_dist
        tp1 = entry + atr_v
        tp2 = entry + atr_v * 2
        tp3 = entry + atr_v * 3
    else:
        entry = round(entry_direct, 6)
        sl = entry + sl_dist
        tp1 = entry - atr_v
        tp2 = entry - atr_v * 2
        tp3 = entry - atr_v * 3

    direct = {
        "label": "Ordre direct (au marche)",
        "entry": entry,
        "sl": round(sl, 6),
        "tp1": round(tp1, 6),
        "tp2": round(tp2, 6),
        "tp3": round(tp3, 6),
        "sl_pct": round(abs(sl - entry) / entry * 100, 2) if entry else 0,
        "tp1_pct": round(abs(tp1 - entry) / entry * 100, 2) if entry else 0,
        "tp2_pct": round(abs(tp2 - entry) / entry * 100, 2) if entry else 0,
        "tp3_pct": round(abs(tp3 - entry) / entry * 100, 2) if entry else 0,
        "r1": round(abs(tp1 - entry) / (abs(sl - entry) + 1e-9), 2),
        "r2": round(abs(tp2 - entry) / (abs(sl - entry) + 1e-9), 2),
        "r3": round(abs(tp3 - entry) / (abs(sl - entry) + 1e-9), 2),
    }
    rr = round(abs(tp3 - entry) / (abs(sl - entry) + 1e-9), 2)

    # ---- Decision : ne pas forcer un trade ----
    # Une config trop faible => PAS DE TRADE (on n'invente pas un signal).
    # Direct au marche uniquement si conviction elevee/moyenne.
    mode = "no_trade"
    alt = None
    abs_score = abs(score)
    if abs_score >= 1.5:
        if conf in ("elevee", "moyenne"):
            mode = "direct"
        else:
            mode = "attente_suggeree"
            pullback = entry + atr_v * 0.45 if bias == "short" else entry - atr_v * 0.45
            alt_sl = pullback + sl_dist if bias == "short" else pullback - sl_dist
            alt = {
                "label": "Position en attente (plus sure)",
                "entry": round(pullback, 6),
                "sl": round(alt_sl, 6),
                "tp1": round(tp1, 6),
                "tp2": round(tp2, 6),
                "tp3": round(tp3, 6),
                "sl_pct": round(abs(alt_sl - pullback) / (abs(pullback) + 1e-9) * 100, 2),
                "trigger": "Retour sur EMA21 + reaction",
                "waiting": True,
            }

    reasons = []
    if ema9_v > ema21_v:
        reasons.append("EMA9 au-dessus de EMA21 (momentum haussier).")
    elif ema9_v < ema21_v:
        reasons.append("EMA9 sous EMA21 (momentum baissier).")
    if ema21_v > ema50_v:
        reasons.append("EMA21 au-dessus de EMA50 (tendance durable).")
    elif ema21_v < ema50_v:
        reasons.append("EMA21 sous EMA50 (tendance durable).")
    if rsi_v >= 55:
        reasons.append("RSI(14) signale une pression acheteuse.")
    elif rsi_v <= 45:
        reasons.append("RSI(14) signale une pression vendeuse.")
    if hist_v > 0:
        reasons.append("Histogramme MACD positif (impulsion acheteuse).")
    elif hist_v < 0:
        reasons.append("Histogramme MACD negatif (impulsion vendeuse).")
    if dist_s1 < 1.0:
        reasons.append("Le prix se situe proche d'un support majeur.")
    if dist_r1 < 1.0:
        reasons.append("Le prix se situe proche d'une resistance majeure.")
    if not reasons:
        reasons.append("Config neutre sur ce timeframe, majoritairement range.")

    if mode == "no_trade":
        # Pas de direct ni d'attente : on sort un plan "neutre" sans entree.
        return {
            "bias": bias,
            "bias_label": "NEUTRE" if abs_score < 1.5 else ("ACHAT (LONG)" if bias == "long" else "VENTE (SHORT)"),
            "trend": trend,
            "confidence": conf,
            "score": round(score, 2),
            "confidence_pct": int(conviction_pct),
            "entry": None,
            "last": last,
            "current_price": last,
            "atr": atr_v,
            "rr": 0,
            "mode": mode,
            "direct": None,
            "alt": None,
            "reasons": reasons,
            "ema9": ema9_v,
            "ema21": ema21_v,
            "ema50": ema50_v,
            "rsi": rsi_v,
            "stoch": stoch_v,
            "pivots": pvs,
            "datetime": None,
            "symbol": candles[-1].get("time", ""),
        }

    plan = {
        "bias": bias,
        "bias_label": "ACHAT (LONG)" if bias == "long" else "VENTE (SHORT)",
        "trend": trend,
        "confidence": conf,
        "score": round(score, 2),
        "confidence_pct": int(conviction_pct),
        "entry": entry,
        "last": last,
        "current_price": last,
        "atr": atr_v,
        "rr": rr,
        "mode": mode,
        "direct": None if mode != "direct" else direct,
        "alt": alt,
        "reasons": reasons,
        "current_price": last,
        "ema9": ema9_v,
        "ema21": ema21_v,
        "ema50": ema50_v,
        "rsi": rsi_v,
        "stoch": stoch_v,
        "pivots": pvs,
        "datetime": None,
        "symbol": candles[-1].get("time", ""),
    }
    d = plan["direct"]
    if d is not None:
        plan["sl"] = d["sl"]
        plan["tp1"] = d["tp1"]
        plan["tp2"] = d["tp2"]
        plan["tp3"] = d["tp3"]
        plan["r"] = d["sl_pct"]
    else:
        plan["sl"] = entry
        plan["tp1"] = tp1
        plan["tp2"] = tp2
        plan["tp3"] = tp3
        plan["r"] = 0
    if alt:
        ref = plan["direct"] or alt
        alt["bias"] = bias
        alt["r"] = alt.get("sl_pct", ref["sl_pct"])
    return plan


def render_chart_png(candles, plan, symbol):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    if len(candles) > 200:
        candles = candles[-200:]

    c = [x["close"] for x in candles]
    h = [x["high"] for x in candles]
    l = [x["low"] for x in candles]
    times = list(range(len(candles)))
    ema9 = _ema(c, 9)
    ema21 = _ema(c, 21)

    fig, (ax, ax2) = plt.subplots(2, 1, figsize=(12, 7), sharex=True,
                                  gridspec_kw={"height_ratios": [3, 1]})
    fig.patch.set_facecolor("#0d1117")
    for a in (ax, ax2):
        a.set_facecolor("#0d1117")
        a.grid(True, color="#1f2733", alpha=0.5, linewidth=0.7)
        a.tick_params(colors="#8b949e")
        for spine in a.spines.values():
            spine.set_color("#1f2733")

    ax.plot(times, c, color="#58a6ff", linewidth=1.3, label="Prix")
    ax.plot(times, ema9, color="#f0b90b", linewidth=1.1, alpha=0.9, label="EMA9")
    ax.plot(times, ema21, color="#ff5252", linewidth=1.2, alpha=0.9, label="EMA21")

    d = plan["direct"] or plan.get("alt")
    entry = d["entry"] if d else c[-1]
    if d:
        sl = d["sl"]
        tp1 = d["tp1"]
        tp2 = d["tp2"]
        tp3 = d["tp3"]
        ax.axhline(entry, color="#2ea043", linestyle="--", linewidth=1.2)
        ax.axhline(sl, color="#f85149", linestyle="--", linewidth=1.2)
        ax.axhline(tp1, color="#58a6ff", linestyle=":", linewidth=1.0)
        ax.axhline(tp2, color="#58a6ff", linestyle=":", linewidth=1.0)
        ax.axhline(tp3, color="#58a6ff", linestyle=":", linewidth=1.0)
        ax.text(len(times) - 1, entry, f" entree {entry:.4f}", color="#2ea043", fontsize=9, va="center")
        ax.text(len(times) - 1, sl, f" SL {sl:.4f}", color="#f85149", fontsize=9, va="center")
    ax.legend(loc="upper left", fontsize=8)

    hist = _macd(c)[2]
    if hist:
        ax2.fill_between(list(range(len(hist))), hist, 0, color="#58a6ff", alpha=0.6)
        ax2.axhline(0, color="#1f2733", linewidth=0.8)
    ax2.set_title("MACD (12,26,9)", color="#8b949e", fontsize=9)

    if plan.get("alt"):
        alt = plan["alt"]
        ax.axhline(alt["entry"], color="#bc8cff", linestyle="-.", linewidth=1.1)
        ax.text(len(times) - 1, alt["entry"], f" attente {alt['entry']:.4f}", color="#bc8cff", fontsize=9, va="center")

    ymin = min(l) - plan["atr"] * 1
    ymax = max(h) + plan["atr"] * 1
    ax.set_ylim(ymin, ymax)

    fig.suptitle(f"{symbol}  —  {plan['bias'].upper()} - {plan['mode']}",
                 color="#e6edf3", fontsize=12, fontweight="bold")
    fig.tight_layout(rect=[0, 0, 1, 0.96])

    import io
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100)
    plt.close(fig)
    buf.seek(0)
    return buf


def overlay_screenshot(data, plan, lo, hi):
    try:
        from PIL import Image, ImageDraw, ImageFont
        import io
        img = Image.open(io.BytesIO(data)).convert("RGB")
        w, h = img.size
        draw = ImageDraw.Draw(img)

        def font(sz):
            for p in (r"C:\Windows\Fonts\arialbd.ttf", r"C:\Windows\Fonts\arial.ttf"):
                try:
                    return ImageFont.truetype(p, sz)
                except Exception:
                    pass
            return ImageFont.load_default()

        cols = {"entry": "#2ea043", "sl": "#f85149", "tp": "#58a6ff", "attente": "#bc8cff"}

        def px(price):
            return h - int((price - lo) / (hi - lo) * h)

        d = plan["direct"]
        y_e = px(d["entry"])
        y_s = px(d["sl"])
        draw.line([(0, y_e), (w, y_e)], fill=cols["entry"], width=3)
        draw.line([(0, y_s), (w, y_s)], fill=cols["sl"], width=3)
        for k in ("tp1", "tp2", "tp3"):
            draw.line([(0, px(d[k])), (w, px(d[k]))], fill=cols["tp"], width=2)
        draw.text((10, max(0, y_e - 22)), f"ENTREE {d['entry']:.4f}", fill=cols["entry"], font=font(20))
        draw.text((10, min(h - 22, y_s)), f"SL {d['sl']:.4f}", fill=cols["sl"], font=font(20))
        draw.text((10, max(0, y_s + 24)), f"TP {d['tp1']:.4f} / {d['tp2']:.4f} / {d['tp3']:.4f}",
                  fill=cols["tp"], font=font(18))
        if plan.get("alt"):
            y_a = px(plan["alt"]["entry"])
            draw.line([(0, y_a), (w, y_a)], fill=cols["attente"], width=2)
            draw.text((10, max(0, y_a - 20)), f"ATTENTE {plan['alt']['entry']:.4f}",
                      fill=cols["attente"], font=font(18))

        out = io.BytesIO()
        img.save(out, format="PNG")
        out.seek(0)
        return out
    except Exception:
        return None