# AIbroker — план реструктуризации и технический долг

> Составлен 2026-07-10 после полного ревью проекта (7.6k LOC src, 6.8k LOC
> tests, 11 docs). Цель: стабильность ответов, минимизация ошибок,
> гарантированный ответ в рамках доступных ключей, асинхронный слой с обратной
> совместимостью. Живёт как единый источник правды по направлению; каждая фаза
> самодостаточна (shippable), с тестами и обновлением docs.

## 1. Честная оценка текущей архитектуры

Не «переписать с нуля» — база хорошая, слои разделены:

| Слой | Файл | Ответственность | Оценка |
|---|---|---|---|
| HTTP | `routes/proxy.py` (305) | validate → delegate → shape | ✅ тонкий, чистый |
| Оркестрация | `services/llm_service.py` (620) | walk chain, rotate keys, classify, record | ⚠️ 3 почти-дубля |
| Маршрут | `routing/chains.py` (204) | capability → порядок провайдеров | ✅ single source |
| Выбор ключа | `routing/selector.py` (278) | атомарный LRU/random pick, reserve | ✅ гонки закрыты |
| Cooldown | `routing/cooldown.py` (215) | адаптивный backoff по сигналу провайдера | ✅ |
| Cost guard | `routing/cost_guard.py` (181) | admission по $-cap | ⚠️ минует embed |
| Провайдеры | `providers/litellm_adapter.py` (450) | call_llm/embed/transcribe | ⚠️ растёт if-провайдер |
| Async | `services/deep_jobs.py` (161) | submit+poll (ТОЛЬКО chat:deep) | ✅ готовый паттерн |
| Мониторинг | `monitor.py` (131) | реоживление dead/cooldown ключей | ✅ |
| Дашборд | `routes/dashboard.py` (1835) | HTML + routes + логика | 🔴 монолит |

## 2. Технический долг (найдено ревью + инцидентами сессии)

### 2.1 Гарантия ответа не выполняется (главное)
`run_chat` сдаётся когда:
- пройден `_MAX_ATTEMPTS_ABS = 60`, ИЛИ
- цепочка пройдена один раз с `_max_keys(provider)` попытками на провайдера (3–5).

**Проблема**: запрос может отдать 503, пока живые ключи ещё не тронуты.
Пример: у cerebras **14 ключей**, но `_MAX_KEYS_BY_PROVIDER["cerebras"]=3` — пробуется только 3. «Гарантия ответа в рамках доступных ключей» требует исчерпывающей ротации по ВСЕМ eligible-ключам, ограниченной не счётчиком, а бюджетом времени.

### 2.2 Неполная таксономия ошибок → «пропавшие» ключи долбятся
`classify_provider_error` знает `rate_limit`/`auth`/`error`. Всё, что не rate_limit/auth (404 NotFound, «model not provisioned», Overloaded, InvalidJSON-storm) → generic `error` → `_penalize` НИЧЕГО не делает → ключ бьётся на каждом pick без паузы.
- **Живой инцидент (07-10)**: nvidia `kimi-k2.6` → 404 «Function not found for account», 30 ошибок/час без cooldown.
- **Живой инцидент (07-05)**: deepseek response_format, anthropic credit-balance, zai invalid-param — все чинились точечно добавлением сигнатур. Это лечение симптома, а не таксономии.

### 2.3 Дрейф моделей в цепочках (нет авто-детекта)
- nvidia `kimi-k2.6` (chat:fast) → 404 на 100% вызовов.
- nvidia `deepseek-v4-pro` (chat:smart) → ~91с, таймаут на 100%.
Работали при подключении 07-05, «протухли». Нет механизма, помечающего модель мёртвой (в отличие от ключа).

### 2.4 Три почти-дубля `run_chat`/`run_embed`/`run_transcribe`
Паттерн pick→decrypt→call→penalize→record→outcome троится. Расходятся непоследовательно: только `run_chat` делает cost-reservation, attempt-budget, JSON-gate. embed/transcribe — нет. Нарушение DRY + SRP.

### 2.5 Растущая if-провайдер сложность в `call_llm`
cloudflare `api_base`, deepseek json_schema→json_object, gemini `reasoning_effort=disable`, voyage — всё ветвлениями внутри одной функции. Каждый новый провайдерский нюанс = правка общего кода. Нарушение open/closed.

### 2.6 Async только для chat:deep
Паттерн submit+poll (`deep_jobs.py` + таблица `deep_jobs`) хорош, но привязан к одной capability. Нужен общий слой для всех типов запросов.

### 2.7 Cost guard минует embed-путь
`run_embed` не зовёт `reserve_cost`. Безвредно пока embed бесплатный (voyage-4), но $-cap там не работает.

### 2.8 `dashboard.py` — монолит 1835 LOC
HTML-рендер + роуты + бизнес-логика в одном файле. Модульность/тестируемость.

### 2.9 `routing.md` — 55 KB накопленных датированных заметок
Отличная история решений, но как справочник тяжёл. Со временем — расслоить (текущее состояние vs changelog).

## 3. Целевая архитектура

Разрешаем главное напряжение задачи: «отдельный отработчик на каждый тип
запроса» (слова пользователя) **против** DRY. Ответ — **политика на capability
+ единый исполняющий движок**:

```
routes/proxy.py            (тонкий HTTP, sync)
routes/jobs.py             (тонкий HTTP, async submit/poll — НОВОЕ)
        │
        ▼
services/engine.py         (ЕДИНЫЙ движок исполнения — НОВОЕ)
   ├── CapabilityPolicy    (декларация на capability: chain, JSON-gate,
   │                        cost-reservation, exhaustiveness, timeout-профиль)
   ├── KeyRotator          (исчерпывающая ротация по eligible-ключам)
   ├── ErrorClassifier     (полная таксономия → политика наказания)
   └── ProviderAdapter[]   (провайдерские нюансы за реестром — НОВОЕ)
        │
        ▼
routing/selector.py        (атомарный pick — как есть)
providers/litellm_adapter  (тонкий вызов — квирки уезжают в адаптеры)
```

**Принципы:**
- **SRP**: ротация, классификация, cost, исполнение, провайдерские квирки — раздельные объекты.
- **Open/Closed**: новый провайдер = новый `ProviderAdapter`; новая capability = новая `CapabilityPolicy`; ни одной правки god-функции.
- **DRY**: один движок вместо трёх `run_*`; политики декларативны.
- **Гарантия ответа**: `KeyRotator` перебирает ВСЕ eligible-ключи всех провайдеров цепочки, пока не успех или genuinely-none-left; ограничение — wall-clock deadline из timeout-профиля, не счётчик попыток.
- **Async-first, sync-compatible**: sync и async роуты зовут ОДИН движок. chat:deep — просто capability с `async_only=True`.

### 3.1 Обработчик на ТИП МОДЕЛИ, не только на провайдера (требование 2026-07-10)

Провайдер — слишком грубая единица. Внутри одного ключа (`api_key`) живут
РАЗНЫЕ модели, и у каждой свои: **затухание (cooldown), подсчёт лимитов,
таймауты, поддерживаемые типы запросов, цена**. Универсальный вызов на всё
подряд — это как раз источник ошибок (nvidia: kimi 404 / deepseek-v4-pro 91с /
nemotron жив — ТРИ разных поведения на ОДНОМ ключе) и перерасхода токенов.

`ProviderAdapter` расширяется до реестра **`ModelHandler`** с ключом
`(provider, model)`:

```
ModelHandler(provider, model):
  supported_request_types   # chat / json_object / json_schema / vision / embed / audio
  timeout_profile           # свой таймаут (nemotron 19мин, kimi 45с, flash 60с)
  cooldown_policy           # своя кривая затухания (RPM-модель ≠ дневная-квота ≠ протухла)
  quota_dimensions          # по чему считать лимит (req/day, tok/day, tok/min, neurons)
  cost_model                # своя цена + free-аллокация (voyage-4 200M/мес, gemini flash-lite …)
  liveness                  # модель может «протухнуть» (404) независимо от живости ключа
  prepare(kwargs)/parse()   # квирки запроса/ответа (json-downgrade, api_base, reasoning_effort)
```

**Почему это критично для экономии токенов (явное требование):**
- Внутри ключа выбор ДЕШЁВОЙ модели под задачу (gemini flash-lite vs pro;
  deepseek-chat vs v4-flash) — решение уровня модели, не провайдера.
- Подсчёт free-аллокации (voyage-4: 200M/мес именно на эту модель, voyage-3: 0)
  — свойство модели, не ключа. Универсальный `_billed_cost` уже спотыкался
  об это (см. routing.md, voyage-4 landmine).
- Затухание: RPM-модель восстанавливается за 60с, дневная-квота — до полуночи,
  протухшая модель (404) — не восстановится вовсе. Единый cooldown на ключ
  ошибочно наказывает живые модели того же ключа.

**Ключевой инвариант**: ошибка КОНКРЕТНОЙ модели (404, InvalidJSON, model-timeout)
наказывает `(ключ, модель)`, а не весь ключ — другие модели того же ключа
остаются доступны. Это прямо чинит §2.2 и §2.3 на уровне архитектуры, а не
сигнатур.

Требует нового состояния: таблица/колонки затухания и счётчиков **на
(api_key_id, model)**, не только на api_key (миграция в Фазе 1/2).

## 4. Таксономия ошибок (целевая)

| Класс | Признаки | Политика |
|---|---|---|
| `rate_limit` | 429, quota, tokens per day/min, retry-after | cooldown (адаптивный), try next key |
| `auth` | 401, 403, credit balance too low | mark_dead, try next key |
| `unavailable` | 404, model not found/provisioned, Overloaded | **skip provider + cooldown провайдера** (модель, не ключ) |
| `timeout` | наш asyncio.wait_for TimeoutError | cooldown ключа (перегрузка), try next |
| `bad_output` | InvalidJSON после гейта | next PROVIDER (не ключ — свойство модели) |
| `transient` | APIConnectionError, reset by peer | retry тот же провайдер, короткий backoff |
| `error` | всё прочее | log + try next (текущий дефолт) |

Провайдер-скоупед сигнатуры (deepseek/voyage/zai) остаются, но становятся частным случаем этой таблицы, а не спец-кейсами в коде.

## 5. План по фазам

Каждая фаза: **отдельные коммиты, тесты, docs, деплой, живая проверка** — как весь текущий рабочий процесс. Ничего не ломаем: sync-эндпоинты не отключаем до отдельного решения.

### Фаза 0 — быстрые вины (сейчас, низкий риск)
Убрать источники ошибок, найденные в ревью 07-10, до всякого рефакторинга:
- Убрать `nvidia` из `chat:fast` (kimi-k2.6 = 404) и `chat:smart` (deepseek-v4-pro = таймаут).
- Добавить `unavailable` (404/NotFound) в классификатор → skip+cooldown.
- Проверить nvidia `chat:deep` (nemotron) отдельно — жив ли.
- **Эффект**: −~36 ошибок/час у Степана, ноль потери ёмкости.
- Тесты: классификация 404; цепочки без мёртвых моделей.

### Фаза 1 — таксономия ошибок + затухание на (ключ, модель) + исчерпывающая ротация
- Полная `ErrorClassifier` (таблица §4), политика наказания на класс.
- **Затухание/счётчики на `(api_key_id, model)`** (§3.1): миграция — новая
  таблица `key_model_state` (cooldown_until, error_count, daily_used per model)
  или колонки-json. Ошибка модели не гасит другие модели ключа.
- `KeyRotator`: перебор всех eligible-ключей до успеха/исчерпания; лимит — deadline (wall-clock), не счётчик. За флагом `exhaustive_rotation` (дефолт off → on после проверки).
- Sync: deadline = min(client budget, nginx). Async: deadline большой.
- **Эффект**: 503 только когда реально нет ни одного пригодного ключа; протухшая модель не блокирует живые.
- Тесты: «14 ключей cerebras — пробуются все»; «tail достижим»; «deadline режет»; «404 модели не гасит другую модель того же ключа».

### Фаза 2 — единый движок + CapabilityPolicy (DRY/SRP)
- Вынести `run_chat`/`run_embed`/`run_transcribe` в один `engine.execute(policy, request)`.
- `CapabilityPolicy` декларирует: chain, JSON-gate, cost-reservation, exhaustiveness, timeout-профиль, async_only.
- embed/transcribe получают cost-reservation (закрывает §2.7).
- **Эффект**: −~150 строк дубля, единообразие поведения.
- Тесты: поведенческие тесты каждой политики; регресс всех трёх старых путей.

### Фаза 3 — реестр ModelHandler / ProviderAdapter (open/closed, §3.1)
- Реестр `ModelHandler[(provider, model)]`: таймаут-профиль, cooldown-политика,
  quota-измерения, cost-модель + free-аллокация, supported_request_types,
  prepare/parse-квирки — всё на уровне модели.
- Провайдерские квирки (cloudflare api_base, deepseek json-downgrade, gemini
  reasoning_effort, voyage) переезжают в соответствующие ModelHandler.
- `call_llm` становится тонким: handler.prepare(kwargs) → litellm → handler.parse(resp).
- Выбор дешёвой модели внутри ключа под задачу — на уровне ModelHandler/policy
  (экономия токенов, §3.1).
- **Эффект**: новый провайдер/модель/квирк — без правок общего кода; корректный
  индивидуальный учёт токенов на модель.
- Тесты: юнит на каждый ModelHandler.

### Фаза 4 — общий async-слой (submit/poll для всех capability) ✅ БРОКЕР ГОТОВ (2026-07-10)
- ✅ Обобщил `deep_jobs` → колонка `capability` (миграция 008) + `POST /v1/jobs?capability=…` (`jobs_submit`) + `GET /v1/jobs/{id}` (`jobs_poll`).
- ✅ sync-эндпоинты остаются; `submit_deep_job` — тонкий wrapper над `submit_job(capability="chat:deep")`.
- ✅ chat:deep — единственная async-only capability; `/v1/deep` + `/v1/deep/{id}` оставлены как обратно-совместимые алиасы.
- ✅ Тесты: submit+poll chat:fast с response_format; валидация capability/scope; 404; backward-compat `/v1/deep`.
- ✅ Docs: `api.md` (полное описание + примеры).
- **ОСТАЁТСЯ (клиентская часть, tech debt §6)**: Вера и Степан мигрируют постепенно — сначала длинные/дорогие ходы, потом остальное. **Sync не отключаем** до полного перехода обоих + подтверждения на проде.
- **Эффект**: клиент может гарантированно получить ответ, не держа соединение; исчерпывающая ротация без упора в nginx/504. (Полная «исчерпывающая ротация» включится в Фазе 1 — сейчас async снимает только ограничение по времени соединения.)

### Фаза 5 — модуляризация dashboard.py
- Расслоить 1835 LOC: рендер (шаблоны) / роуты / query-слой.
- **Эффект**: тестируемость, читаемость.

### Сквозное
- Docs обновляются в той же PR (docs-check gate).
- Покрытие: держим diff-cover ≥75% на новых строках (текущий гейт).
- `routing.md` при Фазе 2 расслоить на «архитектура (текущее)» + «changelog».

## 6. Будущий технический долг (зафиксировано)

- **Async-миграция клиентов** (Фаза 4→∞): перевести Веру и Степана на submit/poll,
  начиная с длинных/дорогих capability. Sync-эндпоинты **не отключать** до тех пор,
  пока оба клиента полностью не перейдут и это не подтверждено на проде. Финальное
  отключение sync — отдельное решение, не в рамках этого плана.
- **Авто-детект дрейфа моделей**: monitor мог бы периодически проверять каждую
  сконфигурированную (provider, model) реальным вызовом и помечать пропавшие,
  как сейчас помечает ключи (§2.3).
- **$-cap на embed** (§2.7): включить при появлении платного embed-провайдера или
  при переводе voyage-ключа в `tier=paid`.
- **routing.md changelog split** (§2.9).

## 7. Риски и принципы безопасности

- Каждая фаза за флагом или обратно совместима; sync-путь работает всегда.
- Исчерпывающая ротация повышает latency — приемлемо по явному решению
  («клиент пусть ждёт дольше, но получит ответ»); для sync ограничено бюджетом
  клиента, для async — нет.
- Ничего не деплоится без: полного прогона тестов, ruff, diff-cover, живой
  проверки на проде (как весь текущий процесс).
- Порядок фаз можно менять; Фаза 0 независима и даёт немедленный эффект.
