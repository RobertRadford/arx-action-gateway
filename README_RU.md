# ARX ChatGPT Action Gateway v3.0

Цель: оставить для CEO один интерфейс — ChatGPT на телефоне — и убрать ручную роль почтальона между АРХ-01 и рабочими участниками.

## Что меняется

Старая схема:

CEO → АРХ-01 в ChatGPT → файлы outbox → ручное копирование в АРХ-02...АРХ-08 → ручное копирование ответов обратно.

Новая схема:

CEO → один Custom GPT «АРХ-01 | ШТАБ» в ChatGPT → GPT Action → Python HTTPS Gateway → API-агенты ARX-02...ARX-08 → сводка ARX-01 → CEO в том же ChatGPT-чате.

## Важное ограничение

Этот шлюз не пишет сообщения в существующие ChatGPT Project-чаты. Он заменяет их оперативную функцию внутренними API-агентами.

Существующие Project-чаты можно оставить как архив, источник контекста, место ручных обсуждений и проектную документацию. Оперативное управление идёт через один Custom GPT с Action.

## Что нужно для работы из ChatGPT mobile

1. Один Custom GPT в ChatGPT: «АРХ-01 | ШТАБ | Action Gateway».
2. В Custom GPT добавлен Action по файлу `OPENAPI_SCHEMA_FOR_GPT_ACTION.yaml`.
3. Python Gateway запущен на публичном HTTPS-адресе.
4. В `.env` указан `OPENAI_API_KEY` и `ARX_ACTION_TOKEN`.
5. В настройках Action установлен Bearer token = `ARX_ACTION_TOKEN`.

## Почему нужен публичный HTTPS

ChatGPT работает в облаке OpenAI. Когда Custom GPT вызывает Action, он обращается к внешнему HTTPS URL. Поэтому `http://127.0.0.1:8787` подходит только для локального теста, но не подходит для реального вызова из ChatGPT.

Варианты:
- Cloudflare Tunnel к вашему локальному ПК;
- ngrok к вашему локальному ПК;
- VPS;
- Render / Fly.io / Railway / другой хостинг Python FastAPI.

Для постоянной мобильной работы лучше облачный/VPS-вариант или постоянно включённый ПК с туннелем.

## Быстрый локальный тест без OpenAI API

1. Запустить `00_INSTALL_REQUIREMENTS.bat`.
2. Запустить `01_CREATE_ENV_FROM_TEMPLATE.bat`.
3. Оставить `ARX_MODE=mock`.
4. Запустить `02_RUN_SELF_TEST_MOCK.bat`.

Ожидаемый результат: создаётся цикл, проект распоряжения, mock-ответы ARX-02...ARX-08 и mock-сводка ARX-01.

## Переход в API-режим

В `.env`:

```text
OPENAI_API_KEY=sk-...
ARX_ACTION_TOKEN=<длинный_случайный_секрет>
OPENAI_MODEL=gpt-5.5
ARX_MODE=api
```

Запустить:

```text
03_START_LOCAL_GATEWAY.bat
```

Проверить локально:

```text
http://127.0.0.1:8787/health
```

После этого опубликовать шлюз как HTTPS и вписать публичный URL в `OPENAPI_SCHEMA_FOR_GPT_ACTION.yaml`.

## Рабочий цикл в ChatGPT

CEO пишет в Custom GPT:

```text
АРХ-01, подготовь распоряжение рабочим агентам: проверить готовность перехода к v1.8. Никаких BAT/PowerShell/EXE, никаких действий с реальными дисками. Нужны риски и следующий безопасный шаг.
```

Custom GPT вызывает `createCycle`, возвращает проект распоряжения и фразу:

```text
ОДОБРЯЮ C20260622_... ARX-123456
```

CEO пишет эту фразу.

Custom GPT вызывает `approveCycle`.

Шлюз вызывает ARX-02...ARX-08 через OpenAI API, собирает handoff-блоки, вызывает ARX-01 для сводки и возвращает результат CEO в том же ChatGPT-чате.

## Где хранятся результаты

```text
data/state/arx_gateway.sqlite3   — состояние циклов и ответы агентов
data/logs                         — журналы шлюза
data/exports                      — markdown-экспорт проектов, handoff и сводок
```
