from http.server import BaseHTTPRequestHandler
import os
import json
import requests
import twstock
import google.generativeai as genai
from duckduckgo_search import DDGS

# --- 設定環境變數 ---
# 請在 Vercel 後台 Environment Variables 設定這兩個變數
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# --- 設定 Gemini ---
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash') # 使用 Flash 模型以確保速度

# --- 輔助函式：發送 TG 訊息 ---
def send_telegram_message(chat_id, text):
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"Telegram 发送失败: {e}")

# --- 輔助函式：搜尋新聞 (雙軌) ---
def search_news(stock_id):
    news_summary = ""
    try:
        with DDGS() as ddgs:
            # 1. 中文新聞 (鉅亨網/MoneyDJ)
            keywords_tw = f"{stock_id} 股票新聞 site:cnyes.com OR site:moneydj.com"
            results_tw = list(ddgs.text(keywords_tw, region='tw-tzh', max_results=2))
            
            # 2. 英文新聞 (國際連動) - 簡單轉換或直接搜代號
            # 這裡為了速度，直接搜 "TW stock news" 或是代號
            keywords_en = f"{stock_id} TW stock news site:reuters.com OR site:bloomberg.com"
            results_en = list(ddgs.text(keywords_en, region='us-en', max_results=2))

            news_summary += "【中文新聞】：\n"
            for r in results_tw:
                news_summary += f"- {r['title']}\n"
            
            news_summary += "\n【國際新聞】：\n"
            for r in results_en:
                news_summary += f"- {r['title']}\n"
                
    except Exception as e:
        news_summary = f"新聞搜尋超時或錯誤: {str(e)}"
    
    return news_summary

# --- 核心處理邏輯 ---
class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            content_length = int(self.headers['Content-Length'])
            post_data = self.rfile.read(content_length)
            data = json.loads(post_data.decode('utf-8'))

            # 確認是否為 TG 訊息
            if "message" in data:
                chat_id = data["message"]["chat"]["id"]
                user_text = data["message"].get("text", "").strip()

                # 簡單判斷是否為股票代號 (4碼數字)
                if user_text.isdigit() and len(user_text) == 4:
                    stock_id = user_text
                    
                    # A. 抓取股價 (使用 twstock)
                    stock = twstock.Stock(stock_id)
                    
                    # 嘗試抓取即時資料 (若收盤後可能要調整邏輯，這裡以即時為主)
                    realtime = twstock.realtime.get(stock_id)
                    
                    if realtime['success']:
                        price = realtime['realtime']['latest_trade_price']
                        # 若無即時成交價（如暫停交易），使用開盤價或昨收
                        if price == '-': 
                            price = realtime['realtime']['best_bid_price'][0]
                        
                        # 簡單計算漲跌 (即時價 - 開盤價 或 昨收) - 這裡做簡單估算給 AI
                        # 為了更精準，我們抓近5日資料算量能
                        fetch_data = stock.fetch_31(len(stock.price)-5, len(stock.price))
                        avg_vol_5 = sum([d.turnover for d in fetch_data]) / 5 if fetch_data else 0
                        # 預估今日量 (簡單用累積成交量代替，盤中會有落差，交給 AI 判斷)
                        current_vol = int(realtime['realtime']['accumulate_trade_volume'])
                        
                        market_data = f"""
                        股票代號: {stock_id}
                        現價: {price}
                        今日成交量: {current_vol} 張 (參考)
                        5日均量: {int(avg_vol_5/1000)} 張 (約略值)
                        (注意：盤中成交量為累積值，需自行推算預估量)
                        """

                        # B. 搜尋新聞
                        news_info = search_news(stock_id)

                        # C. Gemini 分析 (策略漏斗)
                        prompt = f"""
                        你是嚴格的交易教練。請分析以下台股數據與新聞。

                        【數據資訊】
                        {market_data}

                        【新聞資訊】
                        {news_info}

                        【任務目標】
                        請嚴格執行以下『核心過濾漏斗』並輸出結果：

                        🛡️ **第一關：技術動能**
                        - 判斷漲幅動能與量能是否足夠 (成交量是否顯著大於 5日均量)？

                        🛡️ **第二關：美股濾鏡 (國際新聞)**
                        - 從英文新聞判斷美股或國際板塊是否連動助漲？

                        🛡️ **第三關：相對強度 (RS)**
                        - 根據你的知識判斷該股今日表現是否強於大盤？

                        🛡️ **第四關：籌碼與人氣**
                        - 新聞是否提及法人連買？

                        🧠 **情緒模擬器 (關鍵指令)**
                        - 若符合條件：輸出『💡 教練指令：盤中 13:00 確認美股期貨，若紅盤則模擬買進。』
                        - 若開盤不如預期：輸出『💡 恐慌預警：若跌破支撐，09:10 市價撤離。』
                        - 若大漲但不符條件：輸出『💡 FOMO Control：紀錄後悔程度，強制空手 (No Trade)。』

                        請用繁體中文，以條列式清楚輸出分析結果。
                        """

                        response = model.generate_content(prompt)
                        reply_text = response.text

                    else:
                        reply_text = f"找不到代號 {stock_id} 的即時資訊，請確認代號是否正確。"

                    # D. 回傳給 TG
                    send_telegram_message(chat_id, reply_text)

                else:
                    # 若不是股票代號，回傳提示
                    send_telegram_message(chat_id, "請輸入 4 位數台股代號 (例如: 2330) 來進行【策略漏斗分析】。")

            # 回應 Vercel (這是必須的，否則 webhook 會報錯)
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'status': 'ok'}).encode('utf-8'))

        except Exception as e:
            print(f"Error: {e}")
            self.send_response(500)
            self.end_headers()
