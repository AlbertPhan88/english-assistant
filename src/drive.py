"""Google Drive sync — downloads new PDFs from a Drive folder into pdfs/."""
import os
import pickle
from pathlib import Path

from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
CREDS_FILE = "credentials.json"
TOKEN_FILE = "data/token.pickle"


def _get_service():
    creds = None
    if Path(TOKEN_FILE).exists():
        with open(TOKEN_FILE, "rb") as f:
            creds = pickle.load(f)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDS_FILE, SCOPES)
            # Console-friendly: prints URL, user pastes code
            creds = flow.run_console()
        Path(TOKEN_FILE).parent.mkdir(parents=True, exist_ok=True)
        with open(TOKEN_FILE, "wb") as f:
            pickle.dump(creds, f)

    return build("drive", "v3", credentials=creds)


def list_pdfs(folder_id: str) -> list[dict]:
    service = _get_service()
    query = f"'{folder_id}' in parents and mimeType='application/pdf' and trashed=false"
    results = service.files().list(q=query, fields="files(id, name, modifiedTime)").execute()
    return results.get("files", [])


def download_pdf(file_id: str, name: str, dest_dir: str = "pdfs") -> Path:
    service = _get_service()
    Path(dest_dir).mkdir(parents=True, exist_ok=True)
    dest = Path(dest_dir) / name
    request = service.files().get_media(fileId=file_id)
    with open(dest, "wb") as f:
        downloader = MediaIoBaseDownload(f, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
    return dest


def sync_folder(folder_id: str, db_path: str, dest_dir: str = "pdfs") -> int:
    """Download new PDFs and ingest them. Returns count of newly ingested PDFs."""
    from anthropic import Anthropic
    from . import config
    from .extractor import ingest_pdf

    client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    files = list_pdfs(folder_id)
    ingested = 0
    for f in files:
        dest = Path(dest_dir) / f["name"]
        if dest.exists():
            continue
        print(f"  Downloading {f['name']}...")
        download_pdf(f["id"], f["name"], dest_dir)
        added, total = ingest_pdf(dest, db_path, client)
        print(f"  {f['name']}: {added} new idioms from {total} found.")
        ingested += 1
    return ingested
