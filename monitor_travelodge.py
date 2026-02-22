import os
import re
import csv
import json
import argparse
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

GBP_RE = re.compile(r"£\s*([0-9]{1,5}(?:\.[0-9]{2})?)", re.I)


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


def safe_key(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return (s[:80] if s else "item")


def load_state(state_path: Path) -> dict:
    if state_path.exists():
        try:
            return json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(state_path: Path, state: dict) -> None:
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def append_history(csv_path: Path, ts_iso: str, price: float | None, source: str, amounts: list[float], url: str) -> None:
    new_file = not csv_path.exists()
    with csv_path.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["timestamp_utc", "chosen_price_gbp", "source", "all_gbp_amounts_found", "url"])
        w.writerow([
            ts_iso,
            "" if price is None else f"{price:.2f}",
            source,
            ",".join(f"{a:.2f}" for a in amounts),
            url
        ])


def extract_price_from_rate_button(btn) -> float | None:
    # 1) Try split spans (.rate-int / .rate-dec)
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

    # 2) Try inner_text
    try:
        txt = btn.inner_text(timeout=2000)
        amts = extract_gbp_amounts(txt)
        if amts:
            return max(amts)
    except Exception:
        pass

    # 3) Last resort: textContent
    try:
        txt = btn.evaluate("el => el.textContent || ''")
        amts = extract_gbp_amounts(txt)
        if amts:
            return max(amts)
    except Exception:
        pass

    return None

def find_saver_rate_price(page) -> float | None:
    # Search globally (some pages don't put rate buttons under <main>)
    selectors = [
        'button[data-rate-plan-code="SAVER"].selected',
        'button[data-rate-plan-code="SAVER"][aria-pressed="true"]',
        'button[data-rate-plan-code="SAVER"]',
        'button[data-room-rate-type-name="Saver"]',
        'button[data-ratename="Saver rate"]',
        'button[data-ratename*="Saver" i]',
    ]

    for sel in selectors:
        btn = page.locator(sel).first
        try:
            if btn.count() > 0:
                try:
                    btn.wait_for(state="visible", timeout=8000)
                except Exception:
                    pass
                page.wait_for_timeout(200)
                price = extract_price_from_rate_button(btn)
                if price is not None:
                    return price
        except Exception:
            continue

    return None



def choose_best_price(amounts: list[float], last_price: float | None, expected: float | None) -> float | None:
    if not amounts:
        return None
    if last_price is not None:
        return min(amounts, key=lambda x: abs(x - last_price))
    if expected is not None:
        return min(amounts, key=lambda x: abs(x - expected))
    return max(amounts)


def try_accept_cookies(page) -> None:
    for sel in ['button:has-text("Accept all")', 'button:has-text("Accept")']:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                loc.click(timeout=1500)
                page.wait_for_timeout(800)
                break
        except Exception:
            pass



def fetch_price(page, url: str, timeout_ms: int = 45000) -> tuple[float | None, list[float], str]:
    chosen = None
    source = "none"
    amounts: list[float] = []

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(1500)
        try_accept_cookies(page)

        # Scroll to trigger lazy-loaded rates
        for y in [600, 1200, 1800, 2400]:
            try:
                page.evaluate(f"window.scrollTo(0, {y});")
            except Exception:
                pass
            page.wait_for_timeout(400)

        # Wait specifically for any saver-ish button to exist (best effort)
        for sel in ['button[data-rate-plan-code="SAVER"]', 'button[data-room-rate-type-name="Saver"]', 'button[data-ratename*="Saver" i]']:
            try:
                page.locator(sel).first.wait_for(state="attached", timeout=6000)
                break
            except Exception:
                pass

        # DEBUG (keep for now)
        try:
            c1 = page.locator('button[data-rate-plan-code="SAVER"]').count()
            c2 = page.locator('button[data-room-rate-type-name="Saver"]').count()
            c3 = page.locator('button[data-ratename*="Saver" i]').count()
            print(f"DEBUG: saver_counts plan_code={c1} type_name={c2} ratename_like={c3}")
        except Exception as e:
            print(f"DEBUG: saver_counts failed: {e}")

        chosen = find_saver_rate_price(page)
        if chosen is not None:
            source = "saver_button"
        else:
            source = "none"

        # Collect all £ amounts (for history/debug)
        content = page.content()
        text = page.inner_text("body")
        amounts = extract_gbp_amounts(text) + extract_gbp_amounts(content)
        amounts = sorted({round(v, 2) for v in amounts})

    except PlaywrightTimeoutError:
        source = "timeout"
        try:
            content = page.content()
        except Exception:
            content = ""
        try:
            text = page.inner_text("body")
        except Exception:
            text = ""
        amounts = extract_gbp_amounts(text) + extract_gbp_amounts(content)
        amounts = sorted({round(v, 2) for v in amounts})

    except Exception as e:
        print(f"WARN: fetch_price error: {e}")
        source = "error"

    return chosen, amounts, source



def run_one_item(page, item: dict, out_dir: Path, default_drop_pct: float) -> tuple[str, str, float | None, str, list[str]]:
    name = item.get("name") or "unnamed"
    url = item["url"]
    expected = item.get("expected")
    target = item.get("target")
    drop_pct_thr = float(item.get("drop_pct", default_drop_pct))

    key = safe_key(name)
    state_path = out_dir / f"state_{key}.json"
    csv_path = out_dir / f"history_{key}.csv"

    state = load_state(state_path)
    last_price = state.get("last_price_gbp")

    chosen, amounts, source = fetch_price(page, url)

    if chosen is None:
        chosen = choose_best_price(amounts, last_price=last_price, expected=expected)
        if chosen is not None:
            source = "fallback"

    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    append_history(csv_path, ts, chosen, source, amounts, url)

    alerts: list[str] = []

    if chosen is None:
        alerts.append(f"{name}: ERROR no price detected")
        return name, url, chosen, source, alerts

    if target is not None and chosen <= float(target):
        alerts.append(f"{name}: <= target (£{chosen:.2f} <= £{float(target):.2f})")

    if last_price is not None and last_price > 0:
        drop_pct = (last_price - chosen) / last_price * 100.0
        if drop_pct >= drop_pct_thr:
            alerts.append(f"{name}: dropped {drop_pct:.1f}% (£{last_price:.2f} -> £{chosen:.2f})")

    state["last_price_gbp"] = chosen
    state["last_checked_utc"] = ts
    save_state(state_path, state)

    print(f"[{ts}] {name} | chosen=£{chosen:.2f} | source={source} | found={len(amounts)}")
    return name, url, chosen, source, alerts


def main():
    ap = argparse.ArgumentParser(description="Monitor Travelodge prices (single or multiple via JSON config).")
    ap.add_argument("--config", default=None, help="JSON config file for multiple hotels.")
    ap.add_argument("--url", default=None, help="Single Travelodge URL.")
    ap.add_argument("--expected", type=float, default=None, help="Expected price for single URL.")
    ap.add_argument("--target", type=float, default=None, help="Target price for single URL.")
    ap.add_argument("--drop-pct", type=float, default=5.0, help="Default drop percent threshold.")
    ap.add_argument("--out-dir", default=".", help="Output directory for state/history files.")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.config:
        items = json.loads(Path(args.config).read_text(encoding="utf-8"))
        if not isinstance(items, list) or not items:
            raise SystemExit("ERROR: config JSON must be a non-empty list.")
    else:
        if not args.url:
            raise SystemExit("ERROR: provide --config hotels.json OR --url ...")
        items = [{
            "name": "single",
            "url": args.url,
            "expected": args.expected,
            "target": args.target,
            "drop_pct": args.drop_pct,
        }]

    all_alerts: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            for item in items:
                name, url, chosen, source, alerts = run_one_item(page, item, out_dir, default_drop_pct=args.drop_pct)
                if alerts:
                    for a in alerts:
                        all_alerts.append(a + f"\n{url}")
        finally:
            browser.close()

    if all_alerts:
        msg = "Travelodge alerts\n\n" + "\n\n".join(all_alerts)
        print("ALERTS:\n" + "\n".join(all_alerts))
        telegram_send(msg)


if __name__ == "__main__":
    main()
