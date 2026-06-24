import logging, os, json, html, requests
from pathlib import Path

import time
from collections import defaultdict

class _TelegramRateLimiter:
    def __init__(self, cooldown_sec: float = 1.0):
        self.cooldown_sec = cooldown_sec
        self._last_sent = defaultdict(float)

    def allow(self, key: str) -> bool:
        now = time.time()
        last = self._last_sent[key]
        if now - last < self.cooldown_sec:
            return False
        self._last_sent[key] = now
        return True

class OnlySIG(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            return isinstance(msg, str) and msg.lstrip().startswith("SIG ")
        except Exception:
            return False

class ExcludeSIG(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            return not (isinstance(msg, str) and msg.lstrip().startswith("SIG "))
        except Exception:
            return True

class SigOrWarning(logging.Filter):
    """텔레그램으로 보낼 것만 통과: 매매신호(SIG ...) / 체결확인(executor 거래 로거) / WARNING+.

    그 외 잡다한 INFO/DEBUG 로그가 텔레그램으로 새는 것을 막는다.
    ('both' 모드가 필터 없이 모든 INFO+를 보내던 게 텔레그램 폭주의 구조적 원인이었음)
    단, executor의 체결 확인(local_executor_trade: '⊕ 진입 완료' 등)은 INFO지만
    실거래 알림이라 반드시 통과시킨다(SIG 접두어가 없어 예전엔 같이 막히던 버그 수정).
    """
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if record.levelno >= logging.WARNING:
                return True
            if record.name == "local_executor_trade":  # executor 체결 확인 → 통과
                return True
            msg = record.getMessage()
            return isinstance(msg, str) and msg.lstrip().startswith("SIG ")
        except Exception:
            return False

def send_telegram_message(bot_token: str, chat_id: str, message: str):
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    requests.post(
        url,
        data={"chat_id": chat_id, "text": message},   # ✅ parse_mode 제거
        timeout=10,
    ).raise_for_status()

def _guess_dp_from_price(px, min_dp=1, max_dp=4):
    try:
        s = f"{float(px):.10f}".rstrip("0").rstrip(".")
        if "." not in s:
            return min_dp
        dp = len(s.split(".", 1)[1])
        return max(min_dp, min(max_dp, dp))
    except Exception:
        return min_dp

class TelegramLogHandler(logging.Handler):
    def __init__(self, bot_token: str, chat_id: str, level=logging.WARNING):
        super().__init__(level)
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._rl = _TelegramRateLimiter(cooldown_sec=1.0)  # ✅ 추가

    def emit(self, record):
        try:
            msg = record.getMessage()
            if isinstance(msg, str) and msg.lstrip().startswith("SIG "):
                try:
                    obj = json.loads(msg.split(" ", 1)[1])

                    symbol = obj.get("symbol")
                    kind = obj.get("kind")
                    side = obj.get("side")

                    price = obj.get("price")
                    ma100 = obj.get("ma100")
                    d_pct = obj.get("ma_delta_pct") or 0
                    pnl_pct = obj.get("pnl_pct")
                    entry_price = obj.get("entry_price")

                    dp = _guess_dp_from_price(price, min_dp=1, max_dp=4)

                    # ✅ reasons[0] 추출
                    reasons = obj.get("reasons") or []
                    reason0 = ""
                    if isinstance(reasons, list) and reasons:
                        reason0 = str(reasons[0])
                    elif isinstance(reasons, str) and reasons:
                        reason0 = reasons.split(",")[0].strip()

                    # ✅ 플랫폼/엔진(네임스페이스) 표시
                    engine = (obj.get("engine") or obj.get("namespace") or obj.get("source") or "").upper()
                    engine_tag = f"[{engine}]" if engine else ""

                    badge = "🟢" if side == "LONG" else "🔴"
                    title = "진입" if kind == "ENTRY" else "청산"
                    side_kr = "롱" if side == "LONG" else "숏"

                    # 값 포맷 안전화
                    def _fmt1(x, dp=1):
                        try:
                            return f"{float(x):,.{int(dp)}f}"
                        except Exception:
                            return "N/A"

                    pct_txt = "N/A"
                    try:
                        pct_txt = f"{float(d_pct):+.2f}%"
                    except Exception:
                        pass

                    # ✅ 1줄: 헤드라인(짧게)
                    headline = f"{badge} {engine_tag}[{symbol}] {side_kr}{title}신호"

                    # ✅ 2줄: (reason) (PNL ...)
                    line_reason = ""
                    if reason0:
                        line_reason += f"({reason0})"

                    if kind == "EXIT":
                        try:
                            if pnl_pct is not None and pnl_pct != "":
                                line_reason += f" (PNL {float(pnl_pct):+.2f}%)"
                        except Exception:
                            pass

                    # ✅ 3줄: p/M100/Δ
                    line_stats = (
                        f"p: {_fmt1(price, dp)}  "
                        f"M100: {_fmt1(ma100, dp)}  "
                        f"({pct_txt})"
                    )

                    # ✅ 4줄: entry (EXIT만)
                    entry_line = ""
                    if kind == "EXIT":
                        try:
                            if entry_price not in (None, "", 0, 0.0):
                                entry_line = f"entry: {_fmt1(entry_price, dp)}"
                        except Exception:
                            entry_line = ""

                    # ✅ 최종 조합
                    lines = [headline]
                    if line_reason:
                        lines.append(line_reason)
                    lines.append(line_stats)
                    if entry_line:
                        lines.append(entry_line)

                    text = "\n".join(lines)

                    sig_id = obj.get("signal_id") or ""
                    key = f"SIG:{symbol}:{kind}:{side}:{sig_id}"
                    if not self._rl.allow(key):
                        return

                    send_telegram_message(self.bot_token, self.chat_id, text)
                    return
                except Exception as e:
                    print(f"[Telegram prettify failed] {e} | raw={msg}")

            key = f"LOG:{record.levelname}"
            if not self._rl.allow(key):
                return

            send_telegram_message(self.bot_token, self.chat_id, self.format(record))
        except Exception as e:
            print(f"TelegramLogHandler Error: {e}")

def _project_root(start_file: str = __file__) -> Path:
    """파일 위치에서 위로 올라가며 프로젝트 루트를 추정(.git/pyproject/requirements 기준)."""
    p = Path(start_file).resolve()
    for parent in [p.parent] + list(p.parents):
        if (parent / ".git").exists() or (parent / "pyproject.toml").exists() or (parent / "requirements.txt").exists():
            return parent
    return p.parents[1]  # fallback: 파일 기준 한 단계 위

def setup_logger(
    logger_name: str,
    *,
    logger_level: int = logging.DEBUG,
    console_level=logging.DEBUG,
    file_level=logging.INFO,
    enable_telegram: bool = True,
    telegram_level: int = logging.INFO,
    write_signals_file: bool = False,             # ✅ trading 전용으로만 True
    signals_filename: str = "signals.jsonl",

    # ✅ 추가: 호출부에서 주입
    telegram_bot_token: str | None = None,
    telegram_chat_id: str | None = None,

    exclude_sig_in_file: bool = True,           # ✅ 추가
    telegram_mode: str = "sig_only",            # ✅ 추가: 'sig_only' | 'human_only' | 'both'
) -> logging.Logger:
    root = _project_root(__file__)
    log_dir = root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(logger_name)
    logger.setLevel(logger_level)
    logger.propagate = False                     # ✅ 상위 전파 차단

    # ✅ 중복 방지: 기존 핸들러 제거
    for h in list(logger.handlers):
        logger.removeHandler(h)

    # 포맷
    human_fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(name)s - %(message)s",
                                  datefmt="%Y-%m-%d %H:%M:%S")

    # 콘솔
    ch = logging.StreamHandler()
    ch.setLevel(console_level)
    ch.setFormatter(human_fmt)
    logger.addHandler(ch)

    # 파일(사람용): SIG 제외
    fh = logging.FileHandler(log_dir / f"{logger_name}.log", encoding="utf-8")
    fh.setLevel(file_level)
    fh.setFormatter(human_fmt)
    if exclude_sig_in_file:  # ✅ 옵션에 따라 SIG 제외
        fh.addFilter(ExcludeSIG())
    logger.addHandler(fh)

    # 파일(기계용): 이 로거에만 signals.jsonl 사용하고 싶을 때
    if write_signals_file:
        fh_sig = logging.FileHandler(log_dir / signals_filename, encoding="utf-8")
        fh_sig.setLevel(logging.INFO)
        fh_sig.setFormatter(logging.Formatter("%(message)s"))  # JSON 그대로
        fh_sig.addFilter(OnlySIG())
        logger.addHandler(fh_sig)

    # 텔레그램
    if enable_telegram:
        bot = telegram_bot_token
        chat = telegram_chat_id

        if bot and chat:
            th = TelegramLogHandler(bot, chat, level=telegram_level)
            th.setFormatter(logging.Formatter("%(message)s"))
            if telegram_mode == "sig_only":
                th.addFilter(OnlySIG())
            elif telegram_mode == "both":
                # 매매신호(SIG) + 운영경보(WARNING+)만. 잡 INFO 누수 차단.
                th.addFilter(SigOrWarning())
            logger.addHandler(th)

    return logger
