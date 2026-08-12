import os
import json
import sqlite3
import pdfplumber
from google import genai
from flask import Flask, request, jsonify, send_file

app = Flask(__name__)
UPLOAD_FOLDER = './uploads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# 実行環境の環境変数に GEMINI_API_KEY を設定するか、直接文字列で指定してください。
# GitHub等に公開する場合は、直書きせずに環境変数(os.environ.get)を使用してください。
API_KEY = os.environ.get("GEMINI_API_KEY")

# 新しいSDKのClientを使用して初期化します
# APIキーが取得できない場合の簡易的なエラーハンドリングを追加
if not API_KEY:
    print("WARNING: GEMINI_API_KEY が設定されていません。AI解析は失敗します。")
    client = None
else:
    client = genai.Client(api_key=API_KEY)

DB_PATH = 'finance_data.db'

def init_db():
    """SQLiteデータベースとテーブルの作成"""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_name TEXT,
                code TEXT,
                fiscal_year TEXT,
                data_json TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
init_db()

def parse_financial_pdf_smart(tanshin_path, presentation_path=None):
    """
    決算短信と決算説明資料の両方を読み込み、AIに渡す方式。
    """
    # APIキー未設定エラーをフロントに返すためのチェック
    if not client:
        raise ValueError("サーバー側の設定エラー: GEMINI_API_KEY が設定されていません。")

    all_text = ""
    presentation_text = ""

    # STREAMING_CHUNK:決算短信からテキストを抽出（冒頭15ページ）...
    with pdfplumber.open(tanshin_path) as pdf:
        for i, page in enumerate(pdf.pages):
            if i >= 15:
                break
            text = page.extract_text()
            if text:
                all_text += f"--- 短信 Page {i+1} ---\n{text}\n\n"

    # STREAMING_CHUNK:決算説明資料からテキストを抽出（冒頭30ページ）...
    if presentation_path:
        try:
            with pdfplumber.open(presentation_path) as pdf:
                for i, page in enumerate(pdf.pages):
                    if i >= 30: 
                        break
                    text = page.extract_text()
                    if text:
                        presentation_text += f"--- 説明資料 Page {i+1} ---\n{text}\n\n"
        except Exception as e:
            print(f"決算説明資料の読み込みエラー: {e}")

    # STREAMING_CHUNK:プロンプトの構築（AIへの指示）...
    prompt = f"""
    あなたは百戦錬磨の機関投資家・財務アナリストです。提供された「決算短信」と「決算説明資料(ある場合)」のテキストから財務数値を抽出し、指定のJSONフォーマットで出力してください。

    【抽出ルール（絶対厳守）】
    - 指定のJSONフォーマットのみを返すこと。
    - 単位はすべて「百万円」に換算・統一してください（例: テキストが「円」や「十億円」なら百万円に変換）。
    - 損失や減少などのマイナス値はマイナスの数値（例: -100）としてください。「△」や「()」表記はマイナスです。
    - 「金融機関」（銀行・証券など）の判定は慎重に行い、事業会社（小売や製造業で金融子会社を持つ場合など）は誤って金融機関と判定しないでください。真の金融機関で流動/固定の区分がない場合のみ is_financial を true にしてください。
    - IFRSの場合は、売上高を「売上収益」、営業利益を「営業利益」、経常利益を「税引前利益」など適切なラベル名(labels)に設定してください。

    【超重要：B/S（貸借対照表）の負債・純資産の抽出について】
    - 「流動資産」「固定資産(非流動資産)」「流動負債」「固定負債(非流動負債)」は、必ずそれぞれの「合計値」を抽出してください。
    - 🚨【絶対厳守】いかなる場合も、負債の項目（流動負債、固定負債）を理由なく0にしないでください。必ず表の中に数値が存在します。見出しの横に数字がなくても、そのセクションの一番下に合計額があります。
    - IFRS企業の場合、「非流動負債合計」の数値を bs_fixed_liabilities に必ず入れてください。
    - もし「固定負債」という項目がなく、「負債合計」と「流動負債」しかない場合は、「負債合計」から「流動負債」を引いた額を「固定負債(bs_fixed_liabilities)」として計算して入れてください。
    - 🚨【絶対厳守】純資産（資本合計）は、必ずB/S表の最後にある「純資産合計」（IFRSの場合は資本合計）の数値を抽出してください。非支配株主持分が含まれた全体の合計額を探してください。「自己資本」ではありません。

    【AIによる要約（ai_analysis）について】
    - 「決算説明資料のテキスト」が存在する場合は、その内容を大いに反映させてください。
    - 単なる事実の羅列ではなく、**「投資家目線（株価にどう影響するか、企業価値向上に繋がるか）」** で分析してください。
    - 読み手がひと目で判断できるよう、**文頭に必ず「🟢ポジティブ：」「🔴ネガティブ：」「🟡中立：」のいずれかの絵文字ラベルをつけて**、箇条書きで3〜4項目出力してください。
    - ポジティブ要因だけでなく、コスト増や市場環境の悪化、成長の鈍化懸念など、ネガティブな要素（リスク）があれば必ず指摘してください。

    【対象テキスト（決算短信 冒頭15ページ分）】
    {all_text}

    【対象テキスト（決算説明資料 冒頭30ページ分 ※存在する場合のみ）】
    {presentation_text}

    【期待するJSONスキーマ】
    {{
        "company": "企業名",
        "code": "証券コード（数字のみ）",
        "fiscal_year": "対象期（例: 2024年3月期）",
        "is_financial": false,
        "labels": {{ "sales": "売上収益 または 売上高", "op_profit": "営業利益", "ord_profit": "税引前利益 または 経常利益" }},
        "bs_current_assets_prev": 0, "bs_current_assets": 0,
        "bs_fixed_assets_prev": 0, "bs_fixed_assets": 0,
        "bs_current_liabilities_prev": 0, "bs_current_liabilities": 0,
        "bs_fixed_liabilities_prev": 0, "bs_fixed_liabilities": 0,
        "bs_total_assets_prev": 0, "bs_total_assets_now": 0,
        "bs_equity_prev": 0, "bs_equity": 0,
        "pl_sales_prev": 0, "pl_sales_now": 0,
        "pl_op_profit_prev": 0, "pl_op_profit_now": 0,
        "pl_ord_profit_prev": 0, "pl_ord_profit_now": 0,
        "pl_net_profit_prev": 0, "pl_net_profit_now": 0,
        "cf_operating": 0, "cf_investing": 0, "cf_financing": 0,
        "forecast_data": {{ "sales": 0, "op_profit": 0, "net_profit": 0 }},
        "ai_analysis": {{
            "tab1_summary": "当期の業績変動について、投資家目線で分析し、文頭に「🟢ポジティブ：」「🔴ネガティブ：」「🟡中立：」のラベルをつけた箇条書きで出力してください。【400文字程度】",
            "tab2_summary": "来期の見通し・経営戦略について、投資家目線で分析し、文頭に「🟢ポジティブ：」「🔴ネガティブ：」「🟡中立：」のラベルをつけた箇条書きで出力してください。【400文字程度】"
        }}
    }}
    """

    # STREAMING_CHUNK:Gemini APIへ解析リクエストを送信...
    response = client.models.generate_content(
        model='gemini-3.6-flash',
        contents=prompt,
        config=genai.types.GenerateContentConfig(
            response_mime_type="application/json"
        )
    )
    
    # STREAMING_CHUNK:レスポンスをJSONとして安全にパース...
    text = response.text.strip()
    if text.startswith("
```json"):
        text = text[7:]
    elif text.startswith("
```"):
        text = text[3:]
    if text.endswith("
```"):
        text = text[:-3]
        
    return json.loads(text.strip())

@app.route('/')
def index():
    """フロントエンドのHTMLを返します。"""
    return send_file('index.html')

# STREAMING_CHUNK:ファイルのアップロードと解析エンドポイント...
@app.route('/upload', methods=['POST'])
def upload_file():
    if 'tanshin' not in request.files:
        return jsonify({'success': False, 'error': '決算短信のファイルがありません'})
    
    tanshin_file = request.files['tanshin']
    presentation_file = request.files.get('presentation')

    if tanshin_file.filename == '':
        return jsonify({'success': False, 'error': '決算短信のファイルが選択されていません'})

    tanshin_path = os.path.join(app.config['UPLOAD_FOLDER'], 'tanshin_' + tanshin_file.filename)
    tanshin_file.save(tanshin_path)

    presentation_path = None
    if presentation_file and presentation_file.filename != '':
        presentation_path = os.path.join(app.config['UPLOAD_FOLDER'], 'presentation_' + presentation_file.filename)
        presentation_file.save(presentation_path)

    try:
        data = parse_financial_pdf_smart(tanshin_path, presentation_path)
        
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO reports (company_name, code, fiscal_year, data_json)
                VALUES (?, ?, ?, ?)
            ''', (data.get('company'), data.get('code'), data.get('fiscal_year'), json.dumps(data, ensure_ascii=False)))
            
        return jsonify({'success': True, 'data': data})
        
    except Exception as e:
        error_msg = str(e)
        print(f"解析エラー: {error_msg}")
        
        # APIの高負荷・混雑エラーを検知して日本語化
        if "503" in error_msg or "high demand" in error_msg.lower() or "unavailable" in error_msg.lower():
            error_msg = "現在、AIサーバー（Google Gemini）が大変混み合っており一時的に利用できません。数分ほど時間を置いてから、再度「解析スタート」をお試しください。"
            
        return jsonify({'success': False, 'error': error_msg})

# STREAMING_CHUNK:企業検索エンドポイント...
@app.route('/search_companies', methods=['GET'])
def search_companies():
    """DBから企業名またはコードで検索"""
    q = request.args.get('q', '')
    if not q:
        return jsonify([])
        
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, company_name, fiscal_year 
            FROM reports 
            WHERE company_name LIKE ? OR code LIKE ?
            ORDER BY created_at DESC LIMIT 10
        ''', (f'%{q}%', f'%{q}%'))
        
        results = [dict(row) for row in cursor.fetchall()]
        
    return jsonify(results)

# STREAMING_CHUNK:企業データ取得エンドポイント...
@app.route('/get_company_data/<int:id>', methods=['GET'])
def get_company_data(id):
    """指定されたIDの解析済みJSONデータを返す"""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT data_json FROM reports WHERE id = ?', (id,))
        row = cursor.fetchone()
        
    if row:
        return jsonify({'success': True, 'data': json.loads(row[0])})
    else:
        return jsonify({'success': False, 'error': 'データが見つかりません'})

if __name__ == '__main__':
    app.run(debug=True, port=5000)
```eof

画像を確認しました！
GitHubのセキュリティ機能である「シークレットスキャン」が作動して、アップロードをブロックしてくれている状態ですね。これは**非常に優秀でありがたい機能**です！

### なぜエラーが出たのか？
`app.py` の15行目付近に、`API_KEY = "AQ.Ab8RN6JA..."` のように、**実際のGemini APIキーの文字列を直接書き込んだまま**にしていましたよね。
GitHubは世界中に公開される可能性がある場所なので、APIキーのような「秘密のパスワード」がコード内に直接書かれているのを発見すると、「このままアップロードすると、世界中の悪意ある人にAPIキーを盗まれて悪用されちゃうよ！」と警告し、強制的にストップをかけてくれます。

### どうやって解決するか？（環境変数を使う）
クラウド（Renderなど）にデプロイする際の**絶対的な鉄則**として、パスワードやAPIキーはコードに直接書かず、**「環境変数（Environment Variables）」**という、サーバー側だけに設定する「見えない引き出し」から読み込むようにします。

上記で出力した `app.py` では、APIキーを直接書くのをやめ、サーバーから読み込むようにコードを修正しました。
（`API_KEY = os.environ.get("GEMINI_API_KEY")` という形になっています）

### 具体的な解決ステップ

**1. ご自身のMacにある `app.py` を修正する**
お手元の `app.py` を、上記で出力した最新のコードで上書き保存してください。これでAPIキーの文字列がコードから消え去ります。

**2. GitHubへもう一度アップロード（プッシュ）する**
コードからAPIキーが消えたので、もう警告は出ません。
先ほどと同じように、Macのターミナルで以下のコマンドを順番に実行してください。

```bash
git add app.py
git commit -m "Remove API key and use env var"
git push -u origin main
