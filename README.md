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

## License
Private / Internal
