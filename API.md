# CCC — API Reference

Краткое описание публичных эндпоинтов. Авторизация: сессия (cookie) после входа в веб-интерфейс, либо заголовок `X-API-Key` для маркетплейс/внешних клиентов где указано.

---

## Система и здоровье

| Метод | Путь | Описание | Авторизация |
|-------|------|----------|-------------|
| GET | `/health` | Статус приложения, БД, диск (для Docker/nginx) | нет |
| GET | `/system/health` | Расширенный статус: клиенты, договоры, scheduler | сессия |
| GET | `/api/db-check` | Диагностика: список таблиц и количество записей | сессия |

---

## Скоринг

| Метод | Путь | Описание | Авторизация |
|-------|------|----------|-------------|
| POST | `/api/score` | Скоринг по JSON (client_id или данные нового клиента) | сессия, rate limit |
| GET | `/api/hybrid-score/<client_id>` | Гибридный скоринг (правила + ML + поведение) | сессия |
| GET | `/api/behavioral-score/<client_id>` | Поведенческий скоринг | сессия |
| GET | `/api/features/<client_id>` | Feature Store по клиенту | сессия |
| POST | `/api/marketplace/score` | Скоринг для внешних клиентов (лимиты по плану) | **X-API-Key** |

**POST /api/score** — тело JSON: `client_id` (существующий) либо поля нового клиента: `age`, `income_source`, `monthly_income`, `product_amount`, `advance_payment`, `contract_term`, `region`, `position` и т.д. Ответ: `total_score`, `risk_class`, `rate`, `calculator`, `breakdown`.

---

## Клиенты и геоданные

| Метод | Путь | Описание | Авторизация |
|-------|------|----------|-------------|
| GET | `/api/clients/search?q=...` | Поиск клиентов по ФИО/паспорту/телефону | сессия |
| GET | `/api/clients/suggest?q=...` | Подсказки по имени | сессия |
| GET | `/api/geo/regions` | Список регионов | сессия |
| GET | `/api/geo/mfy/<region_id>` | МФЙ по ID региона | сессия |
| GET | `/api/geo/mfy/<region>` | МФЙ по имени региона | сессия |
| GET | `/api/geo/mfy-by-name?region=...` | МФЙ по имени | сессия |

---

## Импорт и пайплайн

| Метод | Путь | Описание | Авторизация |
|-------|------|----------|-------------|
| POST | `/api/import/clients` | Импорт клиентов (JSON/CSV) | сессия, модуль import |
| POST | `/api/import/contracts` | Импорт договоров | сессия |
| POST | `/api/import/payments` | Импорт платежей | сессия |
| POST | `/api/import/analyze` | Анализ файла перед импортом | сессия |
| POST | `/api/import/auto` | Автоимпорт по типу файла | сессия |
| POST | `/api/pipeline/import/clients` | Пайплайн: клиенты | сессия |
| POST | `/api/pipeline/import/contracts` | Пайплайн: договоры | сессия |
| POST | `/api/pipeline/import/payments` | Пайплайн: платежи | сессия |
| POST | `/api/pipeline/recalculate` | Пересчёт агрегатов | сессия |

---

## Экспорт (веб-страницы, не API)

| Метод | Путь | Описание | Авторизация |
|-------|------|----------|-------------|
| GET | `/export/clients` | Excel: клиенты | сессия, модуль export |
| GET | `/export/contracts` | Excel: договоры | сессия |
| GET | `/export/audit` | Excel: аудит-лог | сессия |
| GET | `/export/portfolio` | Excel: портфель (фильтры year, branch, product) | сессия, модуль portfolio |

---

## Аналитика и дашборд

| Метод | Путь | Описание | Авторизация |
|-------|------|----------|-------------|
| POST | `/api/refresh-analytics` | Обновить снимок портфеля (snapshot) | сессия |
| GET | `/api/alerts-count` | Количество критических/всего алертов | сессия |
| GET | `/api/npl-branch-years/<branch>` | НПЛ по годам по филиалу | сессия |
| GET | `/api/aging-branches` | Просрочка (DPD) по филиалам | сессия |
| GET | `/api/aging-years` | Просрочка по годам | сессия |
| GET | `/api/monthly/<year>` | Помесячная разбивка за год | сессия |
| GET | `/api/month-detail/<period>` | Детализация месяца (YYYY-MM) | сессия |
| GET | `/api/at-risk` | Договоры в зоне риска | сессия |
| GET | `/api/vintage` | Винтажный анализ | сессия |

---

## Intelligence и ML

| Метод | Путь | Описание | Авторизация |
|-------|------|----------|-------------|
| POST | `/api/intelligence/compute` | Пересчёт таблиц риска и training dataset | сессия |
| GET | `/api/intelligence/risk/<table>` | Таблица риска (age, region, branch, product и др.) | сессия |
| GET | `/api/intelligence/feedback` | Обратная связь по портфелю | сессия |
| POST | `/api/ml/train` | Обучение ML-модели | сессия |
| GET | `/api/ml/early-defaults` | Прогноз ранних дефолтов | сессия |

---

## Админ и система

| Метод | Путь | Описание | Авторизация |
|-------|------|----------|-------------|
| GET | `/api/integrity` | Проверка целостности данных | сессия |
| POST | `/api/backup` | Создать бэкап БД | сессия |
| GET | `/api/backups` | Список бэкапов | сессия |
| POST | `/api/train-ml` | Обучение ML (альтернативный путь) | сессия |
| GET | `/api/developer/stats` | Статистика для Developer | сессия |
| GET | `/api/developer/plans` | Тарифные планы API | сессия |
| GET | `/api/developer/keys` | Список API-ключей | сессия |
| POST | `/api/developer/keys` | Создать API-ключ (body: name, plan_id) | сессия |
| DELETE | `/api/developer/keys` | Отозвать ключ (key_id) | сессия |
| GET | `/api/white-label` | Настройки white-label | сессия/опционально |
| POST | `/api/white-label` | Установить white-label | сессия |
| GET | `/api/scheduler/status` | Статус фонового планировщика | сессия |

---

## Формат ответов

- Успех: JSON с данными (объект или массив).
- Ошибка: `{"error": "текст"}` с HTTP 4xx/5xx.
- Экспорт Excel: `Content-Disposition: attachment`, тип `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`.

## Переменные окружения (напоминание)

- `SECRET_KEY` — обязательно в production.
- `DB_PATH`, `UPLOAD_DIR`, `EXPORT_DIR` — пути к БД и файлам.
- `LOG_LEVEL` — DEBUG, INFO, WARNING, ERROR.
- `LOG_PATH` — путь к файлу логов (ротация 5 МБ, 3 файла).
