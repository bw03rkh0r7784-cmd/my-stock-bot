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

# --- 初始化 Gemini (使用相容性最好的舊版 SDK) ---
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
        # 搜尋關鍵字：股票代號 + 新聞 (針對台灣來源)
        url = f"https://news.google.com/rss/search?q={stock_id}+tw+stock&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
        response = requests.get(url, timeout=4)
        
        if response.status_code == 200:
            soup = BeautifulSoup(response.content, features="xml")
            items = soup.find_all("item", limit=3)
            
            if not items:
                return "（無相關新聞）"
                
            news_text = "【焦點新聞】：\n"
            for item in items:
                title = item.title.text.split(" - ")[0] # 去除來源後綴
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
            # 1. 安全檢查：確認請求內容
            content_length = int(self.headers.get('Content-Length', 0))
            if content_length == 0:
                self.send_response(200); self.end_headers(); return

            post_data = self.rfile.read(content_length)
            try:
                data = json.loads(post_data.decode('utf-8'))
            except:
                self.send_response(200); self.end_headers(); return

            # 2. 處理 Telegram 訊息
            if "message" in data:
                chat_id = data["message"]["chat"]["id"]
                user_text = data["message"].get("text", "").strip()

                # 如果是股票代號 (4碼數字)
                if user_text.isdigit() and len(user_text) == 4:
                    stock_id = user_text
                    
                    # 回報進度 (避免使用者以為當機)
                    send_telegram_message(chat_id, f"🔍 收到 {stock_id}，正在分析數據與新聞...\n(除錯模式 ON)")

                    # A. 抓股價
                    try:
                        stock = twstock.realtime.get(stock_id)
                    except:
                        stock = {'success': False}

                    if stock.get('success'):
                        price = stock['realtime']['latest_trade_price']
                        # 處理無成交價
                        if price == '-' and stock['realtime']['best_bid_price']:
                            price = stock['realtime']['best_bid_price'][0]
                        elif price == '-':
                            price = "暫無報價"

                        # B. 搜新聞
                        news_info = search_news(stock_id)

                        # C. Gemini 分析 (多模型輪替 + 詳細錯誤回報)
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
                        
                        # 定義模型清單：新舊名稱混合嘗試
                        # gemini-1.5-flash: 最新標準版
                        # gemini-pro: 舊版穩定版
                        # gemini-1.0-pro: 另一種舊版名稱
                        model_list = ['gemini-1.5-flash', 'gemini-pro', 'gemini-1.0-pro']
                        
                        for model_name in model_list:
                            try:
                                model = genai.GenerativeModel(model_name)
                                response = model.generate_content(prompt)
                                ai_reply = response.text
                                break # 成功就跳出迴圈
                            except Exception as e:
                                error_msg = str(e)
                                print(f"嘗試模型 {model_name} 失敗: {error_msg}")
                                # 收集錯誤訊息，以便回傳給使用者看
                                error_log += f"\n❌ {model_name}: {error_msg[:100]}..." 
                                continue

                        # 如果全部失敗，回傳真實錯誤代碼
                        if not ai_reply:
                            ai_reply = f"⚠️ **AI 連線失敗 (Debug Mode)**\n請檢查 API Key 或 Vercel 設定。\n\n詳細錯誤：{error_log}"

                        # D. 回傳最終報告
                        final_msg = f"📊 **{stock_id} 分析報告**\n💰 現價：{price}\n\n{ai_reply}\n\n{news_info}"
                        send_telegram_message(chat_id, final_msg)

                    else:
                        send_telegram_message(chat_id, f"❌ 找不到代號 {stock_id}，請確認。")

            # 3. 回應 Vercel
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'status': 'ok'}).encode('utf-8'))

        except Exception as e:
            print(f"Critical Error: {e}")
            self.send_response(200)
            self.end_headers()
