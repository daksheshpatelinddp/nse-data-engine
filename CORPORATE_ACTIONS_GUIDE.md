# Corporate Actions Adjustment Guide

This repository uses `manual_adjustments.json` to adjust historical Open, High, Low, and Close (OHLC) prices retroactively for complex corporate events that cannot be parsed automatically from simple text fields.

---

## 1. How Price Adjustments Work

When a company undergoes a demerger, spinoff, or rights issue, its stock price drops on the **Ex-Date** (the effective date of the event). 

To prevent false technical signals or broken charts:
1. We calculate a **Backward Adjustment Factor** ($Factor < 1.0$).
2. Every historical candle **before the Ex-Date** is multiplied by this factor:
   $$\text{Adjusted Price} = \text{Historical Price} \times \text{Factor}$$

---

## 2. Calculation Formulas & Examples

### A. Demergers & Spinoffs

On the Ex-Date, NSE conducts a **Special Pre-open Auction Session** to discover the new price of the parent company. 

#### Formula:
$$\text{Discovered Value of Spun-off Entity} = \text{Cum-Date Close Price} - \text{Ex-Date Special Open Price}$$

$$\text{Factor} = \frac{\text{Cum-Date Close Price} - \text{Discovered Value}}{\text{Cum-Date Close Price}} = \frac{\text{Ex-Date Special Open Price}}{\text{Cum-Date Close Price}}$$

#### Real-World Example: Reliance Industries (RIL) & Jio Financial Services (JIOFIN)
* **Cum-Date:** July 19, 2023
* **Record / Ex-Date:** July 20, 2023
* **RIL Closing Price on July 19, 2023:** ₹2,841.85
* **Discovered Jio Financial Price:** ₹261.85
* **RIL Ex-Date Adjusted Value:** ₹2,580.00 ($2,841.85 - 261.85$)

$$\text{Factor} = \frac{2580.00}{2841.85} = 0.907859$$

#### JSON Entry (`manual_adjustments.json`):
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