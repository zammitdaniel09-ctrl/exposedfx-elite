from pathlib import Path
from telegram_worker.universal_signal_ai import extract_and_format
from telegram_worker.provider_profiles import is_promo_text, apply_provider_profile

tests = {
    "sell_stop": """SELLS BELOW 4458
TP1: 4450
TP2: 4448
TP3: 4430
SL: 100 PIPS""",

    "buy_stop": """BUYS ABOVE 4458
TP1: 4465
TP2: 4475
TP3: 4490
SL: 100 PIPS""",

    "sell_limit": """SELL LIMIT XAUUSD 4458
SL 4468
TP1 4448""",

    "buy_limit": """BUY LIMIT XAUUSD 4458
SL 4448
TP1 4468""",
}

for name, text in tests.items():
    print("=" * 70)
    print(name)
    res = extract_and_format(text, "TEST", 1)
    assert res, f"{name} failed to parse"
    print(res["message"])
    parsed = res["parsed"]
    print(parsed)

    if name == "sell_stop":
        assert parsed.get("order_type") == "SELL_STOP", parsed
        assert "SELL STOP" in res["message"], res["message"]

    if name == "buy_stop":
        assert parsed.get("order_type") == "BUY_STOP", parsed
        assert "BUY STOP" in res["message"], res["message"]

    if name == "sell_limit":
        assert parsed.get("order_type") in ("SELL_LIMIT", "LIMIT"), parsed
        assert "SELL STOP" not in res["message"], res["message"]

    if name == "buy_limit":
        assert parsed.get("order_type") in ("BUY_LIMIT", "LIMIT"), parsed
        assert "BUY STOP" not in res["message"], res["message"]

promo = "Just arrived at home, next 10 that checks my Instagram gets free lifetime VIP access"
assert is_promo_text(promo), "promo filter failed"

profiled = apply_provider_profile("SELLS BELOW 4458\nSL 4468\nTP1 4448", 23)
assert "XAUUSD" in profiled, profiled

files = {
    "runtime guard": "telegram_worker/runtime_guard.py",
    "provider profiles": "telegram_worker/provider_profiles.py",
    "signal hub": "telegram_worker/worker_signal_hub.py",
    "imperium worker": "telegram_worker/worker_fixed.py",
    "clean forwarder": "telegram_worker/worker_clean_signal_forwarder.py",
}

for label, file in files.items():
    s = Path(file).read_text(encoding="utf-8-sig")
    print(label, "OK")

signal_hub = Path("telegram_worker/worker_signal_hub.py").read_text(encoding="utf-8-sig")
required = [
    "Signal lifecycle tracking active: True",
    "Provider profiles active: True",
    "Promo filter active: True",
    "Tolerance dedupe active: True",
    "def maybe_send_lifecycle_update",
    "def move_update_from_text",
]

for item in required:
    assert item in signal_hub, f"missing {item}"

print("ALL SELECTED UPGRADE TESTS PASSED")
