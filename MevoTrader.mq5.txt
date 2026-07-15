//+------------------------------------------------------------------+
//|  MevoTrader.mq5                                                  |
//|  Polling al servidor en la nube + ejecución de señales           |
//|  Requiere: Tools → Options → Expert Advisors → Allow WebRequest  |
//+------------------------------------------------------------------+
#property copyright "MevoTrader"
#property version   "1.11"

#include <Trade\Trade.mqh>
#include <Trade\PositionInfo.mqh>

//--- Enums
enum ENUM_LOT_MODE { LOT_FIXED=0, LOT_PERCENT=1 };
enum ENUM_BE_MODE  { BE_BY_RR=0,  BE_BY_USD=1   };

//--- Inputs
input group "═══ SERVIDOR EN LA NUBE ═══"
input string CloudURL          = "https://tu-app.railway.app"; // URL del servidor Railway
input string EA_Secret         = "CAMBIA_ESTO";                // Clave secreta compartida
input int    PollSeconds       = 5;                            // Segundos entre cada consulta

input group "═══ INSTRUMENTOS ═══"
input string AllowedSymbols    = "XAUUSD,EURUSD,GBPUSD";     // Símbolos permitidos (sin sufijo del broker)
input string SymbolMap         = "XAU:XAUUSD,XAUUSD:XAUUSD,BTC:BTCUSD,EURUSD:EURUSD,GBPUSD:GBPUSD"; // Homologación símbolo_señal:símbolo_base
input string SymbolSuffix      = "";                          // Sufijo del broker (ej: ".fs", ".sa", "+", vacío si no aplica)

input group "═══ GESTIÓN DE RIESGO ═══"
input ENUM_LOT_MODE LotMode    = LOT_FIXED;
input double        LotValue   = 0.10;   // Lotes si FIJO, % balance si PORCENTAJE

input group "═══ SL / TP ═══"
input int    DefaultSL_Ticks   = 50;     // SL en ticks si señal no trae SL
input double RR                = 2.0;    // Risk:Reward para calcular TP

input group "═══ OFFSET Y FILTROS ═══"
input int    OffsetTicks       = 5;      // Ticks de desplazamiento al ejecutar
input int    OffsetMinutes     = 2;      // Minutos de espera antes de ejecutar
input int    MaxDeviationTicks = 20;     // Ticks máx de desviación — cancela si supera

input group "═══ BREAK EVEN ═══"
input ENUM_BE_MODE BE_Mode     = BE_BY_RR;
input double       BE_Value    = 1.0;    // RR o USD según el modo

input group "═══ IDENTIFICACIÓN ═══"
input int    MagicNumber       = 20240101;

//--- Estructura señal pendiente
struct PendingSignal {
   string   id;
   string   symbol;
   string   direction;
   double   sl;
   double   tp;
   double   pe;
   datetime received_at;
   bool     active;
};

//--- Globales
CTrade        Trade;
CPositionInfo PosInfo;
PendingSignal Pending;
datetime      LastPoll = 0;

//+------------------------------------------------------------------+
int OnInit()
{
   Trade.SetExpertMagicNumber(MagicNumber);
   Trade.SetDeviationInPoints(50);
   Pending.active = false;
   EventSetTimer(1);
   Print("MevoTrader v1.11 iniciado | Cloud: ", CloudURL);
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason) { EventKillTimer(); }

//+------------------------------------------------------------------+
void OnTimer()
{
   if(TimeCurrent() - LastPoll >= PollSeconds) {
      LastPoll = TimeCurrent();
      PollServer();
   }
   CheckPendingSignal();
   CheckBreakEven();
}

//+------------------------------------------------------------------+
// Homologa el símbolo recibido al nombre exacto del broker
// SymbolMap = "XAU:XAUUSD,BTC:BTCUSD"
string ResolveSymbol(string symbol)
{
   string map = SymbolMap + ",";
   string search = symbol + ":";
   int pos = StringFind(map, search);
   if(pos < 0) return symbol;
   pos += StringLen(search);
   string result = "";
   for(int i = pos; i < StringLen(map); i++) {
      ushort c = StringGetCharacter(map, i);
      if(c == ',') break;
      result += ShortToString(c);
   }
   return result == "" ? symbol : result;
}

//+------------------------------------------------------------------+
void PollServer()
{
   string url     = CloudURL + "/signal/pending?secret=" + EA_Secret;
   string headers = "Content-Type: application/json\r\n";
   char   post[], result[];
   string result_headers;

   int res = WebRequest("GET", url, headers, 5000, post, result, result_headers);
   if(res == -1) return;

   string body = CharArrayToString(result);
   if(body == "" || body == "null" || StringFind(body, "action") < 0) return;

   string action    = ExtractJSON(body, "action");
   string signal_id = ExtractJSON(body, "id");
   string symbol    = ExtractJSON(body, "symbol");
   string direction = ExtractJSON(body, "direction");
   double sl        = StringToDouble(ExtractJSON(body, "sl"));
   double tp        = StringToDouble(ExtractJSON(body, "tp"));
   double pe        = StringToDouble(ExtractJSON(body, "pe"));

   // Quitar sufijo de punto del mensaje (ej: XAUUSD.a → XAUUSD) y homologar
   int dot = StringFind(symbol, ".");
   if(dot > 0) symbol = StringSubstr(symbol, 0, dot);
   symbol = ResolveSymbol(symbol);

   // Verificar símbolo base (sin sufijo del broker)
   if(!IsSymbolAllowed(symbol)) {
      Print("Símbolo no permitido: ", symbol, " — señal descartada");
      AckSignal(signal_id);
      return;
   }

   // Agregar sufijo del broker para la ejecución real
   string broker_symbol = symbol + SymbolSuffix;
   Print("Señal [", signal_id, "]: ", action, " ", broker_symbol, " ", direction);

   if(action == "close") {
      ClosePositions(broker_symbol);
      AckSignal(signal_id);
      return;
   }

   if(action == "open") {
      if(Pending.active && Pending.id == signal_id) return;
      Pending.id = signal_id; Pending.symbol = broker_symbol;
      Pending.direction = direction; Pending.sl = sl;
      Pending.tp = tp; Pending.pe = pe;
      Pending.received_at = TimeCurrent();
      Pending.active = true;
      AckSignal(signal_id);
   }
}

//+------------------------------------------------------------------+
void AckSignal(string signal_id)
{
   if(signal_id == "") return;
   string url = CloudURL + "/signal/ack/" + signal_id + "?secret=" + EA_Secret;
   string headers = "Content-Type: application/json\r\n";
   char post[], result[];
   string result_headers;
   WebRequest("POST", url, headers, 5000, post, result, result_headers);
}

//+------------------------------------------------------------------+
void CheckPendingSignal()
{
   if(!Pending.active) return;
   if(TimeCurrent() - Pending.received_at < OffsetMinutes * 60) return;

   MqlTick tick;
   if(!SymbolInfoTick(Pending.symbol, tick)) { Pending.active = false; return; }

   double tick_size = SymbolInfoDouble(Pending.symbol, SYMBOL_TRADE_TICK_SIZE);
   double price_now = (Pending.direction == "BUY") ? tick.ask : tick.bid;

   if(Pending.pe > 0) {
      double deviation = MathAbs(price_now - Pending.pe) / tick_size;
      if(deviation > MaxDeviationTicks) {
         Print("Señal cancelada — desviación ", DoubleToString(deviation,1), " ticks");
         Pending.active = false;
         return;
      }
   }

   ExecuteTrade(Pending.symbol, Pending.direction, Pending.sl, Pending.tp);
   Pending.active = false;
}

//+------------------------------------------------------------------+
void ExecuteTrade(string symbol, string direction, double sl_price, double tp_price)
{
   MqlTick tick;
   if(!SymbolInfoTick(symbol, tick)) return;

   double tick_size = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_SIZE);
   int    digits    = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   bool   is_buy    = (direction == "BUY");

   double price = is_buy
      ? tick.ask + OffsetTicks * tick_size
      : tick.bid - OffsetTicks * tick_size;
   price = NormalizeDouble(price, digits);

   if(sl_price <= 0)
      sl_price = is_buy
         ? price - DefaultSL_Ticks * tick_size
         : price + DefaultSL_Ticks * tick_size;
   sl_price = NormalizeDouble(sl_price, digits);

   if(tp_price <= 0) {
      double sl_dist = MathAbs(price - sl_price);
      tp_price = is_buy ? price + sl_dist * RR : price - sl_dist * RR;
   }
   tp_price = NormalizeDouble(tp_price, digits);

   double sl_ticks = MathAbs(price - sl_price) / tick_size;
   double lots     = CalculateLots(symbol, sl_ticks);

   Print("Ejecutando ", direction, " ", symbol, " Lots=", lots,
         " Precio=", price, " SL=", sl_price, " TP=", tp_price);

   bool ok = is_buy
      ? Trade.Buy(lots,  symbol, price, sl_price, tp_price, "MevoTrader")
      : Trade.Sell(lots, symbol, price, sl_price, tp_price, "MevoTrader");

   if(!ok) Print("ERROR: ", Trade.ResultRetcodeDescription());
   else    Print("OK — Ticket: ", Trade.ResultOrder());
}

//+------------------------------------------------------------------+
double CalculateLots(string symbol, double sl_ticks)
{
   if(LotMode == LOT_FIXED) return NormalizeLots(symbol, LotValue);
   double balance    = AccountInfoDouble(ACCOUNT_BALANCE);
   double risk_money = balance * LotValue / 100.0;
   double tick_value = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_VALUE);
   if(tick_value <= 0 || sl_ticks <= 0) return NormalizeLots(symbol, 0.01);
   return NormalizeLots(symbol, risk_money / (sl_ticks * tick_value));
}

double NormalizeLots(string symbol, double lots)
{
   double min_lot  = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
   double max_lot  = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MAX);
   double lot_step = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);
   lots = MathFloor(lots / lot_step) * lot_step;
   return MathMax(min_lot, MathMin(lots, max_lot));
}

//+------------------------------------------------------------------+
void CheckBreakEven()
{
   for(int i = PositionsTotal() - 1; i >= 0; i--) {
      if(!PosInfo.SelectByIndex(i)) continue;
      if(PosInfo.Magic() != MagicNumber) continue;

      double open_price = PosInfo.PriceOpen();
      double sl_now     = PosInfo.StopLoss();
      double tick_size  = SymbolInfoDouble(PosInfo.Symbol(), SYMBOL_TRADE_TICK_SIZE);
      int    digits     = (int)SymbolInfoInteger(PosInfo.Symbol(), SYMBOL_DIGITS);
      bool   is_buy     = (PosInfo.PositionType() == POSITION_TYPE_BUY);

      if(MathAbs(sl_now - open_price) < tick_size) continue;

      bool trigger = false;
      if(BE_Mode == BE_BY_USD) {
         trigger = (PosInfo.Profit() >= BE_Value);
      } else {
         double sl_dist = MathAbs(open_price - sl_now);
         MqlTick tick;
         SymbolInfoTick(PosInfo.Symbol(), tick);
         double price_now = is_buy ? tick.bid : tick.ask;
         trigger = (sl_dist > 0 && MathAbs(price_now - open_price) >= sl_dist * BE_Value);
      }

      if(trigger) {
         double new_sl = NormalizeDouble(open_price, digits);
         Trade.PositionModify(PosInfo.Ticket(), new_sl, PosInfo.TakeProfit());
         Print("BE activado — ticket=", PosInfo.Ticket(), " SL → PE=", new_sl);
      }
   }
}

//+------------------------------------------------------------------+
void ClosePositions(string symbol)
{
   int closed = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--) {
      if(!PosInfo.SelectByIndex(i)) continue;
      if(PosInfo.Magic() != MagicNumber) continue;
      if(PosInfo.Symbol() != symbol) continue;
      if(Trade.PositionClose(PosInfo.Ticket())) closed++;
   }
   Print("Cerradas ", closed, " posiciones de ", symbol);
}

bool IsSymbolAllowed(string symbol)
{
   return (StringFind(AllowedSymbols + ",", symbol + ",") >= 0);
}

string ExtractJSON(string json, string key)
{
   string search = "\"" + key + "\"";
   int pos = StringFind(json, search);
   if(pos < 0) return "";
   pos = StringFind(json, ":", pos);
   if(pos < 0) return "";
   pos++;
   while(pos < StringLen(json) && StringGetCharacter(json, pos) == ' ') pos++;
   bool   is_str = (StringGetCharacter(json, pos) == '"');
   if(is_str) pos++;
   string result = "";
   for(int i = pos; i < StringLen(json); i++) {
      ushort c = StringGetCharacter(json, i);
      if(is_str  && c == '"')           break;
      if(!is_str && (c==',' || c=='}')) break;
      result += ShortToString(c);
   }
   StringTrimLeft(result); StringTrimRight(result);
   return result;
}

void OnTick() {}
