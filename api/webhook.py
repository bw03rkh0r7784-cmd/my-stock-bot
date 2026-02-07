# --- 1. 強力鎮壓警告 ---
import warnings
warnings.simplefilter(action='ignore', category=FutureWarning)
warnings.filterwarnings("ignore")

from http.server import BaseHTTPRequestHandler
import os
import json
import requests
import twstock
import statistics
import google.generativeai as genai
from bs4 import BeautifulSoup
import time
import traceback

# --- 環境變數 ---
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# --- 初始化 Gemini ---
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

# --- 輔助函式：發送 TG 訊息 ---
def send_telegram_message(chat_id, text):
    print(f"[DEBUG] 準備發送訊息給 {chat_id}...")
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"[ERROR] 發送訊息失敗: {e}")

# --- 輕量化技術指標 (Yahoo API) ---
def get_technical_analysis(stock_id):
    print(f"[DEBUG] 開始抓取 Yahoo 技術指標: {stock_id}")
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        timeout_val = 2 
        
        # 1. 嘗試上市 (.TW)
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{stock_id}.TW?range=2mo&interval=1d"
        try:
            r = requests.get(url, headers=headers, timeout=timeout_val)
            data = r.json()
        except:
            # 2. 失敗則嘗試上櫃 (.TWO)
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{stock_id}.TWO?range=2mo&interval=1d"
            r = requests.get(url, headers=headers, timeout=timeout_val)
            data = r.json()

        if data['chart']['result'] is None:
            return None

        result = data['chart']['result'][0]
        quote = result['indicators']['quote'][0]
        close_prices = quote['close']
        
        clean_prices = [p for p in close_prices if p is not None]

        if len(clean_prices) < 20:
            return None

        current_price = clean_prices[-1]
        ma5 = statistics.mean(clean_prices[-5:])
        ma20 = statistics.mean(clean_prices[-20:])
        stdev = statistics.stdev(clean_prices[-20:])
        upper_band = ma20 + (2 * stdev)
        bias_5 = ((current_price - ma5) / ma5) * 100

        return {
            "ma5": round(ma5, 2),
            "upper_band": round(upper_band, 2),
            "bias_5": round(bias_5, 2)
        }

    except Exception as e:
        print(f"[ERROR] 技術指標失敗: {e}")
        return None

# --- 新聞搜尋 (關鍵字優化：加入外資投信) ---
def search_dual_news(stock_id):
    print(f"[DEBUG] 開始搜尋新聞: {stock_id}")
    
    # 🇹🇼 國內：加入「外資、投信、主力」關鍵字，確保能抓到籌碼新聞
    url_tw = f"https://news.google.com/rss/search?q={stock_id}+訂單+外資+投信+when:1d&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    
    # 🇺🇸 國際：供應鏈與大客戶
    url_en = f"https://news.google.com/rss/search?q={stock_id}+supply+chain+major+customer+when:1d&hl=en-US&gl=US&ceid=US:en"

    news_text = ""
    
    def fetch_rss(url):
        res_list = []
        try:
            r = requests.get(url, timeout=2) # 放寬到 2 秒
            if r.status_code == 200:
                soup = BeautifulSoup(r.content, features="xml")
                items = soup.find_all("item", limit=2)
                for item in items:
                    title = item.title.text.split(" - ")[0]
                    link = item.link.text
                    res_list.append(f"• [{title}]({link})")
        except: pass
        return res_list

    list_tw = fetch_rss(url_tw)
    list_en = fetch_rss(url_en)

    if not list_tw and not list_en:
        return "（24h 無新聞）"

    if list_tw: news_text += "【🇹🇼 內資/籌碼 (24h)】：\n" + "\n".join(list_tw) + "\n"
    if list_en: news_text += "\n【🇺🇸 供應鏈/外資 (24h)】：\n" + "\n".join(list_en) + "\n"
        
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
                    
                    # 1. 回報收到
                    send_telegram_message(chat_id, f"⚡ v2.9 全面分析啟動：{stock_id}...")

                    # A. 抓即時股價
                    try:
                        stock = twstock.realtime.get(stock_id)
                    except:
                        stock = {'success': False}

                    if stock.get('success'):
                        try:
                            price = float(stock['realtime']['latest_trade_price'])
                        except:
                            try:
                                price = float(stock['realtime']['best_bid_price'][0])
                            except:
                                price = 0
                        
                        # 漲幅計算
                        try:
                            open_price = float(stock['realtime']['open'])
                            change_pct = ((price - open_price) / open_price) * 100
                        except:
                            change_pct = 0
                            
                        safety_price = price * 0.985

                        # B. 技術指標
                        tech_data = get_technical_analysis(stock_id)
                        tech_str = "（Yahoo 連線逾時）"
                        if tech_data:
                            tech_str = f"""
                            - 5MA (地板): {tech_data['ma5']}
                            - 布林上軌 (天花板): {tech_data['upper_band']}
                            - 乖離率: {tech_data['bias_5']}%
                            """

                        # C. 新聞
                        news_info = search_dual_news(stock_id)

                        # D. Gemini 分析 (Prompt 更新：加入第三關籌碼)
                        print("[DEBUG] 呼叫 Gemini...")
                        prompt = f"""
                        你是嚴格的台股操盤教練。
                        股票：{stock_id}，現價：{price} (漲幅 {change_pct:.2f}%)
                        技術：{tech_str}
                        新聞：{news_info}
                        
                        請嚴格執行【v2.9 策略漏斗分析】：

                        🔗 **1. 供應鏈與富爸爸 (Identity)**
                        - 它是誰的關鍵供應商？(如 NVIDIA, Apple)
                        - 富爸爸(客戶)現況如何？有無利空連動？

                        📏 **2. 價格與技術 (Static)**
                        - 支撐：股價是否站穩 5MA？
                        - 壓力：是否觸碰布林上軌或乖離過大？

                        💰 **3. 籌碼與消息 (Chips & News)**
                        - **掃描新聞**：是否有「外資/投信」連買或賣超？
                        - **判斷動向**：是「大戶進場」還是「主力出貨」？
                        - 若無新聞，請註明「無顯著籌碼消息」。

                        🏹 **4. 最終指令 (Action)**
                        - 給出指令：(買進 / 觀望 / 賣出 / 空手)。
                        - **保命機制**：強制輸出『若持有，明日 09:10 跌破 {round(safety_price, 2)} (保命價) 務必執行市價停損』。

                        請用繁體中文，條列式精簡輸出，限制 250 字。
                        """
                        
                        ai_reply = ""
                        # 模型優化：Flash 優先 (避開 Pro 的配額問題)
                        model_list = [
                            'gemini-3-flash-preview',       # 首選
                            'gemini-2.5-flash',   # 備用                            
                        ]
                        
                        for model_name in model_list:
                            try:
                                print(f"[DEBUG] 嘗試模型: {model_name}")
                                model = genai.GenerativeModel(model_name)
                                response = model.generate_content(prompt)
                                ai_reply = response.text
                                break 
                            except Exception as e:
                                print(f"[ERROR] 模型 {model_name} 失敗: {e}")
                                continue

                        if not ai_reply:
                            ai_reply = "⚠️ AI 連線失敗。"

                        final_msg = f"📊 **{stock_id} 籌碼與供應鏈報告**\n💰 現價：{price}\n📉 **保命價：{round(safety_price, 2)}**\n\n{ai_reply}\n\n{news_info}"
                        send_telegram_message(chat_id, final_msg)

                    else:
                        send_telegram_message(chat_id, f"❌ 找不到代號 {stock_id}")

            self.send_response(200); self.end_headers()
            self.wfile.write(json.dumps({'status': 'ok'}).encode('utf-8'))

        except Exception as e:
            print(f"CRITICAL ERROR: {e}")
            traceback.print_exc()
            self.send_response(200); self.end_headers()
