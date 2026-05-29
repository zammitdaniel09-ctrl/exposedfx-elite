# Execution Rules

## Signal acceptance

The system only accepts a signal if it has:

- source
- symbol
- BUY or SELL
- entry price or entry zone
- SL
- TP1

Rejected:

- no SL
- no TP1
- bias only
- analysis only
- TP/SL on wrong side
- very wide zone

## Entry rule

The EA only enters if price is currently inside the entry zone.

BUY:

```txt
ASK >= entry_low
ASK <= entry_high
```

SELL:

```txt
BID >= entry_low
BID <= entry_high
```

If price is outside zone, EA skips the signal.

## Risk

Lot size is based on:

```txt
Risk money = balance × risk %
Loss per 1 lot = SL distance / tick size × tick value
Lot = risk money / loss per 1 lot
```

Risk is split across TP orders.

Default:

```txt
RiskPercent = 1%
MaxRiskPercent = 3%
```

You can set 3-5%, but that is aggressive and should not be default for clients.

## TP split

If TP1, TP2, TP3 exist:

```txt
TP1 order = 50% of risk
TP2 order = 25% of risk
TP3 order = 25% of risk
```

If only TP1 exists:

```txt
100% closes at TP1
```

## Client source selection

EA input:

```txt
AllowedSources = TEST,Market Slayers VIP,Triad FX
```

Only these sources are copied.

## Symbol mapping

EA input example:

```txt
SymbolMap = XAUUSD:XAUUSD.s,GOLD:XAUUSD.s,XAU:XAUUSD.s
```

This handles broker suffixes.
