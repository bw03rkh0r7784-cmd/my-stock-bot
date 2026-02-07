from http.server import BaseHTTPRequestHandler
import os
import json
import requests
import twstock
from google import genai
from bs4 import BeautifulSoup

# --- 環境變數 ---
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# --- 初始化 Gemini ---
client = genai.Client(api_key=GEMINI_API_KEY)

# --- 輔助函式：發送 TG 訊息 ---
def send_telegram_message(chat_id, text):
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=3)
    except Exception as e:
        print(f"TG Send Error: {e}")

# --- 關鍵修正：改用 Google News RSS (速度快、不擋IP) ---
def search_news(stock_id):
    try:
        # 針對台股代號搜尋 (例如: 2330 台積電)
        url = f"https://news.google.com/rss/search?q={stock_id}+tw+stock&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
        response = requests.get(url, timeout=4) # 設定 4 秒超時，避免卡死
        
        if response.status_code == 200:
            soup = BeautifulSoup(response.content, features="xml")
            items = soup.find_all("item", limit=3) # 只抓最新的 3 則
            
            if not items:
                return "（無相關新聞）"
                
            news_text = "【最新新聞】：\n"
            for item in items:
                title = item.title.text
                # 清理標題中多餘的來源名稱 (例如 "- Yahoo奇摩股市")
                title = title.split(" - ")[0]
                news_text += f"• {title}\n"
            return news_text
            
    except Exception as e:
        print(f"RSS Search Error: {e}")
        return "（新聞連線異常，跳過分析）"
    
    return "（查無資料）"

# --- 核心處理邏輯 ---
class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            if content_length == 0:
                self.send_response(200); self.end_headers(); return

            post_data = self.rfile.read(content_length)
            try:
                data = json.loads(post_data.decode('utf-8'))
            except:
                self.send_response(200); self.end_headers(); return

            if "message" in data:
                chat_id = data["message"]["chat"]["id"]
                user_text = data["message"].get("text", "").strip()

                # 收到股票代號
                if user_text.isdigit() and len(user_text) == 4:
                    stock_id = user_text
                    
                    # 1. 先回報「收到指令」(避免使用者以為當機)
                    send_telegram_message(chat_id, f"🔍 收到 {stock_id}，正在分析中...")

                    # 2. 抓股價 (twstock)
                    try:
                        stock = twstock.realtime.get(stock_id)
                    except:
                        stock = {'success': False} # 防爆

                    if stock.get('success'):
                        price = stock['realtime']['latest_trade_price']
                        # 處理無成交價的情況
                        if price == '-' and stock['realtime']['best_bid_price']:
                            price = stock['realtime']['best_bid_price'][0]
                        elif price == '-':
                            price = "暫無報價"

                        # 3. 搜新聞 (使用穩定的 RSS)
                        news_info = search_news(stock_id)

                        # 4. Gemini 綜合分析
                        prompt = f"""
                        你是嚴格的台股教練。
                        股票：{stock_id}
                        現價：{price}
                        新聞：
                        {news_info}
                        
                        請根據以上資訊，用『繁體中文』進行【策略漏斗分析】：
                        1. 技術與動能判斷。
                        2. 新聞面解讀 (利多/利空)。
                        3. 給出明確的操作指令 (買進/觀望/賣出)。
                        請限制在 120 字以內。
                        """
                        
                        try:
                            # 呼叫 Gemini
                            response = client.models.generate_content(
                                model='gemini-1.5-flash',
                                contents=prompt
                            )
                            ai_reply = response.text
                        except Exception as e:
                            ai_reply = f"AI 分析失敗: {str(e)}"

                        # 5. 回傳最終報告
                        final_msg = f"📊 **{stock_id} 分析報告**\n💰 現價：{price}\n\n{ai_reply}\n\n{news_info}"
                        send_telegram_message(chat_id, final_msg)

                    else:
                        send_telegram_message(chat_id, f"❌ 找不到代號 {stock_id}，請確認是否正確。")

            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'status': 'ok'}).encode('utf-8'))

        except Exception as e:
            print(f"Critical Error: {e}")
            self.send_response(200)
            self.end_headers()
