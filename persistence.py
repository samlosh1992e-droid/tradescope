# -*- coding: utf-8 -*-
"""Persistance des donnees critiques malgre le disque ephemere de Render free.

La base SQLite locale est effacee a chaque spin-down/redemarrage. Ce module
sauvegarde les tables importantes (users, subscriptions, payments) sur un
depot GitHub prive via l'API contents, et permet de les restaurer au boot.
Ajout via les variables d'environnement :
  GH_TOKEN   = token GitHub (classique ou fine-grained avec acces content)
  PERSIST_REPO = "user/repo" prive ou ecrire backup.json (defaut fourni)
"""

import base64
import json
import os
import threading
import time
import urllib.error
import urllib.request

TABLES = ("users", "subscriptions", "payments")
DEF_REPO = "samlosh1992e-droid/tradescope-db-private"

_lock = threading.Lock()
_event = threading.Event()
_worker = None
_last_push = [0.0]


def enabled():
    return bool(os.environ.get("GH_TOKEN", "").strip())


def _repo():
    return os.environ.get("PERSIST_REPO", "").strip() or DEF_REPO


def _headers():
    return {
        "Authorization": "Bearer " + os.environ.get("GH_TOKEN", "").strip(),
        "User-Agent": "tradescope-backup",
        "Accept": "application/vnd.github+json",
    }


def snapshot(factory):
    out = {}
    conn = factory()
    try:
        for t in TABLES:
            try:
                rows = conn.execute("SELECT * FROM %s" % t).fetchall()
                out[t] = [dict(r) for r in rows]
            except Exception as e:
                print("[persist] snapshot %s: %s" % (t, e))
                out[t] = []
    finally:
        conn.close()
    return out


def push_now(factory):
    """Sauvegarde synchrone. factory() = appelable renvoyant une connexion. OK si reussi."""
    if not enabled():
        return False
    body = json.dumps(snapshot(factory), ensure_ascii=False)
    return _push_body(body)


def _push_body(body):
    data = {"message": "backup", "content": base64.b64encode(body.encode("utf-8")).decode("utf-8")}
    sha = _get_sha()
    if sha:
        data["sha"] = sha
    url = "https://api.github.com/repos/%s/contents/backup.json" % _repo()
    req = urllib.request.Request(
        url, data=json.dumps(data).encode("utf-8"), method="PUT",
        headers={**_headers(), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            if r.status in (200, 201):
                _last_push[0] = time.time()
            print("[persist] push -> %s" % r.status)
            return r.status in (200, 201)
    except urllib.error.HTTPError as e:
        print("[persist] push error %s: %s" % (e.code, e.read().decode("utf-8")[:300]))
        return False


def _get_sha():
    url = "https://api.github.com/repos/%s/contents/backup.json" % _repo()
    req = urllib.request.Request(url, headers=_headers())
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8")).get("sha")
    except urllib.error.HTTPError:
        return None


def request_push(factory):
    """Sauvegarde asynchrone avec debounce (max 1 push / 20 s)."""
    if not enabled():
        return
    global _worker
    if _worker is None:
        def worker():
            while True:
                _event.wait()
                _event.clear()
                with _lock:
                    try:
                        if time.time() - _last_push[0] >= 20:
                            _push_body(json.dumps(snapshot(factory), ensure_ascii=False))
                        else:
                            time.sleep(3)
                            _event.set()
                    except Exception:
                        pass
        _worker = threading.Thread(target=worker, daemon=True)
        _worker.start()
    _event.set()


def fetch():
    """Renvole le snapshot distant (dict) ou None."""
    if not enabled():
        return None
    url = "https://api.github.com/repos/%s/contents/backup.json" % _repo()
    req = urllib.request.Request(url, headers=_headers())
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            j = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print("[persist] fetch error %s" % e.code)
        return None
    try:
        raw = base64.b64decode(j["content"]).decode("utf-8")
        return json.loads(raw)
    except Exception as e:
        print("[persist] fetch parse: %s" % e)
        return None


def restore(factory):
    """Injecte le snapshot distant dans la base locale. Renvole nb users restaures ou None."""
    data = fetch()
    if not data:
        return None
    conn = factory()
    try:
        for t in TABLES:
            for row in data.get(t) or []:
                cols = [k for k in row.keys()]
                if not cols:
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO %s (%s) VALUES (%s)" % (
                        t, ",".join(cols), ",".join("?" * len(cols))),
                    [row[c] for c in cols])
        conn.commit()
        n = len(data.get("users", []))
        print("[persist] restaure %d users / %d subs / %d payments"
              % (n, len(data.get("subscriptions", [])), len(data.get("payments", []))))
        return n
    except Exception as e:
        print("[persist] restore error: %s" % e)
        return None
    finally:
        conn.close()