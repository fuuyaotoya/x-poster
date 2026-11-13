#!/usr/bin/env python3
"""Post the approved, due rows of derivatives/x/queue.tsv to @ThemeMISELL via the X API.

X API has no scheduling, so this runs every 15 minutes (GitHub Actions x-post.yml) and posts what is due.
Gates, all enforced here (not by convention):
  - approved_by must be filled          (CORE 5: nothing goes public without a named approver)
  - body has no URL                     (voice.md: the link goes in reply 1)
  - weighted length <= 280
  - image required and passes the blank / flat-grey / large-margin check (thresholds from x-auth/x-check-lib.mjs)
  - not already posted, no posted row with the same opening line (double-post guard)
  - more than LATE_LIMIT_H late -> status=missed, needs a new post_at (stale timing is a new decision)
  - per-run write budget and monthly post cap (cost guard)
Default is a dry run; --live actually posts.

queue.tsv columns (tab separated, header row):
  id post_at body_file image reply_text type hook hypothesis_id approved_by status post_id reply_id note
  post_at: "YYYY-MM-DD HH:MM" in JST. body_file / image: paths relative to the repo root.
  reply_text: full text of reply 1 (usually the link with UTM). Empty = no reply.
"""
import argparse, csv, re, sys, unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
QUEUE = REPO / "derivatives/x/queue.tsv"
LABELS = REPO / "growth/x-posts.tsv"
JST = timezone(timedelta(hours=9))
LATE_LIMIT_H = 6
MAX_ROWS_PER_RUN = 2
MONTHLY_CAP = 40
COLUMNS = ["id", "post_at", "body_file", "image", "reply_text", "type", "hook", "hypothesis_id", "approved_by", "status", "post_id", "reply_id", "note"]
LABEL_COLUMNS = ["post_id", "posted_at", "queue_id", "type", "hook", "has_image", "hypothesis_id"]
URL_RE = re.compile(r"https?://|www\.", re.I)


def weighted_len(text: str) -> int:
    """X counts most CJK/full-width characters as 2 and Latin as 1 (twitter-text v3, approximated)."""
    return sum(1 if ord(c) < 0x1100 or unicodedata.east_asian_width(c) in ("Na", "H", "N") else 2 for c in text)


def image_list(row: dict) -> list[str]:
    """The image column may list up to 4 images separated by ';' (posted in that order)."""
    return [s.strip() for s in row["image"].split(";") if s.strip()]


def check_image(path: Path) -> list[str]:
    from PIL import Image  # installed in the workflow; locally: uv run --with pillow
    im = Image.open(path).convert("RGB")
    im.thumbnail((400, 400))
    px = list(im.get_flattened_data() if hasattr(im, "get_flattened_data") else im.getdata())
    n = len(px)
    lum = [0.299 * r + 0.587 * g + 0.114 * b for r, g, b in px]
    mean = sum(lum) / n
    sd = (max(0.0, sum(l * l for l in lum) / n - mean * mean)) ** 0.5
    sat = sum(max(p) - min(p) for p in px) / n
    bg, w, h = px[0], im.width, im.height
    xs, ys = [], []
    for i, p in enumerate(px):
        if abs(p[0] - bg[0]) + abs(p[1] - bg[1]) + abs(p[2] - bg[2]) > 30:
            xs.append(i % w); ys.append(i // w)
    content = ((max(xs) - min(xs) + 1) * (max(ys) - min(ys) + 1) / n) if xs else 0
    problems = []
    if sd < 6:
        problems.append(f"blank or flat grey (sd={sd:.1f})")
    elif sat < 4 and sd < 20:
        problems.append(f"looks like a grey placeholder (sat={sat:.1f}, sd={sd:.1f})")
    if content < 0.55:
        problems.append(f"large margins: content fills {content * 100:.0f}% (crop with x-auth/crop-margin.mjs)")
    return problems


def read_queue() -> list[dict]:
    with QUEUE.open(newline="") as f:
        rows = list(csv.DictReader(f, delimiter="\t"))
    for r in rows:
        for c in COLUMNS:
            r[c] = (r.get(c) or "").strip()
    return rows


def write_queue(rows: list[dict]):
    with QUEUE.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, delimiter="\t", lineterminator="\n")
        w.writeheader(); w.writerows(rows)


def append_label(row: dict, posted_at: str):
    new = not LABELS.exists()
    with LABELS.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LABEL_COLUMNS, delimiter="\t", lineterminator="\n")
        if new:
            w.writeheader()
        w.writerow({"post_id": row["post_id"], "posted_at": posted_at, "queue_id": row["id"], "type": row["type"],
                    "hook": row["hook"], "has_image": "1" if row["image"] else "0", "hypothesis_id": row["hypothesis_id"]})


def gate(row: dict, rows: list[dict], now: datetime) -> tuple[str, list[str]]:
    """Return (decision, problems). decision: post | wait | skip | missed | blocked."""
    if row["status"] not in ("", "queued"):
        return "skip", []
    due = datetime.strptime(row["post_at"], "%Y-%m-%d %H:%M").replace(tzinfo=JST)
    if due > now:
        return "wait", []
    if now - due > timedelta(hours=LATE_LIMIT_H):
        return "missed", [f"{(now - due).total_seconds() / 3600:.0f}h late; set a new post_at"]
    problems = []
    if not row["approved_by"]:
        problems.append("approved_by is empty")
    body_path = REPO / row["body_file"]
    body = body_path.read_text().strip() if body_path.exists() else ""
    if not body:
        problems.append(f"body file missing or empty: {row['body_file']}")
    if URL_RE.search(body):
        problems.append("URL in body (links go in reply 1)")
    if weighted_len(body) > 280:
        problems.append(f"too long: weighted {weighted_len(body)} > 280")
    head = body.splitlines()[0] if body else ""
    for other in rows:
        if other is not row and other["status"].startswith("posted") and other.get("_head") == head and head:
            problems.append(f"same opening line already posted as {other['id']}")
    images = image_list(row)
    if not images:
        problems.append("image required (misell-x rule)")
    elif len(images) > 4:
        problems.append(f"too many images: {len(images)} > 4")
    for img in images:
        if not (REPO / img).exists():
            problems.append(f"image missing: {img}")
        else:
            problems += check_image(REPO / img)
    row["_body"] = body
    return ("blocked" if problems else "post"), problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="actually post (default: dry run)")
    ap.add_argument("--now", help="override current time, 'YYYY-MM-DD HH:MM' JST (testing)")
    args = ap.parse_args()
    now = datetime.strptime(args.now, "%Y-%m-%d %H:%M").replace(tzinfo=JST) if args.now else datetime.now(JST)

    rows = read_queue()
    for r in rows:
        p = REPO / r["body_file"] if r["body_file"] else None
        r["_head"] = p.read_text().strip().splitlines()[0] if p and p.exists() and p.read_text().strip() else ""
    month = now.strftime("%Y-%m")
    posted_this_month = sum(1 for r in rows if r["status"].startswith("posted") and r["post_at"].startswith(month))

    client, done, changed = None, 0, False
    for row in rows:
        decision, problems = gate(row, rows, now)
        if decision in ("skip", "wait"):
            continue
        if decision == "missed":
            row["status"], row["note"], changed = "missed", problems[0], True
            print(f"[missed] {row['id']}: {problems[0]}"); continue
        if decision == "blocked":
            print(f"[blocked] {row['id']}: " + "; ".join(problems)); continue
        if done >= MAX_ROWS_PER_RUN or posted_this_month >= MONTHLY_CAP:
            print(f"[deferred] {row['id']}: run/month cap reached"); continue
        if not args.live:
            print(f"[dry-run] would post {row['id']} ({weighted_len(row['_body'])}w, image={row['image']}, reply={'yes' if row['reply_text'] else 'no'})")
            done += 1; continue
        if client is None:
            from x_api import XClient
            client = XClient(write_limit=MAX_ROWS_PER_RUN * 6)  # up to 4 uploads + post + reply per row
            client.me()  # refuses to run if authenticated as the wrong account
        media = [client.upload_image(str(REPO / img)) for img in image_list(row)]
        row["post_id"] = client.create_post(row["_body"], media_ids=media)
        row["status"], changed = "posted-noreply", True
        write_queue([{k: v for k, v in r.items() if not k.startswith("_")} for r in rows])  # persist before the reply
        append_label(row, now.isoformat())
        if row["reply_text"]:
            row["reply_id"] = client.create_post(row["reply_text"], reply_to=row["post_id"])
        row["status"] = "posted"
        print(f"[posted] {row['id']} -> https://x.com/i/web/status/{row['post_id']}")
        done += 1; posted_this_month += 1
    if changed:
        write_queue([{k: v for k, v in r.items() if not k.startswith("_")} for r in rows])
    return 0


if __name__ == "__main__":
    sys.exit(main())
