# CCC UI — ТЗ 06: Вкладка «Аналитика» (Intelligence)

## Задачи

### 1. Hero-блок
- [x] Заголовок "Credit Intelligence — центр принятия решений" с gradient text
- [x] Подзаголовок мелким текстом
- [x] `.page-header` glass panel background

### 2. Блок «Структура аналитики»
- [x] 12 разделов как widget-карточки с glass фоном
- [x] Кнопки/чипы: → Дашборд, → Портфель, → Алерты, ↓ На этой странице
- [x] Внутренние ссылки привязаны к якорям (risk-matrix-card, recommendations-block)
- [x] Блоки «В разработке» / «Скоро» — с dashed border

### 3. Executive-KPI
- [x] 11 KPI карточек с цветовыми акцентами (kpi-grid + kpi-accent-*)
- [x] Загрузка через `/api/analytics/kpi`
- [x] Фильтрация по филиалу

### 4. Хит-лист и «Под угрозой»
- [x] Hitlist с badge-critical/badge-high/badge-moderate по дням просрочки
- [x] at_risk таблица с бейджами
- [x] Кнопка «Скоринг» в едином стиле

### 5. Риск-матрица и сегменты
- [x] `id="risk-matrix-card"` на карточке
- [x] PD% с цветовым кодом + мини progress bar (.bar + .barf)
- [x] Chart.js bar chart рядом (chRiskAge)
- [x] Income source сегменты с прогресс-барами

### 6. Vintage-анализ и тренды
- [x] Vintage таблица с цветовым кодированием DPD30/60/90/NPL%
- [x] Тренд NPL — Chart.js line chart (chNplTrend) с anomaly highlighting
- [x] Aging бакеты таблица + Chart.js bar (chAging)

### 7. Decision Center
- [x] `id="recommendations-block"` на блоке
- [x] Priority badges (high/medium/low) + цветной бар слева
- [x] Текст "Нет рекомендаций" при пустом списке

### 8. Навигация и взаимодействия
- [x] Smooth scroll при клике по якорям
- [x] Единый стиль кнопок и ссылок
- [x] Адаптивность (g2/g3 → 1fr на mobile)

### Дополнительно реализовано
- [x] 3 Chart.js графика: портфель по филиалам, структура по срокам, тренд выдач/погашений
- [x] Канальная аналитика (responsible_person + sales online/offline)
- [x] Повторные клиенты: распределение + по филиалам
- [x] Сотрудники: топ-10 рискованных + топ-10 лучших
- [x] AI Predictions каркас (3 заглушки)
- [x] Data Readiness индикатор
