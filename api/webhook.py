from http.server import BaseHTTPRequestHandler
import os
import json
import requests
import twstock
import statistics
import google.generativeai as genai
from bs4 import BeautifulSoup
import time

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

# --- 輕量化技術指標計算 (不使用 pandas/yfinance) ---
def get_technical_analysis(stock_id):
    try:
        # 嘗試抓取上市或上櫃數據
        # Yahoo API: range=1mo (一個月), interval=1d (日K)
        headers = {'User-Agent': 'Mozilla/5.0'}
        
        # 先試上市 (.TW)
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{stock_id}.TW?range=2mo&interval=1d"
        r = requests.get(url, headers=headers, timeout=3)
        data = r.json()
        
        # 如果沒資料，改試上櫃 (.TWO)
        if data['chart']['result'] is None:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{stock_id}.TWO?range=2mo&interval=1d"
            r = requests.get(url, headers=headers, timeout=3)
            data = r.json()

        # 解析 JSON
        result = data['chart']['result'][0]
        quote = result['indicators']['quote'][0]
        close_prices = quote['close']
        
        # 過濾掉 None (有時候會有空值)
        clean_prices = [p for p in close_prices if p is not None]

        if len(clean_prices) < 20:
            return None

        current_price = clean_prices[-1]

        # 1. 計算 5MA (地板)
        ma5 = statistics.mean(clean_prices[-5:])
        
        # 2. 計算 布林上軌 (20MA + 2std)
        ma20 = statistics.mean(clean_prices[-20:])
        stdev = statistics.stdev(clean_prices[-20:])
        upper_band = ma20 + (2 * stdev)
        
        # 3. 計算 5日乖離率
        bias_5 = ((current_price - ma5) / ma5) * 100

        return {
            "ma5": round(ma5, 2),
            "upper_band": round(upper_band, 2),
            "bias_5": round(bias_5, 2)
        }

    except Exception as e:
        print(f"Lightweight Tech Error: {e}")
        return None

# --- 輔助函式：搜尋 Google News RSS (雙軌 + 24h) ---
def search_dual_news(stock_id):
    # 國內新聞
    url_tw = f"https://news.google.com/rss/search?q={stock_id}+訂單+展望+when:1d&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    # 國際新聞
    url_en = f"https://news.google.com/rss/search?q={stock_id}+supply+chain+major+customer+when:1d&hl=en-US&gl=US&ceid=US:en"

    news_text = ""
    
    def fetch_rss(url, limit=2):
        res_list = []
        try:
            r = requests.get(url, timeout=4)
            if r.status_code == 200:
                soup = BeautifulSoup(r.content, features="xml")
                items = soup.find_all("item", limit=limit)
                for item in items:
                    title = item.title.text.split(" - ")[0]
                    link = item.link.text
                    res_list.append(f"• [{title}]({link})")
        except: pass
        return res_list

    list_tw = fetch_rss(url_tw)
    list_en = fetch_rss(url_en)

    if not list_tw and not list_en:
        return "（過去 24 小時內無重大新聞，可能有量縮疑慮）"

    if list_tw: news_text += "【🇹🇼 內資焦點 (24h)】：\n" + "\n".join(list_tw) + "\n"
    if list_en: news_text += "\n【🇺🇸 供應鏈觀點 (24h)】：\n" + "\n".join(list_en) + "\n"
        
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
                    
                    # 抓個即時價來顯示
                    try:
                        stock = twstock.realtime.get(stock_id)
                        if stock['success']:
                             price = float(stock['realtime']['latest_trade_price'])
                        else:
                             price = 0
                    except:
                        price = 0

                    send_telegram_message(chat_id, f"🔍 收到 {stock_id}，正在啟動【輕量化極速分析】...")

                    # A. 抓即時股價 (再次確認準確度)
                    # twstock 還是抓即時最快，保留
                    try:
                        stock = twstock.realtime.get(stock_id)
                    except:
                        stock = {'success': False}

                    if stock.get('success'):
                        try:
                            price = float(stock['realtime']['latest_trade_price'])
                        except:
                            price = float(stock['realtime']['best_bid_price'][0]) if stock['realtime']['best_bid_price'] else 0
                        
                        # RS 指標用的漲幅
                        open_price = float(stock['realtime']['open'])
                        if open_price > 0:
                            change_pct = ((price - open_price) / open_price) * 100
                        else:
                            change_pct = 0
                        
                        # 🔥 保命價計算
                        safety_price = price * 0.985

                        # B. 技術指標 (改用輕量版函式)
                        tech_data = get_technical_analysis(stock_id)
                        tech_str = "（技術指標讀取失敗）"
                        if tech_data:
                            tech_str = f"""
                            - 5MA (地板): {tech_data['ma5']}
                            - 布林上軌 (天花板): {tech_data['upper_band']}
                            - 5日乖離率: {tech_data['bias_5']}%
                            """

                        # C. 雙軌新聞
                        news_info = search_dual_news(stock_id)

                        # D. Gemini 分析
                        prompt = f"""
                        你是嚴格的台股供應鏈分析師。
                        
                        【標的資訊】
                        股票：{stock_id}
                        現價：{price} (今日漲幅: {change_pct:.2f}%)
                        
                        【技術參數】
                        {tech_str}
                        
                        【最新情報 (24h)】
                        {news_info}
                        
                        請嚴格執行【v2.6 供應鏈與價格斷面分析】：

                        🔗 **1. 供應鏈身分與富爸爸**
                        - 指出它是誰的關鍵供應商？(例: NVIDIA, Tesla, Apple)
                        - 它是做什麼的？(例: CoWoS 封測, 散熱)

                        📉 **2. 富爸爸現況診斷 (Chain Reaction)**
                        - **現況分析**：該大客戶(如 NVIDIA/Apple) 最近股價表現如何？有無砍單或利空？
                        - **連動判斷**：若客戶端疲弱，即使該股今日上漲，是否為「假漲」？
                        - **警示**：若客戶大跌，請標示「⚠️ 供應鏈利空連動風險」。

                        📏 **3. 價格與情緒拆解**
                        - **靜態支撐**：目前股價是否守住 5MA ({tech_data['ma5'] if tech_data else 'N/A'})？
                        - **動能強度**：今日漲幅 {change_pct:.2f}%，對比大盤氣氛，是「強於大盤」還是「虛漲」？

                        🏹 **4. 最終指令 (Action)**
                        - 給出指令：(買進 / 觀望 / 賣出 / 空手)。
                        - **保命機制**：強制輸出『若持有，明日 09:10 跌破 {round(safety_price, 2)} (保命價) 務必執行市價停損』。

                        請用繁體中文，條列式精簡輸出，限制 250 字。
                        """
                        
                        ai_reply = ""
                        # 模型輪替
                        model_list = ['gemini-3-pro-preview', 'gemini-3-flash-preview', 'gemini-2.0-flash', 'gemini-1.5-flash', 'gemini-pro']
                        
                        success_model = ""
                        for model_name in model_list:
                            try:
                                model = genai.GenerativeModel(model_name)
                                response = model.generate_content(prompt)
                                ai_reply = response.text
                                success_model = model_name
                                break 
                            except: continue

                        if not ai_reply:
                            ai_reply = "⚠️ AI 連線失敗，無法進行分析。"

                        final_msg = f"📊 **{stock_id} 供應鏈解析報告**\n💰 現價：{price}\n📉 **保命價：{round(safety_price, 2)}**\n\n{ai_reply}\n\n{news_info}"
                        send_telegram_message(chat_id, final_msg)

                    else:
                        send_telegram_message(chat_id, f"❌ 找不到代號 {stock_id}")

            self.send_response(200); self.end_headers()
            self.wfile.write(json.dumps({'status': 'ok'}).encode('utf-8'))

        except Exception as e:
            print(f"Error: {e}")
            self.send_response(200); self.end_headers()
