from app import app, init_db, start_btc_scanner

init_db()

try:
    start_btc_scanner()
except Exception:
    pass

application = app