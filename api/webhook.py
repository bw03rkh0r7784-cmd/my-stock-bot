from http.server import BaseHTTPRequestHandler
import os
import json
import requests
import twstock
from google import genai  # 使用新版 SDK
from duckduckgo_search import DDGS

# --- 環境變數 ---
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# --- 初始化新版 Gemini Client ---
client = genai.Client(api_key=GEMINI_API_KEY)

# --- 輔助函式：發送 TG 訊息 ---
def send_telegram_message(chat_id, text):
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"Telegram 发送失败: {e}")

# --- 輔助函式：搜尋新聞 ---
def search_news(stock_id):
    news_summary = ""
    try:
        with DDGS() as ddgs:
            # 簡化搜尋邏輯以避免超時，只搜一次綜合關鍵字
            keywords = f"{stock_id} 股票新聞 site:cnyes.com OR site:moneydj.com"
            results = list(ddgs.text(keywords, region='tw-tzh', max_results=2))
            
            if results:
                news_summary += "【焦點新聞】：\n"
                for r in results:
                    news_summary += f"- [{r['title']}]({r['href']})\n"
            else:
                news_summary = "（暫無重大新聞）"
                
    except Exception as e:
        print(f"News Error: {e}")
        news_summary = "（新聞搜尋連線逾時，跳過分析）"
    
    return news_summary

# --- 核心處理邏輯 ---
class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            # 1. 安全防護：先檢查有沒有收到資料
            content_length = int(self.headers.get('Content-Length', 0))
            if content_length == 0:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'Empty Request')
                return

            # 2. 讀取資料
            post_data = self.rfile.read(content_length)
            try:
                data = json.loads(post_data.decode('utf-8'))
            except json.JSONDecodeError:
                # 這是解決 500 Error 的關鍵：如果資料不是 JSON，優雅結束
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'Invalid JSON')
                return

            # 3. 處理 TG 訊息
            if "message" in data:
                chat_id = data["message"]["chat"]["id"]
                user_text = data["message"].get("text", "").strip()

                if user_text.isdigit() and len(user_text) == 4:
                    stock_id = user_text
                    send_telegram_message(chat_id, f"🔍 收到代號 {stock_id}，正在分析數據與新聞...請稍候")

                    # A. 抓股價
                    stock = twstock.realtime.get(stock_id)
                    
                    if stock['success']:
                        price = stock['realtime']['latest_trade_price']
                        # 若盤中無成交價，嘗試取最佳買賣價
                        if price == '-' and stock['realtime']['best_bid_price']:
                            price = stock['realtime']['best_bid_price'][0]
                        
                        market_info = f"股票：{stock_id} | 現價：{price}"

                        # B. 搜新聞
                        news_info = search_news(stock_id)

                        # C. Gemini 分析 (新版語法)
                        prompt = f"""
                        你是嚴格的台股交易教練。
                        【數據】{market_info}
                        【新聞】{news_info}
                        
                        請根據數據與新聞，執行「策略漏斗分析」：
                        1. 技術面：漲跌動能如何？
                        2. 消息面：是否有法人連買或利多？
                        3. 操作建議：給出一個明確的指令（買進/觀望/逃跑）。
                        請用繁體中文，100字以內。
                        """
                        
                        # 新版 API 呼叫方式
                        response = client.models.generate_content(
                            model='gemini-1.5-flash',
                            contents=prompt
                        )
                        
                        final_reply = f"📊 **{stock_id} 分析報告**\n💰 現價：{price}\n\n{response.text}\n\n{news_info}"
                        send_telegram_message(chat_id, final_reply)

                    else:
                        send_telegram_message(chat_id, f"❌ 找不到 {stock_id} 的即時報價，請確認代號。")

            # 4. 回應 Vercel (打卡下班)
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'status': 'ok'}).encode('utf-8'))

        except Exception as e:
            # 最後一道防線：印出錯誤但不讓伺服器崩潰
            print(f"Critical Error: {e}")
            self.send_response(200) # 回傳 200 騙過 Telegram 避免它一直重試
            self.end_headers()
