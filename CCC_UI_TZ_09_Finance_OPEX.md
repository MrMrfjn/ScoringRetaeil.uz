# CCC UI — ТЗ 09: Finance / OPEX

## Задачи

- [x] Route `/finance` exists with RBAC (executive/director/head_analyst)
- [x] FINANCE_T template with glass page-header
- [x] Sticky filter bar: branch + period selectors
- [x] KPI cards: Общий OPEX, OPEX/договор, Top-категория, Записей
- [x] Chart.js doughnut — структура по категориям (chOpexCat)
- [x] Chart.js line — тренд OPEX по месяцам (chOpexTrend)
- [x] Chart.js bar — OPEX по филиалам (chOpexBranch)
- [x] Category table with progress bars and % column
- [x] API endpoints: `/api/finance/summary`, `/api/finance/by-category`, `/api/finance/by-branch`, `/api/finance/monthly-trend`
- [x] `opex_entries` table in database with indexes
- [x] OPEX_CATEGORIES in settings.py
- [x] Real expenses data imported (3,772 entries from DBF)
- [x] seed_opex_demo_data() function for fallback
- [x] Links to /intelligence and / from finance page
- [x] /finance in NAV for director/executive roles
- [x] Responsive layout (g2/g3 grids)
