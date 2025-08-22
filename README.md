# Trading Bot

# wsl에서 docker 빌딩(잘안됨)
docker build -t trading-bot .
cd /mnt/c/Users/Hyeongeon/Hansoldeco_s3_comp/tradingBot
docker run -p 8000:8000 trading-bot

# test
cmd에서
start chrome --remote-debugging-port=9222 --user-data-dir="C:\chrometemp"

cd C:\Users\Hyeongeon\Hansoldeco_s3_comp\tradingBot
uvicorn app.main:app --port 8000

# 바깥 logs는 prompt에서 실행했을때
# 안에 logs는 파이참 디버그로 했을때

# 토큰 등록 Powershell
$TOKEN=""
$WEBHOOK="https://telewebhook.onrender.com/telegram/webhook/bot1/s1"
$SECRET="h1"

Invoke-RestMethod -Method Post `
  -Uri "https://api.telegram.org/bot$TOKEN/setWebhook" `
  -ContentType "application/json" `
  -Body (@{
    url = $WEBHOOK
    secret_token = $SECRET
    drop_pending_updates = $true
    allowed_updates = @("message","edited_message","channel_post","edited_channel_post")
  } | ConvertTo-Json)