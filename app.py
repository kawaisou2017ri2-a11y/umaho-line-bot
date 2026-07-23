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

def extract_track_condition(text):
    """ユーザー指定の馬場状態（良・稍重・重・不良）を抽出"""
    if '不良' in text:
        return '不良'
    elif '稍重' in text:
        return '稍重'
    elif '重' in text:
        return '重'
    elif '良' in text:
        return '良'
    return None

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

def clean_japanese_output(text):
    """英語思考ログや余計な文を強力除去し、純粋な表データのみ抽出"""
    if not text:
        return ""
    
    # 🏆マーク以降を抽出
    if "🏆" in text:
        text = text[text.find("🏆"):]
    elif "|" in text:
        text = text[text.find("|"):]

    # 日本語・表以外の行（英語のみの行など）を除外
    lines = text.splitlines()
    cleaned_lines = []
    for line in lines:
        if '|' in line or '🏆' in line or re.search(r'[\u3040-\u30ff\u4e00-\u9faf]', line):
            cleaned_lines.append(line)

    return "\n".join(cleaned_lines).strip()

def generate_ai_response(prompt):
    """Gemini応答生成関数"""
    system_instruction = (
        "あなたは競馬のウマホ期待値計算AIです。\n"
        "【最重要ルール】\n"
        "1. 思考プロセス、英語の文言、メモは一切出力禁止。\n"
        "2. 買い目、おすすめの買い方は絶対に出力しないこと。\n"
        "3. 1文字目から必ず「🏆 **ウマホ全馬期待値スコア**」で始め、表形式のみを出力すること。"
    )
    
    models_to_try = [
        'gemini-1.5-flash', 
        'models/gemini-1.5-flash',
        'gemini-1.5-pro', 
        'models/gemini-1.5-pro',
        'gemini-2.0-flash'
    ]
    
    last_err = None

    for model_name in models_to_try:
        try:
            m = genai.GenerativeModel(model_name, system_instruction=system_instruction)
            res = m.generate_content(prompt)
            
            raw_text = None
            if hasattr(res, 'candidates') and res.candidates:
                for candidate in res.candidates:
                    if candidate.content and candidate.content.parts:
                        parts_text = "".join([p.text for p in candidate.content.parts if hasattr(p, 'text')])
                        if parts_text:
                            raw_text = parts_text
                            break

            if not raw_text and hasattr(res, 'text'):
                try:
                    raw_text = res.text
                except Exception:
                    pass

            if raw_text:
                cleaned = clean_japanese_output(raw_text)
                if cleaned:
                    return cleaned
        except Exception as e:
            last_err = e
            continue

    raise Exception(f"AI処理エラー: {str(last_err)}")

def process_async_prediction(user_text, reply_token, user_id):
    """バックグラウンド処理メイン関数"""
    try:
        url = extract_url(user_text)
        specified_condition = extract_track_condition(user_text)
        
        condition_msg = f"（指定馬場条件: 【{specified_condition}】を適用）" if specified_condition else ""

        if url and 'netkeiba' in url:
            line_bot_api.reply_message(
                reply_token,
                TextSendMessage(text=f"【受付完了】\nNetkeibaデータを解析中... 期待値スコアを計算しています 🏇\n{condition_msg}")
            )
            
            race_info, horses = parse_netkeiba(url)
            
            condition_instruction = ""
            if specified_condition:
                condition_instruction = f"【指定馬場条件】本レースの馬場状態は「{specified_condition}」として計算・補正を行ってください。"

            if horses:
                horses_str = "\n".join(horses)
                prompt = f"""
以下の出走馬データに基づき、ウマホの分析ロジックを適用した全馬の期待値スコア表を作成してください。
{condition_instruction}

【レース情報】
{race_info}

【出走馬データ】
{horses_str}

【出力フォーマット】
🏆 **ウマホ全馬期待値スコア**

| 印 | 馬番 | 馬名 | 性齢 | 父（種牡馬） | ウマホ期待値 | 評価 |
|---|---|---|---|---|---|---|
"""
            else:
                prompt = f"以下のテキストから全馬のウマホ期待値スコア表を作成してください。{condition_instruction}\n{user_text}"
        else:
            line_bot_api.reply_message(
                reply_token,
                TextSendMessage(text=f"【受付完了】\n期待値スコアを計算中です... 🏇\n{condition_msg}")
            )
            prompt = f"以下のテキストから全馬のウマホ期待値スコア表を作成してください。{condition_msg}\n{user_text}"

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
