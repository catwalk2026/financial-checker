import os
import json
import sqlite3
import pdfplumber
import re
import threading
import uuid
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
        conn.execute('''
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                status TEXT,
                data_json TEXT,
                error_msg TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
init_db()

def clean_json_string(json_str):
    start_idx = json_str.find('{')
    end_idx = json_str.rfind('}')
    if start_idx != -1 and end_idx != -1:
        json_str = json_str[start_idx:end_idx+1]
    
    json_str = re.sub(r',\s*([\}\]])', r'\1', json_str)
    json_str = re.sub(r'[\x00-\x1F\x7F]', ' ', json_str)
    return json_str

# 🚀 コンセンサスの引数を追加
def parse_financial_pdf_smart(tanshin_path, presentation_path=None, consensus_sales="", consensus_op_profit=""):
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

    # 🚀 AIへの指示にコンセンサス比較のミッションを追加
    prompt = f"""
    あなたはトップティア証券会社のシニア・エクイティアナリストです。提供された「決算短信」から財務数値を抽出し、指定のJSONフォーマットで出力してください。

    【抽出ルール（絶対厳守）】
    - 🚨指定のJSONフォーマットのみを返すこと。JSONの最後の要素には絶対にカンマ(,)をつけないでください。
    - JSONのキーや値の中でダブルクォーテーション(")を使う場合は、必ずエスケープ(\\")してください。
    - 単位はすべて「百万円」に換算・統一してください（例: テキストが「円」や「十億円」なら百万円に変換）。
    - 損失や減少などのマイナス値はマイナスの数値（例: -100）としてください。

    【超重要：B/S（貸借対照表）の負債・純資産の抽出について】
    - 「流動資産」「固定資産(非流動資産)」「流動負債」「固定負債(非流動負債)」は、必ずそれぞれの「合計値」を抽出してください。
    - 🚨【絶対厳守】いかなる場合も、負債の項目を理由なく0にしないでください。
    - 🚨【絶対厳守】純資産（資本合計）は、必ずB/S表の最後にある「純資産合計」（IFRSの場合は資本合計）の数値を抽出してください。「自己資本」ではありません。

    【市場コンセンサス（参考データ）】
    - 売上高コンセンサス: {consensus_sales} 百万円
    - 営業利益コンセンサス: {consensus_op_profit} 百万円
    ※コンセンサスの数値が入力されている場合、会社側の来期予想(forecast_data)と比較し、市場の期待を上回っているか（ポジティブサプライズ）、下回っているか（ネガティブ）を必ず分析に含めてください。

    【AIによる要約（ai_analysis）の極意：プロフェッショナル・インサイト】
    単なる事実の羅列は一切禁止します。
    1. 【業績の因数分解】YoYだけでなくQoQのモメンタムを評価。
    2. 【収益性の持続性とサイクル】足元の高収益（または赤字）は一時的か、構造的か。
    3. 【財務・CFの実態】現金の増加や借入の減少が成長投資や株主還元にどう直結しているか。
    4. 【設備投資(CAPEX)の二面性】成長への布石と、将来の供給過剰リスクを指摘。
    5. 【会社予想vsコンセンサス】コンセンサスとの乖離幅を評価し、株価への影響（カタリストかリスクか）を鋭く考察する。

    【出力フォーマット指定】
    - 重要なキーワードや数値は必ずMarkdownの太字（**テキスト**）を使用してください。
    - 文頭には必ず「🟢ポジティブ：」「🔴ネガティブ：」「🟡要注目(リスク)：」のラベルをつけてください。

    【対象テキスト】
    {all_text}

    【対象資料テキスト】
    {presentation_text}

    【期待するJSONスキーマ】
    {{
        "company": "企業名",
        "code": "証券コード（数字のみ）",
        "fiscal_year": "対象期（例: 2024年3月期）",
        "is_financial": false,
        "labels": {{ "sales": "売上高", "op_profit": "営業利益", "ord_profit": "経常利益" }},
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
        "consensus_data": {{ "sales": 0, "op_profit": 0 }},
        "ai_analysis": {{
            "tab1_summary": "当期の業績・財務について、プロのアナリストとしての鋭い洞察を箇条書きで出力してください。【600〜800文字程度】",
            "tab2_summary": "来期の見通し・コンセンサス比較・リスクについて、今後の株価カタリストを含めた深い洞察を箇条書きで出力してください。【600〜800文字程度】"
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
    data = json.loads(cleaned_json_str)

    # ユーザー入力のコンセンサス数値をJSONに上書き（グラフ描画用）
    if consensus_sales.isdigit():
        data['consensus_data']['sales'] = int(consensus_sales)
    if consensus_op_profit.isdigit():
        data['consensus_data']['op_profit'] = int(consensus_op_profit)

    return data

def run_analysis_job(job_id, tanshin_path, presentation_path, consensus_sales, consensus_op_profit):
    try:
        data = parse_financial_pdf_smart(tanshin_path, presentation_path, consensus_sales, consensus_op_profit)
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute('''
                INSERT INTO reports (company_name, code, fiscal_year, data_json)
                VALUES (?, ?, ?, ?)
            ''', (data.get('company'), data.get('code'), data.get('fiscal_year'), json.dumps(data, ensure_ascii=False)))
            conn.execute("UPDATE jobs SET status = 'done', data_json = ? WHERE id = ?", (json.dumps(data, ensure_ascii=False), job_id))
    except Exception as e:
        error_msg = str(e)
        print(f"解析エラー: {error_msg}")
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("UPDATE jobs SET status = 'error', error_msg = ? WHERE id = ?", (error_msg, job_id))

@app.errorhandler(Exception)
def handle_exception(e):
    return jsonify({'success': False, 'error': f"サーバー内部でエラーが発生しました: {str(e)}"}), 500

@app.route('/')
def index():
    return send_file('index.html')

@app.route('/upload', methods=['POST'])
def upload_file():
    if 'tanshin' not in request.files:
        return jsonify({'success': False, 'error': '決算短信のファイルがありません'})
    
    tanshin_file = request.files['tanshin']
    presentation_file = request.files.get('presentation')
    
    # 🚀 フォームからコンセンサスの値を受け取る
    consensus_sales = request.form.get('consensus_sales', '').strip()
    consensus_op_profit = request.form.get('consensus_op_profit', '').strip()

    if tanshin_file.filename == '':
        return jsonify({'success': False, 'error': '決算短信のファイルが選択されていません'})

    tanshin_path = os.path.join(app.config['UPLOAD_FOLDER'], 'tanshin_' + tanshin_file.filename)
    tanshin_file.save(tanshin_path)

    presentation_path = None
    if presentation_file and presentation_file.filename != '':
        presentation_path = os.path.join(app.config['UPLOAD_FOLDER'], 'presentation_' + presentation_file.filename)
        presentation_file.save(presentation_path)

    job_id = str(uuid.uuid4())
    
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT INTO jobs (id, status) VALUES (?, ?)", (job_id, 'processing'))

    # 引数にコンセンサスを追加してスレッド起動
    thread = threading.Thread(target=run_analysis_job, args=(job_id, tanshin_path, presentation_path, consensus_sales, consensus_op_profit))
    thread.start()

    return jsonify({'success': True, 'job_id': job_id})

@app.route('/status/<job_id>', methods=['GET'])
def check_status(job_id):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT status, data_json, error_msg FROM jobs WHERE id = ?", (job_id,))
        row = cursor.fetchone()
        
    if not row:
        return jsonify({'success': False, 'error': 'ジョブが見つかりません'}), 404
        
    status, data_json, error_msg = row
    if status == 'done':
        return jsonify({'success': True, 'status': status, 'data': json.loads(data_json)})
    elif status == 'error':
        return jsonify({'success': True, 'status': status, 'error': error_msg})
    else:
        return jsonify({'success': True, 'status': status})

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
    app.run(debug=True, port=5000, threaded=True)
