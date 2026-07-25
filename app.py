import os
import re
import asyncio
import aiohttp
from bs4 import BeautifulSoup
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

def parse_race_info(user_text):
    """レース基本情報と各馬のデータを抽出"""
    lines = [l.strip() for l in user_text.strip().splitlines() if l.strip()]
    header = lines[0] if lines else ""

    venue = "札幌"
    for v in ['札幌', '函館', '福島', '新潟', '東京', '中山', '中京', '京都', '阪神', '小倉']:
        if v in header:
            venue = v
            break

    track = "ダート" if "ダート" in header or ("ダ" in header and "芝" not in header) else "芝"
    
    dist_m = re.search(r'(\d{3,4})m?', header)
    distance = f"{dist_m.group(1)}m" if dist_m else "2000m"

    condition = "良"
    for c in ['不良', '稍重', '重', '良']:
        if c in header:
            condition = c
            break

    horses = []
    blocks = re.split(r'\n\s*\n', user_text.strip())

    for block in blocks:
        lines_b = [l.strip() for l in block.splitlines() if l.strip()]
        if not lines_b:
            continue

        bamei, umaban, sire, sex, waku, jockey, dist_change, weight = "不明", 0, "不明", "牡", 1, "不明", "同距離", 450

        for line in lines_b:
            m_horse = re.search(r'^(\d{1,2})[\s\.\:]+([\u30A1-\u30FC]{2,9})', line)
            if m_horse:
                umaban = int(m_horse.group(1))
                bamei = m_horse.group(2)
                continue

            if '種牡馬' in line or '父' in line:
                m_sire = re.search(r'(?:種牡馬|父)[\s\:\：]*([\u30A1-\u30FCa-zA-Z0-9\s]{2,15})', line)
                if m_sire: sire = m_sire.group(1).strip()
            elif '性別' in line or '性齢' in line:
                m_sex = re.search(r'([牡牝セ])', line)
                if m_sex: sex = m_sex.group(1)
            elif '枠' in line:
                m_waku = re.search(r'枠[\s\:\：]*(\d{1,2})', line)
                if m_waku: waku = int(m_waku.group(1))
            elif '騎手' in line:
                m_jockey = re.search(r'騎手[\s\:\：]*([^\s]+)', line)
                if m_jockey: jockey = m_jockey.group(1)
            elif '前走比' in line or '距離' in line:
                if '延長' in line: dist_change = "距離延長"
                elif '短縮' in line: dist_change = "距離短縮"
                else: dist_change = "同距離"
            elif '体重' in line or 'kg' in line:
                m_wt = re.search(r'(\d{3})kg', line)
                if m_wt: weight = int(m_wt.group(1))

        if bamei != "不明":
            horses.append({
                'umaban': umaban,
                'bamei': bamei,
                'sire': sire,
                'sex': sex,
                'track': track,
                'condition': condition,
                'venue': venue,
                'distance': distance,
                'weight': "500kg以上" if weight >= 500 else "500kg未満",
                'waku': str(waku),
                'jockey': jockey,
                'dist_change': dist_change
            })

    return {'venue': venue, 'track': track, 'distance': distance, 'condition': condition}, horses

def extract_metrics_from_html(html_text):
    """
    HTMLテキスト全体から「件数」「連対率」「単勝回収率」の数値を柔軟に抽出する
    """
    soup = BeautifulSoup(html_text, 'html.parser')
    text = soup.get_text(separator=' ')

    # 1. 件数の抽出 (例: "12件", "該当数: 15", "サンプル 8")
    count = 999  # 件数表示がない場合は制限なしとみなす
    count_match = re.search(r'(?:件数|該当|件|データ|サンプル)[^\d]*(\d+)', text)
    if count_match:
        count = int(count_match.group(1))

    # 2. 連対率の抽出 (例: "連対率 25.4%", "連対率: 18.0")
    rentai = None
    rentai_match = re.search(r'連対率[^\d]*(\d+(?:\.\d+)?)%?', text)
    if rentai_match:
        rentai = float(rentai_match.group(1))

    # 3. 単勝回収率の抽出 (例: "単勝回収率 120%", "回収率: 85")
    kaishu = None
    kaishu_match = re.search(r'(?:単勝)?回収率[^\d]*(\d+(?:\.\d+)?)%?', text)
    if kaishu_match:
        kaishu = float(kaishu_match.group(1))

    return count, rentai, kaishu

async def fetch_single_horse(session, horse):
    """1頭分のデータを条件緩和バックトラック付きで取得"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'ja,en-US;q=0.9,en;q=0.8'
    }

    # 可変条件（優先度順: 1.馬場 2.競馬場 3.距離 4.馬体重 5.枠順 6.騎手 7.前走比）
    var_conds = [
        ('condition', horse['condition']),
        ('venue', horse['venue']),
        ('distance', horse['distance']),
        ('weight', horse['weight']),
        ('waku', horse['waku']),
        ('jockey', horse['jockey']),
        ('dist_change', horse['dist_change'])
    ]

    # 優先度の低い可変条件から順に外して再試行
    for i in range(len(var_conds), -1, -1):
        active_vars = dict(var_conds[:i])
        params = {
            'sire': horse['sire'],
            'sex': horse['sex'],
            'track': horse['track'],
            **active_vars
        }

        try:
            async with session.get("https://umaho.jp/search", params=params, headers=headers, timeout=2.5) as res:
                if res.status == 200:
                    html_text = await res.text()
                    count, rentai, kaishu = extract_metrics_from_html(html_text)

                    if rentai is not None and kaishu is not None:
                        # 件数が5件以上あれば採用（件数判定不能な場合も数値が取れていれば採用）
                        if count >= 5:
                            ev = round(rentai * (kaishu / 100.0), 2)
                            return rentai, kaishu, ev
                else:
                    print(f"HTTP Error {res.status} for {horse['bamei']}")
        except Exception as e:
            print(f"Fetch Error for {horse['bamei']}: {e}")

    return 0.0, 0.0, 0.0

async def fetch_all_horses(horses):
    """全頭を一括非同期並列取得"""
    async with aiohttp.ClientSession() as session:
        tasks = [fetch_single_horse(session, h) for h in horses]
        return await asyncio.gather(*tasks)

def build_result_table(race_info, horses):
    """結果テーブル生成"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    stats_list = loop.run_until_complete(fetch_all_horses(horses))
    loop.close()

    results = []
    for h, (rentai, kaishu, ev) in zip(horses, stats_list):
        results.append({
            'umaban': h['umaban'],
            'bamei': h['bamei'],
            'sire': h['sire'],
            'jockey': h['jockey'],
            'rentai': rentai,
            'kaishu': kaishu,
            'ev': ev
        })

    # 期待値順に並び替え
    results.sort(key=lambda x: x['ev'], reverse=True)

    marks = ['◎', '○', '▲', '△', '☆']
    for idx, r in enumerate(results):
        r['mark'] = marks[idx] if idx < len(marks) else '－'

    msg = f"🏟️ **【{race_info['venue']}】{race_info['track']}{race_info['distance']}（{race_info['condition']}馬場）**\n\n"
    msg += "🏆 **ウマホ連対率・回収率に基づく期待値**\n\n"
    msg += "| 印 | 馬番 | 馬名 | 父（種牡馬） | 騎手 | 連対率 | 単勝回収率 | 期待値 |\n"
    msg += "|---|---|---|---|---|---|---|---|\n"

    for r in results:
        msg += f"| {r['mark']} | {r['umaban']} | {r['bamei']} | {r['sire']} | {r['jockey']} | {r['rentai']}% | {r['kaishu']}% | {r['ev']} |\n"

    return msg

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
        race_info, horses = parse_race_info(user_text)
        if horses:
            response_text = build_result_table(race_info, horses)
        else:
            response_text = "⚠️ 出走馬データを正しく読み取れませんでした。"

        line_bot_api.reply_message(reply_token, TextSendMessage(text=response_text))
    except Exception as e:
        line_bot_api.reply_message(reply_token, TextSendMessage(text=f"⚠️ エラーが発生しました:\n{str(e)[:1000]}"))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
