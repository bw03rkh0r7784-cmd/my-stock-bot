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
    print(f"[DEBUG] 開始抓取 Yahoo 技術指標與量能: {stock_id}")
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
        volumes = quote['volume'] # 抓取成交量
        opens = quote['open']
        highs = quote['high']
        lows = quote['low']
        
        # 清洗數據 (移除 None)
        valid_indices = [i for i, x in enumerate(close_prices) if x is not None and volumes[i] is not None]
        clean_close = [close_prices[i] for i in valid_indices]
        clean_vol = [volumes[i] for i in valid_indices]
        clean_open = [opens[i] for i in valid_indices]
        clean_high = [highs[i] for i in valid_indices]
        
        if len(clean_close) < 20:
            return None

        # --- A. 基礎指標 ---
        current_price = clean_close[-1]
        ma5 = statistics.mean(clean_close[-5:])
        ma20 = statistics.mean(clean_close[-20:])
        stdev = statistics.stdev(clean_close[-20:])
        upper_band = ma20 + (2 * stdev)
        bias_5 = ((current_price - ma5) / ma5) * 100

        # --- B. 量能分析 (Volume Analysis) ---
        # 計算 5 日均量
        vol_ma5 = statistics.mean(clean_vol[-6:-1]) # 取前5天(不含今天)的平均
        current_vol = clean_vol[-1]
        # 量能倍數 (今日量 / 5日均量)
        vol_ratio = round(current_vol / vol_ma5, 2) if vol_ma5 > 0 else 1.0

        # --- C. K線型態 (Pattern Recognition) ---
        # 判斷是否為「長上影線」(避雷針)：上影線長度 > 實體長度 * 2
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
            "vol_ratio": vol_ratio,   # 量能倍數
            "candle_type": candle_type # K棒型態
        }

    except Exception as e:
        print(f"[ERROR] 技術/量能計算失敗: {e}")
        return None

# --- 單一 RSS 抓取 (平行用) ---
def fetch_rss_thread(url, tag):
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

# --- 新聞搜尋 (平行加速) ---
def search_dual_news_parallel(stock_id):
    print(f"[DEBUG] 開始搜尋新聞: {stock_id}")
    tw_sources = "site:cnyes.com OR site:moneydj.com OR site:ctee.com.tw OR site:udn.com OR site:bnext.com.tw"
    en_sources = "site:reuters.com OR site:bloomberg.com OR site:cnbc.com OR site:wsj.com"
    
    url_tw = f"https://news.google.com/rss/search?q={stock_id}+({tw_sources})+訂單+外資+when:1d&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    url_en = f"https://news.google.com/rss/search?q={stock_id}+Taiwan+({en_sources})+supply+chain+when:1d&hl=en-US&gl=US&ceid=US:en"

    news_text = ""
    list_tw, list_en = [], []

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        future_tw = executor.submit(fetch_rss_thread, url_tw, "TW")
        future_en = executor.submit(fetch_rss_thread, url_en, "EN")
        try: list_tw = future_tw.result(timeout=3)
        except: pass
        try: list_en = future_en.result(timeout=3)
        except: pass

    if not list_tw and not list_en:
        return "（24h 無權威媒體報導）"

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
                    
                    send_telegram_message(chat_id, f"⚡ v3.2 量價雙測啟動：{stock_id}...")

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

                        # B. 技術指標 + 量能 (Yahoo)
                        tech_data = get_technical_analysis(stock_id)
                        tech_str = "（Yahoo 逾時）"
                        vol_str = "無法計算"
                        if tech_data:
                            # 組合給 AI 看的字串
                            tech_str = f"""
                            - 5MA (地板): {tech_data['ma5']}
                            - 布林上軌 (天花板): {tech_data['upper_band']}
                            - 乖離率: {tech_data['bias_5']}%
                            """
                            vol_str = f"""
                            - 量能倍數: {tech_data['vol_ratio']}倍 (今日量/5日均量)
                            - K棒型態: {tech_data['candle_type']}
                            """

                        # C. 新聞
                        news_info = search_dual_news_parallel(stock_id)

                        # D. Gemini 分析 (加入量能分析)
                        print("[DEBUG] 呼叫 Gemini...")
                        prompt = f"""
                        你是嚴格的量化操盤教練。
                        股票：{stock_id}，現價：{price} (漲幅 {change_pct:.2f}%)
                        
                        【技術與量能數據】
                        {tech_str}
                        {vol_str}
                        
                        【權威新聞】
                        {news_info}
                        
                        請執行【v3.2 全方位量價漏斗】：

                        🔗 **1. 供應鏈與富爸爸**
                        - 它是誰的供應商？富爸爸(如NVIDIA)狀況如何？

                        📊 **2. 量價關係 (Volume & Price) - 關鍵！**
                        - **量能判斷**：量能倍數為 {tech_data['vol_ratio'] if tech_data else 'N/A'} 倍。
                          (>1.2為增量, <0.8為量縮)。是「價漲量增」還是「虛漲」？
                        - **型態判斷**：注意 K 棒型態 ({tech_data['candle_type'] if tech_data else 'N/A'})。若為「長上影線」請警告賣壓。

                        💰 **3. 籌碼與權威觀點**
                        - 權威媒體有無法人動向報導？無報導則視為散戶行情。

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

                        # 加入 Yahoo 股市連結
                        chart_link = f"https://tw.stock.yahoo.com/quote/{stock_id}"
                        
                        final_msg = f"📊 **{stock_id} 量價分析報告**\n💰 現價：{price}\n📉 **保命價：{round(safety_price, 2)}**\n\n{ai_reply}\n\n{news_info}\n🔗 [查看 Yahoo K線圖]({chart_link})"
                        send_telegram_message(chat_id, final_msg)

                    else:
                        send_telegram_message(chat_id, f"❌ 找不到代號 {stock_id}")

            self.send_response(200); self.end_headers()
            self.wfile.write(json.dumps({'status': 'ok'}).encode('utf-8'))

        except Exception as e:
            print(f"CRITICAL ERROR: {e}")
            traceback.print_exc()
            self.send_response(200); self.end_headers()
