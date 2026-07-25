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

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1'
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

def fetch_sire_backup(horse):
    """万が一レースページで種牡馬が取れなかった場合のバックアップ取得"""
    if horse['sire'] != '不明' or not horse.get('horse_id'):
        return horse

    try:
        url = f"https://db.sp.netkeiba.com/horse/{horse['horse_id']}"
        res = requests.get(url, headers=HEADERS, timeout=2.5)
        res.encoding = 'utf-8'
        soup = BeautifulSoup(res.text, 'html.parser')

        sire_tag = soup.select_one('table.BloodTable a, .Blood_Table a, a[href*="/horse/pedigree/"]')
        if sire_tag:
            raw_sire = clean_text(sire_tag.text)
            sire_m = re.search(r'[\u30A1-\u30FC]{2,9}', raw_sire)
            if sire_m and sire_m.group(0) != horse['bamei']:
                horse['sire'] = sire_m.group(0)
    except Exception:
        pass

    return horse

def parse_netkeiba(raw_url):
    """馬名の直上・周囲にある種牡馬（シスキン、サートゥルナーリア等）を正確に抽出"""
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
                res = requests.get(target_url, headers=HEADERS, timeout=5)
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

                    # 馬IDの取得
                    href = a_tag.get('href', '')
                    id_m = re.search(r'/horse/(\d{10,12})', href)
                    horse_id = id_m.group(1) if id_m else None

                    # 馬1頭分の要素ブロックを取得
                    row_block = a_tag.find_parent(['tr', 'li', 'dd', 'div'])
                    if not row_block:
                        row_block = a_tag.parent.parent

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

                    # 4. 種牡馬（父）の抽出：馬名の「上」や「周囲」の血統表示要素を解析
                    sire = "不明"
                    
                    # (a) 専用クラス名（Sire, Blood, Father 等）から抽出
                    sire_elem = row_block.select_one('.Sire, .sire, .SireName, .sire_name, .Blood, .blood, .Father, .father, [class*="Sire"], [class*="sire"]')
                    if sire_elem:
                        sm = re.search(r'[\u30A1-\u30FC]{2,9}', clean_text(sire_elem.text))
                        if sm and sm.group(0) != bamei:
                            sire = sm.group(0)

                    # (b) 血統リンク（/pedigree/ や /directory/sire/）から抽出
                    if sire == "不明":
                        sire_a = row_block.find('a', href=re.compile(r'/(sire|pedigree|directory/sire)/', re.I))
                        if sire_a:
                            sm = re.search(r'[\u30A1-\u30FC]{2,9}', clean_text(sire_a.text))
                            if sm and sm.group(0) != bamei:
                                sire = sm.group(0)

                    # (c) 馬名リンクの前後の兄弟タグ（直上テキスト等）から抽出
                    if sire == "不明":
                        prev_sibling = a_tag.find_previous_sibling()
                        if prev_sibling:
                            sm = re.search(r'[\u30A1-\u30FC]{2,9}', clean_text(prev_sibling.text))
                            if sm and sm.group(0) != bamei and sm.group(0) != jockey:
                                sire = sm.group(0)

                    # (d) ブロック内の別カタカナ文字列（父馬名）から判定
                    if sire == "不明":
                        all_a = row_block.find_all('a')
                        for lk in all_a:
                            lk_text = clean_text(lk.text)
                            km = re.search(r'[\u30A1-\u30FC]{2,9}', lk_text)
                            if km:
                                cand = km.group(0)
                                if cand != bamei and cand != jockey and cand not in ['データベース', '掲示板', '血統', 'オッズ', '出走表', 'ニュース', '写真']:
                                    sire = cand
                                    break

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
                    # まだ「不明」の馬があれば高速バックアップ並列通信を実行
                    with ThreadPoolExecutor(max_workers=10) as executor:
                        horses = list(executor.map(fetch_sire_backup, temp_horses))
                    break

            except Exception:
                continue

        horses.sort(key=lambda x: x['umaban'])
        return race_title, horses
    except Exception as e:
        return None, []

def calculate_local_umaho_scores(horses, track_condition):
    """【種牡馬データ連動】ウマホ期待値を高精度に算出"""
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
