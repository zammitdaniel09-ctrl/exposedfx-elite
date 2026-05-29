//+------------------------------------------------------------------+
//| ImperiumAutoCopierClient_v1.mq5                                  |
//| MVP Telegram Signal -> HTTP Server -> MT5 Copier EA              |
//| Demo-first safety version.                                       |
//+------------------------------------------------------------------+
#property strict
#property version   "1.00"

#include <Trade/Trade.mqh>

CTrade trade;

//-------------------- Inputs --------------------

input string ServerURL              = "http://127.0.0.1:8000";
input string ClientToken            = "change-this-token";
input string AllowedSources         = "TEST,Market Slayers VIP,Triad FX,Gold Trader Sunny";
input string SymbolMap              = "XAUUSD:XAUUSD.s,GOLD:XAUUSD.s,XAU:XAUUSD.s";
input double RiskPercent            = 1.0;
input double MaxRiskPercent         = 3.0;
input bool   OnlyEnterInsideZone    = true;
input int    MaxSignalAgeMinutes    = 30;
input int    PollSeconds            = 5;
input int    MaxSpreadPoints        = 80;
input double MaxDailyLossPercent    = 10.0;
input int    MaxOpenTrades          = 3;
input int    MagicBase              = 260529;
input int    DeviationPoints        = 30;
input bool   CopyEnabled            = true;
input bool   DebugLogs              = true;

//-------------------- State --------------------

datetime lastPoll = 0;
string copiedIds = "|";

//-------------------- Utilities --------------------

void Log(string msg)
{
   if(DebugLogs)
      Print("[ImperiumAutoCopier] ", msg);
}

string Trim(string s)
{
   StringTrimLeft(s);
   StringTrimRight(s);
   return s;
}

bool CsvContains(string csv, string item)
{
   string parts[];
   int n = StringSplit(csv, ',', parts);

   for(int i = 0; i < n; i++)
   {
      if(StringCompare(Trim(parts[i]), Trim(item), false) == 0)
         return true;
   }

   return false;
}

string JsonString(string obj, string key)
{
   string pattern = "\"" + key + "\"";
   int p = StringFind(obj, pattern);
   if(p < 0) return "";

   int colon = StringFind(obj, ":", p);
   if(colon < 0) return "";

   int q1 = StringFind(obj, "\"", colon + 1);
   if(q1 < 0) return "";

   int q2 = StringFind(obj, "\"", q1 + 1);
   if(q2 < 0) return "";

   return StringSubstr(obj, q1 + 1, q2 - q1 - 1);
}

double JsonDouble(string obj, string key)
{
   string pattern = "\"" + key + "\"";
   int p = StringFind(obj, pattern);
   if(p < 0) return 0.0;

   int colon = StringFind(obj, ":", p);
   if(colon < 0) return 0.0;

   int start = colon + 1;
   while(start < StringLen(obj))
   {
      string ch = StringSubstr(obj, start, 1);
      if(ch != " " && ch != "\t") break;
      start++;
   }

   int end = start;
   while(end < StringLen(obj))
   {
      string ch = StringSubstr(obj, end, 1);
      if((ch >= "0" && ch <= "9") || ch == "." || ch == "-")
         end++;
      else
         break;
   }

   string num = StringSubstr(obj, start, end - start);
   return StringToDouble(num);
}

int JsonInt(string obj, string key)
{
   return (int)JsonDouble(obj, key);
}

bool JsonIsNullOrMissing(string obj, string key)
{
   string pattern = "\"" + key + "\"";
   int p = StringFind(obj, pattern);
   if(p < 0) return true;

   int colon = StringFind(obj, ":", p);
   if(colon < 0) return true;

   string rest = StringSubstr(obj, colon + 1, 8);
   StringToLower(rest);

   if(StringFind(rest, "null") >= 0)
      return true;

   return false;
}

string MapSymbol(string raw)
{
   string r = Trim(raw);
   string pairs[];
   int n = StringSplit(SymbolMap, ',', pairs);

   for(int i = 0; i < n; i++)
   {
      string kv[];
      int m = StringSplit(pairs[i], ':', kv);
      if(m == 2)
      {
         string from = Trim(kv[0]);
         string to = Trim(kv[1]);

         if(StringCompare(from, r, false) == 0)
            return to;
      }
   }

   return r;
}

bool AlreadyCopied(int signalId)
{
   string key = "|" + IntegerToString(signalId) + "|";
   return StringFind(copiedIds, key) >= 0;
}

void MarkLocalCopied(int signalId)
{
   string key = "|" + IntegerToString(signalId) + "|";
   if(StringFind(copiedIds, key) < 0)
      copiedIds += IntegerToString(signalId) + "|";
}

double NormalizeVolume(string symbol, double vol)
{
   double minLot = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
   double maxLot = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MAX);
   double step   = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);

   if(vol < minLot) vol = minLot;
   if(vol > maxLot) vol = maxLot;

   if(step > 0)
      vol = MathFloor(vol / step) * step;

   int digits = 2;
   if(step == 0.001) digits = 3;
   if(step == 0.01) digits = 2;
   if(step == 0.1) digits = 1;

   return NormalizeDouble(vol, digits);
}

double CalcLotByRisk(string symbol, double entry, double sl, double riskPercent, double weight)
{
   double balance = AccountInfoDouble(ACCOUNT_BALANCE);
   double riskPct = MathMin(riskPercent, MaxRiskPercent);
   if(riskPct <= 0) riskPct = 1.0;

   double riskMoney = balance * (riskPct / 100.0) * weight;

   double tickSize  = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_SIZE);
   double tickValue = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_VALUE);

   double dist = MathAbs(entry - sl);

   if(tickSize <= 0 || tickValue <= 0 || dist <= 0)
      return 0.0;

   double lossPerLot = (dist / tickSize) * tickValue;

   if(lossPerLot <= 0)
      return 0.0;

   double lots = riskMoney / lossPerLot;
   return NormalizeVolume(symbol, lots);
}

bool PriceInsideZone(string symbol, string direction, double low, double high)
{
   double ask = SymbolInfoDouble(symbol, SYMBOL_ASK);
   double bid = SymbolInfoDouble(symbol, SYMBOL_BID);

   double lo = MathMin(low, high);
   double hi = MathMax(low, high);

   if(direction == "BUY")
      return (ask >= lo && ask <= hi);

   if(direction == "SELL")
      return (bid >= lo && bid <= hi);

   return false;
}

int CountOpenTrades()
{
   int count = 0;

   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(PositionSelectByTicket(ticket))
      {
         long magic = PositionGetInteger(POSITION_MAGIC);
         if(magic >= MagicBase && magic < MagicBase + 100000)
            count++;
      }
   }

   return count;
}

double TodayClosedProfit()
{
   datetime now = TimeCurrent();
   MqlDateTime t;
   TimeToStruct(now, t);
   t.hour = 0;
   t.min = 0;
   t.sec = 0;
   datetime dayStart = StructToTime(t);

   HistorySelect(dayStart, now);

   double profit = 0;

   for(int i = HistoryDealsTotal() - 1; i >= 0; i--)
   {
      ulong deal = HistoryDealGetTicket(i);

      long magic = (long)HistoryDealGetInteger(deal, DEAL_MAGIC);
      if(magic >= MagicBase && magic < MagicBase + 100000)
      {
         profit += HistoryDealGetDouble(deal, DEAL_PROFIT);
         profit += HistoryDealGetDouble(deal, DEAL_COMMISSION);
         profit += HistoryDealGetDouble(deal, DEAL_SWAP);
      }
   }

   return profit;
}

bool DailyLossOk()
{
   if(MaxDailyLossPercent <= 0)
      return true;

   double balance = AccountInfoDouble(ACCOUNT_BALANCE);
   double limit = -balance * (MaxDailyLossPercent / 100.0);
   double today = TodayClosedProfit();

   if(today <= limit)
   {
      Log("Daily loss limit reached. Today P/L=" + DoubleToString(today, 2));
      return false;
   }

   return true;
}

bool SpreadOk(string symbol)
{
   double ask = SymbolInfoDouble(symbol, SYMBOL_ASK);
   double bid = SymbolInfoDouble(symbol, SYMBOL_BID);
   double point = SymbolInfoDouble(symbol, SYMBOL_POINT);

   if(point <= 0)
      return true;

   double spreadPoints = (ask - bid) / point;

   if(spreadPoints > MaxSpreadPoints)
   {
      Log("Spread too high on " + symbol + ": " + DoubleToString(spreadPoints, 1));
      return false;
   }

   return true;
}

//-------------------- HTTP --------------------

string HttpGetPending()
{
   string url = ServerURL + "/api/v1/signals/pending_ea?token=" + ClientToken + "&client_token=" + ClientToken + "&max_age_minutes=" + IntegerToString(MaxSignalAgeMinutes);

   char data[];
   char result[];
   string resultHeaders;

   string headers = "";

   ResetLastError();

   int code = WebRequest(
      "GET",
      url,
      headers,
      10000,
      data,
      result,
      resultHeaders
   );

   if(code == -1)
   {
      Log("WebRequest GET failed. Error=" + IntegerToString(GetLastError()) + ". Add URL in MT5 Tools -> Options -> Expert Advisors.");
      return "";
   }

   string body = CharArrayToString(result);

   if(code < 200 || code >= 300)
   {
      Log("HTTP GET status " + IntegerToString(code) + ": " + body);
      return "";
   }

   return body;
}

void PostCopyResult(int signalId, string status, string detail)
{
   string url = ServerURL + "/api/v1/signals/" + IntegerToString(signalId) + "/copy_result";

   string body =
      "{"
      "\"client_token\":\"" + ClientToken + "\","
      "\"mt5_account\":\"" + IntegerToString((int)AccountInfoInteger(ACCOUNT_LOGIN)) + "\","
      "\"status\":\"" + status + "\","
      "\"detail\":\"" + detail + "\""
      "}";

   char data[];
   StringToCharArray(body, data, 0, WHOLE_ARRAY, CP_UTF8);

   char result[];
   string resultHeaders;

   string headers =
      "Content-Type: application/json\r\n"
      "X-AUTO-TOKEN: " + ClientToken + "\r\n";

   ResetLastError();

   int code = WebRequest(
      "POST",
      url,
      headers,
      10000,
      data,
      result,
      resultHeaders
   );

   if(code == -1)
      Log("WebRequest POST failed. Error=" + IntegerToString(GetLastError()));
}

//-------------------- JSON object splitting --------------------

int ExtractObjects(string json, string &objects[])
{
   ArrayResize(objects, 0);

   int depth = 0;
   int start = -1;
   int count = 0;

   for(int i = 0; i < StringLen(json); i++)
   {
      string ch = StringSubstr(json, i, 1);

      if(ch == "{")
      {
         if(depth == 0)
            start = i;
         depth++;
      }
      else if(ch == "}")
      {
         depth--;
         if(depth == 0 && start >= 0)
         {
            string obj = StringSubstr(json, start, i - start + 1);
            ArrayResize(objects, count + 1);
            objects[count] = obj;
            count++;
            start = -1;
         }
      }
   }

   return count;
}

//-------------------- Execution --------------------

bool OpenOrder(string symbol, string direction, double lots, double sl, double tp, int signalId, string source)
{
   trade.SetExpertMagicNumber(MagicBase + signalId);
   trade.SetDeviationInPoints(DeviationPoints);

   string comment = "IFX_COPY_" + IntegerToString(signalId) + "_" + source;

   bool ok = false;

   if(direction == "BUY")
      ok = trade.Buy(lots, symbol, 0.0, sl, tp, comment);
   else if(direction == "SELL")
      ok = trade.Sell(lots, symbol, 0.0, sl, tp, comment);

   if(!ok)
   {
      Log("Order failed: " + IntegerToString(trade.ResultRetcode()) + " " + trade.ResultRetcodeDescription());
      return false;
   }

   Log("Order opened: " + direction + " " + symbol + " lots=" + DoubleToString(lots, 2) + " signal=" + IntegerToString(signalId));
   return true;
}

void ProcessSignal(string obj)
{
   int signalId = JsonInt(obj, "id");
   if(signalId <= 0)
      return;

   if(AlreadyCopied(signalId))
      return;

   string source = JsonString(obj, "source");
   if(!CsvContains(AllowedSources, source))
   {
      Log("Skipped source not allowed: " + source);
      MarkLocalCopied(signalId);
      return;
   }

   if(!CopyEnabled)
   {
      Log("Copy disabled.");
      return;
   }

   if(!DailyLossOk())
      return;

   if(CountOpenTrades() >= MaxOpenTrades)
   {
      Log("Max open trades reached.");
      return;
   }

   string rawSymbol = JsonString(obj, "symbol");
   string symbol = MapSymbol(rawSymbol);
   string direction = JsonString(obj, "direction");

   if(!SymbolSelect(symbol, true))
   {
      Log("Symbol not available: " + symbol);
      PostCopyResult(signalId, "SKIPPED", "symbol not available");
      MarkLocalCopied(signalId);
      return;
   }

   if(!SpreadOk(symbol))
      return;

   double entryLow = JsonDouble(obj, "entry_low");
   double entryHigh = JsonDouble(obj, "entry_high");
   double sl = JsonDouble(obj, "sl");
   double tp1 = JsonDouble(obj, "tp1");
   double tp2 = JsonIsNullOrMissing(obj, "tp2") ? 0.0 : JsonDouble(obj, "tp2");
   double tp3 = JsonIsNullOrMissing(obj, "tp3") ? 0.0 : JsonDouble(obj, "tp3");

   if(OnlyEnterInsideZone && !PriceInsideZone(symbol, direction, entryLow, entryHigh))
   {
      Log("Skipped: price not inside zone for signal " + IntegerToString(signalId));
      return;
   }

   double ask = SymbolInfoDouble(symbol, SYMBOL_ASK);
   double bid = SymbolInfoDouble(symbol, SYMBOL_BID);
   double entryNow = (direction == "BUY") ? ask : bid;

   double tps[3];
   double weights[3];

   int tpCount = 0;

   if(tp1 > 0)
   {
      tps[tpCount] = tp1;
      weights[tpCount] = 0.50;
      tpCount++;
   }

   if(tp2 > 0)
   {
      tps[tpCount] = tp2;
      weights[tpCount] = 0.25;
      tpCount++;
   }

   if(tp3 > 0)
   {
      tps[tpCount] = tp3;
      weights[tpCount] = 0.25;
      tpCount++;
   }

   if(tpCount == 1)
      weights[0] = 1.0;

   bool anyOpened = false;

   for(int i = 0; i < tpCount; i++)
   {
      double lots = CalcLotByRisk(symbol, entryNow, sl, RiskPercent, weights[i]);

      if(lots <= 0)
      {
         Log("Lot calc failed for signal " + IntegerToString(signalId));
         continue;
      }

      if(OpenOrder(symbol, direction, lots, sl, tps[i], signalId, source))
         anyOpened = true;
   }

   if(anyOpened)
   {
      MarkLocalCopied(signalId);
      PostCopyResult(signalId, "COPIED", "opened in MT5");
   }
}

//-------------------- MT5 events --------------------

int OnInit()
{
   EventSetTimer(PollSeconds);
   trade.SetExpertMagicNumber(MagicBase);

   Log("EA started.");
   Log("ServerURL=" + ServerURL);
   Log("AllowedSources=" + AllowedSources);
   Log("RiskPercent=" + DoubleToString(RiskPercent, 2));

   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   EventKillTimer();
   Log("EA stopped.");
}

void OnTimer()
{
   if(TimeCurrent() - lastPoll < PollSeconds)
      return;

   lastPoll = TimeCurrent();

   string json = HttpGetPending();
   if(json == "")
      return;

   string objects[];
   int n = ExtractObjects(json, objects);

   if(n <= 0)
      return;

   for(int i = 0; i < n; i++)
      ProcessSignal(objects[i]);
}

void OnTick()
{
   // Execution is timer-based.
}
//+------------------------------------------------------------------+
