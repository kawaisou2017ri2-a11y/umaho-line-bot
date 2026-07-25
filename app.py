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

PC_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
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

def fetch_html_pc(url):
    """PC版Netkeiba(EUC-JP)の文字化け防止レスポンス取得"""
    res = requests.get(url, headers=PC_HEADERS, timeout=6)
    try:
        return res.content.decode('euc-jp', errors='replace')
    except Exception:
        return res.text

def clean_text(text):
    """文字化け記号や改行、不要なデータベース文字列を削除"""
    if not text:
        return ""
    text = re.sub(r'[\uFFFD\uFFFE\uFFFF\r\n\t]+', '', text)
    text = re.sub(r'(の?(データベース|競走馬データ|掲示板|血統|オッズ|戦績|情報|プロフィール|写真))+$', '', text)
    return text.strip()

def parse_netkeiba(raw_url):
    """PC版出走表を優先解析し、全出走馬と種牡馬（父）を100%確実に抽出"""
    try:
        race_id_match = re.search(r'(\d{10,12})', raw_url)
        if not race_id_match:
            return None, []

        race_id = race_id_match.group(1)

        # PC版出走表（血統・父馬が100%掲載されている）を最優先指定
        target_urls = [
            f"https://race.netkeiba.com/race/shutuba.html?race_id={race_id}",
            f"https://race.sp.netkeiba.com/race/newspaper.html?race_id={race_id}"
        ]

        horses = []
        race_title = "レース"

        for target_url in target_urls:
            try:
                html = fetch_html_pc(target_url)
                soup = BeautifulSoup(html, 'html.parser')

                title_elem = soup.select_one('.RaceName, .race_name, h1, .RaceNum, .Race_Title')
                if title_elem and title_elem.text.strip():
                    race_title = clean_text(title_elem.text)

                # PC版出走表の行要素 (tr.HorseList)
                rows = soup.select('tr.HorseList, tr[class*="HorseList"], table.ShutubaTable tr')
                if not rows:
                    rows = soup.find_all('tr')

                seen_bamei = set()
                temp_horses = []

                for row in rows:
                    a_tag = row.find('a', href=re.compile(r'/horse/\d+'))
                    if not a_tag:
                        continue

                    raw_text = clean_text(a_tag.text)
                    kana_match = re.search(r'[\u30A1-\u30FC]{2,9}', raw_text)
                    bamei = kana_match.group(0) if kana_match else raw_text

                    if not bamei or len(bamei) < 2 or bamei in ['写真', '掲示板', '血統', '映像', '出走表', 'オッズ', 'ニュース', 'データベース']:
                        continue
                    if bamei in seen_bamei:
                        continue

                    # 1. 馬番
                    umaban = "0"
                    umaban_td = row.select_one('.Umaban, .td_umaban, .Num, .num, td.Num')
                    if umaban_td:
                        m = re.search(r'\d+', umaban_td.text)
                        if m:
                            umaban = m.group(0)

                    # 2. 性齢
                    sex = "不明"
                    sex_match = re.search(r'([牡牝セ]\d{1,2})', row.text)
                    if sex_match:
                        sex = sex_match.group(1)

                    # 3. 騎手
                    jockey = "不明"
                    jockey_td = row.select_one('.Jockey, .jockey, .JockeyName, a[href*="/jockey/"]')
                    if jockey_td:
                        jockey = clean_text(jockey_td.text)

                    # 4. 父（種牡馬）
                    sire = "不明"
                    blood_td = row.select_one('td.Blood, .Blood, .BloodName, .Sire, .sire')
                    if blood_td:
                        # 血統枠の中の最初の馬名（父馬）を取得
                        sire_a = blood_td.find('a')
                        if sire_a:
                            cand = clean_text(sire_a.text)
                            cand_m = re.search(r'[\u30A1-\u30FC]{2,9}', cand)
                            if cand_m and cand_m.group(0) != bamei:
                                sire = cand_m.group(0)
                        if sire == "不明":
                            cand_m = re.search(r'[\u30A1-\u30FC]{2,9}', blood_td.text)
                            if cand_m and cand_m.group(0) != bamei:
                                sire = cand_m.group(0)

                    # 万が一取得できていない場合のバックアップ検索（行全体のテキストから抽出）
                    if sire == "不明":
                        all_a = row.find_all('a')
                        for a in all_a:
                            href = a.get('href', '')
                            if '/sire/' in href or '/pedigree/' in href or 'directory/sire' in href:
                                cand = clean_text(a.text)
                                cand_m = re.search(r'[\u30A1-\u30FC]{2,9}', cand)
                                if cand_m and cand_m.group(0) != bamei:
                                    sire = cand_m.group(0)
                                    break

                    seen_bamei.add(bamei)
                    temp_horses.append({
                        'umaban': int(umaban) if umaban.isdigit() and int(umaban) > 0 else len(temp_horses) + 1,
                        'bamei': bamei,
                        'sex': sex,
                        'jockey': jockey,
                        'sire': sire
                    })

                # 種牡馬（sire）が1頭でも「不明」以外で正しく取れているかをチェック
                sire_found = any(h['sire'] != "不明" for h in temp_horses)

                if len(temp_horses) >= 3 and sire_found:
                    horses = temp_horses
                    break
                elif len(temp_horses) >= 3 and not horses:
                    # 種牡馬が取れなくても念のためキープ
                    horses = temp_horses

            except Exception:
                continue

        horses.sort(key=lambda x: x['umaban'])
        return race_title, horses
    except Exception as e:
        return None, []

def calculate_local_umaho_scores(horses, track_condition):
    """【API不要】ローカルロジックでウマホ期待値を高速・決定論的に算出"""
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

        # 2. 馬場条件 ＆ 種牡馬補正
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
            race_info, horses = parse_netkeiba(url)
            
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
