import os
import json
import time
import requests
import urllib.parse
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
# Permanent session key
app.secret_key = os.getenv("SECRET_KEY", "Bankey_0PAY_Secure_Permanent_Key_100")

# ENV Variables
API_KEY = os.getenv("FIREBASE_API_KEY")
DB_URL = os.getenv("FIREBASE_DATABASE_URL")
UPI_ID = os.getenv("UPI_ID")
FC_COOKIE = os.getenv("FC_COOKIE")

# --- FIREBASE REST API FUNCTIONS ---
def firebase_login(email, password):
    url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={API_KEY}"
    res = requests.post(url, json={"email": email, "password": password, "returnSecureToken": True})
    return res.json()

def firebase_signup(email, password):
    url = f"https://identitytoolkit.googleapis.com/v1/accounts:signUp?key={API_KEY}"
    res = requests.post(url, json={"email": email, "password": password, "returnSecureToken": True})
    return res.json()

def db_get(path):
    res = requests.get(f"{DB_URL}/{path}.json")
    return res.json() if res.ok else None

def db_put(path, data):
    requests.put(f"{DB_URL}/{path}.json", json=data)

def db_patch(path, data):
    requests.patch(f"{DB_URL}/{path}.json", json=data)

# --- ROUTES ---
@app.route('/')
def home():
    if 'uid' in session: 
        return redirect('/dashboard')
    return render_template('auth.html')

@app.route('/api/auth', methods=['POST'])
def auth():
    data = request.json
    action = data.get('action')
    email = data.get('email')
    password = data.get('password')
    
    if action == 'login':
        res = firebase_login(email, password)
        if 'localId' in res:
            uid = res['localId']
            if not db_get(f"users/{uid}"):
                db_put(f"users/{uid}", {"email": email, "balance": 0, "pending": 0, "success": 0, "reject": 0})
    else:
        res = firebase_signup(email, password)
        if 'localId' in res:
            db_put(f"users/{res['localId']}", {"email": email, "balance": 0, "pending": 0, "success": 0, "reject": 0})
            
    if 'idToken' in res:
        session['uid'] = res['localId']
        session.modified = True 
        return jsonify({"status": "success"})
        
    return jsonify({"status": "error", "message": res.get("error", {}).get("message", "Auth Failed")})

@app.route('/dashboard')
def dashboard():
    if 'uid' not in session: return redirect('/')
    return render_template('dashboard.html')

@app.route('/api/user_data', methods=['GET'])
def user_data():
    if 'uid' not in session: return jsonify({"error": "Unauthorized"}), 401
    uid = session['uid']
    user_info = db_get(f"users/{uid}")
    
    if not user_info:
        session.pop('uid', None)
        return jsonify({"error": "User deleted"}), 401
        
    apis = db_get(f"apis") or {}
    my_apis = {k: v for k, v in apis.items() if v.get('uid') == uid}
    return jsonify({"user": user_info, "apis": my_apis})

@app.route('/api/create_api', methods=['POST'])
def create_api():
    if 'uid' not in session: return jsonify({"error": "Unauthorized"}), 401
    api_name = request.json.get('name')
    uid = session['uid']
    
    base_url = request.host_url.rstrip('/') 
    api_id = f"0PAY{int(time.time())}"
    
    db_put(f"apis/{api_id}", {"name": api_name, "uid": uid, "domain": base_url, "created": int(time.time())})
    return jsonify({"status": "success", "api_id": api_id})

@app.route('/api/request_payout', methods=['POST'])
def request_payout():
    if 'uid' not in session: return jsonify({"error": "Unauthorized"}), 401
    uid = session['uid']
    upi = request.json.get('upi')
    amount = float(request.json.get('amount'))
    
    user_data = db_get(f"users/{uid}")
    if user_data['balance'] < amount or amount <= 0:
        return jsonify({"status": "error", "message": "Insufficient Balance"})
    
    db_patch(f"users/{uid}", {
        "balance": user_data['balance'] - amount,
        "pending": user_data['pending'] + amount
    })
    
    req_id = f"REQ{int(time.time())}"
    db_put(f"payout_requests/{req_id}", {"uid": uid, "email": user_data.get('email','Unknown'), "upi": upi, "amount": amount, "status": "Pending", "time": int(time.time())})
    
    return jsonify({"status": "success", "message": "Payout requested successfully"})


# ==========================================
# 🛑 CORE PAYMENT SYSTEM (ENTRY -> REDIRECT -> LOCK)
# ==========================================

@app.route('/pay/<api_id>/<amount>')
def init_payment(api_id, amount):
    api_data = db_get(f"apis/{api_id}")
    if not api_data: return "Error: Invalid API ID", 404
    
    txn_id = f"TXN{int(time.time())}"
    expiry_time = int(time.time()) + 300 
    
    db_put(f"pending_txns/{txn_id}", {
        "api_id": api_id,
        "amount": float(amount),
        "expiry": expiry_time,
        "status": "pending"
    })
    
    return redirect(f"/checkout/{txn_id}")

@app.route('/checkout/<txn_id>')
def checkout_page(txn_id):
    txn_data = db_get(f"pending_txns/{txn_id}")
    
    if not txn_data:
        return "Error: Transaction not found or invalid.", 404
        
    if txn_data.get('status') == 'success':
        return render_template('pay.html', expired=False, success=True)
        
    current_time = int(time.time())
    expiry_time = txn_data['expiry']
    
    if current_time >= expiry_time:
        return render_template('pay.html', expired=True)
        
    time_left = expiry_time - current_time
    amount = txn_data['amount']
    
    upi_string = f"upi://pay?pa={UPI_ID}&pn=0PAY Merchant&am={amount}&tr={txn_id}&cu=INR"
    encoded_upi = urllib.parse.quote(upi_string)
    qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=250x250&data={encoded_upi}"
    
    return render_template('pay.html', amount=amount, txn_id=txn_id, qr_url=qr_url, upi=UPI_ID, time_left=time_left, expired=False, success=False)


# ==========================================
# 🛑 STRICT REAL UTR VERIFICATION (FIXED)
# ==========================================
@app.route('/api/verify_utr', methods=['POST'])
def verify_utr():
    data = request.json
    utr = str(data.get('utr')).strip()
    txn_id = data.get('txn_id')
    
    if not utr.isdigit() or len(utr) != 12:
        return jsonify({"status": "error", "message": "Invalid UTR format. Must be exact 12 digits."})
        
    txn_data = db_get(f"pending_txns/{txn_id}")
    if not txn_data or txn_data['status'] == 'success':
        return jsonify({"status": "error", "message": "Invalid or already completed transaction."})
        
    if db_get(f"used_utrs/{utr}"):
        return jsonify({"status": "error", "message": "UTR already used! Status: REJECTED"})

    amount = float(txn_data['amount'])
    
    # --- REAL COOKIE CHECKING LOGIC WITH ANTI-BOT BYPASS ---
    is_valid = False
    try:
        cookie_str = FC_COOKIE if "app_fc=" in FC_COOKIE else f"app_fc={FC_COOKIE}"
        
        # Ye headers Freecharge ko lagne denge ki asli Insaan check kar raha hai, koi Python script nahi.
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Cookie": cookie_str,
            "Origin": "https://merchant.freecharge.in",
            "Referer": "https://merchant.freecharge.in/"
        }
        
        fc_api_url = f"https://merchant.freecharge.in/api/v1/transaction?utr={utr}"
        
        # 10 second timeout taaki request fail na ho jaldi
        fc_res = requests.get(fc_api_url, headers=headers, timeout=10)
        
        if fc_res.ok:
            fc_data = fc_res.json()
            
            # Logic to handle if Freecharge returns a single transaction dictionary
            if isinstance(fc_data, dict):
                if str(fc_data.get('status', '')).upper() == 'SUCCESS' and float(fc_data.get('amount', 0)) == amount:
                    is_valid = True
            
            # Logic to handle if Freecharge returns a list of transactions
            elif isinstance(fc_data, list):
                for txn in fc_data:
                    if str(txn.get('utr', '')) == utr and str(txn.get('status', '')).upper() == 'SUCCESS' and float(txn.get('amount', 0)) == amount:
                        is_valid = True
                        break
    except Exception as e:
        print(f"FC verification error: {e}")
        pass

    if is_valid:
        api_id = txn_data['api_id']
        api_data = db_get(f"apis/{api_id}")
        uid = api_data['uid']
        
        db_put(f"used_utrs/{utr}", {"amount": amount, "txn_id": txn_id, "time": int(time.time())})
        db_patch(f"pending_txns/{txn_id}", {"status": "success"})
        
        merchant_cut = round(amount * 0.97, 2)
        user_data = db_get(f"users/{uid}")
        db_patch(f"users/{uid}", {
            "balance": user_data['balance'] + merchant_cut,
            "success": user_data['success'] + merchant_cut
        })
        return jsonify({"status": "success", "message": "Payment Verified!"})
    
    return jsonify({"status": "error", "message": "Payment not received yet. Check UTR and try again."})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
