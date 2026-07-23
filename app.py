import os
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

# Geminiモデルの設定
model = genai.GenerativeModel('gemini-1.5-flash')

def get_umaho_stats(sire, sex, track_type, conditions_dict):
    """
    ウマホの条件検索を行い、条件緩和ループを経て回収率・連対率・期待値を返す関数
    """
    # 削除していく優先順位（1番目に外す条件から順に配置）
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
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15'
    }

    while True:
        # 固定条件（種牡馬・性別・芝ダート）＋ 現在有効な可変条件でURL・クエリを構築
        params = {
            'parent': sire,
            'sexes': sex,
            'track_type': track_type,
            **current_vars
        }
        
        try:
            response = requests.get('https://umaho.jp/ranks', params=params, headers=headers, timeout=10)
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # 「過去3年」タグが挿入されているか検証（条件不一致の自動フォールバック検知）
            has_past_3years = '過去3年' in response.text
            
            # 件数の取得
            count_elem = soup.select_one('.result-count')
            count = int(count_elem.text.replace('件', '').strip()) if count_elem else 0
            
            # 条件不成立（過去3年タグ出現）または 5件未満 の場合
            if has_past_3years or count < 5:
                if len(relaxation_order) > 0:
                    remove_key = relaxation_order.pop(0)  # 削除優先度の高い条件から外す
                    current_vars.pop(remove_key, None)
                    continue  # 再検索へ
                else:
                    # 可変条件がすべてなくなっても5件未満の場合、その時点のデータで確定
                    pass
            
            # 数値の取得（単勝回収率・連対率）
            win_recovery_elem = soup.select_one('.win-recovery')
            rentai_rate_elem = soup.select_one('.rentai-rate')
            
            win_recovery = float(win_recovery_elem.text.replace('%', '').replace('円', '').strip()) if win_recovery_elem else 0.0
            rentai_rate = float(rentai_rate_elem.text.replace('%', '').strip()) if rentai_rate_elem else 0.0
            
            # 期待値の計算
            expected_value = (win_recovery * rentai_rate) / 100.0
            
            return {
                'count': count,
                'win_recovery': win_recovery,
                'rentai_rate': rentai_rate,
                'expected_value': expected_value,
                'applied_conditions': current_vars
            }
            
        except Exception as e:
            # エラー発生時は安全策としてデフォルト値を返却
            return {'count': 0, 'win_recovery': 0.0, 'rentai_rate': 0.0, 'expected_value': 0.0, 'applied_conditions': {}}

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
    
    # ユーザーへ一時受付メッセージを即時返信
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=f"【受付完了】\n「{user_text}」のウマホデータ・期待値分析を開始します...")
    )
    
    # Geminiによる総合予想プロンプトの作成と送信
    prompt = f"以下の依頼内容に基づき、競馬予想を行ってください。\n依頼: {user_text}"
    response = model.generate_content(prompt)
    
    # 推しメッセージとして後送する仕組みを構成
    line_bot_api.push_message(
        event.source.user_id,
        TextSendMessage(text=response.text)
    )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
