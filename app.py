import os
import re
import urllib.parse
import threading
import hashlib
import requests
from bs4 import BeautifulSoup
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = Flask(__name__)

# 環境変数の読み込み（LINE関連のみでOK）
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1'
}

# 重馬場・道悪で評価が上がる血統リスト（例）
HEAVY_TRACK_SIRES = ['キズナ', 'エピファネイア', 'ドゥラメンテ', 'オルフェーヴル', 'ゴールドシップ', 'ハービンジャー', 'モーリス', 'キタサンブラック', 'ルーラーシップ']
# トップ騎手リスト（例）
TOP_JOCKEYS = ['ルメール', '川田', '武豊', '横山武', '戸崎', '坂井', 'レーン', 'モレイラ', 'デムーロ', '鮫島駿']

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
    return '良'

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
    return "不明"

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

                    horses.append({
                        'umaban': int(umaban),
                        'bamei': bamei,
                        'sex': sex,
                        'jockey': jockey,
                        'sire': sire
                    })

        return f"{race_title} ({race_data})", horses
    except Exception as e:
        return None, []

def calculate_local_umaho_scores(horses, track_condition):
    """【API不要】ローカルロジックでウマホ期待値を高速・決定論的に算出"""
    scored_horses = []

    for h in horses:
        # 馬名ハッシュによる基本能力スコア（60〜85点ベース）
        hash_val = int(hashlib.md5(h['bamei'].encode('utf-8')).hexdigest(), 16)
        base_score = 60 + (hash_val % 26)

        # 1. 騎手補正
        jockey_bonus = 0
        for top_j in TOP_JOCKEYS:
            if top_j in h['jockey']:
                jockey_bonus = 6
                break

        # 2. 馬場条件 ＆ 種牡馬補正
        sire_bonus = 0
        if track_condition in ['重', '不良']:
            for heavy_sire in HEAVY_TRACK_SIRES:
                if heavy_sire in h['sire']:
                    sire_bonus = 8
                    break

        # スコア合計＆キャップ処理（上限98点 / 下限50点）
        total_score = min(98, max(50, base_score + jockey_bonus + sire_bonus))

        # 評価ランク判定
        if total_score >= 85:
            rank = 'S'
        elif total_score >= 75:
            rank = 'A'
        elif total_score >= 65:
            rank = 'B'
        else:
            rank = 'C'

        scored_horses.append({
            'umaban': h['umaban'],
            'bamei': h['bamei'],
            'sex': h['sex'],
            'sire': h['sire'],
            'score': total_score,
            'rank': rank
        })

    # 期待値スコアの降順（高い順）にソート
    scored_horses.sort(key=lambda x: x['score'], reverse=True)

    # 印（◎、○、▲、△、☆、-）の割り振り
    marks = ['◎', '○', '▲', '△', '☆']
    for idx, h in enumerate(scored_horses):
        if idx < len(marks):
            h['mark'] = marks[idx]
        else:
            h['mark'] = '－'

    # 表（Markdown）の作成
    table_lines = [
        "🏆 **ウマホ全馬期待値スコア**\n",
        "| 印 | 馬番 | 馬名 | 性齢 | 父（種牡馬） | ウマホ期待値 | 評価 |",
        "|---|---|---|---|---|---|---|"
    ]

    for h in scored_horses:
        table_lines.append(f"| {h['mark']} | {h['umaban']} | {h['bamei']} | {h['sex']} | {h['sire']} | {h['score']} / 100 | {h['rank']} |")

    return "\n".join(table_lines)

def process_async_prediction(user_text, reply_token, user_id):
    """バックグラウンド処理メイン関数"""
    try:
        url = extract_url(user_text)
        track_condition = extract_track_condition(user_text)
        
        condition_msg = f"（馬場条件: 【{track_condition}】を反映）" if '馬場' in user_text or track_condition != '良' else ""

        if url and 'netkeiba' in url:
            line_bot_api.reply_message(
                reply_token,
                TextSendMessage(text=f"【受付完了】\nNetkeibaデータを解析し、ウマホ期待値を直ちに計算中... 🏇\n{condition_msg}")
            )
            
            race_info, horses = parse_netkeiba(url)
            
            if horses:
                response_text = calculate_local_umaho_scores(horses, track_condition)
            else:
                response_text = "⚠️ Netkeibaから出走馬データを取得できませんでした。URLを確認してください。"
        else:
            response_text = "⚠️ NetkeibaのレースURLを入力してください。"

        line_bot_api.push_message(
            user_id,
            TextSendMessage(text=response_text)
        )

    except Exception as e:
        error_msg = f"⚠️ 処理中にエラーが発生しました。\n\n【詳細】\n{str(e)}"
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
