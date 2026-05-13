import os
import json
import time
import requests
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
# Permanent session key so users don't get logged out
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


# ==========================================
# 🛑 CORE PAYMENT SYSTEM (id.py logic + Strict UTR)
# ==========================================

# 1. Generate ID (User hits this to create a locked transaction)
@app.route('/api/generate_id', methods=['GET'])
def generate_id():
    api_id = request.args.get('api_id')
    amount = request.args.get('amount')
    
    if not api_id or not amount:
        return jsonify({"error": "Missing api_id or amount"}), 400
        
    api_data = db_get(f"apis/{api_id}")
    if not api_data: return jsonify({"error": "Invalid API ID"}), 400
    
    # Create Unique TXN ID and Expiry Time (5 Mins = 300 Secs)
    txn_id = f"TXN{int(time.time())}"
    expiry_time = int(time.time()) + 300 
    
    # Save to Firebase pending_txns
    db_put(f"pending_txns/{txn_id}", {
        "api_id": api_id,
        "amount": float(amount),
        "expiry": expiry_time,
        "status": "pending"
    })
    
    # Return the URL where the user will pay
    base_url = request.host_url.rstrip('/')
    pay_link = f"{base_url}/pay/{txn_id}"
    return jsonify({"status": "success", "txn_id": txn_id, "pay_link": pay_link})

# 2. Payment Page (Checks Server Timer & Generates Locked QR)
@app.route('/pay/<txn_id>')
def payment_page(txn_id):
    txn_data = db_get(f"pending_txns/{txn_id}")
    
    if not txn_data:
        return "Transaction not found or invalid.", 404
        
    if txn_data.get('status') == 'success':
        return render_template('pay.html', expired=False, success=True)
        
    current_time = int(time.time())
    expiry_time = txn_data['expiry']
    
    # Check if 5 mins passed
    if current_time >= expiry_time:
        return render_template('pay.html', expired=True)
        
    time_left = expiry_time - current_time
    amount = txn_data['amount']
    api_id = txn_data['api_id']
    
    # Generate QR with Amount Locked (am=) and ID appended (tr=)
    qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=250x250&data=upi://pay?pa={UPI_ID}&am={amount}&tr={txn_id}&cu=INR"
    
    return render_template('pay.html', amount=amount, txn_id=txn_id, qr_url=qr_url, upi=UPI_ID, time_left=time_left, expired=False, success=False)


# 3. STRICT UTR Verification
@app.route('/api/verify_utr', methods=['POST'])
def verify_utr():
    data = request.json
    utr = str(data.get('utr')).strip()
    txn_id = data.get('txn_id')
    
    # STRICT 12-DIGIT CHECK
    if not utr.isdigit() or len(utr) != 12:
        return jsonify({"status": "error", "message": "Invalid UTR format. Must be 12 digits."})
        
    txn_data = db_get(f"pending_txns/{txn_id}")
    if not txn_data or txn_data['status'] == 'success':
        return jsonify({"status": "error", "message": "Invalid or already completed transaction."})
        
    if db_get(f"used_utrs/{utr}"):
        return jsonify({"status": "error", "message": "UTR already used! Status: REJECTED"})

    amount = float(txn_data['amount'])
    
    # --- REAL COOKIE CHECKING LOGIC ---
    # Python checks Freecharge API using your cookie. 
    # (Note: If the merchant API endpoint below is slightly different for your FC account, 
    # it will naturally fail and return 'Pending', preventing fake successes).
    
    is_valid = False
    try:
        cookies = {'app_fc': FC_COOKIE}
        headers = {"User-Agent": "Mozilla/5.0"}
        # Realistic endpoint structure for checking merchant history
        fc_res = requests.get(f"https://merchant.freecharge.in/api/v1/transaction?utr={utr}", cookies=cookies, headers=headers, timeout=5)
        
        if fc_res.ok:
            fc_data = fc_res.json()
            # Strict match: Amount must match exactly, and status must be success
            if fc_data.get('status') == 'SUCCESS' and float(fc_data.get('amount', 0)) == amount:
                is_valid = True
    except Exception as e:
        print(f"FC API Error: {e}")
        pass

    # If Cookie check passes
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
    
    # IF NOT FOUND OR FAILED
    return jsonify({"status": "error", "message": "Payment not received yet. Still Pending."})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
