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

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

def generate_ai_response(prompt):
    """APIキーで利用可能なモデルを自動取得して呼び出す安全な関数"""
    last_error = None
    
    # 1. まずは一般的な最新モデル名を直接試す
    for preferred_model in ['gemini-1.5-flash', 'gemini-1.5-pro', 'gemini-2.0-flash']:
        try:
            m = genai.GenerativeModel(preferred_model)
            res = m.generate_content(prompt)
            if res and res.text:
                return res.text
        except Exception as e:
            last_error = e

    # 2. 直指定で全滅した場合、アカウントで利用可能なモデル一覧を動的取得して試す
    try:
        available_models = genai.list_models()
        for m in available_models:
            if 'generateContent' in m.supported_generation_methods:
                try:
                    # モデル名（'models/xxx'）からモデルを生成
                    model_instance = genai.GenerativeModel(m.name)
                    res = model_instance.generate_content(prompt)
                    if res and res.text:
                        return res.text
                except Exception as inner_e:
                    last_error = inner_e
                    continue
    except Exception as list_e:
        last_error = list_e

    raise last_error if last_error else Exception("利用可能なGeminiモデルが見つかりませんでした。GEMINI_API_KEYをご確認ください。")

def process_async_prediction(user_text, reply_token, user_id):
    """バックグラウンドでGemini予想を生成し、LINEへPush送信する関数"""
    try:
        # 1. 即時返信（タイムアウト防止）
        line_bot_api.reply_message(
            reply_token,
            TextSendMessage(text=f"【受付完了】\n「{user_text}」の分析処理を開始しました！\n予想の生成完了まで数秒お待ちください... 🏇")
        )

        # 2. Geminiでの予想生成
        prompt = f"""
あなたはウマホの分析ロジックに熟知したプロの競馬予想AIです。
以下の依頼内容に基づき、説得力のある競馬予想を作成してください。

【依頼内容】
{user_text}

【出力フォーマット】
1. 本命・対抗・穴馬の評価
2. 予想の根拠と解説
3. おすすめの買い目
"""
        response_text = generate_ai_response(prompt)

        # 3. 予想メッセージの送信
        line_bot_api.push_message(
            user_id,
            TextSendMessage(text=response_text)
        )

    except Exception as e:
        error_msg = f"⚠️ エラーが発生しました。\n\n【詳細原因】\n{str(e)}"
        try:
            line_bot_api.push_message(
                user_id,
                TextSendMessage(text=error_msg)
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

    thread = threading.Thread(
        target=process_async_prediction,
        args=(user_text, reply_token, user_id)
    )
    thread.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
