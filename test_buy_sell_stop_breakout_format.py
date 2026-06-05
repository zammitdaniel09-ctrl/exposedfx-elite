from telegram_worker.universal_signal_ai import extract_and_format

tests = {
    "sell_stop_below": """SELLS BELOW 4458
TP1: 4450
TP2: 4448
TP3: 4430
TP4: OPEN
SL: 100 PIPS""",

    "buy_stop_above": """BUYS ABOVE 4458
TP1: 4465
TP2: 4475
TP3: 4490
TP4: OPEN
SL: 100 PIPS""",

    "normal_sell_zone": """SELL XAUUSD
4344-4348
SL 4354
TP1 4342
TP2 4340""",

    "normal_buy_limit": """Xau/usd buy limit:4303
Sl:4300
Tp:4310
Tp:4330
Tp:4350""",
}

for name, text in tests.items():
    print("=" * 70)
    print(name)
    result = extract_and_format(text, "TEST", 1)
    assert result, f"{name} did not parse"
    print(result["message"])
    parsed = result["parsed"]
    print(parsed)

    if name == "sell_stop_below":
        assert parsed.get("order_type") == "SELL_STOP", parsed
        assert "SELL STOP" in result["message"], result["message"]
        assert "Trigger Below" in result["message"], result["message"]
        assert round(parsed["sl"], 2) == 4468.00, parsed

    if name == "buy_stop_above":
        assert parsed.get("order_type") == "BUY_STOP", parsed
        assert "BUY STOP" in result["message"], result["message"]
        assert "Trigger Above" in result["message"], result["message"]
        assert round(parsed["sl"], 2) == 4448.00, parsed

    if name == "normal_sell_zone":
        assert parsed.get("order_type") != "SELL_STOP", parsed
        assert "SELL STOP" not in result["message"], result["message"]

    if name == "normal_buy_limit":
        assert parsed.get("order_type") != "BUY_STOP", parsed
        assert "BUY STOP" not in result["message"], result["message"]

print("ALL BUY/SELL STOP BREAKOUT FORMAT TESTS PASSED")
