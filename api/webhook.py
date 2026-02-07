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
import concurrent.futures # 平行運算核心

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
        # 設定極短超時，避免卡死
        requests.post(url, json=payload, timeout=2)
    except Exception as e:
        print(f"[ERROR] 發送訊息失敗: {e}")

# --- [任務 A] 技術指標 + 量能 (Yahoo API) ---
def task_technical_analysis(stock_id):
    print(f"[THREAD] 啟動 Yahoo 技術抓取: {stock_id}")
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{stock_id}.TW?range=2mo&interval=1d"
        try:
            r = requests.get(url, headers=headers, timeout=2.5)
            data = r.json()
        except:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{stock_id}.TWO?range=2mo&interval=1d"
            r = requests.get(url, headers=headers, timeout=2.5)
            data = r.json()

        if data['chart']['result'] is None: return None

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
        
        if len(clean_close) < 20: return None

        current_price = clean_close[-1]
        ma5 = statistics.mean(clean_close[-5:])
        ma20 = statistics.mean(clean_close[-20:])
        stdev = statistics.stdev(clean_close[-20:])
        upper_band = ma20 + (2 * stdev)
        bias_5 = ((current_price - ma5) / ma5) * 100

        # 量能
        vol_ma5 = statistics.mean(clean_vol[-6:-1])
        current_vol = clean_vol[-1]
        vol_ratio = round(current_vol / vol_ma5, 2) if vol_ma5 > 0 else 1.0

        # K棒
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
    except: return None

# --- [任務 B] 新聞抓取 (通用函式) ---
def task_fetch_news(url):
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

# --- 核心處理邏輯 ---
class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        # 紀錄開始時間，確保不超時
        start_total = time.time()
        
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
                    
                    # 1. 快速查戶口 (本地資料庫，極快)
                    stock_name = ""
                    if stock_id in twstock.codes:
                        stock_name = twstock.codes[stock_id].name
                    
                    # 2. 準備平行任務的 URL
                    tw_sources = "site:cnyes.com OR site:moneydj.com OR site:ctee.com.tw OR site:udn.com OR site:bnext.com.tw"
                    en_sources = "site:reuters.com OR site:bloomberg.com OR site:cnbc.com OR site:wsj.com"
                    term_tw = f'"{stock_id}" "{stock_name}"'
                    url_tw = f"https://news.google.com/rss/search?q={term_tw}+({tw_sources})+訂單+外資+when:1d&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
                    url_en = f"https://news.google.com/rss/search?q={stock_id}+Taiwan+({en_sources})+supply+chain+when:1d&hl=en-US&gl=US&ceid=US:en"

                    send_telegram_message(chat_id, f"⚡ v3.5 平行加速啟動：{stock_id} {stock_name}...")

                    # ==========================================
                    # 🔥 核心優化：同時發射 3 個火箭 (平行運算)
                    # ==========================================
                    tech_data = None
                    list_tw = []
                    list_en = []
                    
                    # 我們使用 ThreadPoolExecutor 開 3 條線
                    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
                        # 1. 發送 Yahoo 任務
                        future_tech = executor.submit(task_technical_analysis, stock_id)
                        # 2. 發送 Google TW 任務
                        future_news_tw = executor.submit(task_fetch_news, url_tw)
                        # 3. 發送 Google EN 任務
                        future_news_en = executor.submit(task_fetch_news, url_en)
                        
                        # 同時，主執行緒去抓 twstock 即時價 (這是第 4 件事)
                        try:
                            stock_rt = twstock.realtime.get(stock_id)
                        except:
                            stock_rt = {'success': False}

                        # 等待所有平行任務回來 (最長等待 3.5 秒，這就是省時間的關鍵！)
                        # 因為大家是一起跑的，所以總時間 = 最慢那個的時間 (約 3s)
                        try: tech_data = future_tech.result(timeout=3.5)
                        except: pass
                        
                        try: list_tw = future_news_tw.result(timeout=3.5)
                        except: pass
                        
                        try: list_en = future_news_en.result(timeout=3.5)
                        except: pass

                    # ==========================================
                    # 資料彙整與檢查
                    # ==========================================
                    
                    # 處理即時價
                    if stock_rt.get('success'):
                        if not stock_name: # 如果前面沒查到，這裡補查
                            stock_name = stock_rt.get('info', {}).get('name', '')
                        try: price = float(stock_rt['realtime']['latest_trade_price'])
                        except: 
                            try: price = float(stock_rt['realtime']['best_bid_price'][0])
                            except: price = 0
                        try: change_pct = ((price - float(stock_rt['realtime']['open'])) / float(stock_rt['realtime']['open'])) * 100
                        except: change_pct = 0
                        safety_price = price * 0.985
                    else:
                        send_telegram_message(chat_id, f"❌ 找不到代號 {stock_id}")
                        self.send_response(200); self.end_headers(); return

                    # 處理技術指標字串
                    tech_str = "（Yahoo 逾時）"
                    vol_str = "無法計算"
                    if tech_data:
                        tech_str = f"- 5MA: {tech_data['ma5']}\n- 布林上軌: {tech_data['upper_band']}\n- 乖離率: {tech_data['bias_5']}%"
                        vol_str = f"- 量能倍數: {tech_data['vol_ratio']}倍\n- K棒: {tech_data['candle_type']}"

                    # 處理新聞字串
                    news_info = ""
                    if list_tw: news_info += "【🇹🇼 權威內資】\n" + "\n".join(list_tw) + "\n"
                    if list_en: news_info += "\n【🇺🇸 權威外資】\n" + "\n".join(list_en) + "\n"
                    if not news_info: news_info = "（24h 無權威新聞）"

                    # 檢查剩餘時間 (Vercel 10s 限制)
                    elapsed = time.time() - start_total
                    print(f"[DEBUG] 資料蒐集耗時: {elapsed:.2f}s")
                    
                    if elapsed > 8.0:
                        # 如果前面花太久，直接回傳簡單版，不問 AI 了，避免超時失敗
                        final_msg = f"⚠️ **{stock_id} 分析超時**\n資料抓取過久，僅提供數據：\n\n💰 現價：{price}\n{tech_str}\n{vol_str}\n\n{news_info}"
                        send_telegram_message(chat_id, final_msg)
                    else:
                        # 時間夠，問 Gemini
                        print("[DEBUG] 呼叫 Gemini...")
                        prompt = f"""
                        你是嚴格的量化操盤教練。
                        股票：{stock_id} {stock_name}，現價：{price} (漲幅 {change_pct:.2f}%)
                        
                        【技術/量能】
                        {tech_str}
                        {vol_str}
                        
                        【權威新聞】
                        {news_info}
                        
                        請執行【v3.5 極速平行分析】：

                        🔗 **1. 供應鏈/產業**
                        - {stock_name} 的產業地位與富爸爸(客戶)狀況。

                        📊 **2. 量價/籌碼診斷**
                        - 量能倍數 {tech_data['vol_ratio'] if tech_data else 'N/A'} 倍。是「真突破」還是「虛漲」？
                        - 若有「長上影線」請警告。
                        - 權威媒體有無法人動向？

                        🏹 **3. 指令 (Action)**
                        - 指令：(買進 / 觀望 / 賣出)。
                        - **保命機制**：強制輸出『若持有，明日 09:10 跌破 {round(safety_price, 2)} (保命價) 務必執行市價停損』。

                        請用繁體中文，精簡輸出，200字內。
                        """
                        
                        ai_reply = ""
                        # 優先用 Flash 確保速度
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

            self.send_response(200); self.end_headers()
            self.wfile.write(json.dumps({'status': 'ok'}).encode('utf-8'))

        except Exception as e:
            print(f"CRITICAL ERROR: {e}")
            traceback.print_exc()
            self.send_response(200); self.end_headers()
