# Trading Bot

Redis 기반 시그널(신호) 스트림을 생성하고, 별도 클라이언트(주문 실행기)들이 이를 구독해서 각자 주문을 수행하는 구조의 트레이딩 봇 프로젝트입니다.

## 구성
- **Signal Generator (TradingBot)**: 시장 데이터/지표를 기반으로 매수/매도 신호를 생성해 Redis Stream에 기록
- **Clients (Executors)**: Redis Stream을 구독(XREAD/XGROUP)하여 신호를 받아 각자 거래소/브로커(MT5 등)에 주문 실행
- **State/Index**: 시그널, 랏(lot), 포지션 상태를 Redis 등에 저장/조회

## 주요 폴더(예시)
- `bots/` : 봇 실행 로직 및 전략
- `core/` : 캔들/지표/실행 엔진, Redis 클라이언트 등
- `controllers/` : MT5 등 외부 주문 실행 연동
- `strategies/` : 매매 전략/시그널 판단 로직

## 빠른 시작
1. (필수) Redis 실행
2. 봇 실행(신호 생성)
3. 클라이언트 실행(신호 구독 후 주문)

## 개발 메모
- 신호는 Redis Stream에 시간순으로 쌓이며, 클라이언트는 `XREAD` 또는 `XGROUP` 기반으로 구독할 수 있습니다.
- 재시작/재접속 시 “과거 신호 재처리” 여부는 **컨슈머 그룹/ACK 정책**으로 제어합니다.

## 📈 Strategy Summary (Trading View)

---

## 🟢 ENTRY

### INIT (첫 진입)

| 방향 | 조건 |
|------|------|
| LONG | (1) 가격 ≤ MA100 × (1 - ma_thr_eff) AND (2) 3분 하락 모멘텀 ≥ momentum_threshold |
| SHORT | (1) 가격 ≥ MA100 × (1 + ma_thr_eff) AND (2) 3분 상승 모멘텀 ≥ momentum_threshold |

→ MA100 기준 충분한 이탈 + 3분 모멘텀 동시 충족 시 진입

---

### SCALE IN (최대 4회)

| 방향 | 조건 |
|------|------|
| LONG | (1) 직전 진입가보다 낮음 AND (2) 3분 하락 모멘텀 ≥ momentum_threshold AND (3) 가격 ≤ MA100 × (1 - ma_thr_eff / 2) |
| SHORT | (1) 직전 진입가보다 높음 AND (2) 3분 상승 모멘텀 ≥ momentum_threshold AND (3) 가격 ≥ MA100 × (1 + ma_thr_eff / 2) |

→ 불리한 방향으로 더 이탈 시 계단식 진입

---

## 🔴 EXIT

### 1️⃣ STOP LOSS (oldest 1개 기준)

| 방향 | 조건 |
|------|------|
| LONG | 가격 ≤ 진입가 × (1 - sl_pct) |
| SHORT | 가격 ≥ 진입가 × (1 + sl_pct) |

- sl_pct = ma_thr_eff × age_factor
- 즉, **진입 당시 MA 이탈폭만큼 추가로 밀리면 손절**
- 보유 시간이 길수록 age_factor 감소 → 손절폭 축소

---

### 2️⃣ TAKE PROFIT (oldest 1개 기준)

| 방향 | 조건 |
|------|------|
| LONG | 가격 ≥ 진입가 × (1 + tp_pct) |
| SHORT | 가격 ≤ 진입가 × (1 - tp_pct) |

- tp_pct = ma_thr_eff × age_factor
- **진입 당시 MA 이탈폭만큼 되돌리면 익절**

---

### 3️⃣ NORMAL (전량 청산)

| 방향 | 조건 |
|------|------|
| LONG | 가격 ≥ MA100 × (1 + ma_thr_eff) |
| SHORT | 가격 ≤ MA100 × (1 - ma_thr_eff) |

→ MA100 완전 복귀 시 전체 청산

---

### 4️⃣ SCALE OUT (부분 익절)

| 방향 | 조건 |
|------|------|
| LONG | 가격 ≥ MA100 × (1 + ma_thr_eff / 3) AND 3분 상승 모멘텀 ≥ momentum_threshold |
| SHORT | 가격 ≤ MA100 × (1 - ma_thr_eff / 3) AND 3분 하락 모멘텀 ≥ momentum_threshold |

→ MA 일부 복귀 구간에서 최근 진입 1개 정리

---

### 5️⃣ INIT OUT (1개 보유 시 빠른 탈출)

| 방향 | 조건 |
|------|------|
| LONG | 가격 ≥ MA100 × (1 + ma_thr_eff / 2) AND 3분 상승 모멘텀 ≥ momentum_threshold |
| SHORT | 가격 ≤ MA100 × (1 - ma_thr_eff / 2) AND 3분 하락 모멘텀 ≥ momentum_threshold |

→ 단일 포지션일 때 빠른 정리

---

## ⚙ 핵심 구조 요약

- 기준선: MA100
- 모멘텀: 3분 봉 기준 변화율
- 최대 진입: 4회
- 손절/익절: 진입가 기준 MA 이탈폭 기반
- 보유 시간이 길수록 SL/TP 폭 축소
- oldest 우선 청산 구조
- 부분청산 + 전량청산 병행

## License
Private / Internal
