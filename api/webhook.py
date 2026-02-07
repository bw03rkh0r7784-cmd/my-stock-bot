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
import concurrent.futures

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
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown", "disable_web_page_preview": True}
    try:
        requests.post(url, json=payload, timeout=3)
    except Exception as e:
        print(f"[ERROR] 發送訊息失敗: {e}")

# --- 技術指標 + 量能分析 (Yahoo API) ---
def get_technical_analysis(stock_id):
    print(f"[DEBUG] 開始抓取 Yahoo 技術指標: {stock_id}")
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        timeout_val = 2 
        
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{stock_id}.TW?range=2mo&interval=1d"
        try:
            r = requests.get(url, headers=headers, timeout=timeout_val)
            data = r.json()
        except:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{stock_id}.TWO?range=2mo&interval=1d"
            r = requests.get(url, headers=headers, timeout=timeout_val)
            data = r.json()

        if data['chart']['result'] is None:
            return None

        result = data['chart']['result'][0]
        quote = result['indicators']['quote'][0]
        
        close_prices = quote['close']
        volumes = quote['volume']
        opens = quote['open']
        highs = quote['high']
        
        valid_indices = [i for i, x in enumerate(close_prices) if x is not None and volumes[i] is not None]
        clean_close = [close_prices[i] for i in valid_indices]
        clean_vol = [volumes[i] for i in valid_indices]
        clean_open = [opens[i] for i in valid_indices]
        clean_high = [highs[i] for i in valid_indices]
        
        if len(clean_close) < 20:
            return None

        current_price = clean_close[-1]
        ma5 = statistics.mean(clean_close[-5:])
        ma20 = statistics.mean(clean_close[-20:])
        stdev = statistics.stdev(clean_close[-20:])
        upper_band = ma20 + (2 * stdev)
        bias_5 = ((current_price - ma5) / ma5) * 100

        # 量能倍數
        vol_ma5 = statistics.mean(clean_vol[-6:-1])
        current_vol = clean_vol[-1]
        vol_ratio = round(current_vol / vol_ma5, 2) if vol_ma5 > 0 else 1.0

        # K棒型態
        today_open = clean_open[-1]
        today_high = clean_high[-1]
        body_size = abs(current_price - today_open)
        upper_shadow = today_high - max(current_price, today_open)
        
        candle_type = "普通K棒"
        if upper_shadow > (body_size * 2) and upper_shadow > (current_price * 0.01):
            candle_type = "⚠️ 長上影線 (賣壓重)"
        elif current_price > today_open and body_size > (current_price * 0.02):
            candle_type = "🔥 實體紅棒 (強勢)"
        elif current_price < today_open and body_size > (current_price * 0.02):
            candle_type = "🟩 實體黑棒 (弱勢)"

        return {
            "ma5": round(ma5, 2),
            "upper_band": round(upper_band, 2),
            "bias_5": round(bias_5, 2),
            "vol_ratio": vol_ratio,
            "candle_type": candle_type
        }

    except Exception as e:
        print(f"[ERROR] 技術指標失敗: {e}")
        return None

# --- 單一 RSS 抓取 ---
def fetch_rss_thread(url):
    res_list = []
    try:
        r = requests.get(url, timeout=2.5)
        if r.status_code == 200:
            soup = BeautifulSoup(r.content, features="xml")
            items = soup.find_all("item", limit=2)
            for item in items:
                title = item.title.text.split(" - ")[0]
                link = item.link.text
                source = "媒體"
                if "cnyes" in link: source = "鉅亨"
                elif "moneydj" in link: source = "MoneyDJ"
                elif "reuters" in link: source = "路透"
                elif "bloomberg" in link: source = "彭博"
                elif "udn" in link: source = "經濟"
                elif "ctee" in link: source = "工商"
                res_list.append(f"• [{source}] [{title}]({link})")
    except: pass
    return res_list

# --- 新聞搜尋 (v3.4 強制鎖定名稱版) ---
def search_dual_news_parallel(stock_id, stock_name):
    print(f"[DEBUG] 開始搜尋新聞: {stock_id} {stock_name}")
    
    # 權威媒體白名單
    tw_sources = "site:cnyes.com OR site:moneydj.com OR site:ctee.com.tw OR site:udn.com OR site:bnext.com.tw"
    en_sources = "site:reuters.com OR site:bloomberg.com OR site:cnbc.com OR site:wsj.com"
    
    # 🔥 關鍵修正：使用 Google 強制匹配語法 (雙引號 + 號)
    # 範例：q="2208" "台船" ...
    # 這會強制 Google 搜尋結果必須「同時包含」代號與名稱
    
    term_tw = f'"{stock_id}" "{stock_name}"'
    term_en = f'"{stock_id}" "{stock_name}"' # 英文搜尋也帶中文名，因為這些外媒報台股通常也會寫中文名或代號
    
    url_tw = f"https://news.google.com/rss/search?q={term_tw}+({tw_sources})+when:1d&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    # 英文搜尋增加 "Taiwan" 強制定位
    url_en = f"https://news.google.com/rss/search?q={stock_id}+Taiwan+({en_sources})+supply+chain+when:1d&hl=en-US&gl=US&ceid=US:en"

    news_text = ""
    list_tw, list_en = [], []

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        future_tw = executor.submit(fetch_rss_thread, url_tw)
        future_en = executor.submit(fetch_rss_thread, url_en)
        try: list_tw = future_tw.result(timeout=3)
        except: pass
        try: list_en = future_en.result(timeout=3)
        except: pass

    if not list_tw and not list_en:
        return "（24h 無權威媒體報導，請依技術面為主）"

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
            try: data = json.loads(post_data.decode('utf-8'))
            except: self.send_response(200); self.end_headers(); return

            if "message" in data:
                chat_id = data["message"]["chat"]["id"]
                user_text = data["message"].get("text", "").strip()
                
                if user_text.isdigit() and len(user_text) == 4:
                    stock_id = user_text
                    
                    # 1. 查戶口：從 twstock 資料庫獲取正確名稱
                    stock_name = ""
                    try:
                        # 優先從 twstock 靜態代碼表查找 (最準)
                        if stock_id in twstock.codes:
                            stock_name = twstock.codes[stock_id].name
                        else:
                            # 查不到 (可能是新股)，嘗試用 realtime 補救
                            rt = twstock.realtime.get(stock_id)
                            stock_name = rt.get('info', {}).get('name', '')
                    except:
                        stock_name = ""

                    # 回報使用者我們查到了什麼
                    send_telegram_message(chat_id, f"⚡ v3.4 鎖定分析：{stock_id} {stock_name}...")

                    # A. 抓即時股價
                    try:
                        stock = twstock.realtime.get(stock_id)
                    except:
                        stock = {'success': False}

                    if stock.get('success'):
                        try: price = float(stock['realtime']['latest_trade_price'])
                        except: 
                            try: price = float(stock['realtime']['best_bid_price'][0])
                            except: price = 0
                        
                        try:
                            open_price = float(stock['realtime']['open'])
                            change_pct = ((price - open_price) / open_price) * 100
                        except: change_pct = 0
                            
                        safety_price = price * 0.985

                        # B. 技術指標 + 量能
                        tech_data = get_technical_analysis(stock_id)
                        tech_str = "（Yahoo 逾時）"
                        vol_str = "無法計算"
                        if tech_data:
                            tech_str = f"""
                            - 5MA (地板): {tech_data['ma5']}
                            - 布林上軌 (天花板): {tech_data['upper_band']}
                            - 乖離率: {tech_data['bias_5']}%
                            """
                            vol_str = f"""
                            - 量能倍數: {tech_data['vol_ratio']}倍
                            - K棒型態: {tech_data['candle_type']}
                            """

                        # C. 新聞 (強制傳入名稱)
                        news_info = search_dual_news_parallel(stock_id, stock_name)

                        # D. Gemini 分析
                        print("[DEBUG] 呼叫 Gemini...")
                        prompt = f"""
                        你是嚴格的量化操盤教練。
                        目標股票：{stock_id} {stock_name}
                        現價：{price} (漲幅 {change_pct:.2f}%)
                        
                        【技術與量能】
                        {tech_str}
                        {vol_str}
                        
                        【權威新聞】
                        {news_info}
                        
                        請執行【v3.4 精準鎖定分析】：

                        🔗 **1. 供應鏈與產業**
                        - {stock_name} 的主要產品與富爸爸是誰？
                        - 若無新聞，請依據你的資料庫回答該公司產業地位。

                        📊 **2. 量價關係 (Volume & Price)**
                        - 量能倍數 {tech_data['vol_ratio'] if tech_data else 'N/A'} 倍。
                        - 判斷是「真突破」還是「虛漲」？
                        - K棒型態 ({tech_data['candle_type'] if tech_data else 'N/A'}) 是否有危險？

                        💰 **3. 籌碼與權威觀點**
                        - 權威媒體有無法人動向？

                        🏹 **4. 最終指令 (Action)**
                        - 指令：(買進 / 觀望 / 賣出)。
                        - **保命機制**：強制輸出『若持有，明日 09:10 跌破 {round(safety_price, 2)} (保命價) 務必執行市價停損』。

                        請用繁體中文，條列式精簡輸出，250字內。
                        """
                        
                        ai_reply = ""
                        model_list = ['gemini-3-flash-preview', 'gemini-2.5-flash']
                        
                        for model_name in model_list:
                            try:
                                model = genai.GenerativeModel(model_name)
                                response = model.generate_content(prompt)
                                ai_reply = response.text
                                break 
                            except: continue

                        if not ai_reply: ai_reply = "⚠️ AI 連線失敗。"

                        chart_link = f"https://tw.stock.yahoo.com/quote/{stock_id}"
                        
                        final_msg = f"📊 **{stock_id} {stock_name} 分析報告**\n💰 現價：{price}\n📉 **保命價：{round(safety_price, 2)}**\n\n{ai_reply}\n\n{news_info}\n🔗 [查看 Yahoo K線圖]({chart_link})"
                        send_telegram_message(chat_id, final_msg)

                    else:
                        send_telegram_message(chat_id, f"❌ 找不到代號 {stock_id}")

            self.send_response(200); self.end_headers()
            self.wfile.write(json.dumps({'status': 'ok'}).encode('utf-8'))

        except Exception as e:
            print(f"CRITICAL ERROR: {e}")
            traceback.print_exc()
            self.send_response(200); self.end_headers()
