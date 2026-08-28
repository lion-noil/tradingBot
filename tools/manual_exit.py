# -*- coding: utf-8 -*-
"""고아 포지션 수동 청산 — executor 정식 EXIT 경로로 주입 (체결·lot정리·trade_record·알림 자동).

EXIT 미체결 고아(신호봇 장부에선 이미 제거된 포지션)를 닫을 때 사용.
executor의 액션 리스너(JSON line/TCP)로 EXIT 메시지를 보낸다. 멱등키가 signal_id 기반이라
재실행 시엔 signal_id가 새로 생성돼 중복 방지가 안 됨 — 체결 확인 후엔 다시 돌리지 말 것.

usage (Windows, MT5 시세용 dacon_comp1 파이썬 권장):
  python tools/manual_exit.py SYMBOL SIDE ENTRY_SIGNAL_ID [strategy] [host:port]
  예) python tools/manual_exit.py USDJPY LONG 7d672ebe...498d s12
"""
import json
import socket
import sys
import time
import uuid

sym = sys.argv[1].upper()
side = sys.argv[2].upper()
open_sid = sys.argv[3]
strat = sys.argv[4] if len(sys.argv) > 4 else ""
host, _, port = (sys.argv[5] if len(sys.argv) > 5 else "127.0.0.1:9010").partition(":")

price = None
try:
    import MetaTrader5 as mt5
    BROKER = {"US100": "US100.F", "US500": "US500.F", "JP225": "JP225.F",
              "BTCUSD": "#BTCUSD", "ETHUSD": "#ETHUSD", "WTI": "USOIL"}
    if mt5.initialize():
        t = mt5.symbol_info_tick(BROKER.get(sym, sym))
        if t:
            price = float(t.bid if side == "LONG" else t.ask)
        mt5.shutdown()
except Exception as e:
    print(f"(시세 조회 생략: {e})")

msg = {
    "ts_ms": int(time.time() * 1000),
    "symbol": sym,
    "action": "EXIT",
    "side": side,
    "price": price,
    "signal_id": f"manualexit{uuid.uuid4().hex[:22]}",
    "close_open_signal_id": open_sid,
    "strategy": strat,
    "signal_only": False,
}
print("send:", json.dumps(msg, ensure_ascii=False))
s = socket.create_connection((host, int(port)), timeout=10)
s.sendall((json.dumps(msg) + "\n").encode("utf-8"))
time.sleep(3)   # executor가 읽을 시간
s.close()
print("done — executor 로그/텔레그램으로 체결 확인할 것")
