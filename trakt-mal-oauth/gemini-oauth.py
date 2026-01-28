import secrets
import requests
from flask import Flask, request, redirect, session, jsonify

app = Flask(__name__)
# Used to sign the session cookie
app.secret_key = secrets.token_hex(32)

CLIENT_ID = '82074cc27b34ad4e921b4a65753f9777'
CLIENT_SECRET = 'a9b39fcaae26becf4b0486d78e54b855c60b8de0fa6cc9ba47168e4e7c4ac928'
REDIRECT_URI = 'http://127.0.0.1:5000/callback'

def generate_new_code_verifier():
    return secrets.token_urlsafe(100)[:128]

@app.route('/')
def index():
    return '<a href="/login">Login with MyAnimeList</a>'

@app.route('/login')
def login():
    # 1. Generate PKCE Verifier
    code_verifier = generate_new_code_verifier()
    # 2. Generate State (CSRF protection)
    state = secrets.token_urlsafe(16)
    
    # Store both in the session to verify later
    session['code_verifier'] = code_verifier
    session['oauth_state'] = state
    
    url = "https://myanimelist.net/v1/oauth2/authorize"
    params = {
        'response_type': 'code',
        'client_id': CLIENT_ID,
        'state': state,
        'code_challenge': code_verifier,
        'redirect_uri': REDIRECT_URI,
    }
    
    auth_url = requests.Request('GET', url, params=params).prepare().url
    return redirect(auth_url)

@app.route('/callback')
def callback():
    # 3. Verify State
    returned_state = request.args.get('state')
    saved_state = session.pop('oauth_state', None)
    
    if not returned_state or returned_state != saved_state:
        return "State mismatch error! This request may have been tampered with.", 400

    # 4. Get the authorization code
    auth_code = request.args.get('code')
    code_verifier = session.pop('code_verifier', None)
    
    if not auth_code:
        return "Authorization failed.", 400

    # 5. Exchange for Token
    token_url = 'https://myanimelist.net/v1/oauth2/token'
    data = {
        'client_id': CLIENT_ID,
        'client_secret': CLIENT_SECRET,
        'code': auth_code,
        'code_verifier': code_verifier,
        'grant_type': 'authorization_code',
        'redirect_uri': REDIRECT_URI
    }
    
    response = requests.post(token_url, data=data)
    
    if response.status_code == 200:
        return jsonify(response.json())
    else:
        return f"Token exchange failed: {response.text}", 400

if __name__ == '__main__':
    app.run(debug=True, port=5000)