from http.server import BaseHTTPRequestHandler
import os
import json
import requests
import twstock
import google.generativeai as genai # 改回舊版 SDK
from bs4 import BeautifulSoup

# --- 環境變數 ---
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# --- 初始化 Gemini (舊版設定方式) ---
genai.configure(api_key=GEMINI_API_KEY)

# --- 輔助函式：發送 TG 訊息 ---
def send_telegram_message(chat_id, text):
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=3)
    except Exception as e:
        print(f"TG Send Error: {e}")

# --- 輔助函式：搜尋 Google News RSS ---
def search_news(stock_id):
    try:
        # 搜尋關鍵字：股票代號 + 新聞
        url = f"https://news.google.com/rss/search?q={stock_id}+tw+stock&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
        response = requests.get(url, timeout=4)
        
        if response.status_code == 200:
            soup = BeautifulSoup(response.content, features="xml")
            items = soup.find_all("item", limit=3)
            
            if not items:
                return "（無相關新聞）"
                
            news_text = "【焦點新聞】：\n"
            for item in items:
                title = item.title.text.split(" - ")[0]
                news_text += f"• {title}\n"
            return news_text
            
    except Exception as e:
        print(f"News Error: {e}")
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

                if user_text.isdigit() and len(user_text) == 4:
                    stock_id = user_text
                    send_telegram_message(chat_id, f"🔍 收到 {stock_id}，正在分析中...")

                    # A. 抓股價
                    try:
                        stock = twstock.realtime.get(stock_id)
                    except:
                        stock = {'success': False}

                    if stock.get('success'):
                        price = stock['realtime']['latest_trade_price']
                        if price == '-' and stock['realtime']['best_bid_price']:
                            price = stock['realtime']['best_bid_price'][0]
                        elif price == '-':
                            price = "暫無報價"

                        # B. 搜新聞
                        news_info = search_news(stock_id)

                        # C. Gemini 分析 (超級備用輪胎機制)
                        prompt = f"""
                        你是嚴格的台股教練。
                        股票：{stock_id}
                        現價：{price}
                        新聞：
                        {news_info}
                        
                        請根據以上資訊，用『繁體中文』進行【策略漏斗分析】：
                        1. 技術與動能判斷。
                        2. 新聞面解讀。
                        3. 給出明確操作指令 (買進/觀望/賣出)。
                        請限制在 100 字以內。
                        """
                        
                        ai_reply = "分析失敗"
                        
                        # 定義模型清單：一個不行就換下一個
                        model_list = ['gemini-1.5-flash', 'gemini-pro', 'gemini-1.5-flash-latest']
                        
                        for model_name in model_list:
                            try:
                                model = genai.GenerativeModel(model_name)
                                response = model.generate_content(prompt)
                                ai_reply = response.text
                                break # 成功了就跳出迴圈
                            except Exception as e:
                                print(f"嘗試模型 {model_name} 失敗: {e}")
                                continue # 失敗了就試下一個

                        if ai_reply == "分析失敗":
                            ai_reply = "⚠️ 所有 AI 模型皆忙線或無法連線，請檢查 API Key 權限。"

                        final_msg = f"📊 **{stock_id} 分析報告**\n💰 現價：{price}\n\n{ai_reply}\n\n{news_info}"
                        send_telegram_message(chat_id, final_msg)

                    else:
                        send_telegram_message(chat_id, f"❌ 找不到代號 {stock_id}")

            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'status': 'ok'}).encode('utf-8'))

        except Exception as e:
            print(f"Critical Error: {e}")
            self.send_response(200)
            self.end_headers()
