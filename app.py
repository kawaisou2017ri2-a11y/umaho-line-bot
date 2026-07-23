import os
import re
import urllib.parse
import threading
import requests
from bs4 import BeautifulSoup
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import google.generativeai as genai

app = Flask(__name__)

# 環境変数の読み込み
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1'
}

def extract_url(text):
    """テキストからURLを抽出"""
    match = re.search(r'https?://[^\s]+', text)
    return match.group(0) if match else None

def backup_horse_search(horse_name):
    """【バックアップ機能】出走表で種牡馬（父）が不明な場合、Netkeiba DBを検索して取得"""
    try:
        encoded_name = urllib.parse.quote(horse_name.encode('euc-jp', errors='ignore'))
        search_url = f"https://db.netkeiba.com/?pid=horse_list&word={encoded_name}"
        res = requests.get(search_url, headers=HEADERS, timeout=4)
        res.encoding = 'euc-jp'
        soup = BeautifulSoup(res.text, 'html.parser')
        
        horse_table = soup.select_one('table.db_h_lst_tbl')
        if horse_table:
            rows = horse_table.select('tr')
            if len(rows) > 1:
                cols = rows[1].select('td')
                if len(cols) > 1:
                    detail_link = cols[1].find('a')['href']
                    detail_res = requests.get(f"https://db.netkeiba.com{detail_link}", headers=HEADERS, timeout=4)
                    detail_res.encoding = 'euc-jp'
                    detail_soup = BeautifulSoup(detail_res.text, 'html.parser')
                    
                    blood_table = detail_soup.select_one('table.blood_table')
                    if blood_table:
                        sire_elem = blood_table.select_one('td[rowspan]')
                        if sire_elem and sire_elem.find('a'):
                            return sire_elem.find('a').text.strip()
    except Exception:
        pass
    return "詳細取得失敗"

def parse_netkeiba(url):
    """NetkeibaのURLからレース条件と全出走馬データを抽出"""
    try:
        res = requests.get(url, headers=HEADERS, timeout=6)
        res.encoding = res.apparent_encoding if res.apparent_encoding else 'utf-8'
        soup = BeautifulSoup(res.text, 'html.parser')
        
        race_title = ""
        title_elem = soup.select_one('.RaceName, .race_name, h1, .RaceNum')
        if title_elem:
            race_title = title_elem.text.strip()

        race_data = ""
        data_elem = soup.select_one('.RaceData01, .race_data, .RaceData, .RaceItem')
        if data_elem:
            race_data = data_elem.text.strip()

        horses = []
        rows = soup.select('tr.HorseList, tr.HorseInfo, table.Shutuba_Table tr, .HorseListTr, tr[class*="Horse"]')
        if not rows:
            rows = soup.select('.Shutuba_Table tr, table tr')

        for row in rows:
            umaban_elem = row.select_one('.Umaban, .td_umaban, td.Num, .umaban')
            bamei_elem = row.select_one('.HorseName, .Horse_Name, .bamei, a[href*="/horse/"]')
            
            if umaban_elem and bamei_elem:
                umaban = umaban_elem.text.strip()
                bamei = bamei_elem.text.strip()
                
                if umaban.isdigit() and bamei:
                    sex_elem = row.select_one('.Barei, .sex_age, .Sex')
                    jockey_elem = row.select_one('.Jockey, .jockey, .JockeyName')
                    sire_elem = row.select_one('.Sire, .sire, .SireName')
                    
                    sex = sex_elem.text.strip() if sex_elem else "不明"
                    jockey = jockey_elem.text.strip() if jockey_elem else "不明"
                    sire = sire_elem.text.strip() if sire_elem else ""

                    if not sire or sire == "不明" or len(sire) < 2:
                        sire = backup_horse_search(bamei)

                    horses.append(f"馬番{umaban}: {bamei} ({sex} / 騎手:{jockey} / 父:{sire})")

        return f"{race_title} ({race_data})", horses
    except Exception as e:
        return None, []

def generate_ai_response(prompt):
    """思考ログ・英語・買い目を一切出力させず表のみ生成させる関数"""
    system_instruction = (
        "あなたは競馬のウマホ期待値計算AIです。"
        "【絶対ルール】"
        "1. 英語の思考プロセスや下書き、メモ、挨拶、文章による解説は絶対に出力しないでください。"
        "2. 「買い目」の推奨やアドバイスは一切禁止です。絶対に記載しないでください。"
        "3. 1文字目から「🏆 **ウマホ全馬期待値スコア**」で始め、スコア一覧表のみを日本語で出力してください。"
    )
    
    models_to_try = ['gemini-1.5-flash', 'gemini-1.5-pro', 'gemini-2.0-flash']
    for preferred_model in models_to_try:
        try:
            m = genai.GenerativeModel(preferred_model, system_instruction=system_instruction)
            res = m.generate_content(prompt)
            if res and res.text:
                return res.text
        except Exception:
            continue

    available_models = genai.list_models()
    for m in available_models:
        if 'generateContent' in m.supported_generation_methods:
            try:
                model_instance = genai.GenerativeModel(m.name, system_instruction=system_instruction)
                res = model_instance.generate_content(prompt)
                if res and res.text:
                    return res.text
            except Exception:
                continue

    raise Exception("AIモデルの呼び出しに失敗しました。")

def process_async_prediction(user_text, reply_token, user_id):
    """バックグラウンドで処理を行うメイン関数"""
    try:
        url = extract_url(user_text)
        
        if url and 'netkeiba' in url:
            line_bot_api.reply_message(
                reply_token,
                TextSendMessage(text="【受付完了】\nNetkeibaの出走表データを解析中... 🏇")
            )
            
            race_info, horses = parse_netkeiba(url)
            
            if horses:
                horses_str = "\n".join(horses)
                prompt = f"""
以下のNetkeibaデータに基づき、ウマホの分析ロジック（固定条件：種牡馬・性別・芝ダ／条件緩和／期待値算出）を適用した【全馬の期待値スコア表】を完全な日本語で作成してください。

【レース情報】
{race_info}

【出走馬データ】
{horses_str}

【最重要指示】
・買い目の記載は一切不要です。絶対に書かないでください。
・1文字目から以下の表形式のみを出力してください。

🏆 **ウマホ全馬期待値スコア**

| 印 | 馬番 | 馬名 | 性齢 | 父（種牡馬） | ウマホ期待値 | 評価 |
|---|---|---|---|---|---|---|
| ◎ | X | 馬名 | 牡X | 種牡馬 | 88 / 100 | S |
| ○ | X | 馬名 | 牝X | 種牡馬 | 79 / 100 | A |
...
"""
            else:
                prompt = f"以下の入力データから買い目を除外した【全馬のウマホ期待値スコア表】のみを日本語で作成してください:\n{user_text}"
        else:
            line_bot_api.reply_message(
                reply_token,
                TextSendMessage(text="【受付完了】\n期待値スコアを計算中です... 🏇")
            )
            prompt = f"以下のテキストから買い目を除外した【全馬のウマホ期待値スコア表】のみを日本語で作成してください:\n{user_text}"

        response_text = generate_ai_response(prompt)

        if len(response_text) > 4500:
            response_text = response_text[:4500] + "\n...(一部省略)"

        line_bot_api.push_message(
            user_id,
            TextSendMessage(text=response_text)
        )

    except Exception as e:
        error_msg = f"⚠️ エラーが発生しました。\n\n【詳細】\n{str(e)}"
        try:
            line_bot_api.push_message(
                user_id,
                TextSendMessage(text=error_msg[:4000])
            )
        except Exception:
            pass

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@app.route("/", methods=['GET'])
def index():
    return "OK", 200

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_text = event.message.text
    reply_token = event.reply_token
    user_id = event.source.user_id

    thread = threading.Thread(
        target=process_async_prediction,
        args=(user_text, reply_token, user_id)
    )
    thread.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
