"""Analyse reelle d'un screenshot de graphique.

Deux moteurs :
  - "local" : vision par ordinateur (cv2 + numpy), sans cle, sans envoi de donnees.
              Lit les bougies (couleurs), la tendance, la structure S/R.
  - "ai"    : un modele de vision (API OpenAI-compatible) si VISION_API_KEY est
              defini. Lis aussi le symbole et l'unite de temps dans l'image.
  - "none"  : si l'image est illisible.

Le verdict de position croise la TELEVISION du screen avec le biais calcule
par l'engine sur les donnees reelles (tout est fait dans app.py).
"""

import base64
import io
import json
import os
import re
import urllib.request
import urllib.error

import cv2
import numpy as np

try:
    import pytesseract
    _TESS_PATHS = [
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
    ]
    for _p in _TESS_PATHS:
        if os.path.exists(_p):
            pytesseract.pytesseract.tesseract_cmd = _p
            break
    _OCR_OK = bool(pytesseract.get_tesseract_version())
except Exception:
    pytesseract = None
    _OCR_OK = False

# Palettes des plateformes courantes (clairs et sombres) en HSV.
GREEN_LOW = np.array([70, 55, 40])
GREEN_HIGH = np.array([175, 255, 255])
RED_LOW = np.array([0, 55, 40])
RED_HIGH = np.array([14, 255, 255])
RED2_LOW = np.array([168, 55, 40])
RED2_HIGH = np.array([180, 255, 255])


def _to_rgb(arr):
    if arr.ndim == 2:
        return cv2.cvtColor(arr, cv2.COLOR_GRAY2RGB)
    if arr.shape[2] == 4:
        return cv2.cvtColor(arr, cv2.COLOR_RGBA2RGB)
    return cv2.cvtColor(arr, cv2.COLOR_BGR2RGB)


def _masks(bgr):
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    green = cv2.inRange(hsv, GREEN_LOW, GREEN_HIGH)
    red1 = cv2.inRange(hsv, RED_LOW, RED_HIGH)
    red2 = cv2.inRange(hsv, RED2_LOW, RED2_HIGH)
    red = cv2.bitwise_or(red1, red2)
    return green, red


def _longest_run(rows):
    """Plus longue sequence continue de lignes -> (debut, fin) en pixels."""
    if len(rows) == 0:
        return None
    splits = np.where(np.diff(rows) > 1)[0] + 1
    groups = np.split(rows, splits)
    best = max(groups, key=len)
    return (int(best.min()), int(best.max()))


def _candle_series(green, red, min_col_rows=2):
    """Reconstruit une serie de niveaux x -> cloture de la bougie.

    Le CORPS est isole des meches (ouverture 3px) : sur une bougie,
    la cloture est le bord du haut du corps si la bougie est haussiere
    (verte) ou le bord du bas si elle est baissiere (rouge) — c'est le
    prix reel affiche par la plateforme.
    On ignore :
      - les colonnes qui couvrent la HAUTEUR de tout le graphique
        (lignes/indicateurs verticaux, pas des bougies) ;
      - les colonnes SANS corps (simple meche de 1 px).
    Sortie : list of (x, close_y, up, top_px, bottom_px).
    """
    H = green.shape[0]
    max_span = int(H * 0.6)
    max_body = int(H * 0.3)
    # Le CORPS d'une bougie est LARGE (>= 5px de cote) : on ouvre le masque
    # horizontalement (5px) pour ne garder QUE des corps. Meches (1-2px),
    # lignes d'indicateur (MAs ~2-3px) et traits verticaux disparaissent.
    kh = np.ones((1, 5), np.uint8)
    gw = cv2.morphologyEx(green, cv2.MORPH_OPEN, kh)
    rw = cv2.morphologyEx(red, cv2.MORPH_OPEN, kh)
    points = []
    g = green > 0
    r = red > 0
    for x in range(green.shape[1]):
        rows_g = np.where(g[:, x])[0]
        rows_r = np.where(r[:, x])[0]
        if len(rows_g) > len(rows_r) and len(rows_g) >= min_col_rows:
            if int(rows_g.max() - rows_g.min()) > max_span:
                continue  # ligne/indicateur vertical -> pas une bougie
            body = _longest_run(np.where(gw[:, x] > 0)[0])
            if body is None:
                continue  # pas de corps (meche/ligne fine)
            close_y = float(body[0])     # verte : cloture = haut du corps
            points.append((x, close_y, True,
                           float(rows_g.min()), float(rows_g.max())))
        elif len(rows_r) > len(rows_g) and len(rows_r) >= min_col_rows:
            if int(rows_r.max() - rows_r.min()) > max_span:
                continue
            body = _longest_run(np.where(rw[:, x] > 0)[0])
            if body is None:
                continue
            close_y = float(body[1])     # rouge : cloture = bas du corps
            points.append((x, close_y, False,
                           float(rows_r.min()), float(rows_r.max())))
    return points


def _aggregate_bars(points, merge_gap=4):
    """Regroupe les colonnes consecutives de meme couleur en barres.

    Chaque barre : (x_debut, x_fin, cloture moyenne, up, top_px, bottom_px,
    cloture de la derniere colonne = bord droit du screenshot).
    """
    if not points:
        return []
    bars = []
    cur = [points[0]]
    for pt in points[1:]:
        same_x = abs(pt[0] - cur[-1][0]) <= merge_gap
        same_dir = pt[2] == cur[-1][2]
        if same_x and same_dir:
            cur.append(pt)
        else:
            xs = [p[0] for p in cur]
            ys = [p[1] for p in cur]
            tops = [p[3] for p in cur]
            bots = [p[4] for p in cur]
            bars.append((xs[0], xs[-1], float(np.mean(ys)), cur[0][2],
                         float(min(tops)), float(max(bots)), float(ys[-1])))
            cur = [pt]
    xs = [p[0] for p in cur]
    ys = [p[1] for p in cur]
    tops = [p[3] for p in cur]
    bots = [p[4] for p in cur]
    bars.append((xs[0], xs[-1], float(np.mean(ys)), cur[0][2],
                 float(min(tops)), float(max(bots)), float(ys[-1])))
    return bars


def _trend(bars, window=24):
    """Biais + force depuis les niveaux des barres (y haut = prix bas)."""
    if len(bars) < 5:
        return None, None
    yy = [b[2] for b in bars][-window:]
    xx = list(range(len(yy)))
    slope, intercept = np.polyfit(xx, yy, 1)
    # ymin -> prix min ; plus le slope est negatif, plus le prix monte.
    pred = slope * np.array(xx) + intercept
    residual = yy - pred
    var_total = np.var(yy) * len(yy)
    var_res = np.sum(residual ** 2)
    r2 = 1 - (var_res / var_total) if var_total > 1e-9 else 0.0
    if slope <= -0.02:
        bias = "long"
    elif slope >= 0.02:
        bias = "short"
    else:
        bias = "neutre"
    strength = min(98, max(25, 40 + abs(slope) * 320 + r2 * 25))
    return bias, strength


def _structure(bars):
    """Supports (min regionaux) et resistances (max regionaux) en pixels."""
    if len(bars) < 6:
        return [], []
    ys = [b[2] for b in bars]
    supports, resistances = [], []
    for i in range(2, len(ys) - 2):
        win = ys[i - 2:i + 3]
        if ys[i] == min(win):
            supports.append(ys[i])
        if ys[i] == max(win):
            resistances.append(ys[i])
    def _cluster(vals, tol):
        out = []
        for v in sorted(vals):
            if out and abs(v - out[-1]) < tol:
                out[-1] = (out[-1] + v) / 2
            else:
                out.append(v)
        return out
    span = (max(ys) - min(ys)) / 8 if max(ys) > min(ys) else 8.0
    return _cluster(supports, span), _cluster(resistances, span)


def _image_info(bgr):
    h, w = bgr.shape[:2]
    area = h * w
    full = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    non_black = np.count_nonzero(full > 18) / area
    return {"size": f"{w}x{h}", "fill": round(non_black, 2)}


def analyse_screenshot(data, method="auto"):
    """Entry point. method: "auto"|"local"|"ai"|"none"."""
    try:
        arr = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if arr is None:
            arr = np.asarray(Image_from_bytes(data))
    except Exception:
        arr = None
    if arr is None:
        return {"ok": False, "method": "none", "error": "image illisible"}
    bgr = cv2.cvtColor(_to_rgb(arr), cv2.COLOR_RGB2BGR)

    info = _image_info(bgr)
    green, red = _masks(bgr)
    # LA colonne des prix (axe) contient souvent du texte colore/antialiase
    # que le masque prend pour des bougies -> on ne cherche les bougies que
    # dans la ZONE DU GRAPHIQUE (hors axe des prix, a droite et a gauche).
    h_img, w_img = green.shape[:2]
    AXIS_MARGIN = 200
    plot = green[:, 0:max(0, w_img - AXIS_MARGIN)] if w_img > AXIS_MARGIN * 2 \
        else green
    plot_r = red[:, 0:max(0, w_img - AXIS_MARGIN)] if w_img > AXIS_MARGIN * 2 \
        else red
    pts = _candle_series(plot, plot_r)
    bars = _aggregate_bars(pts)
    up = sum(1 for b in bars if b[3])
    down = sum(1 for b in bars if not b[3])
    bias, strength = _trend(bars)
    supports, resistances = _structure(bars)

    if method == "ai" and os.environ.get("VISION_API_KEY"):
        ai = _analyse_ai(data)
        if ai:
            return ai

    if len(bars) < 5:
        return {
            "ok": True, "method": "local", "bias": "neutre", "confidence": 0,
            "bars": len(bars), "up": up, "down": down,
            "verdict": "pas_trade",
            "notes": [
                f"{len(bars)} bougie(s) reconnue(s) : l'image est trop peu lisible "
                "pour un verdict fiable."
            ],
            "supports": [], "resistances": [],
            "image": info,
        }

    # Dernieres barres : momentum immediat
    last_n = bars[-8:]
    recent_up = sum(1 for b in last_n if b[3])
    recent = "hausse" if recent_up >= 5 else "baisse" if recent_up <= 3 else "range"

    notes = [
        f"{len(bars)} bougies détectées ({up} haussières, {down} baissières) "
        f"sur une image {info['size']}.",
        f"Momentum immédiat (8 dernières bougies) : {recent}.",
    ]
    if supports:
        notes.append(f"{len(supports)} zone(s) de support visualisée(s).")
    if resistances:
        notes.append(f"{len(resistances)} zone(s) de résistance visualisée(s).")

    verdict = "position_possible" if bias != "neutre" and strength >= 45 else \
        "pas_trade" if bias == "neutre" else "prudence"

    result = {
        "ok": True,
        "method": "local",
        "bias": bias,
        "confidence": int(strength),
        "bars": len(bars),
        "up": up,
        "down": down,
        "recent": recent,
        "verdict": verdict,
        "notes": notes,
        "supports": supports,
        "resistances": resistances,
        "image": info,
    }

    # ---- Entree/SL/TP calcules SEULEMENT depuis l'image ----
    # On lit la graduation de l'axe des prix et l'ATR visuelle (en pixels),
    # convertis en prix reels A PARTIR du screenshot (journal de bord situe
    # sur le screenshot lui-meme, on ne touche pas aux cours du marche).
    if bias in ("long", "short") and len(bars) >= 10:
        levels = _levels_from_image(bars, bgr, bias)
        if levels:
            result["levels"] = levels
            result["verdict"] = verdict
    return result


# ---------------------------------------------------------------------------
# Entree/SL/TP 100 % depuis le screenshot (sans donnees marche)
# ---------------------------------------------------------------------------

_NUM_RE = re.compile(r"^[\d\s.,]{2,}$")


def _ocr_axis_strip(bgr, side="right", width=140):
    """OCR une bande verticale (axe des prix) -> [(y_pixel, prix), ...]."""
    if not _OCR_OK or pytesseract is None:
        return []
    h, w = bgr.shape[:2]
    x0 = w - width if side == "right" else 0
    strip = bgr[:, max(0, x0): min(w, x0 + width)]
    gray = cv2.cvtColor(strip, cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(gray, None, fx=2.2, fy=2.2, interpolation=cv2.INTER_CUBIC)
    _, thr = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    cfg = "--psm 6 -c tessedit_char_whitelist=0123456789.,-"
    try:
        data = pytesseract.image_to_data(thr, config=cfg,
                                         output_type=pytesseract.Output.DICT)
    except Exception as e:
        print(f"[vision][ocr] {side}: {e}")
        return []
    picks = []
    n = len(data.get("text", []))
    for i in range(n):
        txt = (data["text"][i] or "").strip()
        if not txt or not _NUM_RE.match(txt):
            continue
        try:
            num = float(txt.replace(" ", "").replace(",", "."))
        except ValueError:
            continue
        conf = int(data.get("conf", [0] * n)[i])
        if conf < 40:
            continue
        y = data["top"][i] + data["height"][i] / 2
        picks.append((y / 2.2, num))  # /2.2 -> coordonnees d'origine
    return picks


def _fit_axis(points):
    """Fit lineaire robuste prix=f(y) avec rejet des outliers OCR.

    Retourne (slope, intercept, r2, y_min, y_max, prix_min, prix_max)
    ou None. Exige au moins 3 prix cohérents et un axe descendant
    (y augmente => prix baisse), sinon il refuse de chiffrer.
    """
    if len(points) < 3:
        return None
    ys = np.array([p[0] for p in points], dtype=float)
    ps = np.array([p[1] for p in points], dtype=float)
    if np.ptp(ys) < 8 or np.ptp(ps) < 0.00005:
        return None
    mask = np.ones(len(ys), dtype=bool)
    slope = intercept = None
    for _ in range(5):
        A = np.vstack([ys[mask], np.ones(int(mask.sum()))]).T
        sl, ic = np.linalg.lstsq(A, ps[mask], rcond=None)[0]
        resid = ps - (sl * ys + ic)
        mad = np.median(np.abs(resid[mask])) if mask.sum() else 0.0
        keep = np.abs(resid) <= max(mad * 3.0, 0.04 * np.ptp(ps))
        n_prev = int(mask.sum())
        mask = keep
        n_new = int(mask.sum())
        slope, intercept = sl, ic
        if n_new == n_prev and n_new >= 3:
            break
    if slope is None or abs(slope) < 1e-9:
        return None
    if slope > 0:
        # axe inverse (prix croissant quand on descend) : lecture aberrante
        return None
    n = int(mask.sum())
    if n < 3:
        return None
    pred = slope * ys[mask] + intercept
    ss = 1 - np.sum((ps[mask] - pred) ** 2) / \
        (np.sum((ps[mask] - np.mean(ps[mask])) ** 2) + 1e-9)
    if ss < 0.6:
        return None
    # pas de prix entre 2 graduations (ex. 10 pour l'or)
    sorted_p = np.sort(ps[mask])
    diffs = np.diff(sorted_p)
    step = float(np.median(diffs)) if len(diffs) else 0.0
    if step <= 0:
        step = 0.0
    return (slope, intercept, ss,
            float(np.median(ys[mask])), float(np.median(ps[mask])),
            float(np.min(ps[mask])), float(np.max(ps[mask])), step)


def _price_scale(bgr):
    """Essaie les deux cotes, garde le meilleur ajustement d'axe des prix."""
    best = None
    for side in ("right", "left"):
        pts = _ocr_axis_strip(bgr, side=side)
        fit = _fit_axis(pts)
        if fit:
            if best is None or fit[2] > best[2]:
                best = fit
    return best


def _visual_atr_px(bars):
    """Amplitude mediane haut->bas des bougies, en pixels (meres+corps)."""
    extents = [abs(b[5] - b[4]) for b in bars if b[5] > b[4]]
    if not extents:
        return 0.0
    med = float(np.median(extents))
    return med if med >= 1 else 0.0


def _levels_from_image(bars, bgr, bias):
    """Entree/SL/TP1/2/3 calcules depuis le screenshot seul.

    On lit la graduation de l'axe des prix (OCR) et on convertit les pixels
    des bougies en prix. L'entree = dernier cours visible sur l'image,
    le SL/TP = distance ATR *visuelle* du screenshot, en prix.
    L'entree doit tomber dans la plage de prix reelle LUe sur l'image
    (tolérance), sinon on refuse : jamais de prix invente. Renvoie None
    si l'axe des prix est illisible ou incohérent.
    """
    scale = _price_scale(bgr)
    if not scale:
        return None
    slope, intercept, _ss, _, _, plo, phi, step = scale
    last_close_y = bars[-1][6]
    entry = last_close_y * slope + intercept
    # Un vrai analyste cale l'entree sur la graduation VISIBLE :
    # si la cloture tombe juste sous/au-dessus de l'echelle lue, on ajuste a
    # la graduation la plus proche ; si elle en est trop loin, on REFUSE
    # (entree qui n'existe pas sur la photo).
    lim = max(step, abs(phi) * 1e-6)
    snapped = False
    if entry < plo:
        if plo - entry > 0.75 * lim:
            return None
        entry = plo
        snapped = True
    elif entry > phi:
        if entry - phi > 0.75 * lim:
            return None
        entry = phi
        snapped = True
    if bias not in ("long", "short"):
        return None
    span = phi - plo
    if span <= 0:
        return None
    # L'entree EST sur la photo ; les cibles (TP) peuvent sortir un peu
    # (jusqu'a 35 % de la bande visible) mais le SL doit rester VISIBLE.
    extra = span * 0.35
    tgt_up = (phi - entry) + extra      # cibles long vers le haut
    tgt_dn = (entry - plo) + extra      # cibles short vers le bas
    sl_up = phi - entry                 # SL short (au-dessus de l'entree)
    sl_dn = entry - plo                 # SL long (en dessous de l'entree)
    if bias == "long":
        atr_max = min(tgt_up / 3.0, sl_dn / 1.1) if (tgt_up > 0 and sl_dn > 0) else 0.0
    else:
        atr_max = min(tgt_dn / 3.0, sl_up / 1.1) if (tgt_dn > 0 and sl_up > 0) else 0.0
    if atr_max <= span * 0.005:
        return None
    atr_max *= 0.85
    raw_atr = _visual_atr_px(bars) * abs(slope)
    atr = min(max(raw_atr, abs(entry) * 0.0006), atr_max)
    if atr <= 0:
        return None
    sl_dist = atr * 1.1
    sign = 1.0 if bias == "long" else -1.0
    sl = entry - sign * sl_dist
    tp1 = entry + sign * atr
    tp2 = entry + sign * atr * 2
    tp3 = entry + sign * atr * 3
    r = atr * 3 / (sl_dist + 1e-9)
    # outlieres de l'OCR (prix absurdes)
    if entry <= 0 or sl <= 0 or tp3 <= 0:
        return None
    return {
        "entry": round(entry, 6),
        "sl": round(sl, 6),
        "tp1": round(tp1, 6),
        "tp2": round(tp2, 6),
        "tp3": round(tp3, 6),
        "sl_pct": round(sl_dist / entry * 100, 2) if entry else 0,
        "rr": round(r, 2),
        "atr": round(atr, 6),
        "snapped": bool(snapped),
        "axis_min": round(plo, 6),
        "axis_max": round(phi, 6),
        "axis_step": round(step, 6),
        "axis": "échelle lue sur l'image (OCR)",
    }


def _analyse_ai(data):
    """Vision IA via API OpenAI-compatible (README: VISION_API_KEY/VISION_MODEL)."""
    key = os.environ.get("VISION_API_KEY", "").strip()
    url = os.environ.get("VISION_API_URL",
                         "https://api.openai.com/v1/chat/completions").strip()
    model = os.environ.get("VISION_MODEL", "gpt-4o-mini").strip()
    if not key:
        return None
    b64 = base64.b64encode(data).decode("ascii")
    prompt = (
        "Tu es un analyste technique qui lit un screenshot de plateforme de "
        "trading (MT4/MT5, TradingView, etc.). Reponds UNIQUEMENT en JSON valide "
        "avec exactement ces champs : "
        '{"bias":"long|short|neutre","confidence":0-100,"trend":"hausse|baisse|range",'
        '"candles":int,"timeframe":"ex: M15 ou null","symbol":"ex: XAUUSD ou null",'
        '"supports":[nombres approximatifs],"resistances":[nombres approximatifs],'
        '"last_price":nombre ou null,"notes":["une ou deux phrases courtes en francais"],'
        '"verdict":"position_possible|prudence|pas_trade|image_illisible"}. '
        "Lis vraiment les bougies, pas de speculation."
    )
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": [
                {"type": "text", "text": "Analyse ce screenshot de graphique."},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ]},
        ],
        "max_tokens": 700,
        "temperature": 0,
    }
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        content = payload["choices"][0]["message"]["content"]
        start = content.find("{")
        end = content.rfind("}")
        d = json.loads(content[start:end + 1])
        d.setdefault("notes", [])
        d.setdefault("verdict", "pas_trade" if d.get("bias") == "neutre" else "position_possible")
        d["ok"] = True
        d["method"] = "ai"
        d["image"] = {}
        return d
    except (urllib.error.HTTPError, urllib.error.URLError, ValueError,
            KeyError, json.JSONDecodeError) as e:
        print(f"[vision][ai] echec: {type(e).__name__}: {e}")
        return None


def Image_from_bytes(data):
    from PIL import Image
    return Image.open(io.BytesIO(data)).convert("RGB")