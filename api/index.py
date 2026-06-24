from http.server import BaseHTTPRequestHandler
import json
import datetime
import os
import sys
import math

# Force yfinance dependencies (appdirs/platformdirs) to use the writable /tmp directory
# in Vercel's read-only serverless filesystem to prevent cache write permission errors.
try:
    import appdirs as ad
    ad.user_cache_dir = lambda *args: "/tmp"
except ImportError:
    pass

try:
    import platformdirs as pd
    pd.user_cache_dir = lambda *args: "/tmp"
except ImportError:
    pass

import yfinance as yf

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ticker_utils import get_ticker_map, _krx_ticker_lookup
from urllib.parse import urlparse, parse_qs

class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_GET(self):
        try:
            # ?codes=005930,000660 → 임의 종목(예: VI/상하한가)의 현재가·등락률 조회.
            # 없으면 기존처럼 워치리스트+장중 종목 전체. KRX 마스터로 정확한 .KS/.KQ ticker.
            codes_param = (parse_qs(urlparse(self.path).query).get("codes") or [""])[0].strip()
            is_codes = bool(codes_param)
            if is_codes:
                krx = _krx_ticker_lookup()
                ticker_map = {}
                for c in codes_param.split(","):
                    c = c.strip().zfill(6)
                    tk = krx.get(c)
                    if tk:
                        ticker_map[c] = tk      # KRX 마스터에 없는 코드(ETN 등)는 스킵
            else:
                ticker_map = get_ticker_map()
            tickers_list = list(ticker_map.values())

            # Download data from Yahoo Finance
            df = yf.download(tickers_list, period="2d", group_by="ticker", progress=False, threads=True)
            
            formatted_data = {}
            market_state = "CLOSED"
            
            for code, ticker in ticker_map.items():
                try:
                    ticker_df = df[ticker] if len(tickers_list) > 1 else df
                    
                    if ticker_df.empty or len(ticker_df) < 1:
                        continue
                        
                    price_val = ticker_df['Close'].iloc[-1]
                    open_val = ticker_df['Open'].iloc[-1]
                    
                    if math.isnan(price_val) or math.isnan(open_val):
                        continue
                        
                    price = float(price_val)
                    open_price = float(open_val)
                    
                    volume_val = ticker_df['Volume'].iloc[-1] if 'Volume' in ticker_df else 0
                    volume = int(volume_val) if not math.isnan(volume_val) else 0
                    
                    if len(ticker_df) >= 2:
                        prev_close_val = ticker_df['Close'].iloc[-2]
                        prev_close = float(prev_close_val) if not math.isnan(prev_close_val) else price
                    else:
                        prev_close = price
                        
                    change_percent = 0.0
                    if prev_close > 0:
                        change_percent = ((price - prev_close) / prev_close) * 100
                        
                    formatted_data[code] = {
                        "price": price,
                        "rate": change_percent,
                        "volume": volume,
                        "open": open_price,
                        "prevClose": prev_close
                    }
                except Exception as ex:
                    print(f"Error parsing ticker {ticker}: {ex}")
            
            # Determine market state in KST (UTC+9)
            kst_offset = datetime.timezone(datetime.timedelta(hours=9))
            now = datetime.datetime.now(kst_offset)
            time_val = now.hour * 100 + now.minute
            if now.weekday() < 5 and 900 <= time_val <= 1530:
                market_state = "REGULAR"

            # Market indices (KOSPI ^KS11 / KOSDAQ ^KQ11) via Yahoo Finance.
            # KRX direct calls are blocked from datacenter IPs, but Yahoo is not.
            # NOTE: Yahoo index data is delayed ~15-20min (not tick-level realtime).
            indices = {}
            try:
                idx_map = {} if is_codes else {"kospi": "^KS11", "kosdaq": "^KQ11"}
                idx_df = (yf.download(list(idx_map.values()), period="2d",
                                      group_by="ticker", progress=False, threads=True)
                          if idx_map else None)
                for key, tk in idx_map.items():
                    try:
                        d = idx_df[tk] if len(idx_map) > 1 else idx_df
                        if d.empty:
                            continue
                        cur = float(d['Close'].iloc[-1])
                        if math.isnan(cur):
                            continue
                        prev = float(d['Close'].iloc[-2]) if len(d) >= 2 else cur
                        if math.isnan(prev) or prev <= 0:
                            prev = cur
                        rate = ((cur - prev) / prev) * 100 if prev > 0 else 0.0
                        indices[key] = {"price": cur, "rate": rate}
                    except Exception as ex:
                        print(f"Error parsing index {tk}: {ex}")
            except Exception as ex:
                print(f"Index download failed: {ex}")

            response_payload = {
                "status": "success",
                "marketState": market_state,
                "stocks": formatted_data,
                "indices": indices
            }
            
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type')
            self.end_headers()
            self.wfile.write(json.dumps(response_payload).encode('utf-8'))
            
        except Exception as e:
            self.send_response(500)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({"status": "error", "message": str(e)}).encode('utf-8'))
