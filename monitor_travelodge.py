import os
import urllib.request
import urllib.parse
import re
import csv
import json
import argparse
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


GBP_RE = re.compile(r"£\s*([0-9]{1,5}(?:\.[0-9]{2})?)")


def telegram_send(msg: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": msg}).encode("utf-8")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        urllib.request.urlopen(url, data=data, timeout=15).read()
    except Exception as e:
        print(f"WARN: Telegram send failed: {e}")

def extract_gbp_amounts(text: str) -> list[float]:
    vals = []
    for m in GBP_RE.finditer(text):
        try:
            vals.append(float(m.group(1)))
        except ValueError:
            pass
    # de-dup with rounding (page may repeat prices)
    vals = sorted({round(v, 2) for v in vals})
    return vals


def choose_best_price(amounts: list[float], last_price: float | None, expected: float | None) -> float | None:
    if not amounts:
        return None

    # Heuristic:
    # - If we have last_price, pick the amount closest to it
    # - else if expected is given, pick closest to expected
    # - else pick the largest (often "total stay" is larger than nightly)
    if last_price is not None:
        return min(amounts, key=lambda x: abs(x - last_price))
    if expected is not None:
        return min(amounts, key=lambda x: abs(x - expected))
    return max(amounts)


def fetch_price(url: str, timeout_ms: int = 45000) -> tuple[float | None, list[float], str]:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            # small extra wait in case late JS updates price
            page.wait_for_timeout(1500)
            content = page.content()
            text = page.inner_text("body")
        except PlaywrightTimeoutError:
            # still try to grab what we can
            content = page.content()
            try:
                text = page.inner_text("body")
            except Exception:
                text = ""
        finally:
            browser.close()

    # Search both rendered text and HTML for currency values
    amounts = extract_gbp_amounts(text) + extract_gbp_amounts(content)
    # de-dup
    amounts = sorted({round(v, 2) for v in amounts})

    return None, amounts, text[:5000]  # price chosen later; also return some text sample for debug


def load_state(state_path: Path) -> dict:
    if state_path.exists():
        try:
            return json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(state_path: Path, state: dict) -> None:
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def append_history(csv_path: Path, ts_iso: str, price: float | None, amounts: list[float], url: str) -> None:
    new_file = not csv_path.exists()
    with csv_path.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["timestamp_utc", "chosen_price_gbp", "all_gbp_amounts_found", "url"])
        w.writerow([ts_iso, "" if price is None else f"{price:.2f}", ",".join(f"{a:.2f}" for a in amounts), url])


def main():
    ap = argparse.ArgumentParser(description="Monitor Travelodge price changes and log to CSV.")
    ap.add_argument("--url", required=True, help="Travelodge URL (with dates/guests).")
    ap.add_argument("--expected", type=float, default=None, help="Expected total price to help selection (e.g. 214).")
    ap.add_argument("--target", type=float, default=None, help="Alert if price <= target.")
    ap.add_argument("--drop-pct", type=float, default=5.0, help="Alert if price drops by >= this percent vs last seen.")
    ap.add_argument("--state", default="travelodge_state.json", help="State file (stores last seen price).")
    ap.add_argument("--history", default="travelodge_history.csv", help="CSV log file.")
    args = ap.parse_args()

    state_path = Path(args.state)
    csv_path = Path(args.history)

    state = load_state(state_path)
    last_price = state.get("last_price_gbp")

    _, amounts, _sample = fetch_price(args.url)

    chosen = choose_best_price(amounts, last_price=last_price, expected=args.expected)

    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    append_history(csv_path, ts, chosen, amounts, args.url)

    print(f"[{ts}] found {len(amounts)} GBP amounts; chosen_price = {chosen}")

    if chosen is None:
        print("ERROR: Could not detect a price (no £ amounts found).")
        raise SystemExit(2)


    alert_reasons = []

    if args.target is not None and chosen <= args.target:
        alert_reasons.append(f"price <= target ({chosen:.2f} <= {args.target:.2f})")

    if last_price is not None and last_price > 0:
        drop_pct = (last_price - chosen) / last_price * 100.0
        if drop_pct >= args.drop_pct:
            alert_reasons.append(f"dropped {drop_pct:.1f}% (from {last_price:.2f} to {chosen:.2f})")

    # Update state
    state["last_price_gbp"] = chosen
    state["last_checked_utc"] = ts
    save_state(state_path, state)

    if alert_reasons:
        print("ALERT:", "; ".join(alert_reasons))
        telegram_send(f"Travelodge price alert: {args.url}\n" + "; ".join(alert_reasons))
        # Keep it simple: print to console. You can wire this to Telegram/email later.


if __name__ == "__main__":
    main()
