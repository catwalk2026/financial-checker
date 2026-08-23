import os
import json
import sqlite3
import pdfplumber
import re
from google import genai
from flask import Flask, request, jsonify, send_file

app = Flask(__name__)
UPLOAD_FOLDER = './uploads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

API_KEY = os.environ.get("GEMINI_API_KEY")

if not API_KEY:
    print("WARNING: GEMINI_API_KEY が設定されていません。")
    client = None
else:
    client = genai.Client(api_key=API_KEY)

DB_PATH = 'finance_data.db'

def init_db():
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

def clean_json_string(json_str):
    # 生データからJSONブロックのみ抽出
    start_idx = json_str.find('{')
    end_idx = json_str.rfind('}')
    if start_idx != -1 and end_idx != -1:
        json_str = json_str[start_idx:end_idx+1]
    
    # 🚨AIがやりがちな「最後の要素の後の不要なカンマ」を自動削除する（エラーの主原因）
    json_str = re.sub(r',\s*([\}\]])', r'\1', json_str)
    
    # 不完全な改行や制御文字の処理
    json_str = re.sub(r'[\x00-\x1F\x7F]', ' ', json_str)
    return json_str

def parse_financial_pdf_smart(tanshin_path, presentation_path=None):
    if not client:
        raise ValueError("サーバー側の設定エラー: GEMINI_API_KEY が設定されていません。")

    all_text = ""
    presentation_text = ""

    with pdfplumber.open(tanshin_path) as pdf:
        for i, page in enumerate(pdf.pages):
            if i >= 15:
                break
            text = page.extract_text()
            if text:
                all_text += f"--- 短信 Page {i+1} ---\n{text}\n\n"

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

    prompt = f"""
    あなたはトップティア証券会社のシニア・エクイティアナリストです。提供された「決算短信」と「決算説明資料(ある場合)」から財務数値を抽出し、指定のJSONフォーマットで出力してください。

    【抽出ルール（絶対厳守）】
    - 🚨指定のJSONフォーマットのみを返すこと。JSONの最後の要素には絶対にカンマ(,)をつけないでください。
    - JSONのキーや値の中でダブルクォーテーション(")を使う場合は、必ずエスケープ(\\")してください。
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

    【AIによる要約（ai_analysis）の極意：プロフェッショナル・インサイト】
    単なる事実の羅列（「売上が〇〇増えた」等）は一切禁止します。投資家が真に求める「付加価値」を提供するため、以下の思考フレームワークを駆使してテキストを生成してください。

    ＜高度な思考フレームワーク＞
    1. 【業績の因数分解】YoY(前年比)だけでなくQoQ(前四半期比)のモメンタムを評価。なぜ儲かったのか（単価(ASP)上昇か、数量増か、為替か、コスト減か）を構造的に分解する。
    2. 【収益性の持続性とサイクル】足元の高収益（または赤字）は一時的か、構造的か。特に市況産業（半導体メモリ等）の場合は「今サイクルのどこにいるのか」を推測・評価する。
    3. 【財務・CFの実態】現金の増加や借入の減少が「財務リスクの低下」「成長投資への余力」「株主還元余力」にどう直結しているかを見極める。
    4. 【設備投資(CAPEX)の二面性】成長への布石としての評価と同時に、将来の供給過剰や減価償却費増によるダウンサイドリスクも必ず指摘する。
    5. 【株主還元とカタリスト】自社株買い、増配、株式分割など、直接的な株価押し上げ要因を評価する。
    6. 【会社予想vs客観的評価】会社側の強気（弱気）な主張を鵜呑みにせず、「前提条件（需要減など）が崩れた場合のリスク」を必ずアナリスト目線で提示する。

    【出力フォーマット指定】
    - 読みやすくするため、重要なキーワードや数値は必ずMarkdownの太字（**テキスト**）を使用してください。
    - 文頭には必ず「🟢ポジティブ：」「🔴ネガティブ：」「🟡要注目(リスク)：」のラベルをつけてください。

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
            "tab1_summary": "当期の業績・財務・キャッシュフローについて、上記の＜思考フレームワーク＞を駆使し、プロのアナリストとしての鋭い洞察を箇条書きで出力してください。「なぜ儲かったのかの分解」「収益の持続性」「財務の実態」を中心に記述してください。【600〜800文字程度】",
            "tab2_summary": "来期（次四半期）のガイダンス、設備投資、株主還元、および中長期の事業環境サイクルについて、今後の株価カタリストやダウンサイドリスクを含めた深い洞察を箇条書きで出力してください。会社発表に対する客観的なリスク評価を必ず含めてください。【600〜800文字程度】"
        }}
    }}
    """

    response = client.models.generate_content(
        model='gemini-3.6-flash',
        contents=prompt,
        config=genai.types.GenerateContentConfig(
            response_mime_type="application/json"
        )
    )
    
    raw_text = response.text.strip()
    cleaned_json_str = clean_json_string(raw_text)

    return json.loads(cleaned_json_str)

@app.route('/')
def index():
    return send_file('index.html')

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
        
        if "503" in error_msg or "high demand" in error_msg.lower() or "unavailable" in error_msg.lower():
            error_msg = "現在、AIサーバー（Google Gemini）が大変混み合っており一時的に利用できません。数分ほど時間を置いてから、再度「解析スタート」をお試しください。"
            
        return jsonify({'success': False, 'error': error_msg})

@app.route('/search_companies', methods=['GET'])
def search_companies():
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

@app.route('/get_company_data/<int:id>', methods=['GET'])
def get_company_data(id):
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
