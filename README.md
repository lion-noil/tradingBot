# tradingBot

Redis 기반 시그널 생성기와 주문 실행기를 분리한 자동매매 시스템.
시그널 봇(도커 컨테이너)들이 전략 신호를 만들어 실행기로 보내면, 계좌별 실행기가 주문·장부를 책임진다.

## 전략 체계 — 책 3개 × 유니버스 3개

| 책 | 주기 | 전략 패밀리 |
|---|---|---|
| **S11** | 1분봉 | z추세(S11) / z역추세(S12) / 급락페이드(S13) |
| **S22** | 4시간봉 | ewz추세(S14) / 역추세·유동성스윕(S15) 확장 |
| **S33** | 일봉 | 추세(S3) / 역추세(S4) |

유니버스: **크립토**(Bybit USDT 무기한) / **MT5**(지수·귀금속·에너지·크립토 CFD) / **환율**(FX 메이저).
셀(심볼×방향×파라미터) 정본은 `FINAL_PARAMS.md`(비공개 리서치 레포) — `bots/trade_config.py`가 라이브 반영본.

핵심 방법론: 무게이트 베이스 자립 + 상장 전기간 연도균형 검증, 수수료 포함 백테스트, 개별 캡 없음(유니버스 캡 200게임 + 사이징·유효레버리지 가드로 리스크 관리).

## 런타임 구성

```
[신호] signal-s11/s22/s33(+m)  ← 도커(WSL2 docker-ce), 심볼 틱/캔들 구독
   │      MT5 시세: mt5_server(별도 프로젝트, :9000 REST/WS)
   ▼ raw JSON-TCP
[실행] executor-a1  ← Bybit, 도커 (bridge, :9009)
       executor-a2  ← MT5, Windows 네이티브 (:9010, 터미널 IPC 직결)
   ▼
[장부] redis (로컬 자체호스팅, AOF, NTFS 볼륨)
       trading:{ns}:signal(스트림/해시) + signals(오픈 zset) + agent 랏 장부
```

- 감시: `infra/watchdog.py`(executor-a2 포트·도커 이상패턴 텔레그램 경보) + autoheal(하트비트 stale 컨테이너 자동재시작)
- 프론트: SRH(Upstash-REST 호환 게이트웨이, 읽기전용 ACL) → Vercel 대시보드

## 주요 방어 장치

- **개장대기 재시도**: 장닫힘(10018) 거절 시 진입·청산 모두 3분×10회 재시도(주초 개장갭·휴장 중 신호 유실 방지). EXIT 선도착 시 진입 재시도 취소.
- **최소 TP거리 게이트**(`min_tp_pct`): 저변동 레짐(주말 σ붕괴)에서 TP거리 < 왕복수수료 수준이면 진입 스킵 — Bybit 크립토 책 0.22%.
- **filling 모드 폴백**: 10030만 다음 모드 순회, 그 외 거절(10018/10006 등)은 진짜 사유 보존·중단.
- **피드 게이트**: 틱 stale 시 신호 처리 보류(휴장 오탐 방지), 최소주문 스케일업(mult≤16), 체결수량 기반 랏 기록 + 더스트 스윕.
- **시그널 자립 청산**: 만기(max_hold_sec)·TP/SL을 진입 시그널에 박제 — ns를 공유해도 남의 포지션에 내 설정이 적용되지 않음.

## 폴더

- `app/` — 채널(컨테이너) 엔트리포인트(`main.py`)와 로컬 실행기(`local_executor.py`)
- `bots/` — 봇 코어: `trade_config.py`(셀 설정), `trade_bot.py`, `trading/`(신호판정·주문실행), `state/`(시그널·랏 장부), `market/`(캔들·WS 동기화)
- `strategies/` — `s1_reversion.py`(σ/z 레벨 계산, ewz, 페이드 등 공용 산식)
- `controllers/` — Bybit REST/WS, MT5(가격=REST, 주문=터미널 API)
- `tools/` — 장부 복구 도구(`rebuild_v2.py` 로그 리플레이 재구성, `restore_orphan_signals.py` 고아 시그널 복원 등)
- `utils/`, `core/` — 로거(텔레그램 지문억제), redis 클라이언트, 심볼 매퍼

## 실행/배포

```bash
# 신호+실행 전체 (WSL2 docker-ce, compose 2개 파일 필수)
docker compose -f docker-compose.yml -f docker-compose.wsl.yml up -d --build

# executor-a2 (Windows 네이티브)
powershell ../infra/manage.ps1 restart executor-a2
```

- 코드 변경 배포는 반드시 `--build` + **컨테이너 내 grep으로 반영 확인**(BuildKit 캐시가 구코드를 굽는 사례 있음).
- 폐기 채널은 삭제 대신 `profiles: ["retired"]` 봉인(롤백용). 봉인 서비스의 기존 컨테이너는 `docker rm`으로 직접 제거.
- 시크릿: `.env`(API 키·토큰)와 `users.acl`(redis ACL 실비번)은 gitignore — `users.acl.example` 참고.

## License

Private / Internal
