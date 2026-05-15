# Памятка По Боту

## Как Устроен

- Telegram long polling принимает сообщения.
- Текст, голосовые, аудио и фото с подписью сохраняются как идеи.
- Голосовые распознаются локально через `faster-whisper`.
- Groq используется для кнопки `Анализ`.
- SQLite хранит идеи, пользователей, категории, blacklist и служебные lock-и.
- Docker volume `/app/data` хранит базу.
- Docker volume `/app/models` хранит модель Whisper.

## Основные Переменные

```env
VOICE_TRANSCRIBER=faster_whisper
WHISPER_MODEL=small
WHISPER_DEVICE=cpu
WHISPER_COMPUTE_TYPE=int8
WHISPER_LANGUAGE=ru
WHISPER_MODEL_CACHE_DIR=/app/models
VOICE_PROCESSING_TIMEOUT_SECONDS=300

GROQ_TEXT_MODEL=qwen/qwen3-32b
GROQ_BASE_URL=https://api.groq.com
```

`WHISPER_MODEL=small` лучше для русского, но тяжелее. Для слабого сервера используйте `base` или `tiny`.

## Перезапуск

Dokploy:

1. Откройте проект.
2. Нажмите `Deploy` или `Redeploy with build`.
3. Смотрите `Logs`.

Docker Compose:

```bash
docker compose restart ideas-bot
docker compose up -d --build
```

## Логи

Dokploy: вкладка `Logs`.

Docker:

```bash
docker compose logs -f ideas-bot
```

Что искать:

- `Ideas bot started` - бот запущен.
- `Voice transcriber check succeeded` - голосовой движок доступен.
- `Audio prepared for Whisper` - ffmpeg подготовил аудио.
- `Loading faster-whisper model` - модель грузится или скачивается.
- `Whisper transcription finished` - речь распознана.
- `Groq text structuring failed` - проблема с Groq-анализом.

## Если Голос Не Работает

Проверьте:

1. `VOICE_TRANSCRIBER=faster_whisper`.
2. В Dockerfile установлен `ffmpeg` и `libgomp1`.
3. Volume `/app/models` подключён.
4. У сервера есть интернет на первом запуске для скачивания модели с Hugging Face.
5. Голосовое не слишком длинное.

Для диагностики поставьте:

```env
WHISPER_MODEL=tiny
```

Если `tiny` работает, а `small` слишком медленный, серверу не хватает CPU/RAM.

## Если Анализ Не Работает

Проверьте:

1. `GROQ_API_KEY` задан.
2. `GROQ_BASE_URL=https://api.groq.com`.
3. `GROQ_TEXT_MODEL=qwen/qwen3-32b`.

Голос по Groq сейчас не используется. При проверке текущий ключ работал для `/responses`, но давал `401 Invalid API Key` на `/audio/transcriptions`.

## Что Бот Не Делает

- Не распознаёт речь через Groq по умолчанию.
- Не делает веб-панель.
- Не читает содержимое фото без подписи.
- Не шарит идеи между пользователями.
- Не гарантирует идеальное распознавание плохого звука.

## Фиксы В Этой Версии

- Добавлен локальный `faster-whisper`.
- Добавлен кеш моделей `/app/models`.
- Добавлен общий lock `voice_transcription`, чтобы два голосовых не распознавались одновременно.
- Оставлен Groq для текстового анализа.
- Обновлены env, Dockerfile, compose и инструкции Dokploy.
