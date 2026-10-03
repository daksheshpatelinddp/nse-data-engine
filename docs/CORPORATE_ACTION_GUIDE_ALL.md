Here is the complete manual reference guide designed to be placed directly into your repository as **`CORPORATE_ACTIONS_GUIDE.md`**.

It gives you the exact inputs, formulas, and JSON code snippets needed for every corporate action type (Demergers, Spinoffs, Rights Issues, Extraordinary Dividends, Stock Splits, and Bonus Issues).

---

# Corporate Actions Manual Adjustment Guide

This guide explains how to manually calculate and input the **Adjustment Factor** for complex corporate events using the `manual_adjustments.json` control file.

---

## The Master Rule of Price Adjustment

When a corporate action occurs, the stock price drops on its **Ex-Date**. To maintain unbroken technical charts and avoid fake strategy signals:

1. Calculate the **Adjustment Factor** ($Factor < 1.0$).
2. Apply the factor to all historical candles **prior to the Ex-Date**:

$$\text{Adjusted Open} = \text{Raw Open} \times \text{Factor}$$


$$\text{Adjusted High} = \text{Raw High} \times \text{Factor}$$


$$\text{Adjusted Low} = \text{Raw Low} \times \text{Factor}$$


$$\text{Adjusted Close} = \text{Raw Close} \times \text{Factor}$$



---

## 1. Demergers & Spinoffs

On the Ex-Date of a demerger, NSE runs a **Special Pre-open Auction Session** (usually between 9:00 AM and 9:45 AM) to discover the new price of the parent stock.

### Inputs Needed:

* $P_{\text{cum}}$: Closing Price of the parent stock on the last trading day before Ex-Date.
* $P_{\text{ex\_discovered}}$: The discovered Open/Closing price of the parent stock on the Ex-Date morning.

### Formula:

$$\text{Discovered Value of Spun-off Company} = P_{\text{cum}} - P_{\text{ex\_discovered}}$$

$$\text{Factor} = \frac{P_{\text{ex\_discovered}}}{P_{\text{cum}}}$$

### Real Example: Reliance Industries (RIL) & Jio Financial Services (JIOFIN)

* **Cum-Date:** July 19, 2023
* **Ex-Date:** July 20, 2023
* **RIL Cum Close ($P_{\text{cum}}$):** ₹2,841.85
* **RIL Ex-Date Discovered Price ($P_{\text{ex\_discovered}}$):** ₹2,580.00

$$\text{Factor} = \frac{2580.00}{2841.85} = 0.907859$$

### Input in `manual_adjustments.json`:

```json
{
  "RELIANCE": [
    {
      "ex_date": "2023-07-20",
      "event_type": "DEMERGER",
      "description": "Jio Financial Services Demerger",
      "factor": 0.907859
    }
  ]
}

```

---

## 2. Rights Issues

A rights issue allows existing shareholders to purchase additional shares at a discounted offer price.

### Inputs Needed:

* $P_{\text{cum}}$: Cum-rights closing price of the stock.
* $S$: Issue/Subscription price per share of the rights issue.
* $N$: Number of existing shares held.
* $M$: Number of new rights shares offered for every $N$ shares held.

### Formula:

1. **Calculate Theoretical Ex-Rights Price (TERP):**

$$\text{TERP} = \frac{(N \times P_{\text{cum}}) + (M \times S)}{N + M}$$


2. **Calculate Adjustment Factor:**

$$\text{Factor} = \frac{\text{TERP}}{P_{\text{cum}}}$$



### Real Example: Bharti Airtel Rights Issue

* **Cum Close ($P_{\text{cum}}$):** ₹740.00
* **Rights Ratio:** 1 new share for every 14 held ($M=1, N=14$)
* **Issue Price ($S$):** ₹535.00

$$\text{TERP} = \frac{(14 \times 740) + (1 \times 535)}{14 + 1} = \frac{10360 + 535}{15} = ₹726.33$$

$$\text{Factor} = \frac{726.33}{740.00} = 0.981531$$

### Input in `manual_adjustments.json`:

```json
{
  "BHARTIARTL": [
    {
      "ex_date": "2021-09-27",
      "event_type": "RIGHTS_ISSUE",
      "description": "1:14 Rights Issue at 535",
      "factor": 0.981531
    }
  ]
}

```

---

## 3. Extraordinary / Special Dividends (>2% of Stock Price)

Normal dividends are ignored in technical charting. However, extraordinary dividends cause significant gaps that require adjustment.

### Inputs Needed:

* $P_{\text{cum}}$: Cum-dividend closing price on the day prior to Ex-Date.
* $D$: Cash Dividend per share.

### Formula:

$$\text{Factor} = \frac{P_{\text{cum}} - D}{P_{\text{cum}}}$$

### Real Example: Vedanta Special Dividend

* **Cum Close ($P_{\text{cum}}$):** ₹300.00
* **Special Dividend ($D$):** ₹20.50

$$\text{Factor} = \frac{300.00 - 20.50}{300.00} = \frac{279.50}{300.00} = 0.931667$$

### Input in `manual_adjustments.json`:

```json
{
  "VEDL": [
    {
      "ex_date": "2023-04-06",
      "event_type": "SPECIAL_DIVIDEND",
      "description": "Extraordinary Dividend of Rs 20.50",
      "factor": 0.931667
    }
  ]
}

```

---

## 4. Stock Splits (Sub-Divisions)

When face value is reduced (e.g., Face Value ₹10 to ₹2).

### Inputs Needed:

* $FV_{\text{old}}$: Original Face Value.
* $FV_{\text{new}}$: New Face Value.

### Formula:

$$\text{Factor} = \frac{FV_{\text{new}}}{FV_{\text{old}}}$$

### Example: Stock Split 1:5 (Face Value ₹10 to ₹2)

$$\text{Factor} = \frac{2}{10} = 0.200000$$

### Input in `manual_adjustments.json`:

```json
{
  "TATASTEEL": [
    {
      "ex_date": "2022-07-28",
      "event_type": "SPLIT",
      "description": "Stock Split from 10 to 2",
      "factor": 0.200000
    }
  ]
}

```

---

## 5. Bonus Issues

When free additional shares are issued to existing shareholders.

### Inputs Needed:

* $A$: Number of Bonus Shares issued.
* $B$: Number of Existing Shares held.

### Formula:

$$\text{Factor} = \frac{B}{A + B}$$

### Example: Bonus Issue 1:1 ($A=1, B=1$)

$$\text{Factor} = \frac{1}{1 + 1} = 0.500000$$

### Input in `manual_adjustments.json`:

```json
{
  "WIPRO": [
    {
      "ex_date": "2024-12-03",
      "event_type": "BONUS",
      "description": "1:1 Bonus Issue",
      "factor": 0.500000
    }
  ]
}

```

---

## Quick Reference Summary Table

| Action Type | Key Information Required | Calculation Formula |
| --- | --- | --- |
| **Demerger / Spinoff** | Cum Close Price & Ex-Date Discovered Price | $\text{Ex-Date Price} / \text{Cum Price}$ |
| **Rights Issue** | Ratio ($A:B$) & Rights Price ($S$) | $\text{TERP} / P_{\text{cum}}$ |
| **Special Dividend** | Cum Price & Dividend Amount | $(P_{\text{cum}} - D) / P_{\text{cum}}$ |
| **Stock Split** | Old & New Face Value | $FV_{\text{new}} / FV_{\text{old}}$ |
| **Bonus Issue** | Bonus Ratio ($A:B$) | $B / (A + B)$ |

If you want a video explanation of how the math behind stock splits, bonuses, and rights issue adjustments works on the Indian exchanges, check out [Adjustment Factor for Bonus, Stock Splits, Rights | Equity Derivatives](https://www.youtube.com/watch?v=Nl3GrWhcijg&utm_source=gemini). It explains the formula derivations used in derivatives and spot charting adjustments.
