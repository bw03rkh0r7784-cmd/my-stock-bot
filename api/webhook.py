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

# --- 新聞搜尋 (v3.0 權威白名單鎖定) ---
def search_dual_news(stock_id):
    print(f"[DEBUG] 開始搜尋新聞 (權威鎖定模式): {stock_id}")
    
    # 🔥 1. 國內權威白名單 (鉅亨, MoneyDJ, 工商, 經濟, 數位時代)
    # 語法解釋：site:A OR site:B 代表「只搜尋這些網站」
    tw_sources = "site:cnyes.com OR site:moneydj.com OR site:ctee.com.tw OR site:udn.com OR site:bnext.com.tw"
    # 關鍵字：代號 + 關鍵字 + 白名單 + 24小時內
    url_tw = f"https://news.google.com/rss/search?q={stock_id}+({tw_sources})+when:1d&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    
    # 🔥 2. 國際權威白名單 (Reuters, Bloomberg, CNBC, WSJ)
    en_sources = "site:reuters.com OR site:bloomberg.com OR site:cnbc.com OR site:wsj.com"
    # 關鍵字：代號 + Taiwan + 白名單 + 24小時內
    url_en = f"https://news.google.com/rss/search?q={stock_id}+Taiwan+({en_sources})+when:1d&hl=en-US&gl=US&ceid=US:en"

    news_text = ""
    
    def fetch_rss(url):
        res_list = []
        try:
            r = requests.get(url, timeout=2.5) # 給權威媒體多 0.5 秒
            if r.status_code == 200:
                soup = BeautifulSoup(r.content, features="xml")
                items = soup.find_all("item", limit=2) # 各抓 2 則精華
                for item in items:
                    title = item.title.text.split(" - ")[0]
                    link = item.link.text
                    # 顯示來源網站名稱 (從 URL 判斷，增加辨識度)
                    source_tag = "權威媒體"
                    if "cnyes" in link: source_tag = "鉅亨網"
                    elif "moneydj" in link: source_tag = "MoneyDJ"
                    elif "reuters" in link: source_tag = "Reuters"
                    elif "bloomberg" in link: source_tag = "Bloomberg"
                    elif "ctee" in link: source_tag = "工商時報"
                    
                    res_list.append(f"• [{source_tag}] [{title}]({link})")
        except: pass
        return res_list

    list_tw = fetch_rss(url_tw)
    list_en = fetch_rss(url_en)

    if not list_tw and not list_en:
        # 如果權威媒體都沒報，代表這支股票今天「沒量、沒人氣」，這也是重要訊號
        return "（過去 24h 無權威媒體報導，可能無法人關注）"

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
                    send_telegram_message(chat_id, f"⚡ v3.0 權威信賴版啟動：{stock_id}...")

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

                        # C. 新聞 (權威白名單)
                        news_info = search_dual_news(stock_id)

                        # D. Gemini 分析 (Prompt 更新：強調公信力)
                        print("[DEBUG] 呼叫 Gemini...")
                        prompt = f"""
                        你是嚴格的台股操盤教練，只依據【權威數據】判斷。
                        股票：{stock_id}，現價：{price} (漲幅 {change_pct:.2f}%)
                        技術：{tech_str}
                        新聞來源：{news_info}
                        
                        請嚴格執行【v3.0 權威策略分析】：

                        🔗 **1. 供應鏈與富爸爸 (Identity)**
                        - 它是誰的關鍵供應商？(如 NVIDIA, Apple)
                        - 富爸爸(客戶)現況如何？有無利空連動？

                        📏 **2. 價格與技術 (Static)**
                        - 支撐：股價是否站穩 5MA？
                        - 壓力：是否觸碰布林上軌或乖離過大？

                        💰 **3. 籌碼與權威觀點 (Credibility)**
                        - **內資動向**：鉅亨/工商等權威媒體是否提及法人(外資/投信)買賣超？
                        - **外資觀點**：若有 Reuters/Bloomberg 報導，外資對該產業展望是正面還負面？
                        - **防詐警示**：若無權威新聞，請警告「缺乏法人背書，小心假突破」。

                        🏹 **4. 最終指令 (Action)**
                        - 給出指令：(買進 / 觀望 / 賣出 / 空手)。
                        - **保命機制**：強制輸出『若持有，明日 09:10 跌破 {round(safety_price, 2)} (保命價) 務必執行市價停損』。

                        請用繁體中文，條列式精簡輸出，限制 250 字。
                        """
                        
                        ai_reply = ""
                        # 模型優化：Flash 優先
                        model_list = [                            
                            'gemini-3-flash-preview',
                            'gemini-2.5-flash'
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
