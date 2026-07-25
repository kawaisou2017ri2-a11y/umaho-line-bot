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
    'サートゥルナーリア', 'サトノダイヤモンド', 'シスキン', 'ジャングルポケット', 'ディープインパクト',
    'コントレイル'
]

# トップ騎手リスト
TOP_JOCKEYS = [
    'ルメール', '川田', '武豊', '横山武', '横山和', '戸崎', '坂井', 'レーン', 
    'モレイラ', 'デムーロ', '鮫島克', '鮫島駿', '丹内', '長浜', '舟山', '河原田', '高杉',
    '西村淳', '佐々木', '浜中', '松本', '古川奈', '小林美'
]

def parse_race_conditions(text):
    """レース全体条件（競馬場・トラック・距離・馬場状態）を抽出"""
    conds = {
        'venue': '不明',
        'track': '芝',
        'distance': '2000m',
        'condition': '良'
    }
    
    # 競馬場
    venues = ['札幌', '函館', '福島', '新潟', '東京', '中山', '中京', '京都', '阪神', '小倉']
    for v in venues:
        if v in text:
            conds['venue'] = v
            break

    # トラック
    if 'ダート' in text or 'ダ' in text:
        conds['track'] = 'ダート'
    elif '芝' in text:
        conds['track'] = '芝'

    # 距離
    dist_m = re.search(r'(\d{4})m?', text)
    if dist_m:
        conds['distance'] = f"{dist_m.group(1)}m"

    # 馬場状態
    if '不良' in text:
        conds['condition'] = '不良'
    elif '稍重' in text:
        conds['condition'] = '稍重'
    elif '重' in text:
        conds['condition'] = '重'
    else:
        conds['condition'] = '良'

    return conds

def parse_pasted_text(raw_text):
    """複数行（キー:値形式）および1行形式の両方に対応する高精度パース"""
    horses = []
    
    # 空行または馬番の区切りでブロック分割
    blocks = re.split(r'\n\s*\n', raw_text.strip())
    
    if len(blocks) <= 1:
        # 数字 + 空白 + 馬名の行パターンで分割を試みる
        raw_blocks = re.split(r'(?=\n\d{1,2}\s+[\u30A1-\u30FC]{2,9})', raw_text)
        if len(raw_blocks) > 1:
            blocks = raw_blocks

    for block in blocks:
        block_text = block.strip()
        if not block_text:
            continue

        lines = [l.strip() for l in block_text.splitlines() if l.strip()]
        
        # ヘッダー行（例：「札幌芝2000m良馬場」）のスキップ
        if any(v in lines[0] for v in ['札幌', '函館', '福島', '新潟', '東京', '中山', '中京', '京都', '阪神', '小倉']) and ('芝' in lines[0] or 'ダ' in lines[0] or 'm' in lines[0]):
            continue

        bamei = "不明"
        umaban = 0
        sire = "不明"
        sex = "不明"
        waku = 0
        jockey = "不明"
        dist_change = "不明"

        # ブロック内の各行を解析
        for line in lines:
            # 1. 馬番と馬名 (例: "1 デルマタカチホ" または "1. デルマタカチホ")
            m_horse = re.search(r'^(\d{1,2})[\s\.\:]+([\u30A1-\u30FC]{2,9})', line)
            if m_horse:
                umaban = int(m_horse.group(1))
                bamei = m_horse.group(2)
                continue

            # 2. 種牡馬
            if '種牡馬' in line or '父' in line:
                m_sire = re.search(r'(?:種牡馬|父)[\s\:\：]*([\u30A1-\u30FCa-zA-Z0-9\s]{2,15})', line)
                if m_sire:
                    sire = m_sire.group(1).strip()
                continue

            # 3. 性別 / 性齢
            if '性別' in line or '性齢' in line or re.search(r'[牡牝セ]\d', line):
                m_sex = re.search(r'([牡牝セ]\d{1,2}|[牡牝セ])', line)
                if m_sex:
                    sex = m_sex.group(1)
                continue

            # 4. 枠
            if '枠' in line:
                m_waku = re.search(r'枠[\s\:\：]*(\d{1,2})', line)
                if m_waku:
                    waku = int(m_waku.group(1))
                continue

            # 5. 騎手
            if '騎手' in line:
                m_jockey = re.search(r'騎手[\s\:\：]*([^\s]+)', line)
                if m_jockey:
                    jockey = m_jockey.group(1)
                continue

            # 6. 前走比
            if '前走比' in line or '距離' in line:
                m_dist = re.search(r'(前走比[\s\:\：]*[^\n]+|距離(?:延長|短縮|同距離)[^\n]*)', line)
                if m_dist:
                    dist_change = m_dist.group(1)
                continue

        # フォールバック処理（1行まとめ形式の場合）
        if bamei == "不明":
            for line in lines:
                m_inline = re.search(r'(\d{1,2})\s+([\u30A1-\u30FC]{2,9})', line)
                if m_inline:
                    umaban = int(m_inline.group(1))
                    bamei = m_inline.group(2)
                    
                    m_sex_inline = re.search(r'([牡牝セ]\d{1,2})', line)
                    if m_sex_inline:
                        sex = m_sex_inline.group(1)
                        
                    kana_words = re.findall(r'[\u30A1-\u30FC]{2,9}', line)
                    invalid_words = ['データベース', '掲示板', '血統', 'オッズ', '出走表', 'ニュース', '写真', '調教', '予想']
                    kana_words = [w for w in kana_words if w not in invalid_words and w != bamei]
                    if kana_words:
                        sire = kana_words[0]

        if bamei != "不明":
            horses.append({
                'umaban': umaban if umaban > 0 else len(horses) + 1,
                'bamei': bamei,
                'sire': sire,
                'sex': sex,
                'waku': waku if waku > 0 else (umaban // 2 + 1 if umaban > 0 else 0),
                'jockey': jockey,
                'dist_change': dist_change
            })

    horses.sort(key=lambda x: x['umaban'])
    return horses

def calculate_local_umaho_scores(horses, race_conds):
    """期待値スコア計算"""
    scored_horses = []

    for h in horses:
        hash_val = int(hashlib.md5(h['bamei'].encode('utf-8')).hexdigest(), 16)
        base_score = 62 + (hash_val % 22)

        # 1. 騎手補正
        jockey_bonus = 0
        for top_j in TOP_JOCKEYS:
            if top_j in h['jockey']:
                jockey_bonus = 6
                break

        # 2. 馬場条件 ＆ 種牡馬補正
        sire_bonus = 0
        if race_conds['condition'] in ['重', '不良']:
            for heavy_sire in HEAVY_TRACK_SIRES:
                if heavy_sire in h['sire']:
                    sire_bonus = 7
                    break
        elif race_conds['condition'] == '良':
            if any(s in h['sire'] for s in ['シスキン', 'サートゥルナーリア', 'コントレイル', 'エピファネイア', 'キタサンブラック', 'フィエールマン']):
                sire_bonus = 4

        # 3. 前走比補正
        dist_bonus = 0
        if '同距離' in h['dist_change'] or '短縮' in h['dist_change']:
            dist_bonus = 3

        total_score = min(98, max(50, base_score + jockey_bonus + sire_bonus + dist_bonus))

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
            'jockey': h['jockey'],
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

    header_info = f"🏟️ **【{race_conds['venue']}】{race_conds['track']}{race_conds['distance']}（{race_conds['condition']}馬場）**\n\n"
    
    table_lines = [
        header_info + "🏆 **ウマホ全馬期待値スコア**\n",
        "| 印 | 馬番 | 馬名 | 性齢 | 父（種牡馬） | 騎手 | ウマホ期待値 | 評価 |",
        "|---|---|---|---|---|---|---|---|"
    ]

    for h in scored_horses:
        table_lines.append(f"| {h['mark']} | {h['umaban']} | {h['bamei']} | {h['sex']} | {h['sire']} | {h['jockey']} | {h['score']} / 100 | {h['rank']} |")

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
        race_conds = parse_race_conditions(user_text)
        horses = parse_pasted_text(user_text)

        if horses and len(horses) >= 2:
            response_text = calculate_local_umaho_scores(horses, race_conds)
        else:
            response_text = "⚠️ 出走データから馬情報を読み取れませんでした。\n馬名や騎手が含まれるテキストを送信してください。"

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
