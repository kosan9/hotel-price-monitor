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

GBP_RE = re.compile(r"£\s*([0-9]{1,5}(?:\.[0-9]{2})?)", re.I)


def extract_price_from_rate_button(btn) -> float | None:
    # 1) Try the split spans if present
    try:
        int_loc = btn.locator(".rate-int").first
        dec_loc = btn.locator(".rate-dec").first
        if int_loc.count() > 0 and dec_loc.count() > 0:
            int_part = int_loc.inner_text(timeout=2000).strip()
            dec_part = dec_loc.inner_text(timeout=2000).strip()
            if int_part.isdigit() and dec_part.isdigit():
                return float(f"{int_part}.{dec_part}")
    except Exception:
        pass

    # 2) Try reading the button text and extracting the FIRST £amount
    try:
        txt = btn.inner_text(timeout=2000)
        amts = extract_gbp_amounts(txt)
        if amts:
            # If multiple amounts appear, pick the largest (usually total)
            return max(amts)
    except Exception:
        pass

    # 3) Last resort: read all textContent (sometimes inner_text is empty)
    try:
        txt = btn.evaluate("el => el.textContent || ''")
        amts = extract_gbp_amounts(txt)
        if amts:
            return max(amts)
    except Exception:
        pass

    return None


def find_saver_rate_price(page) -> float | None:
    btn = page.locator('button[data-rate-plan-code="SAVER"]').first
    try:
        if btn.count() == 0:
            return None
        # wait until it has some text
        btn.wait_for(state="visible", timeout=15000)
        page.wait_for_timeout(500)  # small settle
        return extract_price_from_rate_button(btn)
    except Exception:
        return None

# def find_saver_rate_price(page) -> float | None:
#     main = page.locator("main")
#
#     # Wait (up to 15s) for any Saver rate buttons to appear
#     try:
#         main.locator('button[data-rate-plan-code="SAVER"]').first.wait_for(state="attached", timeout=15000)
#     except Exception:
#         pass
#
#     # Prefer a Saver button that is "selected", but don't rely on it
#     candidates = [
#         'button[data-rate-plan-code="SAVER"].selected',
#         'button[data-rate-plan-code="SAVER"][aria-pressed="true"]',
#         'button[data-rate-plan-code="SAVER"]',
#         'button[data-room-rate-type-name="Saver"]',
#         'button[data-ratename*="Saver" i]',
#     ]
#
#     for sel in candidates:
#         btn = main.locator(sel).first
#         try:
#             if btn.count() > 0:
#
#                 try:
#                     print("DEBUG: saver_button_outer_html =", btn.evaluate("el => el.outerHTML")[:400])
#                 except Exception as e:
#                     print("DEBUG: saver_button_outer_html failed:", e)
#
#                 price = extract_price_from_rate_button(btn)
#                 if price is not None:
#                     return price
#         except Exception:
#             continue
#
#     return None



def pick_total_from_block(amounts: list[float], floor: float = 80.0) -> float | None:
    # Ignore tiny UI prices; pick the biggest remaining (often total stay)
    cand = [a for a in amounts if a >= floor]
    return max(cand) if cand else None

def find_rate_price_by_keyword(page, keyword: str, floor: float = 80.0, max_hits: int = 8) -> float | None:
    """
    Find price inside the same DOM block that contains `keyword` (case-insensitive).
    We search inside <main>, then for each hit we walk up parents and extract £ amounts.
    """
    main = page.locator("main")

    # Regex text match inside main (more reliable than `text=/.../i` string selector)
    hits = main.get_by_text(re.compile(keyword, re.I))

    try:
        count = hits.count()
    except Exception as e:
        print(f"WARN: could not count keyword hits for '{keyword}': {e}")
        return None

    n = min(count, max_hits)
    best = None

    for i in range(n):
        node = hits.nth(i)
        cur = node
        for _ in range(6):
            try:
                txt = cur.inner_text(timeout=2000)
            except Exception:
                txt = ""

            amts = extract_gbp_amounts(txt)
            total = pick_total_from_block(amts, floor=floor)

            if total is not None:
                best = total if best is None else min(best, total)
                break

            # go up one parent
            cur = cur.locator("xpath=..")

    return best


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
    for m in GBP_RE.finditer(text or ""):
        try:
            vals.append(float(m.group(1)))
        except ValueError:
            pass
    return sorted({round(v, 2) for v in vals})

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
    """
    Returns: (chosen_price, all_amounts_found, sample_text)
    Priority:
      1) Try to extract price from the block containing "Saver" (reduces other-hotel noise)
      2) Fallback: scan whole page for all £ amounts
    """
    chosen = None
    amounts = []
    sample_text = ""

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            page.wait_for_timeout(5000)

            # Best-effort cookie accept (Travelodge may show a banner)
            for sel in [
                'button:has-text("Accept all")',
                'button:has-text("Accept")',
                'text=Accept all cookies',
            ]:
                try:
                    loc = page.locator(sel).first
                    if loc.count() > 0:
                        loc.click(timeout=1500)
                        page.wait_for_timeout(1000)
                        break
                except Exception:
                    pass

            try:
                n = page.locator('button[data-rate-plan-code="SAVER"]').count()
                print(f"DEBUG: saver_buttons_count = {n}")
            except Exception as e:
                print(f"DEBUG: saver_buttons_count failed: {e}")

            chosen = find_saver_rate_price(page)
            print(f"DEBUG: saver_extracted_price = {chosen}")

            # 2) Always collect full-page amounts for debugging/history
            content = page.content()
            text = page.inner_text("body")
            sample_text = text[:5000]

            amounts = extract_gbp_amounts(text) + extract_gbp_amounts(content)
            amounts = sorted({round(v, 2) for v in amounts})

        except PlaywrightTimeoutError:
            # best-effort capture
            try:
                content = page.content()
            except Exception:
                content = ""
            try:
                text = page.inner_text("body")
            except Exception:
                text = ""
            sample_text = text[:5000]
            amounts = extract_gbp_amounts(text) + extract_gbp_amounts(content)
            amounts = sorted({round(v, 2) for v in amounts})

        finally:
            browser.close()

    return chosen, amounts, sample_text




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

    chosen, amounts, _sample = fetch_price(args.url)

    # If Saver extraction failed, fallback to old heuristic
    if chosen is None:
        chosen = choose_best_price(amounts, last_price=last_price, expected=args.expected)

        print(f"DEBUG: final_chosen_price = {chosen}")

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
