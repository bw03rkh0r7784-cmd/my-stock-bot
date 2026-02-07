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

# --- 內部工具：抓取單一 RSS 來源 ---
def fetch_rss_feed(url, limit=2):
    news_list = []
    try:
        response = requests.get(url, timeout=3)
        if response.status_code == 200:
            soup = BeautifulSoup(response.content, features="xml")
            items = soup.find_all("item", limit=limit)
            for item in items:
                title = item.title.text
                link = item.link.text
                # Google RSS 標題格式通常是 "標題 - 媒體名稱"
                # 我們保留這個格式，這樣就知道是哪家媒體報導的
                news_list.append(f"• [{title}]({link})")
    except Exception as e:
        print(f"RSS Fetch Error: {e}")
    return news_list

# --- 核心功能：雙軌新聞搜尋 (國內 + 國際) ---
def search_dual_news(stock_id):
    # 1. 國內新聞 (台灣地區, 中文, 過去24小時)
    # 關鍵字：股票代號 (例如 2330)
    url_tw = f"https://news.google.com/rss/search?q={stock_id}+when:1d&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    
    # 2. 國際新聞 (美國地區, 英文, 過去24小時)
    # 關鍵字：股票代號 + "Taiwan" (例如 2330 Taiwan) 以確保搜到該股的英文報導
    # 這樣可以搜到 Reuters, Bloomberg 對台股的英文報導
    url_en = f"https://news.google.com/rss/search?q={stock_id}+Taiwan+stock+when:1d&hl=en-US&gl=US&ceid=US:en"

    news_text = ""
    
    # --- 執行搜尋 ---
    list_tw = fetch_rss_feed(url_tw, limit=2) # 抓 2 則中文
    list_en = fetch_rss_feed(url_en, limit=2) # 抓 2 則英文

    if not list_tw and not list_en:
        return "（過去 24 小時內無國內外重大新聞）"

    if list_tw:
        news_text += "【🇹🇼 國內焦點 (24h)】：\n" + "\n".join(list_tw) + "\n"
    
    if list_en:
        news_text += "\n【🇺🇸 國際觀點 (24h)】：\n" + "\n".join(list_en) + "\n"
        
    return news_text

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
                    
                    send_telegram_message(chat_id, f"🔍 收到 {stock_id}，正在進行【雙軌新聞掃描】與【策略漏斗分析】...")

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

                        # B. 雙軌搜新聞 (國內+國際)
                        news_info = search_dual_news(stock_id)

                        # C. Gemini 分析
                        prompt = f"""
                        你是嚴格的台股量化教練。
                        股票：{stock_id}
                        現價：{price}
                        新聞資料：
                        {news_info}
                        
                        請根據以上資訊，嚴格執行【2.2版 核心過濾漏斗】：
                        
                        🛡️ **第一關：技術動能**
                        - 判斷漲跌與動能。

                        🛡️ **第二關：美股濾鏡 (國際新聞)**
                        - 根據【國際觀點】新聞，判斷外資對該產業(如半導體/AI)的態度。
                        - 若無國際新聞，請註明「無國際連動資訊」。

                        🛡️ **第三關：籌碼與消息**
                        - 根據【國內焦點】判斷是否有法人連買或主力動向。

                        🧠 **教練指令 (操作建議)**
                        - 綜合判斷後，給出明確指令：(買進 / 觀望 / 賣出 / 空手)。
                        - 若有重大利空，請觸發「恐慌預警」。

                        請用繁體中文，條列式回答，限制 150 字以內。
                        """
                        
                        ai_reply = ""
                        error_log = ""
                        success_model = ""
                        
                        # 2026年 2月 最新模型清單
                        model_list = [
                            'gemini-3-pro-preview',
                            'gemini-3-flash-preview',
                            'gemini-2.5-flash',
                            'gemini-2.0-flash',                            
                            'gemini-2.0-flash-exp',
                            'gemini-1.5-flash'
                        ]
                        
                        for model_name in model_list:
                            try:
                                model = genai.GenerativeModel(model_name)
                                response = model.generate_content(prompt)
                                ai_reply = response.text
                                success_model = model_name
                                break 
                            except Exception as e:
                                error_log += f"\n❌ {model_name}: Fail"
                                continue

                        if not ai_reply:
                            ai_reply = f"⚠️ AI 連線失敗。\n錯誤紀錄：{error_log}"
                        else:
                            ai_reply += f"\n(🤖 模型：{success_model})"

                        final_msg = f"📊 **{stock_id} 雙軌分析報告**\n💰 現價：{price}\n\n{ai_reply}\n\n{news_info}"
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
