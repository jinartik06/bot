FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV RUNNING_IN_DOCKER=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg libgomp1 tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN python -m pip install --no-cache-dir --upgrade pip wheel \
    && python -m pip install --no-cache-dir -r requirements.txt

COPY src ./src

VOLUME ["/app/data"]

CMD ["python", "-m", "src.main"]
