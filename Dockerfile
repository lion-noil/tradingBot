# Dockerfile
FROM python:3.10-slim

# 환경 변수 설정
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# 작업 디렉토리
WORKDIR /app

# 의존성 복사 및 설치
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# 전체 프로젝트 복사
COPY . .

# Playwright 브라우저 설치 (필수)
RUN apt-get update && apt-get install -y curl gnupg && \
    pip install playwright && \
    playwright install chromium

# 서버 실행
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
