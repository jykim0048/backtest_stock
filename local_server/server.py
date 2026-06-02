import http.server
import socketserver
import json
import math
import os
import sys
import datetime
import traceback

import yfinance as yf

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ticker_utils import get_ticker_map

PORT = 8000


class TradingDashboardHandler(http.server.SimpleHTTPRequestHandler):
    
    def end_headers(self):
        # Enable CORS for development
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200, "OK")
        self.end_headers()

    def do_GET(self):
        # Handle API calls to retrieve real stock prices
        if self.path == '/api/prices':
            self.fetch_real_prices()
        else:
            # Fallback to serving static dashboard.html file
            if self.path == '/' or self.path == '/index.html':
                self.path = '/dashboard.html'
            super().do_GET()

    def fetch_real_prices(self):
        try:
            ticker_map = get_ticker_map()
            tickers_list = list(ticker_map.values())
            
            # Download the last 2 days of historical data for all tickers concurrently
            df = yf.download(tickers_list, period="2d", group_by="ticker", progress=False, threads=True)
            
            formatted_data = {}
            market_state = "CLOSED"
            
            for code, ticker in ticker_map.items():
                try:
                    # yfinance returns MultiIndex if multiple tickers are passed
                    ticker_df = df[ticker] if len(tickers_list) > 1 else df
                    
                    if ticker_df.empty or len(ticker_df) < 1:
                        continue
                        
                    price_val = ticker_df['Close'].iloc[-1]
                    open_val = ticker_df['Open'].iloc[-1]
                    
                    # Safe check for NaN in case of failed download
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
                    print(f"Error parsing ticker {ticker}: {ex}", file=sys.stderr)
            
            # Determine market state in KST
            # Python's zoneinfo can be used, or datetime.timezone
            # Using simple offset for compatibility: KST is UTC+9
            kst_offset = datetime.timezone(datetime.timedelta(hours=9))
            now = datetime.datetime.now(kst_offset)
            time_val = now.hour * 100 + now.minute
            if now.weekday() < 5 and 900 <= time_val <= 1530:
                market_state = "REGULAR"
            
            response_payload = {
                "status": "success",
                "marketState": market_state,
                "stocks": formatted_data
            }
            
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(response_payload).encode('utf-8'))
            return
            
        except Exception as e:
            print(f"Exception in fetch_real_prices: {e}", file=sys.stderr)
            traceback.print_exc()
            self.send_response(500)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            error_response = {
                "status": "error",
                "message": str(e)
            }
            self.wfile.write(json.dumps(error_response).encode('utf-8'))

if __name__ == '__main__':
    # Force working directory to the parent's directory (root) to find dashboard.html
    script_dir = os.path.dirname(os.path.abspath(__file__))
    parent_dir = os.path.dirname(script_dir)
    os.chdir(parent_dir)
    
    # Configure socket reuse to prevent port-in-use errors during quick restarts
    socketserver.TCPServer.allow_reuse_address = True
    
    with socketserver.TCPServer(("", PORT), TradingDashboardHandler) as httpd:
        print("==========================================================")
        print("   QUANT ANTIGRAVITY - REALTIME INTEGRATION SERVER")
        print(f"   Listening at: http://localhost:{PORT}")
        print("   Serving static UI: dashboard.html")
        print("==========================================================")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down integration server...")
            sys.exit(0)
