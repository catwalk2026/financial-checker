import os
import json
import sqlite3
import pdfplumber
import re
import threading
import uuid
from google import genai
from flask import Flask, request, jsonify, send_file

# 🚀 スクレイピング用ライブラリ
try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None
    print("WARNING: requests または beautifulsoup4 がインストールされていません。", flush=True)

app = Flask(__name__)
UPLOAD_FOLDER = './uploads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

API_KEY = os.environ.get("GEMINI_API_KEY")

if not API_KEY:
    print("WARNING: GEMINI_API_KEY が設定されていません。", flush=True)
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

# 🚀 【最強版】IFISからどんな表構造でも数字を引っこ抜く関数
def fetch_japan_consensus(code):
    sales, op_profit = "", ""
    if not BeautifulSoup:
        print("❌ BeautifulSoupがないため自動取得をスキップします。", flush=True)
        return sales, op_profit
        
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    
    print(f"🌐 IFIS(株予報)から {code} のコンセンサスを取得します...", flush=True)
    try:
        url_ifis = f"https://kabuyoho.ifis.co.jp/index.php?action=tp1&sa=report_con&bcode={code}"
        res = requests.get(url_ifis, headers=headers, timeout=5)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, 'html.parser')
            for tr in soup.find_all('tr'):
                # 行の中にあるすべてのセル（td, th）を取得
                cells = tr.find_all(['td', 'th'])
                if not cells: continue
                
                # 1番目のセル（ラベル）を確認
                label = cells[0].get_text(strip=True)
                if "売上" in label or "営業利" in label or "営業益" in label:
                    nums = []
                    # ラベル以外のセルから数値をしらみつぶしに抽出
                    for cell in cells[1:]:
                        txt = cell.get_text(strip=True).replace(',', '')
                        match = re.search(r'[-]?\d+', txt)
                        if match:
                            nums.append(match.group(0))
                            
                    if nums:
                        # IFISの表は通常 [会社予想, コンセンサス] の順なので、2番目(インデックス1)を採用
                        val = nums[1] if len(nums) >= 2 else nums[0]
                        
                        if "売上" in label and not sales:
                            sales = val
                        elif ("営業利" in label or "営業益" in label) and not op_profit:
                            op_profit = val
                            
            print(f"✅ IFIS抽出完了: 売上={sales}, 営利={op_profit}", flush=True)
        else:
            print(f"⚠️ IFISアクセス拒否 (ステータスコード: {res.status_code})", flush=True)
    except Exception as e:
        print(f"⚠️ IFIS通信エラー: {e}", flush=True)

    return sales, op_profit

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

    # コンセンサスの自動取得
    if not consensus_sales or not consensus_op_profit:
        match = re.search(r'(?:証券コード|コード番号|銘柄コード|コード)[^\d]*([0-9０-９]{4})', all_text[:3000])
        if match:
            code = match.group(1).translate(str.maketrans('０１２３４５６７８９', '0123456789'))
            print(f"🔍 証券コード {code} を検出。コンセンサスを自動取得します...", flush=True)
            auto_sales, auto_op_profit = fetch_japan_consensus(code)
            if not consensus_sales: consensus_sales = auto_sales
            if not consensus_op_profit: consensus_op_profit = auto_op_profit
        else:
            print("⚠️ 証券コードがPDFから検出できませんでした。", flush=True)

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
            print(f"決算説明資料の読み込みエラー: {e}", flush=True)

    prompt = f"""
    あなたはトップティア証券会社のシニア・エクイティアナリストです。提供された「決算短信」から財務数値を抽出し、指定のJSONフォーマットで出力してください。

    【抽出ルール（絶対厳守）】
    - 🚨指定のJSONフォーマットのみを返すこと。JSONの最後の要素には絶対にカンマ(,)をつけないでください。
    - JSONのキーや値の中でダブルクォーテーション(")を使う場合は、エスケープしてください。
    - 単位はすべて「百万円」に換算・統一してください（例: テキストが「円」や「十億円」なら百万円に変換）。
    - マイナス値はマイナスの数値（例: -100）としてください。

    【超重要：B/S（貸借対照表）の負債・純資産の抽出について】
    - 🚨負債の項目を理由なく0にしないでください。
    - 🚨純資産は、必ずB/S表の最後にある「純資産合計」（IFRSの場合は資本合計）の数値を抽出してください。「自己資本」ではありません。

    【市場コンセンサス（参考データ）】
    - 売上高コンセンサス: {consensus_sales} 百万円
    - 営業利益コンセンサス: {consensus_op_profit} 百万円
    ※コンセンサスの数値が存在する場合、会社側の来期予想(forecast_data)と比較し、市場の期待を上回っているか（ポジティブサプライズ）、下回っているか（ネガティブ）を必ず分析に含めてください。

    【AIによる要約（ai_analysis）の極意】
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

    print("🤖 Gemini APIへのリクエストを開始します...", flush=True)
    response = client.models.generate_content(
        model='gemini-3.6-flash',
        contents=prompt,
        config=genai.types.GenerateContentConfig(
            response_mime_type="application/json"
        )
    )
    print("✅ Gemini APIからのレスポンスを受信しました！", flush=True)
    
    raw_text = response.text.strip()
    cleaned_json_str = clean_json_string(raw_text)
    data = json.loads(cleaned_json_str)

    try:
        if consensus_sales: data['consensus_data']['sales'] = int(consensus_sales)
    except ValueError:
        pass
    try:
        if consensus_op_profit: data['consensus_data']['op_profit'] = int(consensus_op_profit)
    except ValueError:
        pass

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
        print(f"解析エラー: {error_msg}", flush=True)
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

if __name__ == '__main__':
    app.run(debug=True, port=5000, threaded=True)
