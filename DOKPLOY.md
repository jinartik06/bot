# Deploy В Dokploy

Актуальная схема: один контейнер `ideas-bot`. Голосовые распознаются локально через `faster-whisper`, фото сохраняются в `/app/data/photos`, OCR идёт через Tesseract, AI-разбор идёт через Groq.

## 1. Тип Приложения

В Dokploy создайте приложение типа `Docker Compose`.

Нужные файлы:

- `Dockerfile`
- `docker-compose.yml`
- `requirements.txt`
- папка `src`

## 2. Environment

Вставьте переменные без кавычек и без пробелов в начале строк:

```env
BOT_TOKEN=токен_из_BotFather
GROQ_API_KEY=ключ_Groq
GROQ_BASE_URL=https://api.groq.com

ADMIN_TELEGRAM_IDS=1015730938
ALLOWED_TELEGRAM_IDS=
ALLOW_ALL_USERS=true

GROQ_TEXT_MODEL=qwen/qwen3-32b
GROQ_TRANSCRIBE_MODEL=whisper-large-v3-turbo

VOICE_TRANSCRIBER=faster_whisper
WHISPER_MODEL=small
WHISPER_DEVICE=cpu
WHISPER_COMPUTE_TYPE=int8
WHISPER_CPU_THREADS=2
WHISPER_NUM_WORKERS=1
WHISPER_LANGUAGE=ru
WHISPER_BEAM_SIZE=5
WHISPER_VAD_FILTER=false
WHISPER_MODEL_CACHE_DIR=/app/models
VOICE_LOCK_WAIT_SECONDS=180
VOICE_LOCK_TTL_SECONDS=600

FFMPEG_BINARY=ffmpeg
MEDIA_COMMAND_TIMEOUT_SECONDS=180
VOICE_PROCESSING_TIMEOUT_SECONDS=300

PHOTO_STORAGE_DIR=/app/data/photos
PHOTO_VISION_ENABLED=true
GROQ_VISION_MODEL=meta-llama/llama-4-scout-17b-16e-instruct
PHOTO_VISION_TIMEOUT_SECONDS=60
PHOTO_OCR_ENABLED=true
TESSERACT_BINARY=tesseract
PHOTO_OCR_LANGUAGE=rus+eng
PHOTO_OCR_TIMEOUT_SECONDS=30

DATABASE_PATH=/app/data/ideas.db
DEFAULT_TIMEZONE=Europe/Moscow
DEFAULT_DIGEST_WEEKDAY=6
DEFAULT_DIGEST_TIME=19:00
SHORT_INPUT_CHAR_LIMIT=900
```

## 3. Volumes

В `docker-compose.yml` уже есть:

```yaml
volumes:
  ideas_bot_data:
  whisper_models:
```

`ideas_bot_data` хранит базу идей и фото.  
`whisper_models` хранит скачанную модель Whisper. Не удаляйте их при redeploy.

## 4. Первый Deploy

1. Нажмите `Deploy` или `Redeploy with build`.
2. Откройте `Logs`.
3. Дождитесь:

```text
Voice config: transcriber=faster_whisper ...
Voice transcriber check succeeded: faster-whisper ...
Ideas bot started
```

Первое голосовое может обрабатываться долго, потому что модель `small` скачивается в `/app/models`.

## 5. Проверка

В Telegram:

1. `/start` - бот отвечает.
2. Отправьте длинный текст с несколькими идеями - бот сам создаёт несколько карточек.
3. Отправьте голосовое 5-10 секунд - бот отвечает `Слушаю и разбираю идею...`, затем сохраняет карточку.
4. Отправьте фото с подписью - бот сохраняет фото и создаёт карточку; если vision-модель или Tesseract видят содержимое/текст на фото, это попадёт в подробности. Фото без подписи/vision/OCR сохраняется без выдуманного AI-описания.
5. Откройте `💭 Мысли` несколько раз - список должен обновляться одним сообщением, без повторной рассылки карточек.
6. Откройте любую мысль из списка и проверьте действия `Продолжить`, `Переименовать`, `Категория`, `Архивировать`, `Удалить`.
7. Откройте `🖼 Альбом`, проверьте просмотр и удаление фото.
8. Откройте `✍️ Продолжить мысль`, выберите карточку и нажмите `Продолжить`, затем проверьте `🔍 Поиск`, `📎 Архив`.
9. `/admin` - у администратора открывается управление пользователями.

## 6. Логи

```bash
docker compose logs -f ideas-bot
```

Успешный голос:

```text
Voice input received
Voice runtime lock acquired
Audio prepared for Whisper
Loading faster-whisper model
Whisper transcription finished
Voice runtime lock released
Voice transcription succeeded
```

Если модель не скачивается, в логах будут ошибки Hugging Face или сети.

## 7. Частые Проблемы

`Conflict: terminated by other getUpdates request`

Тот же Telegram bot token запущен в другом месте. Остановите локальный процесс или старый контейнер.

Голосовые долго обрабатываются

Первый запуск скачивает модель. Для слабого CPU можно поставить:

```env
WHISPER_MODEL=base
```

или для быстрой проверки:

```env
WHISPER_MODEL=tiny
```

Голосовые не распознаются

Проверьте:

```env
VOICE_TRANSCRIBER=faster_whisper
WHISPER_MODEL_CACHE_DIR=/app/models
FFMPEG_BINARY=ffmpeg
```

Анализ не работает

Проверьте:

```env
GROQ_API_KEY=...
GROQ_TEXT_MODEL=qwen/qwen3-32b
```

Groq audio не используется по умолчанию, потому что текущий проверенный ключ возвращал `401 Invalid API Key` на `/audio/transcriptions`, хотя текстовый `/responses` работал.

OCR фото не работает

Проверьте:

```env
PHOTO_OCR_ENABLED=true
TESSERACT_BINARY=tesseract
PHOTO_OCR_LANGUAGE=rus+eng
```

В Dockerfile уже устанавливается `tesseract-ocr` и `tesseract-ocr-rus`. Если OCR недоступен, бот всё равно сохраняет фото и работает по подписи.

Vision-анализ фото не работает

Проверьте:

```env
PHOTO_VISION_ENABLED=true
GROQ_API_KEY=...
GROQ_VISION_MODEL=meta-llama/llama-4-scout-17b-16e-instruct
```

Если vision-модель недоступна или Groq вернул ошибку, бот не падает и продолжает разбор по подписи/OCR.

## 8. Обновление

1. Запушьте изменения.
2. В Dokploy нажмите `Redeploy with build`.
3. Проверьте логи.
4. Убедитесь, что volumes остались подключены.
