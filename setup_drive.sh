#!/bin/bash
set -e
cd "$(dirname "$0")"

echo "=== Step 1: Google Drive OAuth ==="
.venv/bin/python -c "
import pickle
from pathlib import Path
from google_auth_oauthlib.flow import InstalledAppFlow

flow = InstalledAppFlow.from_client_secrets_file('credentials.json', ['https://www.googleapis.com/auth/drive.readonly'])
flow.redirect_uri = 'urn:ietf:wg:oauth:2.0:oob'
auth_url, _ = flow.authorization_url(prompt='consent', access_type='offline')
print('\nOpen this URL in your browser:\n')
print(auth_url)
code = input('\nPaste the authorization code here: ').strip()
flow.fetch_token(code=code)
Path('data').mkdir(exist_ok=True)
open('data/token.pickle', 'wb').write(__import__('pickle').dumps(flow.credentials))
print('Token saved!')
"

echo ""
echo "=== Step 2: Downloading PDFs ==="
.venv/bin/python download_slides.py

echo ""
echo "=== Step 3: Ingesting idioms ==="
.venv/bin/python -m src.main ingest pdfs/*.pdf

echo ""
echo "=== Step 4: Generating funny examples ==="
.venv/bin/python -m src.main fill-examples

echo ""
echo "=== Done! Run: .venv/bin/python -m src.main stats ==="
