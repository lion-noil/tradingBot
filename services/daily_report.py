# services/daily_report.py
import os
import io
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
import json  # (신호 파싱 시 필요)

import matplotlib
matplotlib.use("Agg")  # 서버(헤드리스) 렌더링
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from matplotlib import font_manager, rcParams

from matplotlib import gridspec

def _draw_footer(ax_footer, footer_lines: list[str], *, max_rows_per_col=14):
    """
    ax_footer: 아래쪽 빈 축(axes). 눈금/프레임 없음.
    footer_lines: "1) 07:57:48  숏 진입 ..." 같은 문자열 리스트.
    max_rows_per_col: 한 컬럼에 표시할 최대 줄 수.
    """
    ax_footer.axis("off")

    if not footer_lines:
        return

    # 컬럼 수 결정 (줄이 많으면 2~3 컬럼으로 자동 분할)
    if len(footer_lines) <= max_rows_per_col:
        ncols = 1
    elif len(footer_lines) <= max_rows_per_col * 2:
        ncols = 2
    else:
        ncols = 3

    rows = (len(footer_lines) + ncols - 1) // ncols
    columns = []
    for c in range(ncols):
        chunk = footer_lines[c*rows:(c+1)*rows]
        columns.append("\n".join(chunk))

    # 각 컬럼을 좌→우로 배치
    left_margin = 0.01
    col_w = (1.0 - left_margin*2) / ncols
    for i, col_text in enumerate(columns):
        x = left_margin + i * col_w
        ax_footer.text(
            x, 0.98, col_text,
            transform=ax_footer.transAxes,
            ha="left", va="top", fontsize=9,
            bbox=dict(boxstyle="round,pad=0.35", fc="white", ec="#888", alpha=0.9)
        )


def set_korean_font(
    preferred_families=(
        "Noto Sans CJK KR",   # Linux/Mac에 잘 깔림
        "NanumGothic",        # Linux: fonts-nanum
        "Malgun Gothic",      # Windows
        "Apple SD Gothic Neo" # macOS
    ),
    local_files=(
        # 프로젝트 안에 두면 가장 확실
        "assets/fonts/NotoSansKR-Regular.otf",
        "assets/fonts/NanumGothic.ttf",
    )
):
    rcParams["axes.unicode_minus"] = False  # '−' 깨짐 방지

    # 1) 시스템에 설치된 폰트 목록
    installed = {f.name for f in font_manager.fontManager.ttflist}
    for name in preferred_families:
        if name in installed:
            rcParams["font.family"] = [name]
            rcParams["font.sans-serif"] = [name]
            return name

    # 2) 로컬 파일(프로젝트 포함) 시도
    candidates = [os.getenv("KOREAN_FONT_PATH")] + list(local_files)
    for p in [c for c in candidates if c]:
        p = str((Path(__file__).resolve().parents[2] / p) if not os.path.isabs(p) else p)
        if os.path.exists(p):
            try:
                font_manager.fontManager.addfont(p)
                fp = font_manager.FontProperties(fname=p)
                fname = fp.get_name() or "sans-serif"
                rcParams["font.family"] = [fname]
                rcParams["font.sans-serif"] = [fname]
                return fname
            except Exception:
                pass

    # 3) 최종 폴백
    rcParams["font.family"] = ["DejaVu Sans"]
    rcParams["font.sans-serif"] = ["DejaVu Sans"]
    return "DejaVu Sans"

# 한 번만 호출
_chosen = set_korean_font()


TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
_TZ = "Asia/Seoul"

_scheduler = None  # 모듈 레벨에 보관(가비지 콜렉션 방지)

def _side_kr(side: str) -> str:
    return "롱" if side == "LONG" else "숏"
def _reason_kr(sig: dict) -> str:
    rc = (sig.get("extra") or {}).get("reason_code")
    if rc == "MA_RETOUCH_LONG":  return "MA100 재접근(롱)"
    if rc == "MA_RETOUCH_SHORT": return "MA100 재접근(숏)"
    if rc == "TIME_LIMIT":       return "보유시간 초과"
    return (sig.get("reasons") or ["청산"])[0]

def _format_signal_label(sig: dict) -> str:
    kind = sig.get("kind")     # ENTRY/EXIT
    side = sig.get("side")     # LONG/SHORT
    price = sig.get("price")
    d = sig.get("ma_delta_pct")
    mom = sig.get("momentum_pct")

    d_str = f"{d * 100:+.2f}%" if isinstance(d, (int, float)) else "N/A"
    mom_str = f"{mom * 100:+.2f}%" if isinstance(mom, (int, float)) else "N/A"

    if kind == "ENTRY":
        line1 = f"{_side_kr(side)} 진입 • {price:,.2f}"
        line2 = f"Δ {d_str} • 모멘텀 {mom_str}" if mom is not None else f"Δ {d_str}"
        return f"{line1}\n{line2}"
    else:
        line1 = f"{_side_kr(side)} 청산 • {price:,.2f}"
        line2 = f"이유: {_reason_kr(sig)}"
        return f"{line1}\n{line2}"

def _marker_style(sig: dict):
    # 모양/색상 통일: EN=삼각형, EX=다이아
    kind = sig.get("kind"); side = sig.get("side")
    if kind == "ENTRY" and side == "LONG":
        return dict(marker="^", color="#2e7d32")   # 초록 ▲
    if kind == "ENTRY" and side == "SHORT":
        return dict(marker="v", color="#c62828")   # 빨강 ▼
    if kind == "EXIT":
        return dict(marker="D", color="#6a1b9a")   # 보라 ◆
    return dict(marker="o", color="gray")


def _load_signals_jsonl(path: str, symbol: str, start_ms: int, end_ms: int) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    out = []
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            # "SIG {...}" 형태도 허용
            if line.startswith("SIG "):
                line = line.split(" ", 1)[1]
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if obj.get("symbol") != symbol:
                continue
            ts = obj.get("ts")
            # ts가 ISO면 파싱해서 ms로, 이미 ms면 그대로
            if isinstance(ts, (int, float)):
                ms = int(ts)
            else:
                try:
                    # '2025-09-06T21:18:31.859180+09:00' 같은 ISO
                    ms = int(datetime.fromisoformat(ts).timestamp() * 1000)
                except Exception:
                    continue
            if ms < start_ms or ms > end_ms:
                continue
            obj["_ms"] = ms
            out.append(obj)
    # 시간순
    out.sort(key=lambda x: x["_ms"])
    return out


def _project_root(start_file: str = __file__) -> Path:
    """
    현재 파일 위치에서 위로 올라가며 프로젝트 루트를 추정.
    .git / pyproject.toml / requirements.txt 중 하나가 보이면 그 위치를 루트로 간주.
    """
    p = Path(start_file).resolve()
    for parent in [p.parent] + list(p.parents):
        if (parent / ".git").exists() or (parent / "pyproject.toml").exists() or (parent / "requirements.txt").exists():
            return parent
    # 그래도 못 찾으면 daily_report.py 기준으로 최상위 쪽으로 보정
    return p.parents[2]  # e.g. .../app/services/daily_report.py → 프로젝트 루트

def _resolve_signals_path(path_like: str) -> Path:
    """
    signals.jsonl의 '신뢰 가능한' 절대경로를 얻는다.
    - 절대경로면 그대로 사용
    - 상대경로면 '프로젝트 루트' 기준을 최우선으로, 없으면 CWD, 그다음 CWD의 부모도 시도
    - 최종적으로 존재하는 첫 후보를 반환. 전혀 없으면 루트 기준 경로를 반환(새로 만들 용도)
    """
    p = Path(path_like)
    if p.is_absolute():
        return p

    root = _project_root(__file__)
    candidates = [
        root / path_like,              # ✅ 우선: 프로젝트 루트/logs/signals.jsonl
        Path.cwd() / path_like,        #     : 현재 작업 디렉터리 기준
        Path.cwd().parent / path_like  #     : app/에서 실행했을 때 대비
    ]
    for c in candidates:
        if c.exists():
            return c
    # 없더라도 루트 기준 경로를 기본으로 돌려주고, 상위에서 생성해 사용
    return candidates[0]


# -------------------- 캔들 데이터 유틸 --------------------
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


# -------------------- 기간 계산: 06:50 컷오프 --------------------
def _prev_window_ms_by_cutoff(hour: int = 6, minute: int = 50, tz_name: str = _TZ):
    tz = ZoneInfo(tz_name)
    now_local = datetime.now(tz)

    cutoff_today = datetime(now_local.year, now_local.month, now_local.day, hour, minute, tzinfo=tz)
    # 스케줄이 정확히 06:50에 돈다고 가정하지만, 안전하게 now가 컷오프 이전일 수도 있게 처리
    end_local = cutoff_today if now_local >= cutoff_today else (cutoff_today - timedelta(days=1))
    start_local = end_local - timedelta(days=1)

    # 자정(날짜 변경선) 위치 (윈도우 내부의 00:00)
    midnight_local = datetime(end_local.year, end_local.month, end_local.day, 0, 0, 0, tzinfo=tz)

    start_ms = int(start_local.timestamp() * 1000)
    # end_ms 포함 비교(<=) 하므로 1ms 빼서 포함구간 유지
    end_ms   = int(end_local.timestamp() * 1000) - 1
    midnight_ms = int(midnight_local.timestamp() * 1000)

    # 타이틀 표기를 위해 기간 문자열 반환
    period_label = f"{start_local.strftime('%Y-%m-%d %H:%M')} → {end_local.strftime('%Y-%m-%d %H:%M')}"

    return (start_local.date(), end_local.date()), start_ms, end_ms, midnight_ms, period_label


# -------------------- 신호(JSONL) 로드 --------------------
def _iso_to_ms(s: str) -> int:
    s = s.replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    return int(dt.timestamp() * 1000)

def _load_signals(jsonl_path: str, symbol: str, start_ms: int, end_ms: int):
    """
    logs/signals.jsonl 파일에서 해당 심볼 + 시간창에 해당하는 SIG 라인을 파싱.
    반환: [{ts_ms, kind, side, price, ma100, ma_delta_pct, momentum_pct, thresholds}, ...] (시간순)
    """
    out = []
    try:
        with open(jsonl_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or not line.startswith("SIG "):
                    continue
                try:
                    obj = json.loads(line.split(" ", 1)[1])
                except Exception:
                    continue
                if obj.get("symbol") != symbol:
                    continue
                ts = obj.get("ts")
                if not ts:
                    continue
                ts_ms = _iso_to_ms(ts) if isinstance(ts, str) else int(ts)
                if ts_ms < start_ms or ts_ms > end_ms:
                    continue

                out.append({
                    "ts_ms": ts_ms,
                    "kind": obj.get("kind"),
                    "side": obj.get("side"),
                    "price": obj.get("price"),
                    "ma100": obj.get("ma100"),
                    "ma_delta_pct": obj.get("ma_delta_pct"),
                    "momentum_pct": obj.get("momentum_pct"),
                    "thresholds": obj.get("thresholds", {}) or {},
                    "reasons": obj.get("reasons") or [],
                    "extra": obj.get("extra") or {},
                })
    except FileNotFoundError:
        # 신호 파일이 아직 없을 수 있음
        pass
    # 시간순 정렬
    out.sort(key=lambda x: x["ts_ms"])
    return out


# -------------------- 렌더링 --------------------

# ─── 기존 정의를 아래로 교체 ───────────────────────────────────────────
def _annotate_signals(ax, rows_window, signals: list[dict], tz: ZoneInfo,
                      ma_series: list[float] | None = None):
    if not signals:
        return
    import numpy as np
    ts_list = [r[0] for r in rows_window]
    highs   = [r[2] for r in rows_window]
    lows    = [r[3] for r in rows_window]
    ymin, ymax = min(lows), max(highs)
    yrange = max(1e-6, ymax - ymin)

    def nearest_idx(ms):
        return min(range(len(ts_list)), key=lambda i: abs(ts_list[i] - ms))

    from collections import defaultdict
    clusters = defaultdict(list)

    for sig in signals:
        ms = sig.get("_ms") or sig.get("ts_ms")
        x  = nearest_idx(ms)

        side = sig.get("side")
        kind = sig.get("kind")

        # 기본 앵커: 진입은 side 방향 extremum,
        #           청산은 MA 가 유효하면 MA, 아니면 extremum
        ma_val = None
        if ma_series and 0 <= x < len(ma_series):
            mv = ma_series[x]
            if isinstance(mv, (int, float)) and mv > 0:
                ma_val = mv

        if kind == "EXIT" and ma_val is not None:
            y_anchor = ma_val
        else:
            y_anchor = lows[x] if side == "LONG" else highs[x]

        clusters[x].append((x, y_anchor, sig))

    base_gap = yrange * 0.03
    lane_gap = yrange * 0.05
    dx_pattern = [0.0, 1.2, -1.2, 2.4, -2.4, 3.6, -3.6]
    max_labels_per_cluster = 12
    fs = 8

    for x, items in clusters.items():
        above = [it for it in items if it[2].get("side") == "SHORT"]
        below = [it for it in items if it[2].get("side") == "LONG"]

        def _place(group, place_above: bool):
            lanes_used = 0
            for i, (xx, y_anchor, sig) in enumerate(group[:max_labels_per_cluster]):
                lane = lanes_used; lanes_used += 1
                sign = +1 if place_above else -1
                y_text = y_anchor + sign * (base_gap + lane * lane_gap)
                dx = dx_pattern[i % len(dx_pattern)]
                xx_text = float(np.clip(xx + dx, 0, len(rows_window) - 1))

                style = _marker_style(sig)
                n = sig.get("_idx", "?")

                ax.scatter([xx], [y_anchor], s=34, color=style["color"],
                           marker=style["marker"], zorder=6,
                           edgecolor="white", linewidth=0.6)

                ax.annotate(
                    f"{n}",
                    xy=(xx, y_anchor), xycoords="data",
                    xytext=(xx_text, y_text), textcoords="data",
                    ha="center", va="bottom" if place_above else "top",
                    fontsize=fs, color=style["color"], zorder=7,
                    bbox=dict(boxstyle="round,pad=0.2", fc="white",
                              ec=style["color"], lw=0.9, alpha=0.95),
                    arrowprops=dict(arrowstyle="-", lw=0.8,
                                    color=style["color"], alpha=0.9)
                )

        _place(above, True)
        _place(below, False)





def _render_candles_png(rows_window, ma_window_vals, title="",
                        vlines_ms: list[int] | None = None,
                        signals: list[dict] | None = None):
    if not rows_window:
        raise ValueError("빈 데이터")

    tz = ZoneInfo(_TZ)
    times  = [datetime.fromtimestamp(r[0]/1000, tz=tz) for r in rows_window]
    opens  = [r[1] for r in rows_window]
    highs  = [r[2] for r in rows_window]
    lows   = [r[3] for r in rows_window]
    closes = [r[4] for r in rows_window]
    xs = list(range(len(rows_window)))

    # ── 상/하 2분할: 위(차트), 아래(설명)
    fig, (ax, ax_footer) = plt.subplots(
        2, 1, figsize=(16, 7), dpi=150,
        gridspec_kw={"height_ratios": [5, 1]}
    )
    plt.subplots_adjust(hspace=0.06)  # 축 간 간격만 살짝

    # ===== 상단: 차트 =====
    ax.set_title(title)
    ax.grid(True, linestyle="--", alpha=0.3)

    width = 0.6
    up_color   = "#26a69a"
    down_color = "#ef5350"

    # 캔들
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

    # MA100
    if any(v is not None for v in ma_window_vals):
        seg_x, seg_y = [], []
        for i, v in enumerate(ma_window_vals):
            if v is None:
                if seg_x:
                    ax.plot(seg_x, seg_y, color="orange", linewidth=1.8, label="MA100")
                    seg_x, seg_y = [], []
                continue
            seg_x.append(xs[i]); seg_y.append(v)
        if seg_x:
            ax.plot(seg_x, seg_y, color="orange", linewidth=1.8, label="MA100")
        ax.legend(loc="upper left")

    # 축/라벨 (패딩 넉넉히)
    ymin, ymax = min(lows), max(highs)
    pad = (ymax - ymin) * 0.04 if ymax > ymin else 1
    ax.set_xlim(-1, len(rows_window))
    ax.set_ylim(ymin - pad, ymax + pad)
    step = max(1, len(rows_window)//12)
    ax.set_xticks(xs[::step], [times[i].strftime("%H:%M") for i in range(0, len(rows_window), step)])

    # 자정 점선(축 범위 세팅 뒤에)
    if vlines_ms:
        ts_list = [r[0] for r in rows_window]
        for ms in vlines_ms:
            if ts_list[0] <= ms <= ts_list[-1]:
                idx = min(range(len(ts_list)), key=lambda i: abs(ts_list[i] - ms))
                ax.axvline(idx, linestyle="--", linewidth=1.0, color="gray", alpha=0.8)
                ax.text(idx, ymin, "00:00", fontsize=8, color="gray",
                        ha="center", va="bottom", rotation=0, alpha=0.8)

    # MA 대비 ↑max / ↓min
    i_up = i_dn = -1
    up_best = dn_best = -1.0
    for i, ma in enumerate(ma_window_vals):
        if not (isinstance(ma, (int, float)) and ma > 0):
            continue
        up_pct = (highs[i] - ma) / ma
        dn_pct = (ma - lows[i]) / ma
        if up_pct > up_best:
            up_best, i_up = up_pct, i
        if dn_pct > dn_best:
            dn_best, i_dn = dn_pct, i

    def _annotate_extreme(i, pct, y, color, label):
        if i < 0:
            return
        t = times[i].strftime("%H:%M:%S")
        txt = f"{label} {pct*100:.2f}% {t}"
        ax.scatter([xs[i]], [y], s=40, color=color, edgecolor="white",
                   linewidth=0.8, zorder=9, clip_on=False)
        ax.annotate(
            txt, (xs[i], y),
            textcoords="offset points",
            xytext=(6, 10 if y == highs[i] else -18),
            ha="left", va="bottom" if y == highs[i] else "top",
            fontsize=9, color=color, weight="bold",
            bbox=dict(boxstyle="round,pad=0.25", fc="white", ec=color, lw=0.8, alpha=0.9),
            zorder=9, clip_on=False
        )

    if i_up >= 0: _annotate_extreme(i_up, up_best, highs[i_up], "#2962ff", "↑ max")
    if i_dn >= 0: _annotate_extreme(i_dn, dn_best, lows[i_dn],  "#d32f2f", "↓ min")

    # 신호(차트엔 번호만)
    footer_lines = []
    if signals:
        numbered = _enumerate_signals(signals)
        _annotate_signals(ax, rows_window, numbered, tz, ma_series=ma_window_vals)
        for s in numbered:
            footer_lines.append(_signal_footer_line(s))

    # ===== 하단: 설명 =====
    _draw_footer(ax_footer, footer_lines, max_rows_per_col=14)

    # 저장
    buf = io.BytesIO()
    plt.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()




# -------------------- 전송 --------------------
def _send_telegram_photo(token: str, chat_id: str | int, png_bytes: bytes, caption: str = ""):
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    files = {"photo": ("report.png", png_bytes, "image/png")}
    data = {"chat_id": str(chat_id), "caption": caption}
    resp = requests.post(url, data=data, files=files, timeout=20)
    resp.raise_for_status()
    return resp.json()


# -------------------- 메인 엔트리 --------------------
def run_daily_report_from_cache(get_bot, symbol="BTCUSDT", system_logger=None,
                               signals_path: str = "logs/signals.jsonl"):
    bot = get_bot()
    if bot is None:
        raise RuntimeError("bot 인스턴스가 없습니다.")
    candles_deque = getattr(bot, "candles", None) or getattr(bot, "candels", None)
    if candles_deque is None:
        raise RuntimeError("bot.candles(또는 bot.candels)가 없습니다.")

    (_, _), start_ms, end_ms, midnight_ms, period_label = _prev_window_ms_by_cutoff(6, 50, _TZ)

    signals_abs = _resolve_signals_path(signals_path)

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

    # 4) 신호 로드(+ 윈도우 필터)
    signals = _load_signals(str(signals_abs), symbol, start_ms, end_ms)

    title = f"{symbol} • {period_label} (KST) — 1m"
    png = _render_candles_png(
        rows_win, ma_win, title=title,
        vlines_ms=[midnight_ms],
        signals=signals  # ← 신호 전달
    )

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 미설정")

    _send_telegram_photo(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, png, caption=title)
    if system_logger:
        system_logger.info("✅ 일일 리포트 전송 완료(캐시+MA100+신호 마킹)")
    return {"ok": True, "count": len(rows_win), "signals": len(signals)}



def init_daily_report_scheduler(get_bot, system_logger=None, hour=6, minute=50, tz=_TZ, symbol="BTCUSDT"):

    global _scheduler
    _scheduler = AsyncIOScheduler(timezone=tz)
    trigger = CronTrigger(hour=hour, minute=minute, timezone=tz)
    _scheduler.add_job(lambda: run_daily_report_from_cache(get_bot, symbol=symbol, logger=system_logger),
                       trigger, id="daily_report", replace_existing=True)
    _scheduler.start()
    if system_logger:
        system_logger.debug(f"🕖 APScheduler 등록: 매일 {hour:02d}:{minute:02d} 리포트(캐시)")
    return _scheduler

# --- 번호 붙이기 / 라벨 포맷 ---
def _enumerate_signals(signals: list[dict]) -> list[dict]:
    """시간순으로 1..N 번호를 매겨 반환(_idx 추가)."""
    sigs = sorted(signals, key=lambda s: s.get("_ms") or s.get("ts_ms") or 0)
    for i, s in enumerate(sigs, start=1):
        s["_idx"] = i
    return sigs

def _signal_footer_line(sig: dict) -> str:
    """아래쪽 설명용 한 줄 텍스트."""
    n    = sig.get("_idx")
    kind = sig.get("kind")     # ENTRY / EXIT
    side = sig.get("side")     # LONG / SHORT
    px   = sig.get("price")
    d    = sig.get("ma_delta_pct")
    mom  = sig.get("momentum_pct")
    ts   = sig.get("ts") or sig.get("_ms")

    side_kr = "롱" if side == "LONG" else "숏"
    if kind == "ENTRY":
        # 예: ① 07:57:48  숏 진입 111,369.90  (Δ +0.70%, 모멘텀 +0.23%)
        d_txt  = f"Δ {d*100:+.2f}%" if d is not None else "Δ N/A"
        m_txt  = f", 모멘텀 {mom*100:+.2f}%" if mom is not None else ""
        t_txt  = (datetime.fromisoformat(ts).strftime("%H:%M:%S")
                  if isinstance(ts, str) else "")
        return f"{n:>2}) {t_txt}  {side_kr} 진입 {px:,.2f}  ({d_txt}{m_txt})"
    else:
        # EXIT: 이유 한글화
        reason = _reason_kr(sig)
        t_txt  = (datetime.fromisoformat(ts).strftime("%H:%M:%S")
                  if isinstance(ts, str) else "")
        return f"{n:>2}) {t_txt}  {side_kr} 청산 {px:,.2f}  (이유: {reason})"

