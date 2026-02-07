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
import concurrent.futures # 引入平行運算模組

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
        # 發送訊息也設個超時，避免卡死
        requests.post(url, json=payload, timeout=3)
    except Exception as e:
        print(f"[ERROR] 發送訊息失敗: {e}")

# --- 輕量化技術指標 (Yahoo API) ---
def get_technical_analysis(stock_id):
    print(f"[DEBUG] 開始抓取 Yahoo 技術指標: {stock_id}")
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        # 嚴格限時 2 秒
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

# --- 單一 RSS 抓取函式 (給執行緒用的) ---
def fetch_rss_thread(url, tag):
    res_list = []
    try:
        # 每個請求限時 2.5 秒
        r = requests.get(url, timeout=2.5)
        if r.status_code == 200:
            soup = BeautifulSoup(r.content, features="xml")
            items = soup.find_all("item", limit=2) # 限制抓 2 則
            for item in items:
                title = item.title.text.split(" - ")[0]
                link = item.link.text
                
                # 簡單的來源標記
                source = "媒體"
                if "cnyes" in link: source = "鉅亨"
                elif "moneydj" in link: source = "MoneyDJ"
                elif "reuters" in link: source = "路透"
                elif "bloomberg" in link: source = "彭博"
                elif "udn" in link: source = "經濟"
                elif "ctee" in link: source = "工商"
                
                res_list.append(f"• [{source}] [{title}]({link})")
    except Exception as e:
        print(f"[DEBUG] RSS {tag} 抓取失敗: {e}")
    return res_list

# --- 新聞搜尋 (v3.1 平行加速版) ---
def search_dual_news_parallel(stock_id):
    print(f"[DEBUG] 開始搜尋新聞 (平行加速模式): {stock_id}")
    start_time = time.time()
    
    # 權威白名單
    tw_sources = "site:cnyes.com OR site:moneydj.com OR site:ctee.com.tw OR site:udn.com OR site:bnext.com.tw"
    en_sources = "site:reuters.com OR site:bloomberg.com OR site:cnbc.com OR site:wsj.com"
    
    url_tw = f"https://news.google.com/rss/search?q={stock_id}+({tw_sources})+訂單+外資+when:1d&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    url_en = f"https://news.google.com/rss/search?q={stock_id}+Taiwan+({en_sources})+supply+chain+when:1d&hl=en-US&gl=US&ceid=US:en"

    news_text = ""
    list_tw = []
    list_en = []

    # 🔥 使用 ThreadPoolExecutor 同時發送兩個請求
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        # 發送任務
        future_tw = executor.submit(fetch_rss_thread, url_tw, "TW")
        future_en = executor.submit(fetch_rss_thread, url_en, "EN")
        
        # 等待結果 (最多等 3 秒，超過就放棄，避免卡死)
        try:
            list_tw = future_tw.result(timeout=3)
        except: list_tw = []
        
        try:
            list_en = future_en.result(timeout=3)
        except: list_en = []

    print(f"[DEBUG] 新聞搜尋完成，耗時: {time.time() - start_time:.2f}秒")

    if not list_tw and not list_en:
        return "（24h 無權威媒體報導，可能量縮無大人顧）"

    if list_tw: news_text += "【🇹🇼 權威內資 (24h)】：\n" + "\n".join(list_tw) + "\n"
    if list_en: news_text += "\n【🇺🇸 權威外資 (24h)】：\n" + "\n".join(list_en) + "\n"
        
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
                    send_telegram_message(chat_id, f"⚡ v3.1 平行加速版：{stock_id} 分析中...")

                    # A. 抓即時股價 (twstock)
                    try:
                        stock = twstock.realtime.get(stock_id)
                    except:
                        stock = {'success': False}

                    if stock.get('success'):
                        try:
                            price = float(stock['realtime']['latest_trade_price'])
                        except:
                            try: price = float(stock['realtime']['best_bid_price'][0])
                            except: price = 0
                        
                        try:
                            open_price = float(stock['realtime']['open'])
                            change_pct = ((price - open_price) / open_price) * 100
                        except: change_pct = 0
                            
                        safety_price = price * 0.985

                        # B. 技術指標 (Yahoo)
                        tech_data = get_technical_analysis(stock_id)
                        tech_str = "（Yahoo 逾時）"
                        if tech_data:
                            tech_str = f"""
                            - 5MA (地板): {tech_data['ma5']}
                            - 布林上軌 (天花板): {tech_data['upper_band']}
                            - 乖離率: {tech_data['bias_5']}%
                            """

                        # C. 新聞 (使用平行加速版)
                        news_info = search_dual_news_parallel(stock_id)

                        # D. Gemini 分析
                        print("[DEBUG] 呼叫 Gemini...")
                        prompt = f"""
                        你是嚴格的台股操盤教練，只信賴權威數據。
                        股票：{stock_id}，現價：{price} (漲幅 {change_pct:.2f}%)
                        技術：{tech_str}
                        權威新聞：{news_info}
                        
                        請嚴格執行【v3.1 權威策略漏斗】：

                        🔗 **1. 供應鏈與富爸爸 (Identity)**
                        - 它是誰的關鍵供應商？(如 NVIDIA, Apple)
                        - 富爸爸(客戶)現況如何？有無連動風險？

                        📏 **2. 價格與技術 (Static)**
                        - 支撐：股價是否站穩 5MA？
                        - 壓力：是否觸碰布林上軌或乖離過大？

                        💰 **3. 籌碼與權威觀點 (Credibility)**
                        - **掃描新聞**：權威媒體(鉅亨/路透)有無提到法人(外資/投信)動向？
                        - **防詐判斷**：若無權威報導，請警告「無法人背書，小心假拉抬」。

                        🏹 **4. 最終指令 (Action)**
                        - 給出指令：(買進 / 觀望 / 賣出 / 空手)。
                        - **保命機制**：強制輸出『若持有，明日 09:10 跌破 {round(safety_price, 2)} (保命價) 務必執行市價停損』。

                        請用繁體中文，條列式精簡輸出，限制 250 字。
                        """
                        
                        ai_reply = ""
                        # 模型優化：優先使用 Flash
                        model_list = ['gemini-3-flash-preview', 'gemini-2.5-flash']
                        
                        for model_name in model_list:
                            try:
                                print(f"[DEBUG] 嘗試模型: {model_name}")
                                model = genai.GenerativeModel(model_name)
                                response = model.generate_content(prompt)
                                ai_reply = response.text
                                break 
                            except: continue

                        if not ai_reply:
                            ai_reply = "⚠️ AI 連線失敗。"

                        final_msg = f"📊 **{stock_id} 權威分析報告**\n💰 現價：{price}\n📉 **保命價：{round(safety_price, 2)}**\n\n{ai_reply}\n\n{news_info}"
                        send_telegram_message(chat_id, final_msg)

                    else:
                        send_telegram_message(chat_id, f"❌ 找不到代號 {stock_id}")

            self.send_response(200); self.end_headers()
            self.wfile.write(json.dumps({'status': 'ok'}).encode('utf-8'))

        except Exception as e:
            print(f"CRITICAL ERROR: {e}")
            traceback.print_exc()
            self.send_response(200); self.end_headers()
