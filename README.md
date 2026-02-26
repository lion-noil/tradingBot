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

### INIT2 / INIT3 (INIT 이후 15분 이내)

| 방향 | 조건 |
|------|------|
| LONG | INIT 이후 15분 이내 <br> INIT2: 가격 ≤ INIT_price × (1 - ma_thr_eff × 1) <br> INIT3: 가격 ≤ INIT_price × (1 - ma_thr_eff × 2) |
| SHORT | INIT 이후 15분 이내 <br> INIT2: 가격 ≥ INIT_price × (1 + ma_thr_eff × 1) <br> INIT3: 가격 ≥ INIT_price × (1 + ma_thr_eff × 2) |

→ INIT 기준 추가 이탈 시 빠른 확장 진입

---

### SCALE IN (최대 4회)

| 방향 | 조건 |
|------|------|
| LONG | (1) 직전 진입가보다 낮음 AND (2) 3분 하락 모멘텀 ≥ momentum_threshold AND (3) 가격 ≤ MA100 × (1 - ma_thr_eff / 2) |
| SHORT | (1) 직전 진입가보다 높음 AND (2) 3분 상승 모멘텀 ≥ momentum_threshold AND (3) 가격 ≥ MA100 × (1 + ma_thr_eff / 2) |

→ 불리한 방향으로 더 이탈 시 계단식 진입 (30분 쿨다운)

---

## 🔴 EXIT

---

### 1️⃣ STOP LOSS (oldest 1개 기준)

| 방향 | 조건 |
|------|------|
| LONG | 가격 ≤ 진입가 × (1 - sl_pct) |
| SHORT | 가격 ≥ 진입가 × (1 + sl_pct) |

- sl_pct = ma_thr_eff × age_factor
- 보유 시간이 길수록 age_factor 감소 → 손절폭 축소

#### 📌 Age Factor 정책

| 보유 시간 | age_factor |
|------------|------------|
| < 1h | 3.0 |
| 1 ~ 2h | 2.5 |
| 2 ~ 12h | 2.0 |
| 12 ~ 24h | 1.5 |
| > 24h | 1.0 |

---

### 2️⃣ TAKE PROFIT (oldest 1개 기준)

| 방향 | 조건 |
|------|------|
| LONG | 가격 ≥ 진입가 × (1 + tp_pct) |
| SHORT | 가격 ≤ 진입가 × (1 - tp_pct) |

- tp_pct = ma_thr_eff × age_factor
- 진입 당시 MA 이탈폭만큼 되돌리면 익절

---

### 3️⃣ RISK CONTROL (구조 정리)

| 조건 | 청산 |
|------|------|
| 3~4개 랏 보유 AND 평균진입가 대비 ±0.3% 유리 | 3개 → oldest 1개 <br> 4개 → 전체 청산 |

→ 다중 랏 위험 구간에서 빠른 리스크 축소

---

### 4️⃣ NORMAL (전량 청산)

| 방향 | 조건 |
|------|------|
| LONG | 가격 ≥ MA100 × (1 + ma_thr_eff) |
| SHORT | 가격 ≤ MA100 × (1 - ma_thr_eff) |

→ MA100 완전 복귀 시 전체 청산

---

### 5️⃣ SCALE OUT (부분 익절, newest 1개 청산)

| 방향 | 조건 |
|------|------|
| LONG | (1) 가격 ≥ 직전 랏 진입가 AND (2) 가격 ≥ MA100 × (1 + ma_thr_eff / 2) |
| SHORT | (1) 가격 ≤ 직전 랏 진입가 AND (2) 가격 ≤ MA100 × (1 - ma_thr_eff / 2) |

- 직전 랏(prev entry) 기준 회귀 확인
- 모멘텀 조건 없음
- scaleout_cooldown 적용

→ 최근 진입 물량부터 점진적 감량

---

### 6️⃣ INIT OUT (1개 보유 시 빠른 탈출)

| 방향 | 조건 |
|------|------|
| LONG | 가격 ≥ MA100 × (1 + ma_thr_eff / 2) AND 3분 상승 모멘텀 ≥ momentum_threshold |
| SHORT | 가격 ≤ MA100 × (1 - ma_thr_eff / 2) AND 3분 하락 모멘텀 ≥ momentum_threshold |

→ 단일 포지션일 때 빠른 정리

---

### 7️⃣ NEAR TOUCH (근접 청산)

| 조건 | 청산 |
|------|------|
| newest 보유시간 ≤ near_touch_window_sec AND MA100 근접 도달 | newest 1개 |

→ 최근 진입 물량의 빠른 경량화

---

## ⚙ 핵심 구조 요약

- 기준선: MA100
- 모멘텀: 3분 봉 변화율
- 최대 진입: 4회
- oldest 우선 청산 구조
- SL/TP = MA 이탈폭 기반 동적 조정
- 다중 랏 시 0.3% 회복 시 구조 정리
- 부분청산 + 전량청산 병행
- 평균회귀 기반 다단계 포지션 관리 전략

## License
Private / Internal
