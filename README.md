# Bybit Trading Bot

🚀 Bybit 웹 UI를 Playwright로 자동 제어하는 매매 봇

## 기능
- 수동 진입: 키보드 ↑ 매수, ↓ 매도, → 청산
- 자동 진입: MA100 기반 전략
- 로그 파일 기록

# docker 빌딩
docker build -t trading-bot .

# 테스트 실행
docker run -p 8000:8000 trading-bot