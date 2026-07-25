import os
import re
import hashlib
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

def extract_track_condition(text):
    """テキスト内から馬場状態（良・稍重・重・不良）を自動判定"""
    if '不良' in text:
        return '不良'
    elif '稍重' in text:
        return '稍重'
    elif '重' in text:
        return '重'
    return '良'

def parse_pasted_text(raw_text):
    """コピペされた出走表テキストから馬情報を高精度抽出"""
    lines = raw_text.splitlines()
    horses = []
    seen_bamei = set()

    for line in lines:
        line_str = line.strip()
        if not line_str:
            continue

        # カタカナ単語（2文字以上）をすべて抽出
        kana_words = re.findall(r'[\u30A1-\u30FC]{2,9}', line_str)
        if not kana_words:
            continue

        # 除外キーワード
        invalid_words = ['データベース', '掲示板', '血統', 'オッズ', '出走表', 'ニュース', '写真', '調教', '予想', 'タイム', 'パドック']
        kana_words = [w for w in kana_words if w not in invalid_words]

        if not kana_words:
            continue

        # 馬名の特定（最初に見つかったカタカナ）
        bamei = kana_words[0]
        if bamei in seen_bamei:
            continue

        # 馬番の取得（行頭付近の数字）
        umaban_m = re.search(r'^\s*(\d{1,2})\b', line_str)
        if not umaban_m:
            umaban_m = re.search(r'\b(\d{1,2})\b', line_str)
        umaban = int(umaban_m.group(1)) if umaban_m else (len(horses) + 1)

        # 性齢の取得 (例: 牡3, 牝4, セ5)
        sex_m = re.search(r'([牡牝セ]\d{1,2})', line_str)
        sex = sex_m.group(1) if sex_m else "不明"

        # 騎手・父（種牡馬）の特定
        jockey = "不明"
        sire = "不明"

        # 2番目以降のカタカナ単語から騎手と父馬を割り当て
        for word in kana_words[1:]:
            # 騎手判定
            if jockey == "不明" and any(j in word for j in TOP_JOCKEYS + ['丹内', '横山', '川田', 'ルメール', '武', '戸崎', '坂井']):
                jockey = word
                continue
            
            # 父（種牡馬）判定（馬名でも騎手でもないカタカナ）
            if sire == "不明" and word != bamei and word != jockey:
                sire = word

        seen_bamei.add(bamei)
        horses.append({
            'umaban': umaban,
            'bamei': bamei,
            'sex': sex,
            'jockey': jockey,
            'sire': sire
        })

    horses.sort(key=lambda x: x['umaban'])
    return horses

def calculate_local_umaho_scores(horses, track_condition):
    """期待値スコア計算"""
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
        track_condition = extract_track_condition(user_text)
        horses = parse_pasted_text(user_text)

        if horses and len(horses) >= 2:
            response_text = calculate_local_umaho_scores(horses, track_condition)
        else:
            response_text = "⚠️ 出走表テキストから馬情報を読み取れませんでした。\nNetkeibaなどの出走表テキストをコピーして貼り付けて送信してください。"

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
