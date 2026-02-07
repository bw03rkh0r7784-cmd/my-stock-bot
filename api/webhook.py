from http.server import BaseHTTPRequestHandler
import os
import json
import requests
import twstock
import google.generativeai as genai
from bs4 import BeautifulSoup

# --- 環境變數 ---
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# --- 初始化 Gemini ---
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

# --- 輔助函式：發送 TG 訊息 ---
def send_telegram_message(chat_id, text):
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"TG Send Error: {e}")

# --- 輔助函式：搜尋 Google News RSS ---
def search_news(stock_id):
    try:
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
                    
                    send_telegram_message(chat_id, f"🔍 收到 {stock_id}，正在啟用最新模型 (2.5/3.0) 分析中...")

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

                        # C. Gemini 分析
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
                        
                        ai_reply = ""
                        error_log = ""
                        success_model = ""
                        
                        # --- 2026年 2月 最新模型清單 ---
                        # 根據 Google 官方公告：
                        # 1. gemini-2.5-flash (目前主力穩定版)
                        # 2. gemini-3-flash-preview (最新一代預覽版)
                        # 3. gemini-2.0-flash (將於 2026/3/31 退休)
                        model_list = [
                            'gemini-2.5-flash',       # 優先：2.5 穩定版
                            'gemini-2.0-flash',       # 次選：2.0 舊版 (尚未退休)
                            'gemini-3-flash-preview', # 嘗試：3.0 預覽版
                            'gemini-2.0-flash-exp',   # 備用：2.0 實驗版
                            'gemini-1.5-flash'        # 最後手段：1.5 (可能已失效)
                        ]
                        
                        for model_name in model_list:
                            try:
                                model = genai.GenerativeModel(model_name)
                                response = model.generate_content(prompt)
                                ai_reply = response.text
                                success_model = model_name
                                break 
                            except Exception as e:
                                error_msg = str(e)
                                # 紀錄錯誤但不中斷，繼續試下一個
                                error_log += f"\n❌ {model_name}: 失敗"
                                continue

                        if not ai_reply:
                            ai_reply = f"⚠️ 所有模型皆連線失敗。\n請檢查 API Key 權限。\n錯誤紀錄：{error_log}"
                        else:
                            ai_reply += f"\n(🤖 使用模型：{success_model})"

                        final_msg = f"📊 **{stock_id} 分析報告**\n💰 現價：{price}\n\n{ai_reply}\n\n{news_info}"
                        send_telegram_message(chat_id, final_msg)

                    else:
                        send_telegram_message(chat_id, f"❌ 找不到代號 {stock_id}")

            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({'status': 'ok'}).encode('utf-8'))

        except Exception as e:
            print(f"Critical Error: {e}")
            self.send_response(200)
            self.end_headers()
