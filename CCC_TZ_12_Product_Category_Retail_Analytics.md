# CCC — ТЗ 12: Product & Category Management Analytics
## Retail Credit — Product Sales, Margin, Assortment & Supplier Analysis

**Role:** Senior retail analytics expert & category management strategist  
**Goal:** Maximize revenue, margin, turnover, branch profitability, supplier efficiency  
**Output:** Professional analytical report + dashboard system (4 main tabs + advanced analysis)

---

## Data model assumptions (mapping to CCC / extensions)

| Concept | In CCC / Extension |
|--------|----------------------|
| **Product** | `contracts.product_type` (e.g. 3%, 4%, рассрочка) or **goods category** if company sells physical goods on credit |
| **Category** | Product category (e.g. Electronics, Furniture) — add `product_category` to contracts or use `product_type` as category |
| **Branch** | `contracts.branch` (Ф1–Ф7) |
| **Month / Year** | `contracts.contract_date`, payment dates |
| **Revenue** | Interest + fees (or `product_amount` as sales volume; margin = interest income) |
| **Margin** | Interest income per contract / product; margin % = (interest / product_amount)*100 |
| **Supplier** | **New dimension:** link product/category to supplier (table `suppliers`, `contracts.supplier_id` or `product_supplier`). If absent, use “internal product” or “n/a” and skip TAB 4 or use “product origin” proxy. |

If the business is **credit-only** (no physical goods): treat “product” = credit product type, “revenue” = interest income, “margin” = interest margin, “sales volume” = `SUM(product_amount)`, “turnover” = contract count or portfolio velocity.

---

# TAB 1 — PRODUCT SALES & MARGIN ANALYSIS

## Goal
Analyze product performance by category, branch, and time to drive revenue and margin.

## 1.1 Breakdown dimensions
- [x] **By category** — product category (or product_type group)
- [x] **By branch** — Ф1 … Ф7
- [x] **By month** — `strftime('%Y-%m', contract_date)` (or payment month)
- [x] **By year** — `strftime('%Y', contract_date)`

## 1.2 Calculated metrics (per product, per branch, per period)

| Metric | Formula / Source |
|--------|-------------------|
| **Sales volume** | `SUM(product_amount)` (total credit issued) |
| **Revenue** | `SUM(interest_income)` or proxy: `SUM(paid_amount) - SUM(advance_payment)` or fee field if exists |
| **Total margin** | Revenue − cost of funds (or use interest as margin if no COF) |
| **Margin %** | `100 * total_margin / SUM(product_amount)` or per contract |

*If interest not stored separately:* use `(interest_rate/100) * product_amount * term` as proxy for interest income per contract.

## 1.3 Identified segments
- [x] **Top revenue products** — sort by revenue DESC, top N (e.g. 20)
- [x] **Top margin products** — sort by margin % DESC
- [x] **Low margin products** — margin % below threshold (e.g. &lt; 2%)
- [x] **Slow moving** — low contract count or low volume vs. average

## 1.4 ABC analysis
- [x] **Revenue ABC:**  
  - Sort products by revenue DESC, compute cumulative % of total revenue.  
  - **A** = products contributing to first ~80% of revenue (top ~20% of products).  
  - **B** = next ~15% of revenue (~30% of products).  
  - **C** = remaining ~5% of revenue (~50% of products).
- [ ] **Output:** product_id/code, revenue, cum_%_revenue, ABC_class.

## 1.5 XYZ analysis (demand stability)
- [x] **X** — stable demand (e.g. low coefficient of variation of monthly sales).  
- [x] **Y** — moderate variation.  
- [x] **Z** — high variation / unpredictable.  
- [ ] Use monthly sales volume or contract count per product; CV = std_dev/mean over last 12 months.

## 1.6 Outputs for TAB 1
- [x] **Full product performance table** (product, category, branch, month, year, sales_volume, revenue, margin, margin_pct, contract_count).
- [x] **Category-level summary** (category, total_revenue, total_margin, margin_pct, share_of_revenue).
- [x] **Trends over time** — monthly/quarterly trend charts (revenue, margin %, volume by product or category).

---

# TAB 2 — TOP PRODUCTS & PROFIT GROWTH STRATEGY

## Goal
Identify most profitable opportunities and propose concrete growth actions.

## 2.1 Definitions
- [x] **Top products by profit** — sort by total margin (absolute), top N.
- [x] **Top categories by margin** — aggregate by category, sort by margin % and total margin.

## 2.2 Qualitative analysis (drivers)
- [ ] **Why they perform well** — branch mix, customer segment, price point, term.
- [ ] **Demand drivers** — seasonality, campaign, region.
- [ ] **Price sensitivity** — compare margin % and volume across branches/periods; note elasticity if data allows.

## 2.3 Proposed strategies (per product/category)
- [ ] **Increase sales volume** — target branches/segments with below-average penetration.
- [ ] **Increase margin** — pricing/rate adjustment, fee structure.
- [ ] **Bundle products** — combine high-margin with high-volume product (e.g. insurance + loan).
- [ ] **Upsell / cross-sell** — next best product by segment (e.g. 3% → 4% where acceptable).

## 2.4 Forecast scenarios
- [x] **Scenario 1:** If sales volume +10% (by product/category) → new revenue and profit (using current margin %).
- [x] **Scenario 2:** If margin +5% (relative) on selected products → new profit.
- [x] **Output:** table with current vs. projected revenue and profit per product/category.

## 2.5 Outputs for TAB 2
- [ ] **Growth strategy per product** (product, current_revenue, current_margin, strategy_1, strategy_2, projected_increase).
- [ ] **Projected revenue/profit increase** (summary by scenario).

---

# TAB 3 — SHELF / ASSORTMENT OPTIMIZATION

## Goal
Optimize “assortment” (which products to offer where) and prioritize availability.

*In credit context:* “shelf” = product availability in branch/segment; “out-of-stock” = product not offered or suspended in that branch/period.

## 3.1 Product role classification
- [ ] **Must always be in stock** — A-class products, critical for revenue (from ABC).
- [ ] **Optional** — B-class or supporting products.
- [ ] **Remove** — C-class, low margin, slow moving; consider withdrawal from selected branches.

## 3.2 Lost profit estimation
- [ ] **If product absent 10 days:**  
  - Estimate: (monthly_revenue / 30) * 10, or use historical daily rate.  
- [ ] **If product absent 30 days:**  
  - Full month revenue loss + margin loss.  
- [ ] **Break down by branch and category.**

## 3.3 Priority tiers
- [ ] **Tier 1 (Critical)** — high margin + high demand (A in ABC, X or Y in XYZ).
- [ ] **Tier 2 (Supporting)** — medium margin or medium demand.
- [ ] **Tier 3 (Low priority)** — low margin, low volume, or Z demand.

## 3.4 Shelf structure (example)
- [ ] **Tier 1** → always available, promoted.  
- **Tier 2** → standard availability.  
- **Tier 3** → limited or pilot branches only.

## 3.5 Outputs for TAB 3
- [x] **Product priority list** (product, tier, branch, must_stock, optional, remove_candidate).
- [x] **Lost profit estimation** (product, branch, loss_10d, loss_30d).
- [x] **Shelf strategy** (branch, category, tier_1_products, tier_2_products, tier_3_products).

---

# TAB 4 — SUPPLIER ANALYSIS & OPTIMIZATION

## Goal
Improve supplier efficiency (if supplier dimension exists).

## 4.1 Supplier dimensions
- [ ] **By product performance** — revenue and margin per supplier.
- [ ] **By margin** — average margin %, total margin per supplier.
- [ ] **Delivery consistency** — if delivery dates exist: % on-time, average delay.
- [ ] **Stock availability** — % of period with stock (if stock data exists).

## 4.2 Classification
- [ ] **Non-performing** — low revenue, low margin, or high risk (e.g. many NPL contracts linked to supplier’s products).
- [ ] **Low margin** — margin % below threshold.
- [ ] **High risk** — high NPL rate or high volatility.

## 4.3 Contribution metrics
- [ ] **Supplier contribution to revenue** — % of total revenue.
- [ ] **Supplier contribution to profit** — % of total margin.

## 4.4 Proposed actions
- [ ] **Replace** — for C-suppliers (low contribution, low margin).
- [ ] **Renegotiate** — for B-suppliers (improve margin or terms).
- [ ] **Increase volume** — for A-suppliers (strategic partners).

## 4.5 Supplier strategy
- [ ] **A suppliers** → strategic partners (long-term, volume growth).
- [ ] **B suppliers** → maintain, optimize mix.
- [ ] **C suppliers** → replace or reduce share.

## 4.6 Outputs for TAB 4
- [x] **Supplier ranking** (supplier_id, revenue, margin, margin_pct, contribution_pct, class).
- [x] **Optimization plan** (supplier, current_state, action, expected_impact).
- [x] **Expected financial impact** (revenue/profit change if actions applied).

*If no supplier data:* TAB 4 can be “Product origin / channel” or “Partner” analysis using existing dimensions (e.g. product_type as proxy).

---

# ADDITIONAL REQUIREMENTS — SEGMENTATION

All analyses must be breakable by:
- [ ] **Branch**
- [ ] **Month**
- [ ] **Year**
- [ ] **Product category** (or product_type)
- [ ] **Supplier** (if available)

Report must include:
- [ ] Structured tables (export to Excel/CSV).
- [ ] Clear segmentation (filters in UI).
- [ ] Consistent profit/margin calculations (single definition document).
- [ ] Actionable recommendations (text field or tag per product/category/supplier).

---

# ADVANCED ANALYSIS

## 1. Lost profit analysis
- [ ] **Out-of-stock** — profit lost when product was not offered (by branch/month); use historical rate or benchmark.
- [ ] **Low margin items** — opportunity cost: if same volume were in high-margin product, extra profit = volume * (margin_high − margin_low).
- [ ] **Slow turnover** — capital tied in long-term contracts; opportunity cost of capital (e.g. rate * average days).

## 2. Product lifecycle
- [ ] **New** — launched in last N months; track revenue/margin trend.
- [ ] **Growing** — positive growth in volume or revenue vs. previous period.
- [ ] **Declining** — negative growth; flag for assortment or pricing review.

## 3. Branch-level insights
- [ ] **Best performers** — by revenue, margin %, or profit per branch (vs. network average).
- [ ] **Underperformers** — below average; drill-down by product/category to find gaps.
- [ ] **Recommendations** — per branch: which products to push, which to reduce, which to add.

---

# FINAL RESULT — DELIVERABLES

1. **Executive-level report** (PDF or printable):
   - Summary page (KPI, top products, top branches, main risks).
   - One section per TAB (1–4) with tables and short commentary.
   - Advanced analysis summary (lost profit, lifecycle, branch insights).
   - Appendix: methodology, definitions, data period.

2. **Category management dashboard** (UI):
   - **TAB 1** — Product sales & margin (tables + charts by category/branch/month/year).
   - **TAB 2** — Top products & growth strategy (rankings, scenarios, recommendations).
   - **TAB 3** — Assortment/shelf (priority list, lost profit, tier view).
   - **TAB 4** — Supplier (ranking, actions, impact).
   - Filters: branch, month, year, category, supplier.
   - Export: tables to CSV/Excel.

3. **Profit optimization system** (actions):
   - List of “sell more” products.
   - List of “improve margin” products.
   - List of “remove or reduce” products.
   - List of “strategic supplier” vs “replace” suppliers.
   - Optional: link to CCC alerts or tasks for follow-up.

---

# IMPLEMENTATION CHECKLIST (To-Do)

## Backend / Data
- [x] Extend schema: revenue/margin fields or formulas; product_category; supplier (if needed).
- [ ] Implement aggregation functions: product × branch × month × year (sales volume, revenue, margin, margin_pct).
- [ ] ABC/XYZ calculation (revenue cumulative %, CV by product).
- [ ] Lost profit and lifecycle logic (out-of-stock proxy, growth rate).
- [ ] Supplier aggregation and ranking (if supplier dimension added).

## API
- [x] `GET /api/analytics/products/performance` (filters: branch, month, year, category).
- [x] `GET /api/analytics/products/abc-xyz`.
- [x] `GET /api/analytics/products/growth-strategy` (top products, scenarios).
- [x] `GET /api/analytics/products/assortment` (tiers, lost profit).
- [x] `GET /api/analytics/suppliers/ranking` and optimization (if applicable).

## Frontend (Dashboard)
- [x] TAB 1: Product performance table + category summary + trend charts.
- [x] TAB 2: Top products table + strategy cards + scenario forecast panel.
- [x] TAB 3: Priority list + lost profit table + shelf structure view.
- [x] TAB 4: Supplier table + actions + impact summary.
- [x] Global filters (branch, month, year, category, supplier).
- [x] Export buttons for all main tables.

## Report
- [ ] Report template (summary + TAB 1–4 + advanced) with generated tables and charts.
- [ ] Export to PDF or Word (optional).

This specification enables the company to sell the most profitable products, remove weak ones, optimize the supplier network (if applicable), and maximize profit per branch through a single report and dashboard system.
