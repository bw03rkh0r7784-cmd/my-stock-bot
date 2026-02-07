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
        requests.post(url, json=payload, timeout=2)
    except Exception as e:
        print(f"[ERROR] 發送訊息失敗: {e}")

# --- [任務 A] 技術指標 + 量能 ---
def task_technical_analysis(stock_id):
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{stock_id}.TW?range=2mo&interval=1d"
        try:
            r = requests.get(url, headers=headers, timeout=2.0)
            data = r.json()
        except:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{stock_id}.TWO?range=2mo&interval=1d"
            r = requests.get(url, headers=headers, timeout=2.0)
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
        vol_ma5 = statistics.mean(clean_vol[-6:-1])
        current_vol = clean_vol[-1]
        vol_ratio = round(current_vol / vol_ma5, 2) if vol_ma5 > 0 else 1.0

        today_open = clean_open[-1]
        today_high = clean_high[-1]
        body_size = abs(current_price - today_open)
        upper_shadow = today_high - max(current_price, today_open)
        
        candle_type = "普通"
        if upper_shadow > (body_size * 2) and upper_shadow > (current_price * 0.01):
            candle_type = "⚠️長上影(賣壓)"
        elif current_price > today_open and body_size > (current_price * 0.02):
            candle_type = "🔥實紅(強勢)"
        elif current_price < today_open and body_size > (current_price * 0.02):
            candle_type = "🟩實黑(弱勢)"

        return {
            "ma5": round(ma5, 2),
            "upper_band": round(upper_band, 2),
            "bias_5": round(bias_5, 2),
            "vol_ratio": vol_ratio,
            "candle_type": candle_type
        }
    except: return None

# --- [任務 B] 新聞抓取 ---
def task_fetch_news(url):
    res_list = []
    try:
        r = requests.get(url, timeout=2.0)
        if r.status_code == 200:
            soup = BeautifulSoup(r.content, features="xml")
            items = soup.find_all("item", limit=2)
            for item in items:
                title = item.title.text.split(" - ")[0]
                link = item.link.text
                if "sitemap" in link or link.endswith(".xml"): continue
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

# --- [任務 C] Gemini 生成 (修正模型清單) ---
def task_ask_gemini(prompt):
    # 🔥 v4.1 修正：移除 1.5，改用 Lite 當主力
    model_priority = [
        'gemini-2.5-flash-lite',  # 主力：速度極快，額度應較高
        'gemini-3-flash-preview', # 備用：預覽版額度通常不錯
        'gemini-2.5-flash'        # 最後手段：每日 20 次限制
    ]
    
    for model_name in model_priority:
        try:
            print(f"[AI] 嘗試模型: {model_name}")
            model = genai.GenerativeModel(
                model_name,
                generation_config=genai.types.GenerationConfig(
                    max_output_tokens=800, 
                    temperature=0.7
                )
            )
            response = model.generate_content(prompt)
            if response.text:
                return f"(🤖 {model_name})\n{response.text}"
        except Exception as e:
            print(f"[AI ERROR] {model_name} 失敗: {e}")
            continue
            
    return "⚠️ **AI 全面額度已滿或連線失敗**"

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
                    
                    # 1. 快速查戶口
                    stock_name = ""
                    if stock_id in twstock.codes:
                        stock_name = twstock.codes[stock_id].name
                    
                    # 2. 準備 URL
                    tw_sources = "site:cnyes.com OR site:moneydj.com OR site:ctee.com.tw OR site:udn.com OR site:bnext.com.tw"
                    en_sources = "site:reuters.com OR site:bloomberg.com OR site:cnbc.com OR site:wsj.com"
                    term_tw = f'"{stock_id}" "{stock_name}"'
                    url_tw = f"https://news.google.com/rss/search?q={term_tw}+({tw_sources})+訂單+外資+when:1d&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
                    url_en = f"https://news.google.com/rss/search?q={stock_id}+Taiwan+({en_sources})+supply+chain+when:1d&hl=en-US&gl=US&ceid=US:en"

                    send_telegram_message(chat_id, f"⚡ v4.1 Lite極速版：{stock_id} {stock_name}...")

                    # ==========================================
                    # 🚀 平行抓資料
                    # ==========================================
                    tech_data = None
                    list_tw = []
                    list_en = []
                    stock_rt = {'success': False}
                    
                    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                        future_tech = executor.submit(task_technical_analysis, stock_id)
                        future_news_tw = executor.submit(task_fetch_news, url_tw)
                        future_news_en = executor.submit(task_fetch_news, url_en)
                        def get_rt():
                            try: return twstock.realtime.get(stock_id)
                            except: return {'success': False}
                        future_rt = executor.submit(get_rt)

                        try: tech_data = future_tech.result(timeout=3.0)
                        except: pass
                        try: list_tw = future_news_tw.result(timeout=3.0)
                        except: pass
                        try: list_en = future_news_en.result(timeout=3.0)
                        except: pass
                        try: stock_rt = future_rt.result(timeout=3.0)
                        except: pass

                    # 資料處理
                    if stock_rt.get('success'):
                        if not stock_name: stock_name = stock_rt.get('info', {}).get('name', '')
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

                    tech_str = "Yahoo逾時"
                    vol_str = "N/A"
                    if tech_data:
                        tech_str = f"5MA:{tech_data['ma5']}, 布林上:{tech_data['upper_band']}, 乖離:{tech_data['bias_5']}%"
                        vol_str = f"量能:{tech_data['vol_ratio']}倍, K棒:{tech_data['candle_type']}"

                    news_info = ""
                    if list_tw: news_info += "【🇹🇼內資】" + " ".join(list_tw)
                    if list_en: news_info += " 【🇺🇸外資】" + " ".join(list_en)
                    if not news_info: news_info = "無權威新聞"

                    # ==========================================
                    # 🚀 Gemini 生成 (Lite 優先)
                    # ==========================================
                    print("[DEBUG] 呼叫 Gemini...")
                    
                    prompt = f"""
                    你現在是量化交易系統。直接輸出分析結果。
                    
                    【標的】{stock_id} {stock_name}
                    【數據】現價 {price} (漲幅 {change_pct:.2f}%)
                    【技術】{tech_str}
                    【量能】{vol_str}
                    【新聞】{news_info}
                    
                    請執行以下分析：
                    1. **供應鏈地位**：說明關鍵客戶與產業地位。
                    2. **量價診斷**：根據量能倍數 ({tech_data['vol_ratio'] if tech_data else 'N/A'}) 與 K棒型態，判斷是真突破還是虛漲？
                    3. **籌碼判斷**：根據新聞判斷法人動向。
                    4. **操作指令**：(買進/觀望/賣出) 與 保命價 {round(safety_price, 2)}。
                    
                    請用繁體中文，條列式回答。
                    """
                    
                    ai_reply = None
                    
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ai_executor:
                        ai_future = ai_executor.submit(task_ask_gemini, prompt)
                        try:
                            # 7秒超時
                            ai_reply = ai_future.result(timeout=7.0) 
                        except concurrent.futures.TimeoutError:
                            print("[WARN] Gemini 思考超時")
                            ai_reply = "⚠️ **AI 連線逾時** (請參考上方數據)"
                        except Exception:
                            ai_reply = "⚠️ AI 發生錯誤"

                    # 最終發送
                    chart_link = f"https://tw.stock.yahoo.com/quote/{stock_id}"
                    final_msg = f"📊 **{stock_id} {stock_name}**\n💰 現價：{price}\n📉 **保命：{round(safety_price, 2)}**\n\n{ai_reply}\n\n{news_info}\n🔗 [K線圖]({chart_link})"
                    send_telegram_message(chat_id, final_msg)

            self.send_response(200); self.end_headers()
            self.wfile.write(json.dumps({'status': 'ok'}).encode('utf-8'))

        except Exception as e:
            print(f"CRITICAL ERROR: {e}")
            traceback.print_exc()
            self.send_response(200); self.end_headers()
