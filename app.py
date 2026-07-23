import os
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
genai.configure(api_key=GEMINI_API_KEY)

model = genai.GenerativeModel('gemini-1.5-flash')

def get_umaho_stats(sire, sex, track_type, conditions_dict):
    """
    ウマホの条件検索を行い、条件緩和ループを経て回収率・連対率・期待値を返す関数
    """
    relaxation_order = [
        'prev_dist_type',  # 7. 前走比
        'jockey',          # 6. 騎手
        'frame',           # 5. 枠順
        'weight_type',     # 4. 馬体重(500kg以上/以下)
        'distance',        # 3. 距離
        'venue',           # 2. 競馬場
        'track_condition'  # 1. 馬場状態
    ]
    
    current_vars = conditions_dict.copy()
    headers = {'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15'}

    while True:
        params = {
            'parent': sire,
            'sexes': sex,
            'track_type': track_type,
            **current_vars
        }
        
        try:
            response = requests.get('https://umaho.jp/ranks', params=params, headers=headers, timeout=5)
            soup = BeautifulSoup(response.text, 'html.parser')
            
            has_past_3years = '過去3年' in response.text
            count_elem = soup.select_one('.result-count')
            count = int(count_elem.text.replace('件', '').strip()) if count_elem else 0
            
            if has_past_3years or count < 5:
                if len(relaxation_order) > 0:
                    remove_key = relaxation_order.pop(0)
                    current_vars.pop(remove_key, None)
                    continue
                else:
                    pass
            
            win_recovery_elem = soup.select_one('.win-recovery')
            rentai_rate_elem = soup.select_one('.rentai-rate')
            
            win_recovery = float(win_recovery_elem.text.replace('%', '').replace('円', '').strip()) if win_recovery_elem else 0.0
            rentai_rate = float(rentai_rate_elem.text.replace('%', '').strip()) if rentai_rate_elem else 0.0
            expected_value = (win_recovery * rentai_rate) / 100.0
            
            return {
                'count': count,
                'win_recovery': win_recovery,
                'rentai_rate': rentai_rate,
                'expected_value': expected_value,
                'applied_conditions': current_vars
            }
        except Exception:
            return {'count': 0, 'win_recovery': 0.0, 'rentai_rate': 0.0, 'expected_value': 0.0, 'applied_conditions': {}}

def process_async_prediction(user_text, reply_token, user_id):
    """
    バックグラウンドでGemini予想を生成し、LINEへPush送信する関数
    """
    try:
        # 1. まず即時返信（タイムアウト回避）
        line_bot_api.reply_message(
            reply_token,
            TextSendMessage(text=f"【受付完了】\n「{user_text}」の分析処理を開始しました！\n予想の生成完了まで数秒お待ちください... 🏇")
        )

        # 2. Geminiプロンプトの作成と生成
        prompt = f"""
あなたはウマホの分析ロジック（固定条件：種牡馬・性別・芝ダート／優先度に基づく条件緩和／期待値算出）に熟知した競馬予想AIプロです。

以下のユーザーからの指示・レース情報に基づき、ウマホでの期待値算出プロセスの解説を含めた説得力のある競馬予想を作成してください。

【ユーザーの依頼】
{user_text}

【出力フォーマット】
1. 本命・対抗・穴馬の評価
2. ウマホ分析（固定条件と期待値の観点からの考察）
3. おすすめの買い目
"""
        response = model.generate_content(prompt)

        # 3. 生成された予想文をPushメッセージで送信
        line_bot_api.push_message(
            user_id,
            TextSendMessage(text=response.text)
        )

    except Exception as e:
        # 万が一エラーが発生した場合もLINEに通知
        try:
            line_bot_api.push_message(
                user_id,
                TextSendMessage(text=f"申し訳ありません。予想処理中にエラーが発生しました。\nもう一度お試しください。")
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

    # 非同期スレッドで処理をバックグラウンド実行（LINEの10秒タイムアウトを防止）
    thread = threading.Thread(
        target=process_async_prediction,
        args=(user_text, reply_token, user_id)
    )
    thread.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
