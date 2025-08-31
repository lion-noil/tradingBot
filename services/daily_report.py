# services/daily_report.py
import os
import io
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import matplotlib
matplotlib.use("Agg")  # 서버(헤드리스) 렌더링
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
_TZ = "Asia/Seoul"

_scheduler = None  # 모듈 레벨에 보관(가비지 콜렉션 방지)

def _slice_from_cache_with_lookback(candles_deque, start_ms: int, end_ms: int, lookback_minutes: int):
    """전날 구간보다 lookback_minutes 만큼 앞선 데이터까지 포함해서 슬라이스."""
    if not candles_deque:
        return []
    def get(c, key):
        return c.get(key) if isinstance(c, dict) else getattr(c, key)
    lb_ms = start_ms - lookback_minutes * 60_000

    rows = []
    for c in candles_deque:  # 오름차순 가정
        st = int(get(c, "start"))
        if st < lb_ms:
            continue
        if st > end_ms:
            break
        rows.append([
            st,
            float(get(c, "open")),
            float(get(c, "high")),
            float(get(c, "low")),
            float(get(c, "close")),
        ])
    return rows  # 확장된 범위(룩백+전날)

def _ma_from_series(values, window: int):
    """단순이동평균. values 길이와 동일한 리스트 반환(None으로 시작)."""
    n = len(values)
    ma = [None] * n
    if window <= 0 or n < window:
        return ma
    csum = 0.0
    for i, v in enumerate(values):
        csum += v
        if i >= window:
            csum -= values[i - window]
        if i >= window - 1:
            ma[i] = csum / window
    return ma


def _prev_day_window_ms(tz_name: str = _TZ):
    tz = ZoneInfo(tz_name)
    now_local = datetime.now(tz)
    prev_day = (now_local - timedelta(days=1)).date()
    start_local = datetime(prev_day.year, prev_day.month, prev_day.day, 0, 0, 0, tzinfo=tz)
    end_local   = start_local + timedelta(days=1)
    start_ms = int(start_local.timestamp() * 1000)
    end_ms   = int(end_local.timestamp() * 1000) - 1
    return prev_day, start_ms, end_ms

def _slice_from_cache(candles_deque, start_ms: int, end_ms: int):
    if not candles_deque:
        return []
    def get(c, key):
        return c.get(key) if isinstance(c, dict) else getattr(c, key)

    rows = []
    # 오래된→최신(오름차순) 가정. 아니라면 정렬이 필요함.
    for c in candles_deque:
        st = int(get(c, "start"))
        if st < start_ms:
            continue
        if st > end_ms:
            break
        rows.append([st,
                     float(get(c, "open")),
                     float(get(c, "high")),
                     float(get(c, "low")),
                     float(get(c, "close"))])
    return rows

def _render_candles_png(rows_window, ma_window_vals, title=""):
    """
    rows_window: 전날 00:00~24:00 구간만 (오름차순)
    ma_window_vals: rows_window와 1:1 정렬된 MA 값 리스트(일부 None 가능)
    """
    if not rows_window:
        raise ValueError("빈 데이터")

    tz = ZoneInfo(_TZ)
    times  = [datetime.fromtimestamp(r[0]/1000, tz=tz) for r in rows_window]
    opens  = [r[1] for r in rows_window]
    highs  = [r[2] for r in rows_window]
    lows   = [r[3] for r in rows_window]
    closes = [r[4] for r in rows_window]

    fig = plt.figure(figsize=(16, 6), dpi=150)
    ax = plt.gca()
    ax.set_title(title)
    ax.grid(True, linestyle="--", alpha=0.3)

    xs = list(range(len(rows_window)))
    width = 0.6
    up_color   = "#26a69a"
    down_color = "#ef5350"

    # --- 캔들(심지+몸통)
    for i in range(len(rows_window)):
        o, h, l, c = opens[i], highs[i], lows[i], closes[i]
        up = c >= o
        color = up_color if up else down_color

        ax.vlines(xs[i], l, h, color=color, linewidth=0.8)
        bottom = min(o, c)
        height = max(abs(c - o), 1e-9)
        rect = Rectangle((xs[i] - width/2, bottom), width, height,
                         facecolor=color, edgecolor=color, linewidth=0.6)
        ax.add_patch(rect)

    # --- MA 라인(주황)
    if any(v is not None for v in ma_window_vals):
        seg_x, seg_y = [], []
        for i, v in enumerate(ma_window_vals):
            if v is None:
                if seg_x:
                    ax.plot(seg_x, seg_y, color="orange", linewidth=1.8, label=f"MA100")
                    seg_x, seg_y = [], []
                continue
            seg_x.append(xs[i]); seg_y.append(v)
        if seg_x:
            ax.plot(seg_x, seg_y, color="orange", linewidth=1.8, label=f"MA100")
        ax.legend(loc="upper left")

    # --- MA100으로부터 가장 멀리 떨어진 고점/저점 찾기 (퍼센트)
    far_up = (-1, -1.0)   # (idx, pct)
    far_dn = (-1, -1.0)
    for i, ma in enumerate(ma_window_vals):
        if ma is None or ma <= 0:
            continue
        up_pct = (highs[i] - ma) / ma
        dn_pct = (ma - lows[i]) / ma
        if up_pct > far_up[1]:
            far_up = (i, up_pct)
        if dn_pct > far_dn[1]:
            far_dn = (i, dn_pct)

    def _annotate(i, pct, y, color, label):
        if i < 0:
            return
        t = times[i].strftime("%H:%M:%S")
        txt = f"{label} {pct*100:.2f}% {t}"
        ax.scatter([xs[i]], [y], s=36, color=color, edgecolor="white", linewidth=0.8, zorder=5)
        ax.annotate(txt, (xs[i], y),
                    textcoords="offset points", xytext=(6, 8),
                    fontsize=9, color=color, weight="bold",
                    bbox=dict(boxstyle="round,pad=0.2", fc="white", ec=color, lw=0.8, alpha=0.85))
    # 위/아래 마킹
    if far_up[0] >= 0:
        _annotate(far_up[0], far_up[1], highs[far_up[0]], "#2962ff", "↑max")
    if far_dn[0] >= 0:
        _annotate(far_dn[0], far_dn[1], lows[far_dn[0]],  "#d32f2f", "↓max")

    # 축/라벨
    step = max(1, len(rows_window)//12)
    ax.set_xticks(xs[::step], [times[i].strftime("%H:%M") for i in range(0, len(rows_window), step)])
    ax.set_xlim(-1, len(rows_window))
    ymin, ymax = min(lows), max(highs)
    pad = (ymax - ymin) * 0.02 if ymax > ymin else 1
    ax.set_ylim(ymin - pad, ymax + pad)

    buf = io.BytesIO()
    plt.tight_layout()
    plt.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()



def _send_telegram_photo(token: str, chat_id: str | int, png_bytes: bytes, caption: str = ""):
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    files = {"photo": ("report.png", png_bytes, "image/png")}
    data = {"chat_id": str(chat_id), "caption": caption}
    resp = requests.post(url, data=data, files=files, timeout=20)
    resp.raise_for_status()
    return resp.json()

def run_daily_report_from_cache(get_bot, symbol="BTCUSDT", logger=None):
    bot = get_bot()
    if bot is None:
        raise RuntimeError("bot 인스턴스가 없습니다.")
    candles_deque = getattr(bot, "candles", None) or getattr(bot, "candels", None)
    if candles_deque is None:
        raise RuntimeError("bot.candles(또는 bot.candels)가 없습니다.")

    prev_day, start_ms, end_ms = _prev_day_window_ms(_TZ)

    MA_WIN = 100
    LOOKBACK_MIN = MA_WIN - 1  # 99분

    # 1) 전날+룩백 범위 가져오기
    rows_ext = _slice_from_cache_with_lookback(
        candles_deque, start_ms, end_ms, lookback_minutes=LOOKBACK_MIN
    )
    if not rows_ext:
        raise RuntimeError("전날 데이터가 캐시에 없습니다. (서버 재시작/누락 가능)")

    # 2) 확장 범위에서 MA100 계산
    closes_ext = [r[4] for r in rows_ext]
    ma_ext = _ma_from_series(closes_ext, MA_WIN)

    # 3) 확장 범위 중 전날 구간만 다시 선택(렌더용 배열과 MA를 1:1 정렬)
    rows_win, ma_win = [], []
    for r, ma in zip(rows_ext, ma_ext):
        if start_ms <= r[0] <= end_ms:
            rows_win.append(r)
            ma_win.append(ma)

    if not rows_win:
        raise RuntimeError("전날 구간 슬라이스 결과가 비었습니다.")

    title = f"{symbol} • {prev_day.strftime('%Y-%m-%d')} 1m Candles (KST) — {len(rows_win)} bars"
    png = _render_candles_png(rows_win, ma_win, title=title)

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 미설정")

    _send_telegram_photo(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, png, caption=title)
    if logger:
        logger.info("✅ 일일 리포트 전송 완료(캐시 사용, MA100 포함)")
    return {"ok": True, "count": len(rows_win)}


def init_daily_report_scheduler(get_bot, logger=None, hour=6, minute=50, tz=_TZ, symbol="BTCUSDT"):
    """
    FastAPI startup에서 호출:
        scheduler = init_daily_report_scheduler(lambda: bot, logger=error_logger)
    """
    global _scheduler
    _scheduler = AsyncIOScheduler(timezone=tz)
    trigger = CronTrigger(hour=hour, minute=minute, timezone=tz)
    _scheduler.add_job(lambda: run_daily_report_from_cache(get_bot, symbol=symbol, logger=logger),
                       trigger, id="daily_report", replace_existing=True)
    _scheduler.start()
    if logger:
        logger.debug(f"🕖 APScheduler 등록: 매일 {hour:02d}:{minute:02d} 리포트(캐시)")
    return _scheduler
