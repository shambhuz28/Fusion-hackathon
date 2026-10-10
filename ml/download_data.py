"""Download and extract the ESA Kelvins Collision Avoidance Challenge dataset.

Source: https://zenodo.org/records/4463683 (CDMs received by ESA 2015-2019, anonymised).
The zip is fetched in parallel byte ranges (Zenodo is slow on a single stream) and the
competition files are extracted to ml/data/raw/:
    train_data.csv          all CDMs of the 13,154 training events
    test_data.csv           CDMs of the 2,167 test events issued >= 2 days before TCA
    test_data_private.csv   the last CDM of every test event (true final risk = label)

Usage:
    python ml/download_data.py
"""

import io
import shutil
import sys
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

URL = ("https://zenodo.org/api/records/4463683/files/"
       "Collision%20Avoidance%20Challenge%20-%20Dataset.zip/content")
RAW_DIR = Path(__file__).resolve().parent / "data" / "raw"
ZIP_PATH = RAW_DIR / "dataset.zip"
COMPETITION_DIR = "Collision Avoidance Challenge - Dataset/kelvins_competition_data/"
WANTED = ("train_data.csv", "test_data.csv", "test_data_private.csv")
N_PARTS = 10


def remote_size():
    req = urllib.request.Request(URL, method="HEAD")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return int(resp.headers["Content-Length"])


def fetch_range(start, end, part_path):
    if part_path.exists() and part_path.stat().st_size == end - start + 1:
        return
    req = urllib.request.Request(URL, headers={"Range": f"bytes={start}-{end}"})
    with urllib.request.urlopen(req, timeout=600) as resp, open(part_path, "wb") as out:
        shutil.copyfileobj(resp, out, 1 << 20)
    if part_path.stat().st_size != end - start + 1:
        raise RuntimeError(f"{part_path.name}: incomplete download")


def download_zip():
    size = remote_size()
    if ZIP_PATH.exists() and ZIP_PATH.stat().st_size == size:
        print(f"Using existing {ZIP_PATH}")
        return
    parts_dir = RAW_DIR / "parts"
    parts_dir.mkdir(parents=True, exist_ok=True)
    chunk = -(-size // N_PARTS)
    ranges = [(i * chunk, min(size, (i + 1) * chunk) - 1, parts_dir / f"p{i}") for i in range(N_PARTS)]
    print(f"Downloading {size / 1e6:.0f} MB in {N_PARTS} parallel parts ...")
    with ThreadPoolExecutor(N_PARTS) as pool:
        list(pool.map(lambda r: fetch_range(*r), ranges))
    with open(ZIP_PATH, "wb") as out:
        for _, _, part in ranges:
            with open(part, "rb") as f:
                shutil.copyfileobj(f, out, 1 << 20)
    shutil.rmtree(parts_dir)


def extract():
    with zipfile.ZipFile(ZIP_PATH) as zf:
        for name in ("test_data.csv", "test_data_private.csv"):
            with zf.open(COMPETITION_DIR + name) as src, open(RAW_DIR / name, "wb") as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
        # The training CDMs sit in a zip inside the zip.
        with zf.open(COMPETITION_DIR + "train_data.zip") as inner_file:
            inner_bytes = io.BytesIO(inner_file.read())
        with zipfile.ZipFile(inner_bytes) as inner, inner.open("train_data.csv") as src, \
                open(RAW_DIR / "train_data.csv", "wb") as dst:
            shutil.copyfileobj(src, dst, 1 << 20)
    for name in WANTED:
        print(f"Extracted {RAW_DIR / name}")


def main():
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    if all((RAW_DIR / name).exists() for name in WANTED):
        print(f"Dataset already present in {RAW_DIR}")
        return 0
    download_zip()
    extract()
    missing = [name for name in WANTED if not (RAW_DIR / name).exists()]
    if missing:
        print(f"Zip did not contain {missing}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
