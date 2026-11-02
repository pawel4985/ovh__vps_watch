#!/usr/bin/env python3
"""
Monitor dostępności VPS w OVHcloud (np. VPS-1 2027).
Wysyła e-mail, gdy plan pojawi się w lokalizacji, w której wcześniej go nie było.

Użycie:
  python ovh_vps_watch.py --list         # dostępność obserwowanych planów
  python ovh_vps_watch.py --list-all     # cały katalog planów VPS
  python ovh_vps_watch.py --test-email   # sprawdź, czy wysyłka maili działa
  python ovh_vps_watch.py                # jedno sprawdzenie (pod cron / Harmonogram zadań)
  python ovh_vps_watch.py --loop 300     # sprawdzaj w pętli co 300 sekund

Tylko biblioteka standardowa Pythona (3.8+), nic nie trzeba instalować.
"""

import argparse
import json
import os
import smtplib
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path


def env(name, default=""):
    """Zmienna środowiskowa; pusta (np. nieustawiony sekret na GitHubie) = wartość domyślna."""
    return os.environ.get(name) or default


# ============================ KONFIGURACJA ============================
# Wpisz wartości tutaj albo ustaw je jako zmienne środowiskowe.

OVH_API = "https://eu.api.ovh.com/1.0"
SUBSIDIARY = env("OVH_SUBSIDIARY", "PL")

# Konkretne planCode'y po przecinku (np. "vps-2027-model1").
# Puste = automatycznie wszystkie plany z katalogu pasujące do PLAN_FILTER.
PLAN_CODES = env(
    "OVH_PLAN_CODES", "vps-2027-model1,vps-2027-model2,vps-2027-model3,vps-2027-model4"
)
PLAN_FILTER = env("OVH_PLAN_FILTER", "2027")
# Pomijaj plany Local Zone (".LZ" - wolniejszy dysk sieciowy zamiast NVMe).
SKIP_LZ = env("OVH_SKIP_LZ", "1") == "1"

# Obserwowane centra danych po przecinku. Np. sama Europa:
# "WAW,GRA,SBG,EU-WEST-RBX,DE,UK,EU-SOUTH-MIL"
# Puste = wszystkie lokalizacje.
WATCH_DCS = env("OVH_WATCH_DCS", "")

# System podawany tylko wtedy, gdy API odrzuci zapytanie bez parametru "os".
FALLBACK_OS = env("OVH_OS", "Ubuntu 26.04")

# Dla jakiego systemu liczy się dostępność: "linux", "windows" albo "any".
# Pole "status" z API oznacza "jakikolwiek system", więc np. VPS dostępny
# tylko z Windowsem wyglądałby na dostępny - dlatego domyślnie "linux".
OS_TYPE = env("OVH_OS_TYPE", "linux")

SMTP_HOST = env("SMTP_HOST", "smtp.wp.pl")
SMTP_PORT = int(env("SMTP_PORT", "465"))  # 465 = SSL, 587 = STARTTLS
SMTP_USER = env("SMTP_USER", "twoj.adres@wp.pl")
SMTP_PASSWORD = env("SMTP_PASSWORD", "")  # hasło do poczty (lub hasło do programów pocztowych)
MAIL_FROM = env("MAIL_FROM", SMTP_USER)
MAIL_TO = env("MAIL_TO", SMTP_USER)  # kilka adresów po przecinku

STATE_FILE = Path(env("STATE_FILE", Path(__file__).with_name("ovh_vps_state.json")))
# ======================================================================

# Tylko do ładniejszych komunikatów; nieznane kody wyświetlą się bez zmian.
DC_NAMES = {
    "WAW": "Warszawa", "GRA": "Gravelines", "SBG": "Strasbourg", "EU-WEST-RBX": "Roubaix",
    "DE": "Frankfurt", "UK": "Londyn", "EU-SOUTH-MIL": "Mediolan", "BHS": "Beauharnois",
    "SGP": "Singapur", "SYD": "Sydney", "YNM": "Mumbaj",
    "EU-CENTRAL-LZ-PRG": "Praga LZ", "EU-SOUTH-LZ-MAD": "Madryt LZ",
    "EU-WEST-LZ-AMS": "Amsterdam LZ", "EU-WEST-LZ-BRU": "Bruksela LZ",
    "EU-WEST-LZ-MRS": "Marsylia LZ", "EU-WEST-LZ-VIE": "Wiedeń LZ",
    "EU-WEST-LZ-ZRH": "Zurych LZ",
}


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def dc_label(code):
    return f"{DC_NAMES[code]} [{code}]" if code in DC_NAMES else code


def watched(dc):
    wanted = {d.strip().upper() for d in WATCH_DCS.split(",") if d.strip()}
    return not wanted or dc in wanted


def api_get(path, params):
    url = f"{OVH_API}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": "ovh-vps-watch/1.0"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def fetch_catalog_plans():
    """Lista (planCode, nazwa) z publicznego katalogu VPS."""
    catalog = api_get("/order/catalog/public/vps", {"ovhSubsidiary": SUBSIDIARY})
    return [
        (p.get("planCode", ""), p.get("invoiceName") or p.get("planCode", ""))
        for p in catalog.get("plans", [])
    ]


def matches_filter(code, name):
    if SKIP_LZ and code.upper().endswith(".LZ"):
        return False
    return PLAN_FILTER.lower() in f"{code} {name}".lower()


def discover_plans():
    """Zwraca {planCode: nazwa} planów do obserwowania."""
    if PLAN_CODES.strip():
        codes = [c.strip() for c in PLAN_CODES.split(",") if c.strip()]
        try:
            names = dict(fetch_catalog_plans())
        except Exception:
            names = {}
        return {c: names.get(c, c) for c in codes}
    return {c: n for c, n in fetch_catalog_plans() if matches_filter(c, n)}


def get_datacenters(plan_code):
    """Zwraca {KOD_DC: status} dla planu; status to 'available' albo 'out-of-stock'."""
    params = {"ovhSubsidiary": SUBSIDIARY, "planCode": plan_code}
    try:
        data = api_get("/vps/order/rule/datacenter", params)
    except urllib.error.HTTPError as e:
        if e.code != 400:
            raise
        params["os"] = FALLBACK_OS
        data = api_get("/vps/order/rule/datacenter", params)

    items = data.get("datacenters", []) if isinstance(data, dict) else data
    result = {}
    for dc in items:
        code = str(dc.get("datacenter", "?")).upper()
        key = {"linux": "linuxStatus", "windows": "windowsStatus"}.get(OS_TYPE.lower())
        result[code] = (dc.get(key) if key else None) or dc.get("status") or "unknown"
    return result


def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def send_email(subject, body):
    if not SMTP_PASSWORD:
        raise RuntimeError("Brak SMTP_PASSWORD w konfiguracji")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = MAIL_FROM
    msg["To"] = MAIL_TO
    msg.set_content(body)
    ctx = ssl.create_default_context()
    if SMTP_PORT == 465:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx, timeout=30) as s:
            s.login(SMTP_USER, SMTP_PASSWORD)
            s.send_message(msg)
    else:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
            s.starttls(context=ctx)
            s.login(SMTP_USER, SMTP_PASSWORD)
            s.send_message(msg)


def check():
    plans = discover_plans()
    if not plans:
        log(f"Brak planów pasujących do filtra '{PLAN_FILTER}' - uruchom z --list.")
        return

    first_run = not STATE_FILE.exists()
    old = load_state()
    new, fresh, summary = {}, [], []
    ok = 0

    for code, name in plans.items():
        try:
            statuses = get_datacenters(code)
        except Exception as e:
            log(f"{name}: błąd zapytania ({e}) - pomijam w tym przebiegu")
            if code in old:
                new[code] = old[code]
            continue
        ok += 1
        avail = sorted(dc for dc, st in statuses.items() if st == "available" and watched(dc))
        new[code] = avail
        prev = set(old.get(code, []))
        fresh += [(name, dc) for dc in avail if dc not in prev]
        line = f"{name} ({code}): " + (", ".join(dc_label(d) for d in avail) or "brak")
        summary.append(line)
        log(line)

    if ok == 0:
        return  # nic nie udało się sprawdzić - nie ruszamy stanu

    if first_run:
        log("Pierwsze uruchomienie - zapisuję stan bazowy, bez wysyłania maila.")
    elif fresh:
        subject = "OVH VPS dostępny: " + ", ".join(f"{n} w {dc}" for n, dc in fresh)
        body = (
            "Pojawiła się nowa dostępność VPS w OVHcloud:\n\n"
            + "\n".join(f"  • {n} - {dc_label(dc)}" for n, dc in fresh)
            + "\n\nAktualnie dostępne (obserwowane lokalizacje):\n  "
            + "\n  ".join(summary)
            + f"\n\nZamówienie: https://www.ovhcloud.com/{SUBSIDIARY.lower()}/vps/"
            + f"\nSprawdzono: {datetime.now():%Y-%m-%d %H:%M}\n"
        )
        try:
            send_email(subject, body)
            log(f"Wysłano maila: {subject}")
        except Exception as e:
            log(f"Nie udało się wysłać maila ({e}) - spróbuję przy następnym sprawdzeniu.")
            return  # nie zapisujemy stanu, żeby powiadomienie nie przepadło

    save_state(new)


def list_mode(show_all=False):
    if PLAN_CODES.strip():
        plans = discover_plans()
    else:
        all_plans = fetch_catalog_plans()
        plans = {c: n for c, n in all_plans if matches_filter(c, n)}
        if show_all:
            print(f"Cały katalog VPS ({SUBSIDIARY}), * = obserwowane:")
            for code, name in all_plans:
                print(f"  {'*' if code in plans else ' '} {code:32} {name}")
            return

    if not plans:
        print(f"Brak planów pasujących do '{PLAN_FILTER}'. Pełny katalog: --list-all")
        return

    for code, name in plans.items():
        print(f"\n{name} ({code}):")
        try:
            for dc, st in sorted(get_datacenters(code).items()):
                mark = "[OK]" if st == "available" else "[--]"
                note = "" if watched(dc) else "  (nieobserwowane)"
                print(f"  {mark} {dc_label(dc):32} {st}{note}")
        except Exception as e:
            print(f"  błąd: {e}")


def main():
    ap = argparse.ArgumentParser(description="Monitor dostępności VPS OVHcloud")
    ap.add_argument("--list", action="store_true", help="pokaż dostępność obserwowanych planów")
    ap.add_argument("--list-all", action="store_true", help="pokaż cały katalog planów VPS")
    ap.add_argument("--test-email", action="store_true", help="wyślij testowego maila")
    ap.add_argument("--loop", type=int, metavar="SEK", help="sprawdzaj co SEK sekund (min. 60)")
    args = ap.parse_args()

    if args.list or args.list_all:
        list_mode(show_all=args.list_all)
    elif args.test_email:
        send_email("Test: monitor VPS OVH", "Jeśli to czytasz, wysyłka maili działa.")
        print("Wysłano testowego maila.")
    elif args.loop:
        interval = max(60, args.loop)
        log(f"Start - sprawdzam co {interval} s (Ctrl+C kończy).")
        try:
            while True:
                try:
                    check()
                except Exception as e:
                    log(f"Nieoczekiwany błąd: {e}")
                time.sleep(interval)
        except KeyboardInterrupt:
            log("Zatrzymano.")
    else:
        check()


if __name__ == "__main__":
    main()
