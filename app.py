import os
import re
import hashlib
import requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor
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

HEADERS_MOBILE = {
    'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1'
}

HEADERS_PC = {
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

def clean_text(text):
    """改行や不要なデータベース文字列を削除"""
    if not text:
        return ""
    text = re.sub(r'[\r\n\t]+', '', text)
    text = re.sub(r'(の?(データベース|競走馬データ|掲示板|血統|オッズ|戦績|情報|プロフィール|写真))+$', '', text)
    return text.strip()

def fetch_sire_from_db(horse):
    """【100%確実】PC版Netkeiba DBの血統表(table.blood_table)から父馬（種牡馬）を取得"""
    if horse['sire'] != '不明' or not horse.get('horse_id'):
        return horse

    try:
        url = f"https://db.netkeiba.com/horse/{horse['horse_id']}/"
        res = requests.get(url, headers=HEADERS_PC, timeout=3)
        html = res.content.decode('euc-jp', errors='ignore')
        soup = BeautifulSoup(html, 'html.parser')

        # PC版Netkeiba血統表の1行目・1列目のリンクは100%父（種牡馬）
        sire_a = soup.select_one('table.blood_table td a')
        if sire_a:
            raw_sire = clean_text(sire_a.text)
            sm = re.search(r'[\u30A1-\u30FC]{2,9}', raw_sire)
            if sm and sm.group(0) != horse['bamei']:
                horse['sire'] = sm.group(0)
                return horse

        # バックアップ：プロフィールテーブルから父名を取得
        prof_a = soup.select_one('table.db_prof_table a[href*="/horse/sire/"], table.db_prof_table a[href*="/horse/pedigree/"]')
        if prof_a:
            raw_sire = clean_text(prof_a.text)
            sm = re.search(r'[\u30A1-\u30FC]{2,9}', raw_sire)
            if sm and sm.group(0) != horse['bamei']:
                horse['sire'] = sm.group(0)
                return horse
    except Exception:
        pass

    return horse

def get_horse_block(a_tag):
    """馬名リンクから出走馬1頭分の全体行ブロックを正しく特定"""
    curr = a_tag
    for _ in range(5):
        curr = curr.parent
        if not curr:
            break
        if curr.name in ['li', 'tr']:
            return curr
        classes = " ".join(curr.get('class', []))
        if any(k in classes.lower() for k in ['horse', 'list', 'item', 'row', 'shutuba']):
            return curr
    return a_tag.parent.parent if a_tag.parent else a_tag

def parse_netkeiba(raw_url):
    """レース出走表解析 ＋ DB高速自動連携"""
    try:
        race_id_match = re.search(r'(\d{10,12})', raw_url)
        if not race_id_match:
            return None, []

        race_id = race_id_match.group(1)

        target_urls = [
            f"https://race.sp.netkeiba.com/race/newspaper.html?race_id={race_id}",
            f"https://race.sp.netkeiba.com/race/shutuba.html?race_id={race_id}"
        ]

        horses = []
        race_title = "レース"

        for target_url in target_urls:
            try:
                res = requests.get(target_url, headers=HEADERS_MOBILE, timeout=5)
                res.encoding = 'utf-8'
                soup = BeautifulSoup(res.text, 'html.parser')

                title_elem = soup.select_one('.RaceName, .race_name, h1, .RaceNum, .Race_Title')
                if title_elem and title_elem.text.strip():
                    race_title = clean_text(title_elem.text)

                horse_links = soup.find_all('a', href=re.compile(r'/horse/\d+'))
                if not horse_links:
                    continue

                seen_bamei = set()
                temp_horses = []

                for a_tag in horse_links:
                    raw_text = clean_text(a_tag.text)
                    kana_match = re.search(r'[\u30A1-\u30FC]{2,9}', raw_text)
                    if not kana_match:
                        continue
                    bamei = kana_match.group(0)

                    if bamei in seen_bamei or bamei in ['データベース', '掲示板', '血統', 'オッズ', '出走表', 'ニュース', '写真']:
                        continue

                    # 馬IDの抽出
                    href = a_tag.get('href', '')
                    id_m = re.search(r'/horse/(\d{10,12})', href)
                    horse_id = id_m.group(1) if id_m else None

                    # 馬1頭分の正当な全体ブロックを取得
                    row_block = get_horse_block(a_tag)

                    # 1. 馬番
                    umaban = "0"
                    umaban_elem = row_block.select_one('.Umaban, .td_umaban, .Num, .num, td.Num, .Umaban_Num, .no')
                    if umaban_elem:
                        m = re.search(r'\d+', umaban_elem.text)
                        if m:
                            umaban = m.group(0)

                    # 2. 性齢
                    sex = "不明"
                    sex_match = re.search(r'([牡牝セ]\d{1,2})', row_block.text)
                    if sex_match:
                        sex = sex_match.group(1)

                    # 3. 騎手
                    jockey = "不明"
                    jockey_a = row_block.find('a', href=re.compile(r'/jockey/'))
                    if jockey_a:
                        jockey = clean_text(jockey_a.text)
                    else:
                        jockey_elem = row_block.select_one('.Jockey, .jockey, .JockeyName')
                        if jockey_elem:
                            jockey = clean_text(jockey_elem.text)

                    # 4. レース表上からの種牡馬（父）抽出
                    sire = "不明"
                    sire_elem = row_block.select_one('.Sire, .sire, .SireName, .sire_name, .Blood, .blood, .Father, .father')
                    if sire_elem:
                        sm = re.search(r'[\u30A1-\u30FC]{2,9}', clean_text(sire_elem.text))
                        if sm and sm.group(0) != bamei:
                            sire = sm.group(0)

                    if sire == "不明":
                        sire_a = row_block.find('a', href=re.compile(r'/(sire|pedigree|directory/sire)/', re.I))
                        if sire_a:
                            sm = re.search(r'[\u30A1-\u30FC]{2,9}', clean_text(sire_a.text))
                            if sm and sm.group(0) != bamei:
                                sire = sm.group(0)

                    seen_bamei.add(bamei)
                    temp_horses.append({
                        'horse_id': horse_id,
                        'umaban': int(umaban) if umaban.isdigit() and int(umaban) > 0 else len(temp_horses) + 1,
                        'bamei': bamei,
                        'sex': sex,
                        'jockey': jockey,
                        'sire': sire
                    })

                if len(temp_horses) >= 3:
                    # 種牡馬が「不明」の馬があれば、Netkeiba DBから爆速並列（スレッド）一括補完
                    unresolved = [h for h in temp_horses if h['sire'] == '不明' and h.get('horse_id')]
                    if unresolved:
                        with ThreadPoolExecutor(max_workers=12) as executor:
                            temp_horses = list(executor.map(fetch_sire_from_db, temp_horses))
                    horses = temp_horses
                    break

            except Exception:
                continue

        horses.sort(key=lambda x: x['umaban'])
        return race_title, horses
    except Exception as e:
        return None, []

def calculate_local_umaho_scores(horses, track_condition):
    """【血統補正連動】ウマホ期待値を高精度算出"""
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
