from http.server import BaseHTTPRequestHandler
import json
import yfinance as yf
import datetime
import os
import math

def get_ticker_map():
    try:
        # Resolve to root directory relative to this function inside 'api' folder
        root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        json_path = os.path.join(root_dir, 'daily_market_report.json')
        if os.path.exists(json_path):
            with open(json_path, 'r', encoding='utf-8') as f:
                stocks = json.load(f)
                new_map = {}
                for stock in stocks:
                    code = stock['code']
                    market = stock['market']
                    suffix = '.KS' if market == 'KOSPI' else '.KQ'
                    new_map[code] = f"{code}{suffix}"
                return new_map
    except Exception as e:
        print(f"Error loading dynamic tickers: {e}")
        
    # Default fallback
    return {
        "066570": "066570.KS", # LG전자
        "011070": "011070.KS", # LG이노텍
        "035420": "035420.KS", # 네이버
        "090360": "090360.KQ"  # 로보스타
    }

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
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
            if now.weekday() >= 0 and now.weekday() <= 4 and time_val >= 900 and time_val <= 1530:
                market_state = "REGULAR"
            
            response_payload = {
                "status": "success",
                "marketState": market_state,
                "stocks": formatted_data
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
            self.end_headers()
            self.wfile.write(json.dumps({"status": "error", "message": str(e)}).encode('utf-8'))
