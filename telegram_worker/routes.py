# telegram_worker/routes.py
# source_topic=None means all messages from that source.

ROUTES = [
    {"name": "Triad FX", "source_chat": -1002817163788, "source_topic": 8988, "dest_chat": -1003918958200, "dest_topic": 2},
    {"name": "Gold Trader Sunny", "source_chat": -1002817163788, "source_topic": 6357, "dest_chat": -1003918958200, "dest_topic": 4},
    {"name": "NS Trades", "source_chat": -1002817163788, "source_topic": 4101, "dest_chat": -1003918958200, "dest_topic": 5},
    {"name": "Platinum Intro Channel", "source_chat": -1002817163788, "source_topic": 13419, "dest_chat": -1003918958200, "dest_topic": 6},
    {"name": "TGF Montana", "source_chat": -1002817163788, "source_topic": 12143, "dest_chat": -1003918958200, "dest_topic": 7},
    {"name": "BroadFX", "source_chat": -1002817163788, "source_topic": 17785, "dest_chat": -1003918958200, "dest_topic": 8},
    {"name": "Trading Central FX VIP", "source_chat": -1002385852838, "source_topic": 40484, "dest_chat": -1003918958200, "dest_topic": 11},
    {"name": "McGarry and Gunter VIP", "source_chat": -1002385852838, "source_topic": 8, "dest_chat": -1003918958200, "dest_topic": 14},
    {"name": "GTMO VIP", "source_chat": -1002385852838, "source_topic": 13, "dest_chat": -1003918958200, "dest_topic": 10},
    {"name": "T Marz", "source_chat": -1002385852838, "source_topic": 65498, "dest_chat": -1003918958200, "dest_topic": 9},
    {"name": "ICT Trader", "source_chat": -1002385852838, "source_topic": 62159, "dest_chat": -1003918958200, "dest_topic": 16},
    {"name": "SOL Gibbs", "source_chat": -1002385852838, "source_topic": 35671, "dest_chat": -1003918958200, "dest_topic": 12},
    {"name": "Sniper Pro Academy", "source_chat": -1002385852838, "source_topic": 65675, "dest_chat": -1003918958200, "dest_topic": 13},
    {"name": "Olly Matthews", "source_chat": -1002385852838, "source_topic": 44752, "dest_chat": -1003918958200, "dest_topic": 15},
    {"name": "LIFETIME VIP", "source_chat": -1003902184158, "source_topic": None, "dest_chat": -1003918958200, "dest_topic": 18},
    {"name": "1% VIP SIGNALS", "source_chat": -1003903223523, "source_topic": None, "dest_chat": -1003918958200, "dest_topic": 17},
    {"name": "Premium I Live Trade", "source_chat": -1003814307529, "source_topic": None, "dest_chat": -1003918958200, "dest_topic": 25},
    {"name": "Market Slayers VIP", "source_chat": -1003838973021, "source_topic": None, "dest_chat": -1003918958200, "dest_topic": 23},
    {"name": "Dropout VIP", "source_chat": -1003840418063, "source_topic": None, "dest_chat": -1003918958200, "dest_topic": 22},
    {"name": "A4xXAUr PREMIUM", "source_chat": -1003423126440, "source_topic": None, "dest_chat": -1003918958200, "dest_topic": 29},
    {"name": "MANJOX TRADES", "source_chat": -1003887896696, "source_topic": None, "dest_chat": -1003918958200, "dest_topic": 31},
    {"name": "BOLZEGHA VIP", "source_chat": -1002900239477, "source_topic": None, "dest_chat": -1003918958200, "dest_topic": 35},
    {"name": "ELX Premium", "source_chat": -1003393003521, "source_topic": None, "dest_chat": -1003918958200, "dest_topic": 36},
    {"name": "GotMeKayed", "source_chat": -1001971304203, "source_topic": None, "dest_chat": -1003918958200, "dest_topic": 33},
    {"name": "KEY / ALCHEMIST 1", "source_chat": -1003681070311, "source_topic": None, "dest_chat": -1003918958200, "dest_topic": 28},
    {"name": "KEY / ALCHEMIST 2", "source_chat": -1003951604481, "source_topic": None, "dest_chat": -1003918958200, "dest_topic": 28},
    {"name": "MSC Premium", "source_chat": -1003909296106, "source_topic": None, "dest_chat": -1003918958200, "dest_topic": 34},
    {"name": "Master Premium", "source_chat": -1003457399744, "source_topic": None, "dest_chat": -1003918958200, "dest_topic": 20},

    # Added requested routes.
    {"name": "1% VIP SIGNALS", "source_chat": -1003903223523, "source_topic": None, "dest_chat": -1003918958200, "dest_topic": 17},
    {"name": "ExposedFX Route 362", "source_chat": -1002444443378, "source_topic": None, "dest_chat": -1003918958200, "dest_topic": 362},

    # Exact same source/destination topic requested. Worker skips exact self-routes to prevent duplicates/loops.
    {"name": "Imperium Internal Topic 363", "source_chat": -1003918958200, "source_topic": 363, "dest_chat": -1003918958200, "dest_topic": 363},
]
