"""Download all session main + review PDFs from Google Drive, then ingest."""
import base64
import io
import os
import pickle
import sys
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
TOKEN_FILE = "data/token.pickle"
CREDS_FILE = "credentials.json"
PDF_DIR = Path("pdfs")

# All main + review PDFs collected from Drive (session: (main_id, main_name, review_id, review_name))
SESSIONS = [
    (5,  "1KAO4o4-bw7T1wy4YDVoMbnfrDzZceDAC", "Session 5.pdf",        "16Niz-bjBiXoS0C0Gp5THMUaKD1hqwFje", "Session 5 - Review.pdf"),
    (6,  "1093sUCe8mGOKORMVUFMN4bpf6yDBMucs", "Session 6.pdf",        "1dkyOw0dJUMeKHOBV41jWBULb46o7BT8w", "Session 6 - Review.pdf"),
    (7,  "1d1lS2EHsMb96pc8RrC1OqwFJPXnAvfg7", "Session 7.pdf",        "1mkVq0WC0D2ZdXRUgvolUUj6FsUExs48M", "Session 7 - Review.pdf"),
    (8,  "11oP_-0KeuvEtG1ZVQy8FWtyRyj4y0u06", "Session 8.pdf",        "12WLmJ8xthEo1D4SVHomVmKoD-JxggPtt", "Session 8 - Review.pdf"),
    (9,  "1B71uy9Tc3jhu_Zpflk3DTqa9q2ntsdK-", "Session 9.pdf",        "1QbrLjezQ4qQezVBhivqge3c9pefJrX_J", "Session 9 - Review.pdf"),
    (10, "1_B7DpTNOfl_z46PseViwj49k23pAH7FO", "Session 10.pdf",       "1ztI7EcWxFPcYGTwVlc87UT-M_ixlOylz", "Session 10 - Review.pdf"),
    (11, "1In6EwnHl2OvoMjdHpPond0pb9ym342Dc", "Session 11.pdf",       "1_kbkB5SlnPC8kn60IfeFMLAE0C3cgrOE", "Session 11 - Review.pdf"),
    (12, "1Lq6KBHDYD--Liu3w3SC5PmhBGlpBMZPW", "Session 12.pdf",       "1SClLim6T_HBQxJ_EgFPs5Hw4kZZWCt1w", "Session 12 - Review.pdf"),
    (13, "1ejJJeUQ26EyCgu2-3GCDynCNC92DJ6tI", "Session 13.pdf",       "11Gy27jTpXIESS7d1MtLNYHr0oFIclR9e", "Session 13 - Review.pdf"),
    (14, "1dsdhExYoOe7WLL2tyYxYnnx5sB6I_kLR", "Session 14.pdf",       "1EZKG49eTZqUF88aG_SE0A42uZMM-TPOy", "Session 14 - Review.pdf"),
    (15, "1h5ELFplRS98CDiJyXuNlXr864MtRrPpu", "Session 15.pdf",       "1BlidQ2LJun58IAZieUyhmdgAodv-SZU1", "Session 15 - Review.pdf"),
    (16, "17PobAc0ydkvbaRp8mEFo_R-o4AtXxSee", "Session 16.pdf",       "11nAf-cUqcNUovC7uK0NcEXIAJz8R7Q0a", "Session 16 - Review.pdf"),
    (17, "1cbbf6mrRdIXXghMR4VzcAl3XRDgBFwdG", "Session 17.pdf",       "1e6zpOFLFeHWlEDzzZMfcmL1IWe0g8L2H", "Session 17 - Review.pdf"),
    (18, "16uUNjssuNZQRb9cLOBV4uJRSgj4aW-hq", "Session 18.pdf",       "1x5pbwoeYzVaYJLjyrU385oesOoyMJ8v5", "Session 18 - Review.pdf"),
    (19, "162Mja8DqTlykN0ixo_WQPzyQrDuxVvO3", "Session 19.pdf",       "1tULkV1EipC7-7F1Ou_fJyt6TtKwHZ0zz", "Session 19 - Review.pdf"),
    (20, "1ri1cs_e7fIFyKIkJWQYYC_GshnZGoqYo", "Session 20.pdf",       "1gz7PKnywq7hU2bqeJOvA03MmL70X9BQw", "Session 20 - Review.pdf"),
    (21, "1JiKezK4WWQsyTpaD9vPQYmtlgILViYa4", "Session 21.pdf",       "1Th7kNLjp7EoNz7jC-BDF_--e7xv-VUca", "Session 21 - Review.pdf"),
    (22, "1igu1wCWtqgrslcca9feVdQCnFjDjWJ3g", "Session 22.pdf",       "1GlvXIyWoT9AlFz2qPUm7ie6Cvc35XLTi", "Session 22 - Review.pdf"),
    (23, "1ZT9_-qvjikE-142sz-XPrF451WApJ-_p", "Session 23.pdf",       "1TmmJzVdFxq8pEhA3GTvWJvsBS7j46SHY", "Session 23 - Review.pdf"),
    (24, "1e2rGLLrJM8-x9U-NhULUwu5jWa7wutKP", "Session 24.pdf",       "1pVx9mp203AMxOr_beB4Zu87JF6XhUZrr", "Session 24 - Review.pdf"),
]


def get_creds():
    creds = None
    if Path(TOKEN_FILE).exists():
        with open(TOKEN_FILE, "rb") as f:
            creds = pickle.load(f)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(TOKEN_FILE, "wb") as f:
            pickle.dump(creds, f)
    return creds


def download_file(service, file_id: str, dest: Path) -> None:
    request = service.files().get_media(fileId=file_id)
    with open(dest, "wb") as f:
        downloader = MediaIoBaseDownload(f, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()


def main():
    creds = get_creds()
    if not creds or not creds.valid:
        print("No valid token found. Run the OAuth flow first.")
        sys.exit(1)

    service = build("drive", "v3", credentials=creds)

    # Show which account is authenticated
    about = service.about().get(fields="user").execute()
    print(f"Authenticated as: {about['user']['emailAddress']}\n")

    PDF_DIR.mkdir(exist_ok=True)

    to_download = []
    for s, main_id, main_name, rev_id, rev_name in SESSIONS:
        for fid, fname in [(main_id, main_name), (rev_id, rev_name)]:
            dest = PDF_DIR / fname
            if dest.exists():
                print(f"  skip (exists): {fname}")
            else:
                to_download.append((fid, fname, dest))

    print(f"\nDownloading {len(to_download)} PDFs...\n")
    for fid, fname, dest in to_download:
        print(f"  Downloading {fname}...", end=" ", flush=True)
        download_file(service, fid, dest)
        size_kb = dest.stat().st_size // 1024
        print(f"{size_kb}KB")

    print(f"\nDone. {len(to_download)} files saved to {PDF_DIR}/")


if __name__ == "__main__":
    main()
