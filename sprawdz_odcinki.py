# -*- coding: utf-8 -*-
"""
sprawdz_odcinki.py

Raz w tygodniu sprawdza w TVmaze daty odcinków i nowe sezony dla seriali z tabeli "Seriale"
w Griście i na tej podstawie:
  * dopasowuje serial do TVmaze (zapisuje tvmaze_id - dopasowanie robi się tylko raz),
  * wylicza "kolejny" odcinek do nagrania, jego datę emisji i liczbę odcinków do nadrobienia,
  * zmienia status:
      - kompletny / niekompletny  -> oczekiwany, gdy zapowiedziano nowy sezon
                                  -> emitowany, gdy nowy sezon już się zaczął,
      - oczekiwany <-> emitowany  zależnie od tego, czy kolejny odcinek już wyszedł,
      - emitowany/oczekiwany -> kompletny, gdy serial się skończył i masz już wszystko,
  * pilnuje sezonów: gdy wszystkie odcinki sezonu są wyemitowane i nagrane, przenosi sezon do
    historii ([✓]) i przelicza status,
  * zapisuje w tabeli daty odcinków aktualnego sezonu (kolumna odcinki_tvm), z których korzysta widget,
  * uzupełnia PUSTE opisy (kolumna opis) i plakaty (kolumna plakat_url) danymi z TVmaze - po angielsku;
    nigdy nie nadpisuje tego, co już jest w tabeli (np. polskiego opisu z TMDb).

Prawdą o tym, co nagrane, jest kolumna "odcinki_nagrane" (format jak na Liście życzeń, np.
"[~] S03: 04/10 odc. {01-04}"). Przy pierwszym sprawdzeniu skrypt tworzy ją z kolumny "nastepny"
(numer następnego odcinka do nagrania) albo z historii sezonów.

Jest oszczędny dla API Grista (plan Pro: 40 000 wywołań na dobę): odczyt tabeli jednym wywołaniem,
zapisy partiami po 50 i tylko dla wierszy, w których coś się zmieniło.
Dane o odcinkach: TVmaze (tvmaze.com), licencja CC BY-SA.

Ustawienia (zmienne środowiskowe):
  GRIST_API_KEY          klucz Grista (w GitHub Actions: Secrets)
  TRYB                   "podglad" (domyślnie: nic nie zapisuje, tylko pokazuje co by zmieniło)
                         albo "zapis"
  LIMIT_ZMIAN_STATUSU    bezpiecznik: jeśli skrypt chce zmienić statusy w więcej niż tylu serialach
                         naraz (domyślnie 60), statusów NIE zmienia i prosi o ręczne sprawdzenie.
                         0 = bez limitu (do pierwszego, ręcznego uruchomienia)
  MAKS_POZYCJI           ile seriali maksymalnie przetworzyć w jednym uruchomieniu (0 = wszystkie)
  NADPISZ_OPISY          1 = zamień WSZYSTKIE opisy i plakaty na te z TVmaze (spójność); domyślnie tylko puste
  WSZYSTKIE              1 = sprawdź każdy serial (np. do jednorazowego odświeżenia opisów i plakatów)
  TYLKO_LINKI            1 = jednorazowo zamień link_epguides na prawdziwy adres TVmaze (dla wierszy
                         z już znanym tvmaze_id); nic więcej nie liczy ani nie zmienia
"""

import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone

# ================== KONFIGURACJA ==================
GRIST_API_KEY = os.environ.get("GRIST_API_KEY", "TU_WKLEJ_SWOJ_KLUCZ_API")
GRIST_DOC_ID = "tKjpkmVjijQjuWRrm4kix2"
GRIST_SITE = "https://dio.getgrist.com"
TABELA = "Seriale"

TRYB = os.environ.get("TRYB", "podglad").strip().lower()
LIMIT_ZMIAN_STATUSU = int(os.environ.get("LIMIT_ZMIAN_STATUSU", "60") or 0)
MAKS_POZYCJI = int(os.environ.get("MAKS_POZYCJI", "0") or 0)
NADPISZ_OPISY = os.environ.get("NADPISZ_OPISY", "").lower() in ("1", "true", "tak")   # zamień WSZYSTKIE opisy i plakaty na te z TVmaze
WSZYSTKIE = os.environ.get("WSZYSTKIE", "").lower() in ("1", "true", "tak")           # sprawdź każdy serial, nie tylko te ze zmianami
TYLKO_LINKI = os.environ.get("TYLKO_LINKI", "").lower() in ("1", "true", "tak")       # jednorazowo: zamień linki na TVmaze, pomiń resztę logiki

PAUZA_TVMAZE = 0.6          # limit TVmaze: co najmniej 20 zapytań / 10 s
PARTIA_ZAPISU = 50
ODSWIEZ_PO_DNIACH = 40      # serial bez zmian w TVmaze sprawdzamy ponownie najpóźniej po tylu dniach

KOLUMNY = [                 # kolumny, które skrypt założy w tabeli Seriale, jeśli ich brakuje
    ("tvmaze_id", "Int", "tvmaze id"),
    ("sprawdzono", "Date", "sprawdzono"),
    ("kolejny", "Text", "kolejny odcinek"),
    ("kolejny_data", "Date", "data kolejnego"),
    ("zalegle", "Int", "do nadrobienia"),
    ("nastepny", "Text", "następny do nagrania"),
    ("odcinki_nagrane", "Text", "odcinki nagrane"),
    ("odcinki_tvm", "Text", "odcinki TVmaze"),
    ("status_tvm", "Text", "status TVmaze"),
    ("plakat_url", "Text", "plakat URL"),
]
# ================== KONIEC KONFIGURACJI ==================

TVMAZE = "https://api.tvmaze.com"
NAGLOWKI_TVMAZE = {"User-Agent": "seriale-baza/1.0 (prywatny skrypt, tygodniowe sprawdzanie odcinkow)"}
WYWOLANIA_GRISTA = 0
WYWOLANIA_TVMAZE = 0


# ---------- TVmaze ----------

def tvmaze(session, sciezka, params=None, ponowien=4):
    """Zwraca sparsowany JSON albo None (np. 404). Przy 429 czeka i ponawia."""
    global WYWOLANIA_TVMAZE
    for proba in range(ponowien + 1):
        WYWOLANIA_TVMAZE += 1
        try:
            resp = session.get(TVMAZE + sciezka, params=params, headers=NAGLOWKI_TVMAZE, timeout=60)
        except Exception:
            if proba == ponowien:
                return None
            time.sleep(3 * (proba + 1))
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            if proba == ponowien:
                return None
            czekaj = resp.headers.get("Retry-After", "")
            time.sleep(float(czekaj) if czekaj.isdigit() else 5 * (proba + 1))
            continue
        if resp.status_code != 200:
            return None
        time.sleep(PAUZA_TVMAZE)
        return resp.json()
    return None


def przygotuj_tytul(tytul):
    """Baza: 'Wire, The' -> szukamy 'The Wire'."""
    t = (tytul or "").strip()
    t = re.sub(r"\s*\(\d{4}\)\s*$", "", t)
    for rodzajnik in (", The", ", A", ", An"):
        if t.endswith(rodzajnik):
            t = rodzajnik[2:] + " " + t[: -len(rodzajnik)]
            break
    return t.strip()


def norm(nazwa):
    s = (nazwa or "").lower().replace("&", "and")
    s = re.sub(r"[^a-z0-9]+", "", s)
    return re.sub(r"^the", "", s)


def kody_krajow_wiersza(kraj):
    return {("GB" if k.strip().upper() == "UK" else k.strip().upper()) for k in (kraj or "").split("/") if k.strip()}


def kody_krajow_pokazu(show):
    kody = set()
    for klucz in ("network", "webChannel"):
        n = show.get(klucz) or {}
        kod = (n.get("country") or {}).get("code")
        if kod:
            kody.add(kod)
    return kody


def zapytania_wyszukiwania(tytul):
    """Pełny tytuł, a jeśli ma przedrostek uniwersum ("Star Wars: Andor"), to także sama końcówka po dwukropku."""
    pelny = przygotuj_tytul(tytul)
    wynik = [pelny]
    if ": " in pelny:
        koniec = pelny.rsplit(": ", 1)[1].strip()
        if len(koniec) >= 4 and koniec not in wynik:
            wynik.append(koniec)
    return wynik


def szukaj_pokazu(session, tytul, rok, kraj):
    """Szuka serialu w TVmaze (kilka wariantów zapytania) i zwraca dopasowany show albo None."""
    for q in zapytania_wyszukiwania(tytul):
        wyniki = tvmaze(session, "/search/shows", params={"q": q})
        show = dopasuj_pokaz(wyniki, tytul, rok, kraj)
        if show:
            return show
    return None


def dopasuj_pokaz(wyniki, tytul, rok, kraj):
    """Wybiera z wyników wyszukiwania właściwy serial (nazwa + rok premiery + kraj) albo None."""
    szukany = norm(przygotuj_tytul(tytul))
    kraje_wiersza = kody_krajow_wiersza(kraj)
    kandydaci = []
    for w in (wyniki or [])[:8]:
        s = w.get("show") or {}
        nazwa = norm(s.get("name"))
        if not nazwa or not szukany:
            continue
        if nazwa == szukany:
            pkt = 4
        elif len(nazwa) >= 5 and szukany.endswith(nazwa):
            pkt = 2          # u Ciebie tytuł ma przedrostek uniwersum, np. "Star Wars: Andor" a TVmaze ma "Andor"
        elif len(szukany) >= 5 and nazwa.endswith(szukany):
            pkt = 2          # odwrotnie: TVmaze dodaje przedrostek
        elif nazwa.startswith(szukany) or szukany.startswith(nazwa):
            pkt = 1
        else:
            continue
        rok_p = int(s["premiered"][:4]) if s.get("premiered") else None
        if rok and rok_p:
            roznica = abs(int(rok) - rok_p)
            pkt += 3 if roznica == 0 else 2 if roznica == 1 else -3
        if kraje_wiersza & kody_krajow_pokazu(s):
            pkt += 1
        kandydaci.append((pkt, w.get("score") or 0, s))
    if not kandydaci:
        return None
    pkt, _, show = max(kandydaci, key=lambda k: (k[0], k[1]))
    return show if (pkt >= 5 or (pkt >= 4 and not rok)) else None


def parsuj_odcinki(dane):
    """Zwykłe odcinki (bez specjali): [(sezon, numer, data_emisji_lub_None)], posortowane."""
    wynik = []
    for e in dane or []:
        if e.get("season") is None or e.get("number") is None:
            continue
        d = None
        if e.get("airdate"):
            try:
                d = date.fromisoformat(e["airdate"])
            except ValueError:
                d = None
        wynik.append((int(e["season"]), int(e["number"]), d))
    return sorted(wynik, key=lambda x: (x[0], x[1]))


def czysty_tekst(html_tekst):
    """Opis z TVmaze przychodzi jako HTML - zamieniamy na zwykły tekst."""
    t = re.sub(r"<[^>]+>", " ", html_tekst or "")
    t = re.sub(r"&nbsp;", " ", t)
    for kod, znak in (("&amp;", "&"), ("&quot;", '"'), ("&#39;", "'"), ("&apos;", "'"), ("&lt;", "<"), ("&gt;", ">")):
        t = t.replace(kod, znak)
    return re.sub(r"\s+", " ", t).strip()


def pokaz_i_odcinki(session, tvmaze_id):
    """
    Jednym zapytaniem: (status serialu w TVmaze, lista odcinków, {"opis":..., "plakat":...}).
    Przy błędzie: (None, None, {}).
    """
    dane = tvmaze(session, f"/shows/{tvmaze_id}", params={"embed": "episodes"})
    if dane is None:
        return None, None, {}
    kody = kody_krajow_pokazu(dane)
    kod = sorted(kody)[0] if kody else ""
    info = {
        "opis": czysty_tekst(dane.get("summary")),
        "plakat": ((dane.get("image") or {}).get("medium") or ""),
        "rok": int(dane["premiered"][:4]) if dane.get("premiered") else None,
        "kraj": "UK" if kod == "GB" else kod,
        "link": dane.get("url") or "",
    }
    lista = (dane.get("_embedded") or {}).get("episodes")
    if lista is None:
        lista = tvmaze(session, f"/shows/{tvmaze_id}/episodes")
        if lista is None:
            return dane.get("status"), None, info
    return dane.get("status"), parsuj_odcinki(lista), info


# ---------- logika statusów ----------

def sezony_z_historii(tekst):
    """'[✓] S01 (2018): 24 odc.' -> {1: 24}"""
    wynik = {}
    for m in re.finditer(r"S(\d{1,3})(?:\s*\(\d{4}\))?:\s*(\d+|\?\?)", tekst or ""):
        nr = int(m.group(1))
        ile = 0 if m.group(2) == "??" else int(m.group(2))
        if nr not in wynik or (wynik[nr] == 0 and ile > 0):
            wynik[nr] = ile
    return wynik


def parsuj_nastepny(tekst):
    """'S09E18' / 'S02E01-08' / 'S01' -> ('odc', s, e); 'FILM'/'SPECIAL' -> ('film',); pusty -> None"""
    t = (tekst or "").strip().upper()
    if not t:
        return None
    if t in ("FILM", "SPECIAL"):
        return ("film",)
    m = re.match(r"^S(\d+)E(\d+)", t)
    if m:
        return ("odc", int(m.group(1)), int(m.group(2)))
    m = re.match(r"^S(\d+)$", t)
    if m:
        return ("odc", int(m.group(1)), 1)
    return None


def pad2(n):
    return f"{int(n):02d}"


def parsuj_nagrane(tekst):
    """'[~] S03: 03/10 odc. {01-03,05}' -> {3: {1, 2, 3, 5}}"""
    wynik = {}
    for linia in (tekst or "").split("\n"):
        m = re.search(r"S(\d{1,3}):\s*(\d+)\s*/\s*(\d+|\?\?)", linia)
        if not m:
            continue
        nr, n = int(m.group(1)), int(m.group(2))
        zbior = set()
        b = re.search(r"\{([^}]*)\}", linia)
        if b:
            for cz in b.group(1).split(","):
                cz = cz.strip()
                r = re.match(r"^(\d+)\s*-\s*(\d+)$", cz)
                if r:
                    zbior.update(range(int(r.group(1)), min(int(r.group(2)), 9999) + 1))
                elif cz.isdigit():
                    zbior.add(int(cz))
        elif n > 0:
            zbior = set(range(1, n + 1))
        wynik[nr] = zbior
    return wynik


def zakresy(zbior):
    a = sorted(zbior)
    wyjscie, start, poprz = [], None, None
    for e in a:
        if start is None:
            start = poprz = e
        elif e == poprz + 1:
            poprz = e
        else:
            wyjscie.append(pad2(start) if start == poprz else f"{pad2(start)}-{pad2(poprz)}")
            start = poprz = e
    if start is not None:
        wyjscie.append(pad2(start) if start == poprz else f"{pad2(start)}-{pad2(poprz)}")
    return ",".join(wyjscie)


def serializuj_nagrane(mapa, liczby):
    """{3: {1,2,3}} + {3: 10} -> '[~] S03: 03/10 odc. {01-03}' (ten sam format zapisuje widget)."""
    linie = []
    for nr in sorted(mapa):
        razem = liczby.get(nr, 0)
        trzymane = {e for e in mapa[nr] if razem == 0 or e <= razem}
        n = len(trzymane)
        sym = "[ ]" if n == 0 else ("[◇]" if razem and n >= razem else "[~]")
        linia = f"{sym} S{pad2(nr)}: {pad2(n)}/{pad2(razem) if razem else '??'} odc."
        if n > 0:
            linia += " {" + zakresy(trzymane) + "}"
        linie.append(linia)
    return "\n".join(linie)


def ustaw_sezon_w_historii(tekst, nr, ile, rok=None):
    """Wpisuje sezon do historii jako ukończony: zmienia liczbę w istniejącej linii albo dopisuje nową.
    Rok (rozpoczęcia sezonu), jeśli podany i linia go jeszcze nie ma, wstawiany jest zaraz po
    numerze sezonu: "[✓] S01 (2018): 24 odc.". Sezon z zerową liczbą odcinków nie jest tu wpisywany
    w ogóle (sezon "ukończony" bez żadnego odcinka to sprzeczność - takiej linii nie tworzymy)."""
    if not ile:
        return tekst
    linie = (tekst or "").rstrip().split("\n") if (tekst or "").strip() else []
    wzor_z_rokiem = re.compile(rf"(S0*{nr}\s*\(\d{{4}}\):\s*)(\d+|\?\?)")
    wzor_bez_roku = re.compile(rf"(S0*{nr}:\s*)(\d+|\?\?)")
    for i, l in enumerate(linie):
        if wzor_z_rokiem.search(l):
            linie[i] = wzor_z_rokiem.sub(lambda m: f"{m.group(1)}{pad2(ile)}", l, count=1)
            return "\n".join(linie)
        if wzor_bez_roku.search(l):
            if rok:
                linie[i] = wzor_bez_roku.sub(lambda m: f"S{pad2(nr)} ({rok}): {pad2(ile)}", l, count=1)
            else:
                linie[i] = wzor_bez_roku.sub(lambda m: f"{m.group(1)}{pad2(ile)}", l, count=1)
            return "\n".join(linie)
    linia = f"[✓] S{pad2(nr)}" + (f" ({rok})" if rok else "") + f": {pad2(ile)} odc."
    linie.append(linia)
    return "\n".join(linie)


def tokeny_sezonu(odcinki, sezon):
    """Daty emisji odcinków 1..N sezonu jako tekst: '2026-10-23 2026-10-30 ?' ('?' = brak daty)."""
    dane = {o[1]: o[2] for o in odcinki if o[0] == sezon}
    if not dane:
        return ""
    return " ".join((dane[n].isoformat() if dane.get(n) else "?") for n in range(1, max(dane) + 1))


def znajdz_kolejny(odcinki, start):
    for o in odcinki:
        if (o[0], o[1]) >= start:
            return o
    return None


def ocen(pola, odcinki, status_tv, dzis):
    """
    Zwraca słownik z wyliczonymi wartościami albo None, gdy dla tego serialu nic nie liczymy.
    Klucze: status, powod, kolejny, kolejny_data, zalegle, historia, nagrane (mapa), nagrane_txt,
            tvm (tekst dat), zakonczone (lista sezonów przeniesionych do historii)
    """
    status = pola.get("status") or ""
    if status not in ("oczekiwany", "emitowany", "kompletny", "niekompletny"):
        return None
    aktywny = status in ("oczekiwany", "emitowany")
    nast = parsuj_nastepny(pola.get("nastepny"))
    if nast and nast[0] == "film":
        return None

    historia = pola.get("historia_sezonow") or ""
    hist = sezony_z_historii(historia)
    najw = max(hist) if hist else 0
    nagrane = parsuj_nagrane(pola.get("odcinki_nagrane"))
    liczby = {}
    for (sez, num, _) in odcinki:
        liczby[sez] = max(liczby.get(sez, 0), num)

    if not aktywny and najw == 0 and not nagrane:
        return None                                # brak historii sezonów - nie zgadujemy

    # inicjalizacja "odcinków nagranych" (tylko raz, dla serialu w trakcie)
    zainicjowano = False
    if aktywny and not nagrane:
        if nast and najw >= nast[1]:
            nast = None            # sezon z "następnego do nagrania" jest już w historii - dane z importu są nieaktualne
        if nast:
            # "następny do nagrania" = S/E oznacza, że wszystko wcześniej jest już nagrane; sezony,
            # których nie ma w historii, oznaczamy jako nagrane (skrypt sam wpisze je do historii)
            for sez in liczby:
                if sez < nast[1] and sez not in hist:
                    nagrane[sez] = set(range(1, liczby[sez] + 1))
                    zainicjowano = True
            if nast[1] in liczby:
                nagrane[nast[1]] = set(range(1, min(nast[2] - 1, liczby[nast[1]]) + 1))
                zainicjowano = True
        elif najw in hist and liczby.get(najw, 0) > hist[najw]:
            nagrane[najw] = set(range(1, hist[najw] + 1))
            zainicjowano = True

    # sezony w całości wyemitowane i nagrane -> do historii
    zakonczone = []
    for sez in sorted(nagrane):
        ep_sez = [o for o in odcinki if o[0] == sez]
        if ep_sez and all(o[1] in nagrane[sez] for o in ep_sez) and all(o[2] and o[2] <= dzis for o in ep_sez):
            zakonczone.append((sez, len(ep_sez)))
    for sez, ile in zakonczone:
        daty_sezonu = [o[2] for o in odcinki if o[0] == sez and o[2]]
        rok_sezonu = min(daty_sezonu).year if daty_sezonu else None
        historia = ustaw_sezon_w_historii(historia, sez, ile, rok_sezonu)
        del nagrane[sez]
    hist2 = sezony_z_historii(historia)
    najw2 = max(hist2) if hist2 else 0

    def nagrany(sez, num):
        return (num in nagrane[sez]) if sez in nagrane else sez <= najw2

    kolejny = next((o for o in odcinki if not nagrany(o[0], o[1])), None)
    if kolejny and not aktywny and status_tv == "Ended" and kolejny[2] and (dzis - kolejny[2]).days > 400:
        kolejny = None                             # stary, zakończony serial z inną numeracją w TVmaze

    pewne = bool(nast) or bool(nagrane) or bool(zakonczone) or bool(pola.get("odcinki_nagrane")) or bool(najw2 and liczby and najw2 >= max(liczby))
    nowy_status, powod = status, ""
    if kolejny:
        sez, num, d = kolejny
        pierwszy = num == min(o[1] for o in odcinki if o[0] == sez)
        if pierwszy:
            nowy_status = "emitowany" if (d and d <= dzis) else "oczekiwany"
            powod = (f"nowy sezon S{pad2(sez)}" if not aktywny else f"kolejny S{pad2(sez)}E{pad2(num)}") + \
                    (f" od {d.isoformat()}" if d else " (bez daty)")
        else:
            nowy_status, powod = "emitowany", f"sezon trwa, kolejny S{pad2(sez)}E{pad2(num)}"
    elif aktywny:
        if pewne and status_tv == "Ended":
            nowy_status, powod = "kompletny", "serial zakończony, wszystko nagrane"
        else:
            nowy_status, powod = "oczekiwany", "brak kolejnego odcinka w TVmaze"
    if zakonczone and nowy_status == status:
        powod = "sezon " + ", ".join(f"S{pad2(s_)}" for s_, _ in zakonczone) + " przeniesiony do historii"

    zalegle = len([o for o in odcinki if not nagrany(o[0], o[1]) and o[2] and o[2] <= dzis])

    # daty odcinków do widgetu: od sezonu kolejnego odcinka (max 3 sezony) albo ostatni sezon
    if kolejny:
        sezony_tvm = sorted({o[0] for o in odcinki if o[0] >= kolejny[0]})[:3]
    elif aktywny and odcinki and nowy_status != "kompletny" and max(liczby) > najw2:
        sezony_tvm = [max(liczby)]
    else:
        sezony_tvm = []
    tvm = "\n".join(f"S{pad2(sez)}: {tokeny_sezonu(odcinki, sez)}" for sez in sezony_tvm)

    return {
        "status": nowy_status,
        "powod": powod,
        "kolejny": f"S{pad2(kolejny[0])}E{pad2(kolejny[1])}" if kolejny else "",
        "kolejny_data": kolejny[2] if kolejny else None,
        "zalegle": zalegle,
        "historia": historia,
        "nagrane": nagrane,
        "nagrane_txt": serializuj_nagrane(nagrane, liczby),
        "tvm": tvm,
        "zakonczone": zakonczone,
        "zainicjowano": zainicjowano,
    }


def epoka(d):
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()) if d else None


# ---------- Grist ----------

def grist(session, metoda, sciezka, **kw):
    global WYWOLANIA_GRISTA
    WYWOLANIA_GRISTA += 1
    naglowki = {"Authorization": f"Bearer {GRIST_API_KEY}"}
    if "json" in kw:
        naglowki["Content-Type"] = "application/json"
    return session.request(metoda, f"{GRIST_SITE}/api/docs/{GRIST_DOC_ID}{sciezka}",
                           headers=naglowki, timeout=90, **kw)


KOLUMNY_ZYCZEN = [
    ("tvmaze_id", "Int", "tvmaze id"),
    ("plakat_url", "Text", "plakat URL"),
]


def zapewnij_kolumny(session, tabela=None, kolumny=None):
    tabela = tabela or TABELA
    kolumny = kolumny or KOLUMNY
    resp = grist(session, "GET", f"/tables/{tabela}/columns")
    if resp.status_code != 200:
        raise RuntimeError(f"Odczyt kolumn nie powiódł się ({resp.status_code}): {resp.text[:300]}")
    istniejace = {k.get("id") for k in resp.json().get("columns", [])}
    brakujace = [k for k in kolumny if k[0] not in istniejace]
    if not brakujace:
        return
    resp = grist(session, "POST", f"/tables/{tabela}/columns", json={"columns": [
        {"id": i, "fields": {"type": typ, "label": etykieta}} for (i, typ, etykieta) in brakujace]})
    if resp.status_code != 200:
        raise RuntimeError(f"Tworzenie kolumn nie powiodło się ({resp.status_code}): {resp.text[:300]}")
    print(f"Utworzono kolumny w tabeli {tabela}: " + ", ".join(k[0] for k in brakujace))


def wyrownaj_partie(partia, oryginalne):
    """
    Grist odrzuca PATCH, jeśli rekordy w jednej paczce mają różne zestawy pól
    (błąd: "PATCH requires all records to have same fields"). Brakujące pola
    uzupełniamy bieżącą wartością z Grista - to nie zmienia danych, tylko
    wyrównuje kształt paczki.
    """
    if not partia:
        return partia
    wszystkie_klucze = set()
    for rec in partia:
        wszystkie_klucze |= set(rec["fields"].keys())
    for rec in partia:
        oryg = oryginalne.get(rec["id"]) or {}
        for k in wszystkie_klucze:
            if k not in rec["fields"]:
                rec["fields"][k] = oryg.get(k)
    return partia


def zapisz(session, partia, tabela=None, oryginalne=None):
    if not partia:
        return
    if oryginalne:
        partia = wyrownaj_partie(partia, oryginalne)
    resp = grist(session, "PATCH", f"/tables/{tabela or TABELA}/records", json={"records": partia})
    if resp.status_code != 200:
        print(f"  BŁĄD zapisu partii ({len(partia)} wierszy): {resp.status_code} {resp.text[:200]}")


def podmien_linki(session, tabela, zapis):
    """Jednorazowo: dla wierszy z już znanym tvmaze_id pobiera prawdziwy adres strony z TVmaze
    (bez wyszukiwania, bez sprawdzania odcinków) i podmienia nim link_epguides."""
    resp = grist(session, "GET", f"/tables/{tabela}/records")
    if resp.status_code != 200:
        print(f"{tabela}: odczyt nie powiódł się ({resp.status_code}) - pomijam.")
        return
    wiersze = [w for w in resp.json().get("records", []) if (w["fields"].get("tvmaze_id") or 0) > 0]
    print(f"\n{tabela}: {len(wiersze)} pozycji z numerem TVmaze do sprawdzenia linku.")
    partia = []
    for nr, w in enumerate(wiersze, start=1):
        f = w["fields"]
        tid = f["tvmaze_id"]
        try:
            dane = tvmaze(session, f"/shows/{tid}")
            link = (dane or {}).get("url") or ""
            if link and link != (f.get("link_epguides") or ""):
                print(f"[{nr}/{len(wiersze)}] {f.get('tytul')}: {f.get('link_epguides') or '(brak)'} -> {link}")
                partia.append({"id": w["id"], "fields": {"link_epguides": link}})
        except Exception as e:
            print(f"[{nr}/{len(wiersze)}] {f.get('tytul')}: BŁĄD {type(e).__name__}: {e}")
        if zapis and len(partia) >= PARTIA_ZAPISU:
            zapisz(session, partia, tabela)
            partia = []
    if zapis:
        zapisz(session, partia, tabela)
    else:
        print(f"  Tryb podglądu - {len(partia)} linków do zmiany, nic nie zapisano.")


def przetworz_liste_zyczen(session, zapis):
    """Uzupełnia Listę życzeń danymi z TVmaze: numer, opis, plakat, rok, kraj i sezony (całość)."""
    resp = grist(session, "GET", "/tables/ListaZyczen/records")
    if resp.status_code != 200:
        print(f"\nLista życzeń: odczyt nie powiódł się ({resp.status_code}) - pomijam.")
        return
    wiersze = resp.json().get("records", [])
    oryginalne_zyczenia = {w["id"]: dict(w["fields"]) for w in wiersze}
    do_zrobienia = []
    for w in wiersze:
        f = w["fields"]
        tid = f.get("tvmaze_id")
        if tid == -1:
            continue
        brak = not tid or not (f.get("opis") or "").strip() or not (f.get("plakat_url") or "").strip() \
            or not (f.get("historia_sezonow") or "").strip()
        if brak or NADPISZ_OPISY:
            do_zrobienia.append(w)
    print(f"\nLista życzeń: {len(wiersze)} pozycji, do uzupełnienia {len(do_zrobienia)}.")
    if not do_zrobienia:
        return
    if zapis:
        zapewnij_kolumny(session, "ListaZyczen", KOLUMNY_ZYCZEN)
    partia = []
    for w in do_zrobienia:
        f = w["fields"]
        tytul = f.get("tytul") or ""
        try:
            tid = f.get("tvmaze_id")
            pola = {}
            if not tid:
                show = szukaj_pokazu(session, tytul, f.get("rok"), f.get("kraj"))
                if not show:
                    print(f"  {tytul}: nie znaleziono w TVmaze.")
                    partia.append({"id": w["id"], "fields": {"tvmaze_id": -1}})
                    continue
                tid = show["id"]
                pola["tvmaze_id"] = tid
            _, odcinki, info = pokaz_i_odcinki(session, tid)
            if info.get("opis") and info["opis"] != (f.get("opis") or "") and (NADPISZ_OPISY or not (f.get("opis") or "").strip()):
                pola["opis"] = info["opis"]
            if info.get("plakat") and info["plakat"] != (f.get("plakat_url") or "") and (NADPISZ_OPISY or not (f.get("plakat_url") or "").strip()):
                pola["plakat_url"] = info["plakat"]
            if info.get("link") and info["link"] != (f.get("link_epguides") or ""):
                pola["link_epguides"] = info["link"]
            if info.get("rok") and not f.get("rok"):
                pola["rok"] = info["rok"]
            if info.get("kraj") and not (f.get("kraj") or "").strip():
                pola["kraj"] = info["kraj"]
            if odcinki and not (f.get("historia_sezonow") or "").strip():
                licz = {}
                for (sez, num, _) in odcinki:
                    licz[sez] = max(licz.get(sez, 0), num)
                pola["historia_sezonow"] = "\n".join(f"[✓] S{pad2(sez)}: {pad2(licz[sez])} odc." for sez in sorted(licz) if sez > 0)
            if pola:
                partia.append({"id": w["id"], "fields": pola})
                print(f"  {tytul}: uzupełniono {', '.join(sorted(pola))}.")
        except Exception as e:
            print(f"  {tytul}: BŁĄD {type(e).__name__}: {e}")
    if zapis:
        for i in range(0, len(partia), PARTIA_ZAPISU):
            zapisz(session, partia[i:i + PARTIA_ZAPISU], "ListaZyczen", oryginalne=oryginalne_zyczenia)
    else:
        print("  Tryb podglądu - nic nie zapisano.")


def main():
    try:
        import requests
    except ImportError:
        print("Biblioteka 'requests' nie jest zainstalowana (pip install requests).")
        return 1
    if "TU_WKLEJ" in GRIST_API_KEY:
        print("Brak klucza Grista (zmienna GRIST_API_KEY).")
        return 1
    if TRYB not in ("podglad", "zapis"):
        print("TRYB musi mieć wartość 'podglad' albo 'zapis'.")
        return 1

    zapis = TRYB == "zapis"
    print(f"=== Tryb: {'ZAPIS' if zapis else 'PODGLĄD (nic nie zostanie zapisane)'} ===")
    session = requests.Session()
    dzis = datetime.now(timezone.utc).date()

    if TYLKO_LINKI:
        for tabela in [TABELA, "ListaZyczen"]:
            podmien_linki(session, tabela, zapis)
        print(f"\n=== Zakończono (tylko linki). Wywołań API Grista: {WYWOLANIA_GRISTA} "
              f"(limit planu Pro: 40 000 na dobę) ===")
        return 0

    try:
        if zapis:
            zapewnij_kolumny(session)
        resp = grist(session, "GET", f"/tables/{TABELA}/records")
        if resp.status_code != 200:
            raise RuntimeError(f"Odczyt tabeli {TABELA} nie powiódł się ({resp.status_code}): {resp.text[:300]}")
        wiersze = resp.json().get("records", [])
    except RuntimeError as e:
        print(f"BŁĄD: {e}")
        return 1
    oryginalne_pola = {w["id"]: dict(w["fields"]) for w in wiersze}
    print(f"Pobrano {len(wiersze)} seriali z Grista.")

    zmienione = tvmaze(session, "/updates/shows", params={"since": "month"}) or {}
    print(f"TVmaze: {len(zmienione)} seriali zmienionych w ostatnim miesiącu.")

    prog_odswiezania = epoka(dzis - timedelta(days=ODSWIEZ_PO_DNIACH))
    do_wyszukania, do_sprawdzenia = [], []
    for w in wiersze:
        f = w["fields"]
        tid = f.get("tvmaze_id")
        if tid == -1:
            continue                                   # oznaczone jako "nie ma w TVmaze"
        if not tid:
            do_wyszukania.append(w)
            continue
        aktywny = f.get("status") in ("oczekiwany", "emitowany")
        if WSZYSTKIE or aktywny or str(tid) in zmienione or not f.get("sprawdzono") or (f.get("sprawdzono") or 0) < prog_odswiezania:
            do_sprawdzenia.append(w)
    kolejka = do_wyszukania + do_sprawdzenia
    if MAKS_POZYCJI > 0:
        kolejka = kolejka[:MAKS_POZYCJI]
    print(f"Do dopasowania: {len(do_wyszukania)}, do sprawdzenia odcinków: {len(do_sprawdzenia)}, "
          f"w tym uruchomieniu: {len(kolejka)}.\n")

    partia_biezaca = []          # zapisy bez zmiany statusu - idą na bieżąco
    zmiany_statusu = []          # [(wiersz, pola_do_zapisu, opis)] - decyzja na końcu
    nieznalezione = []
    ile_zapisanych = 0

    for nr, w in enumerate(kolejka, start=1):
        f = w["fields"]
        tytul = f.get("tytul") or ""
        try:
            tid = f.get("tvmaze_id")
            nowe_pola = {}
            if not tid:
                show = szukaj_pokazu(session, tytul, f.get("rok"), f.get("kraj"))
                if not show:
                    print(f"[{nr}/{len(kolejka)}] {tytul}: nie znaleziono w TVmaze.")
                    nieznalezione.append(tytul)
                    nowe_pola["tvmaze_id"] = -1
                    partia_biezaca.append({"id": w["id"], "fields": nowe_pola})
                    continue
                tid = show["id"]
                nowe_pola["tvmaze_id"] = tid

            status_tv, odcinki, info = pokaz_i_odcinki(session, tid)
            if odcinki is None:
                print(f"[{nr}/{len(kolejka)}] {tytul}: nie udało się pobrać odcinków.")
                continue

            wynik = ocen(f, odcinki, status_tv, dzis)
            zmiana_pol = dict(nowe_pola)
            opis_zmiany = ""
            # opis i plakat z TVmaze: uzupełniamy puste, a przy NADPISZ_OPISY zamieniamy wszystkie
            if info.get("opis") and info["opis"] != (f.get("opis") or "") and (NADPISZ_OPISY or not (f.get("opis") or "").strip()):
                zmiana_pol["opis"] = info["opis"]
            if info.get("plakat") and info["plakat"] != (f.get("plakat_url") or "") and (NADPISZ_OPISY or not (f.get("plakat_url") or "").strip()):
                zmiana_pol["plakat_url"] = info["plakat"]
            if info.get("link") and info["link"] != (f.get("link_epguides") or ""):
                zmiana_pol["link_epguides"] = info["link"]
            if wynik:
                nowa_data = epoka(wynik["kolejny_data"])
                if (f.get("kolejny") or "") != wynik["kolejny"]:
                    zmiana_pol["kolejny"] = wynik["kolejny"]
                if f.get("kolejny_data") != nowa_data:
                    zmiana_pol["kolejny_data"] = nowa_data
                if (f.get("zalegle") or 0) != wynik["zalegle"]:
                    zmiana_pol["zalegle"] = wynik["zalegle"]
                if wynik["nagrane"] != parsuj_nagrane(f.get("odcinki_nagrane")):
                    zmiana_pol["odcinki_nagrane"] = wynik["nagrane_txt"]
                if wynik["historia"] != (f.get("historia_sezonow") or ""):
                    zmiana_pol["historia_sezonow"] = wynik["historia"]
                if wynik["tvm"] != (f.get("odcinki_tvm") or ""):
                    zmiana_pol["odcinki_tvm"] = wynik["tvm"]
                if (status_tv or "") != (f.get("status_tvm") or ""):
                    zmiana_pol["status_tvm"] = status_tv or ""
                if wynik["zakonczone"]:
                    print(f"[{nr}/{len(kolejka)}] {tytul}: sezon zakończony i nagrany -> historia: "
                          + ", ".join(f"S{pad2(s_)} ({ile} odc.)" for s_, ile in wynik["zakonczone"]))
                if wynik["zainicjowano"]:
                    zmiana_pol["nastepny"] = ""      # import wykorzystany - nie wracamy do niego
                    print(f"[{nr}/{len(kolejka)}] {tytul}: utworzono odcinki_nagrane z następnego do nagrania.")
                if wynik["status"] != (f.get("status") or ""):
                    opis_zmiany = f"{f.get('status')} -> {wynik['status']} ({wynik['powod']})"
                    print(f"[{nr}/{len(kolejka)}] {tytul}: STATUS {opis_zmiany}; kolejny {wynik['kolejny'] or '-'}, "
                          f"do nadrobienia {wynik['zalegle']}")
                    zmiany_statusu.append((w, zmiana_pol, wynik["status"], opis_zmiany))
                    continue
            if zmiana_pol or not f.get("sprawdzono"):
                zmiana_pol["sprawdzono"] = epoka(dzis)
                partia_biezaca.append({"id": w["id"], "fields": zmiana_pol})
            if wynik and wynik["kolejny"]:
                print(f"[{nr}/{len(kolejka)}] {tytul}: kolejny {wynik['kolejny']}"
                      f"{' - ' + wynik['kolejny_data'].isoformat() if wynik['kolejny_data'] else ''}, "
                      f"do nadrobienia {wynik['zalegle']}.")
        except Exception as e:
            print(f"[{nr}/{len(kolejka)}] {tytul}: BŁĄD {type(e).__name__}: {e}")
            continue

        if zapis and len(partia_biezaca) >= PARTIA_ZAPISU:
            zapisz(session, partia_biezaca[:PARTIA_ZAPISU], oryginalne=oryginalne_pola)
            ile_zapisanych += PARTIA_ZAPISU
            partia_biezaca = partia_biezaca[PARTIA_ZAPISU:]

    # koniec pętli: reszta zapisów bez statusu
    if zapis:
        zapisz(session, partia_biezaca, oryginalne=oryginalne_pola)
        ile_zapisanych += len(partia_biezaca)

    # zmiany statusów - z bezpiecznikiem
    print(f"\nZmiany statusów do wprowadzenia: {len(zmiany_statusu)}.")
    if zmiany_statusu:
        if not zapis:
            print("Tryb podglądu - statusy nie zostały zmienione.")
        elif LIMIT_ZMIAN_STATUSU and len(zmiany_statusu) > LIMIT_ZMIAN_STATUSU:
            print(f"UWAGA: to więcej niż bezpiecznik ({LIMIT_ZMIAN_STATUSU}). Statusów NIE zmieniam - przejrzyj listę "
                  f"powyżej i uruchom skrypt ręcznie z wyłączonym limitem (LIMIT_ZMIAN_STATUSU=0).")
        else:
            partia = []
            for (w, pola, nowy_status, _) in zmiany_statusu:
                pola = dict(pola)
                pola["status"] = nowy_status
                pola["sprawdzono"] = epoka(dzis)
                partia.append({"id": w["id"], "fields": pola})
                if len(partia) >= PARTIA_ZAPISU:
                    zapisz(session, partia, oryginalne=oryginalne_pola)
                    partia = []
            zapisz(session, partia, oryginalne=oryginalne_pola)
            print("Statusy zmienione.")

    try:
        przetworz_liste_zyczen(session, zapis)
    except Exception as e:
        print(f"\nLista życzeń: BŁĄD {type(e).__name__}: {e}")

    if nieznalezione:
        print(f"\nNie znaleziono w TVmaze ({len(nieznalezione)}) - oznaczone tvmaze_id = -1 "
              f"(żeby spróbować ponownie, wyczyść tę komórkę):")
        for t in nieznalezione:
            print(f"  - {t}")

    print(f"\n=== Zakończono. Wywołań API Grista: {WYWOLANIA_GRISTA} (limit planu Pro: 40 000 na dobę), "
          f"zapytań do TVmaze: {WYWOLANIA_TVMAZE} ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
