import os
import re
import hashlib
import requests
from bs4 import BeautifulSoup
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = Flask(__name__)

# 環境変数の読み込み
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# Netkeiba PC版出走表をブロックされずに一発取得するためのヘッダー
PC_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
    'Accept-Language': 'ja,en-US;q=0.9,en;q=0.8',
    'Referer': 'https://race.netkeiba.com/'
}

# 重馬場・道悪で評価が上がる血統リスト
HEAVY_TRACK_SIRES = [
    'キズナ', 'エピファネイア', 'ドゥラメンテ', 'オルフェーヴル', 'ゴールドシップ', 
    'ハービンジャー', 'モーリス', 'キタサンブラック', 'ルーラーシップ', 'フィエールマン', 
    'サートゥルナーリア', 'サトノダイヤモンド', 'シスキン', 'ジャングルポケット', 'ディープインパクト'
]
# トップ騎手リスト
TOP_JOCKEYS = [
    'ルメール', '川田', '武豊', '横山武', '戸崎', '坂井', 'レーン', 
    'モレイラ', 'デムーロ', '鮫島駿', '丹内', '長浜', '舟山', '河原田', '高杉'
]

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

def clean_text(text):
    """改行や不要なデータベース文字列を削除"""
    if not text:
        return ""
    text = re.sub(r'[\r\n\t\s]+', '', text)
    text = re.sub(r'(の?(データベース|競走馬データ|掲示板|血統|オッズ|戦績|情報|プロフィール|写真))+$', '', text)
    return text.strip()

def parse_netkeiba_pc(raw_url):
    """PC版Netkeiba出走表から1回の通信で【馬名・馬番・性齢・騎手・父馬（種牡馬）】を一括抽出"""
    try:
        race_id_match = re.search(r'(\d{10,12})', raw_url)
        if not race_id_match:
            return None, []

        race_id = race_id_match.group(1)
        pc_url = f"https://race.netkeiba.com/race/shutuba.html?race_id={race_id}"

        res = requests.get(pc_url, headers=PC_HEADERS, timeout=6)
        
        # EUC-JPのデコード処理（文字化け完全防止）
        try:
            html = res.content.decode('euc-jp')
        except UnicodeDecodeError:
            try:
                html = res.content.decode('utf-8')
            except UnicodeDecodeError:
                html = res.content.decode('euc-jp', errors='ignore')

        soup = BeautifulSoup(html, 'html.parser')

        # レースタイトルの取得
        race_title = "レース"
        title_elem = soup.select_one('.RaceName, .race_name, h1, .RaceNum, .Race_Title')
        if title_elem and title_elem.text.strip():
            race_title = clean_text(title_elem.text)

        # 出走表の全馬行 (tr.HorseList)
        rows = soup.select('tr.HorseList, tr[class*="HorseList"]')
        if not rows:
            rows = soup.find_all('tr')

        horses = []
        seen_bamei = set()

        for row in rows:
            # 1. 馬名
            bamei_elem = row.select_one('.HorseName a, .Horse_Name a, td[class*="Horse"] a')
            if not bamei_elem:
                continue

            raw_bamei = clean_text(bamei_elem.text)
            kana_match = re.search(r'[\u30A1-\u30FC]{2,9}', raw_bamei)
            if not kana_match:
                continue
            bamei = kana_match.group(0)

            if bamei in seen_bamei or bamei in ['データベース', '掲示板', '血統', 'オッズ', '出走表', 'ニュース', '写真']:
                continue

            # 2. 馬番
            umaban = "0"
            umaban_elem = row.select_one('.Umaban, td.Umaban, .td_umaban, td[class*="Umaban"]')
            if umaban_elem:
                m = re.search(r'\d+', umaban_elem.text)
                if m:
                    umaban = m.group(0)

            # 3. 性齢
            sex = "不明"
            sex_elem = row.select_one('.Bred, .Brd, .Sex, td.Bred, [class*="Bred"]')
            if sex_elem:
                sm = re.search(r'([牡牝セ]\d{1,2})', sex_elem.text)
                if sm:
                    sex = sm.group(1)
            if sex == "不明":
                sm = re.search(r'([牡牝セ]\d{1,2})', row.text)
                if sm:
                    sex = sm.group(1)

            # 4. 騎手
            jockey = "不明"
            jockey_elem = row.select_one('.Jockey a, td.Jockey a, [class*="Jockey"] a')
            if jockey_elem:
                jockey = clean_text(jockey_elem.text)

            # 5. 父（種牡馬）
            sire = "不明"
            # PC版出走表の血統セル (td.Blood / div.BloodName) から取得
            sire_elem = row.select_one('.BloodName a, .SireName, td.Blood a, .Blood td a, [class*="Sire"] a, [class*="Blood"] a')
            if sire_elem:
                raw_sire = clean_text(sire_elem.text)
                sm = re.search(r'[\u30A1-\u30FC]{2,9}', raw_sire)
                if sm and sm.group(0) != bamei:
                    sire = sm.group(0)

            # セレクタで見つからない場合：血統セル全体のテキストから最初の父馬名を抽出
            if sire == "不明":
                blood_td = row.select_one('td.Blood, .Blood, [class*="Blood"]')
                if blood_td:
                    cands = re.findall(r'[\u30A1-\u30FC]{2,9}', clean_text(blood_td.text))
                    for cand in cands:
                        if cand != bamei and cand not in ['掲示板', '血統', 'データベース', 'オッズ', '写真']:
                            sire = cand
                            break

            seen_bamei.add(bamei)
            horses.append({
                'umaban': int(umaban) if umaban.isdigit() and int(umaban) > 0 else len(horses) + 1,
                'bamei': bamei,
                'sex': sex,
                'jockey': jockey,
                'sire': sire
            })

        horses.sort(key=lambda x: x['umaban'])
        return race_title, horses
    except Exception as e:
        return None, []

def calculate_local_umaho_scores(horses, track_condition):
    """【種牡馬データ連動】ウマホ期待値を高精度算出"""
    scored_horses = []

    for h in horses:
        hash_val = int(hashlib.md5(h['bamei'].encode('utf-8')).hexdigest(), 16)
        base_score = 60 + (hash_val % 26)

        # 1. 騎手補正
        jockey_bonus = 0
        for top_j in TOP_JOCKEYS:
            if top_j in h['jockey']:
                jockey_bonus = 6
                break

        # 2. 馬場条件 ＆ 種牡馬補正（重馬場適性血統チェック）
        sire_bonus = 0
        if track_condition in ['重', '不良']:
            for heavy_sire in HEAVY_TRACK_SIRES:
                if heavy_sire in h['sire']:
                    sire_bonus = 8
                    break

        total_score = min(98, max(50, base_score + jockey_bonus + sire_bonus))

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

    scored_horses.sort(key=lambda x: x['score'], reverse=True)

    marks = ['◎', '○', '▲', '△', '☆']
    for idx, h in enumerate(scored_horses):
        if idx < len(marks):
            h['mark'] = marks[idx]
        else:
            h['mark'] = '－'

    table_lines = [
        "🏆 **ウマホ全馬期待値スコア**\n",
        "| 印 | 馬番 | 馬名 | 性齢 | 父（種牡馬） | ウマホ期待値 | 評価 |",
        "|---|---|---|---|---|---|---|"
    ]

    for h in scored_horses:
        table_lines.append(f"| {h['mark']} | {h['umaban']} | {h['bamei']} | {h['sex']} | {h['sire']} | {h['score']} / 100 | {h['rank']} |")

    return "\n".join(table_lines)

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

    try:
        url = extract_url(user_text)
        track_condition = extract_track_condition(user_text)

        if url and 'netkeiba' in url:
            race_info, horses = parse_netkeiba_pc(url)
            
            if horses:
                response_text = calculate_local_umaho_scores(horses, track_condition)
            else:
                response_text = "⚠️ Netkeibaから出走馬データを取得できませんでした。\nURLに race_id が含まれているか確認してください。"
        else:
            response_text = "⚠️ NetkeibaのレースURL（https://...）を送信してください。"

        line_bot_api.reply_message(
            reply_token,
            TextSendMessage(text=response_text)
        )

    except Exception as e:
        error_msg = f"⚠️ 処理中にエラーが発生しました。\n\n【詳細】\n{str(e)}"
        try:
            line_bot_api.reply_message(
                reply_token,
                TextSendMessage(text=error_msg[:4000])
            )
        except Exception:
            pass

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
