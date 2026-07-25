import os
import re
import asyncio
from bs4 import BeautifulSoup
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from curl_cffi.requests import AsyncSession

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# アクセス遮断を防ぐための同時リクエスト数制限（最大2並列）
SEMAPHORE = asyncio.Semaphore(2)

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
    """HTMLテキストから「件数」「連対率」「単勝回収率」を抽出"""
    soup = BeautifulSoup(html_text, 'html.parser')
    text = soup.get_text(separator=' ')

    count = 999
    count_match = re.search(r'(?:件数|該当|件|データ|サンプル)[^\d]*(\d+)', text)
    if count_match:
        count = int(count_match.group(1))

    rentai = None
    rentai_match = re.search(r'連対率[^\d]*(\d+(?:\.\d+)?)%?', text)
    if rentai_match:
        rentai = float(rentai_match.group(1))

    kaishu = None
    kaishu_match = re.search(r'(?:単勝)?回収率[^\d]*(\d+(?:\.\d+)?)%?', text)
    if kaishu_match:
        kaishu = float(kaishu_match.group(1))

    return count, rentai, kaishu

async def fetch_single_horse(session, horse):
    """1頭分のデータをChrome偽装通信＋条件緩和バックトラック付きで取得"""
    var_conds = [
        ('condition', horse['condition']),
        ('venue', horse['venue']),
        ('distance', horse['distance']),
        ('weight', horse['weight']),
        ('waku', horse['waku']),
        ('jockey', horse['jockey']),
        ('dist_change', horse['dist_change'])
    ]

    last_status = 200

    async with SEMAPHORE:
        for i in range(len(var_conds), -1, -1):
            active_vars = dict(var_conds[:i])
            params = {
                'sire': horse['sire'],
                'sex': horse['sex'],
                'track': horse['track'],
                **active_vars
            }

            try:
                # curl_cffi により Chrome 120 のTLS/ブラウザ通信を完全模倣
                res = await session.get(
                    "https://umaho.jp/search",
                    params=params,
                    timeout=5.0
                )
                last_status = res.status_code

                if res.status_code == 200:
                    count, rentai, kaishu = extract_metrics_from_html(res.text)

                    if rentai is not None and kaishu is not None:
                        if count >= 5:
                            ev = round(rentai * (kaishu / 100.0), 2)
                            return rentai, kaishu, ev, 200
            except Exception:
                pass

            await asyncio.sleep(0.2)

    return 0.0, 0.0, 0.0, last_status

async def fetch_all_horses(horses):
    """全頭のデータをChrome偽装AsyncSessionで一括取得"""
    async with AsyncSession(impersonate="chrome120") as session:
        tasks = [fetch_single_horse(session, h) for h in horses]
        return await asyncio.gather(*tasks)

def build_result_table(race_info, horses):
    """結果テーブルまたはエラーメッセージの生成"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    stats_list = loop.run_until_complete(fetch_all_horses(horses))
    loop.close()

    blocked_count = sum(1 for item in stats_list if item[3] in [403, 429])
    if blocked_count > 0:
        return f"⚠️ **ウマホからのアクセスが拒否されました (HTTP {stats_list[0][3]})**\n\nCloudflare/WAF制限が適用されています。"

    results = []
    zero_count = 0
    for h, (rentai, kaishu, ev, status) in zip(horses, stats_list):
        if ev == 0.0:
            zero_count += 1
        results.append({
            'umaban': h['umaban'],
            'bamei': h['bamei'],
            'sire': h['sire'],
            'jockey': h['jockey'],
            'rentai': rentai,
            'kaishu': kaishu,
            'ev': ev
        })

    if zero_count == len(horses):
        return "⚠️ **ウマホからデータを抽出できませんでした**\n\n検索URLパラメータまたはテキスト解析規則の確認が必要です。"

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
