import hashlib
from datetime import datetime
from zoneinfo import ZoneInfo

import gspread
import pandas as pd
import streamlit as st
from google.oauth2.service_account import Credentials

# ---------------- SETTINGS ----------------
st.set_page_config(page_title="Betting League", layout="wide")

SHEET_NAME = "Betting League Sheet"   # name of your Google Sheet
START_CREDITS = 100
TZ = ZoneInfo("Africa/Johannesburg")

GAME_COLS = ["GameID", "Week", "Sport", "TeamA", "TeamB", "Handicap",
             "OddsA", "OddsB", "OddsDraw", "Cutoff", "ScoreA", "ScoreB", "Winner", "Status"]
USER_COLS = ["User", "PIN", "Credits"]
BET_COLS = ["Week", "User", "GameID", "Pick", "Odds", "Stake", "Result", "Payout", "Placed"]
LOCK_COLS = ["GameID", "User", "Locked"]   # one row per locked bet


# ---------------- GOOGLE SHEETS ----------------
@st.cache_resource
def get_book():
    scope = ["https://www.googleapis.com/auth/spreadsheets",
             "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(
        dict(st.secrets["connections"]["gsheets"]), scopes=scope)
    return gspread.authorize(creds).open(SHEET_NAME)


@st.cache_data(ttl=30)
def read_tab(name):
    book = get_book()
    try:
        ws = book.worksheet(name)
    except gspread.WorksheetNotFound:
        if name != "BetLocks":
            raise
        # The BetLocks tab is made automatically the first time it is needed
        ws = book.add_worksheet(title="BetLocks", rows=1000, cols=len(LOCK_COLS))
        ws.append_row(LOCK_COLS)
    return ws.get_all_records()


def load(name, cols):
    df = pd.DataFrame(read_tab(name))
    for c in cols:
        if c not in df.columns:
            df[c] = ""
    df = df[cols].astype(object)  # plain columns so numbers and text can mix
    blank = df.apply(lambda r: all(str(v).strip() == "" for v in r), axis=1)
    return df[~blank] if len(df) else df  # index kept: sheet row = index + 2


def refresh():
    st.cache_data.clear()


def overwrite(name, df):
    ws = get_book().worksheet(name)
    ws.clear()
    ws.update(range_name="A1",
              values=[list(df.columns)] + df.astype(str).values.tolist(),
              value_input_option="RAW")


# ---------------- HELPERS ----------------
def num(v, default=None):
    try:
        s = str(v).strip().replace(",", ".")
        return float(s) if s else default
    except ValueError:
        return default


def hash_pin(pin):
    return "h:" + hashlib.sha256(pin.encode()).hexdigest()


def parse_cutoff(v):
    s = str(v).strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M",
                "%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=TZ)
        except ValueError:
            pass
    return None


def now():
    return datetime.now(TZ)


def is_open(g):
    if str(g["Status"]).strip().lower() in ("settled", "void"):
        return False
    cut = parse_cutoff(g["Cutoff"])
    return cut is not None and now() < cut


def hcp_txt(h):
    return "" if not h else f" ({h:+g})"


def winner_value(g):
    """A, B or Draw typed in the Winner column, else None."""
    w = str(g["Winner"]).strip().lower()
    return {"a": "A", "b": "B", "draw": "Draw", "c": "None"}.get(w)


def outcome(g):
    """Returns 'A', 'B' or 'Draw'. Uses the Winner column if filled,
    otherwise the scores with the handicap applied to TeamA."""
    w = winner_value(g)
    if w:
        return w
    margin = num(g["ScoreA"]) + num(g["Handicap"], 0) - num(g["ScoreB"])
    if margin > 0:
        return "A"
    if margin < 0:
        return "B"
    return "Draw"


# ---------------- SETTLEMENT ----------------
def has_scores(g):
    """True when the game has a result: Winner filled, or both scores."""
    if winner_value(g):
        return True
    return num(g["ScoreA"]) is not None and num(g["ScoreB"]) is not None


def result_text(g):
    if str(g["Status"]).strip().lower() == "void":
        return "Void: all stakes refunded"
    h = num(g["Handicap"], 0)
    res = outcome(g)
    if res == "None":
        return "Neither won: all bets lost"
    if res == "A":
        return f"{g['TeamA']}{hcp_txt(h)} wins"
    if res == "B":
        return f"{g['TeamB'] or 'Option B'}{hcp_txt(-h)} wins"
    return "Draw" if num(g["OddsDraw"]) is not None else "Tie on handicap: stakes refunded"


def settle_game(gid):
    """Settles only the bets on one game. Returns number of bets settled."""
    refresh()
    games = load("Games", GAME_COLS)
    users = load("Users", USER_COLS)
    bets = load("Bets", BET_COLS)
    games["GameID"] = games["GameID"].astype(str)
    bets["GameID"] = bets["GameID"].astype(str)

    g_idx = games.index[games["GameID"] == gid][0]
    g = games.loc[g_idx]
    void = str(g["Status"]).strip().lower() == "void"
    if str(g["Status"]).strip().lower() == "settled":
        return 0  # already done by someone else
    if not void and not has_scores(g):
        return None

    payouts, count = {}, 0
    for i, b in bets.iterrows():
        if b["GameID"] != gid or str(b["Result"]).strip():
            continue
        stake = num(b["Stake"], 0)
        if void:
            result, pay = "Void", stake
        else:
            res = outcome(g)
            if res == b["Pick"]:
                result, pay = "Won", round(stake * num(b["Odds"], 0), 2)
            elif res == "Draw" and num(g["OddsDraw"]) is None:
                result, pay = "Push", stake
            else:
                result, pay = "Lost", 0
        bets.at[i, "Result"] = result
        bets.at[i, "Payout"] = pay
        payouts[b["User"]] = payouts.get(b["User"], 0) + pay
        count += 1

    for i, u in users.iterrows():
        if u["User"] in payouts:
            users.at[i, "Credits"] = round(num(u["Credits"], 0) + payouts[u["User"]], 2)

    overwrite("Bets", bets)
    overwrite("Users", users)
    if not void:
        ws = get_book().worksheet("Games")
        col = ws.row_values(1).index("Status") + 1
        ws.update_cell(g_idx + 2, col, "Settled")
    refresh()
    return count


# ---------------- LOOK & FEEL ----------------
import html as _html

def esc(v):
    return _html.escape(str(v))


# ---------------- TEAM LOGOS ----------------
# Logo files go in a "logos" folder next to this file, named like
# south_africa.png (png, jpg, webp or svg). Add a line here for a new team.
import base64, os, re

LOGO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logos")
# stem (logo file name): (short name shown in multis, names to look for)
COUNTRY_INFO = {
    "fiji": ("FIJ", ["fiji", "fij"]),
    "japan": ("JPN", ["japan", "jpn", "jap"]),
    "new_zealand": ("NZ", ["new zealand", "nz", "all blacks"]),
    "south_africa": ("SA", ["south africa", "sa", "rsa", "springboks", "boks"]),
    "england": ("ENG", ["england", "eng"]),
    "wales": ("WAL", ["wales", "wal"]),
    "argentina": ("ARG", ["argentina", "argentinia", "argies", "arg", "pumas"]),
    "australia": ("AUS", ["australia", "aus", "wallabies"]),
    "ireland": ("IRE", ["ireland", "ire", "irl"]),
    "italy": ("ITA", ["italy", "ita"]),
    "scotland": ("SCO", ["scotland", "sco"]),
    "france": ("FRA", ["france", "fra"]),
    "samoa": ("SAM", ["samoa", "sam"]),
    "tonga": ("TON", ["tonga", "ton"]),
    "georgia": ("GEO", ["georgia", "geo"]),
    "portugal": ("POR", ["portugal", "por"]),
    "uruguay": ("URU", ["uruguay", "uru"]),
    "usa": ("USA", ["usa", "united states"]),
    "canada": ("CAN", ["canada", "can"]),
    "romania": ("ROU", ["romania", "rou"]),
    "spain": ("ESP", ["spain", "esp"]),
    "chile": ("CHI", ["chile", "chi"]),
    "namibia": ("NAM", ["namibia", "nam"]),
}

IMG_TYPES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
             "jfif": "image/jpeg", "webp": "image/webp", "svg": "image/svg+xml"}


def _norm(name):
    return name.strip().lower().replace(" ", "_").replace("-", "_")


@st.cache_data(ttl=300)
def logo_files():
    """{stem: file path} for every image in the logos folder.
    'New Zealand.PNG' and 'new_zealand.png' both become 'new_zealand'."""
    found = {}
    if os.path.isdir(LOGO_DIR):
        for f in os.listdir(LOGO_DIR):
            stem, _, ext = f.rpartition(".")
            if stem and ext.lower() in IMG_TYPES:
                found[_norm(stem)] = os.path.join(LOGO_DIR, f)
    return found


def countries():
    """Known countries plus any extra logo file in the folder (e.g. kenya.png),
    so a new nation works just by uploading its logo."""
    info = dict(COUNTRY_INFO)
    for stem in logo_files():
        if stem not in info:
            info[stem] = (stem.replace("_", " ")[:3].upper(), [stem.replace("_", " ")])
    return info


@st.cache_data(ttl=300)
def logo_uri(stem):
    path = logo_files().get(stem)
    if not path:
        return None
    with open(path, "rb") as f:
        mime = IMG_TYPES[path.rpartition(".")[2].lower()]
        return f"data:{mime};base64," + base64.b64encode(f.read()).decode()


def find_country(text):
    """First country in the text: (start, end, stem), or None."""
    hits = []
    for stem, (_, names) in countries().items():
        for n in names:
            m = re.search(rf"\b{re.escape(n)}\b", str(text), re.IGNORECASE)
            if m:
                hits.append((m.start(), m.end(), stem))
    return min(hits) if hits else None


def is_multi(g):
    return str(g["Sport"]).strip().lower() == "multi"


def multi_logos(text):
    """A multi as a row of logos. Each part is split on commas, & or +.
    A part without a logo file is shown as text, so nothing goes missing."""
    out = []
    for part in re.split(r"\s*(?:,|&|\+|\band\b)\s*", str(text)):
        if not part.strip():
            continue
        hit = find_country(part)
        uri = logo_uri(hit[2]) if hit else None
        if uri:
            extra = (part[:hit[0]] + part[hit[1]:]).strip()  # e.g. a handicap like -26.5
            short = countries()[hit[2]][0]
            out.append(f'<span class="leg"><img class="mlogo" src="{uri}" alt="{esc(short)}" '
                       f'title="{esc(part)}"><span class="code">{esc(short)}'
                       f'{f" <em>{esc(extra)}</em>" if extra else ""}</span></span>')
        else:
            out.append(f'<span class="leg">{esc(part)}</span>')
    return "".join(out)


def with_logos(text):
    """Team name with a small logo in front of every country it mentions."""
    t = str(text)
    found = []
    for stem, (_, names) in countries().items():
        for n in names:
            m = re.search(rf"\b{re.escape(n)}\b", t, re.IGNORECASE)
            if m:
                found.append((m.start(), stem))
                break
    imgs = "".join(f'<img class="logo" src="{logo_uri(stem)}" alt="">'
                   for _, stem in sorted(found) if logo_uri(stem))
    return imgs + esc(t)

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@500;600;700&family=Barlow:wght@400;500;600&display=swap');

:root {
  --pitch: #0F3B2E; --turf: #1E6B4F; --chalk: #F5F7F4; --line: #D5DED8;
  --ink: #13201B; --muted: #5B6B64; --flag: #F2B705; --loss: #B4441C;
}
html, body, .stApp, .stApp p, .stApp label, .stApp input, .stApp button {
  font-family: 'Barlow', 'Segoe UI', Arial, sans-serif;
}
.block-container { max-width: 900px; padding-top: 2rem; }
[data-testid="stHeader"] { background: transparent; }

/* Scoreboard header */
.hero {
  background: var(--pitch); color: #fff; border-radius: 10px;
  padding: 22px 26px; display: flex; justify-content: space-between;
  align-items: flex-end; gap: 16px; flex-wrap: wrap;
  border-bottom: 5px solid var(--flag);
}
.hero .league {
  font-family: 'Barlow Condensed', 'Arial Narrow', sans-serif;
  font-weight: 700; font-size: 2.6rem; line-height: 1; letter-spacing: .5px;
}
.hero .sub { color: #B9D3C7; margin-top: 6px; font-size: 1rem; }
.hero .credits {
  font-family: 'Barlow Condensed', 'Arial Narrow', sans-serif; font-weight: 600;
  font-size: 3rem; line-height: 1; color: var(--flag); text-align: right;
}
.hero .clabel { color: #B9D3C7; text-align: right; font-size: .95rem; }

/* Tabs */
.stTabs [data-baseweb="tab-list"] { gap: 4px; border-bottom: 2px solid var(--line); }
.stTabs [data-baseweb="tab"] {
  font-family: 'Barlow Condensed', 'Arial Narrow', sans-serif;
  font-size: 1.15rem; font-weight: 600; padding: 8px 14px; color: var(--muted);
}
.stTabs [aria-selected="true"] { color: var(--pitch) !important; }
.stTabs [data-baseweb="tab-highlight"] { background: var(--pitch); height: 3px; }

/* Match cards */
[data-testid="stVerticalBlockBorderWrapper"] {
  background: #fff; border-color: var(--line) !important;
  border-left: 5px solid var(--turf) !important;
}
.match .sport { color: var(--muted); font-size: .9rem; }
.match .teams {
  font-family: 'Barlow Condensed', 'Arial Narrow', sans-serif;
  font-size: 1.7rem; font-weight: 600; color: var(--ink); line-height: 1.15;
}
.match .teams em { font-style: normal; color: var(--turf); font-size: 1.2rem; margin-left: 4px; }
.match .teams .vs { color: var(--muted); font-size: 1.1rem; margin: 0 10px; }
.match .close { color: var(--muted); font-size: .92rem; margin-top: 2px; }

/* Tables */
.tbl-wrap { overflow-x: auto; }
table.board { width: 100%; border-collapse: collapse; font-size: 1rem; color: var(--ink); }
table.board th {
  text-align: left; font-weight: 600; color: var(--muted); font-size: .9rem;
  padding: 8px 10px; border-bottom: 2px solid var(--line);
}
table.board td { padding: 11px 10px; border-bottom: 1px solid var(--line); background: #fff; }
table.board td.num, table.board th.num { text-align: right; font-variant-numeric: tabular-nums; }
table.board tr.lead td { background: #FFF8E1; }
table.board tr.lead td:first-child { box-shadow: inset 4px 0 0 var(--flag); }
.rank { font-family: 'Barlow Condensed', 'Arial Narrow', sans-serif; font-size: 1.3rem; font-weight: 700; }
.big { font-family: 'Barlow Condensed', 'Arial Narrow', sans-serif; font-size: 1.3rem; font-weight: 600; }
.up { color: var(--turf); font-size: .85rem; margin-left: 4px; }
.down { color: var(--loss); font-size: .85rem; margin-left: 4px; }
img.logo { height: 1.25em; width: auto; vertical-align: -0.2em; margin-right: 6px; }
table.board img.logo { height: 1.15em; margin-right: 4px; }
.multi { display: flex; align-items: center; gap: 10px; margin: 6px 0; flex-wrap: wrap; }
.multi .mlabel {
  font-family: 'Barlow Condensed', 'Arial Narrow', sans-serif; font-weight: 700;
  background: var(--pitch); color: #fff; border-radius: 6px;
  width: 1.7em; height: 1.7em; display: inline-flex; align-items: center; justify-content: center;
}
.multi .mlabel.b { background: var(--turf); }
.multi .odds { color: var(--muted); font-size: .95rem; margin-left: 4px; }
.leg { display: inline-flex; flex-direction: column; align-items: center; gap: 2px;
       font-weight: 600; color: var(--ink); min-width: 2.6em; }
.leg .code { font-family: 'Barlow Condensed', 'Arial Narrow', sans-serif; font-size: .9rem;
             letter-spacing: .5px; line-height: 1; }
.leg em { font-style: normal; color: var(--turf); }
img.mlogo { height: 2.1em; width: auto; border-radius: 4px; }
table.board img.mlogo { height: 1.4em; }
table.board .leg { margin-right: 6px; vertical-align: middle; }
.chip { padding: 2px 9px; border-radius: 20px; font-size: .85rem; font-weight: 600; }
.chip.Won { background: #DDF0E6; color: var(--turf); }
.chip.Lost { background: #F8E3DA; color: var(--loss); }
.chip.Push, .chip.Void, .chip.Pending { background: #ECEFED; color: var(--muted); }
.empty { color: var(--muted); padding: 24px 0; }
h3.week-title {
  font-family: 'Barlow Condensed', 'Arial Narrow', sans-serif; font-weight: 700;
  font-size: 1.5rem; color: var(--pitch); margin: 1rem 0 .4rem;
}
.lockbar { background: #FFF8E1; border-left: 4px solid var(--flag); padding: 10px 14px;
           border-radius: 6px; margin: 4px 0 10px; color: var(--ink); }
.lockhelp { color: var(--muted); font-size: .92rem; margin: 6px 0 2px; }
.game-head { font-family: 'Barlow Condensed', 'Arial Narrow', sans-serif; font-weight: 600;
             font-size: 1.25rem; color: var(--ink); margin: 14px 0 4px; }
.chip.Locked { background: #E3EAF6; color: #2B4A7A; }

.stButton button, .stFormSubmitButton button {
  background: var(--pitch); color: #fff; border: none; font-weight: 600;
}
.stButton button:hover, .stFormSubmitButton button:hover { background: var(--turf); color: #fff; }
</style>
""", unsafe_allow_html=True)


def closes_text(g):
    cut = parse_cutoff(g["Cutoff"])
    left = cut - now()
    days, hrs = left.days, left.seconds // 3600
    mins = (left.seconds % 3600) // 60
    if days > 0:
        rel = f"{days} day{'s' if days > 1 else ''} {hrs} h"
    elif hrs > 0:
        rel = f"{hrs} h {mins} min"
    else:
        rel = f"{mins} min"
    return f"Betting closes {cut.strftime('%a %d %b, %H:%M')} (in {rel})"


def money(v):
    return f"{v:,.2f}".rstrip("0").rstrip(".")


def wk(v):
    """Week as text, so 1, 1.0 and "1" are the same week."""
    n = num(v)
    return str(int(n)) if n is not None and n == int(n) else str(v).strip()


def pick_label(g, pick):
    h = num(g["Handicap"], 0)
    if pick == "A":
        return f"{'A: ' if is_multi(g) else ''}{g['TeamA']}{hcp_txt(h)}"
    if pick == "B":
        return f"{'B: ' if is_multi(g) else ''}{g['TeamB']}{hcp_txt(-h)}"
    return "Draw"


def pick_html(g, pick):
    if is_multi(g) and pick in ("A", "B"):
        return f"{pick}: " + multi_logos(g[f"Team{pick}"])
    return with_logos(pick_label(g, pick))


def game_title_html(g):
    if is_multi(g):
        return "Multi"
    b = f" v {with_logos(g['TeamB'])}" if str(g["TeamB"]).strip() else ""
    return with_logos(g["TeamA"]) + b


def game_card_html(g):
    h = num(g["Handicap"], 0)
    if is_multi(g):
        teams_html = ""
        for side, cls in (("A", ""), ("B", " b")):
            team, odds = g[f"Team{side}"], num(g[f"Odds{side}"])
            if str(team).strip() and odds is not None:
                teams_html += (f'<div class="multi"><span class="mlabel{cls}">{side}</span>'
                               f'{multi_logos(team)}<span class="odds">@ {odds:g}</span></div>')
    else:
        a_h = f"<em>{h:+g}</em>" if h else ""
        b_h = f"<em>{-h:+g}</em>" if h else ""
        vs_part = (f'<span class="vs">v</span>{with_logos(g["TeamB"])}{b_h}'
                   if str(g["TeamB"]).strip() else "")
        teams_html = f'<div class="teams">{with_logos(g["TeamA"])}{a_h}{vs_part}</div>'
    return f"""
<div class="match">
  <div class="sport">{esc(g['Sport'])}</div>
  {teams_html}
  <div class="close">{closes_text(g)}</div>
</div>"""


def save_picks(user, wgames, new, lock):
    """Replaces the user's saved (unlocked) bets on these open games with the new ones.
    Old stakes are given back first. With lock=True the new bets are locked.
    Returns an error message, or None if it worked."""
    refresh()
    users_now = load("Users", USER_COLS)
    bets_now = load("Bets", BET_COLS)
    locks_now = load("BetLocks", LOCK_COLS)
    bets_now["GameID"] = bets_now["GameID"].astype(str)

    gids = {g["GameID"] for g in wgames}
    locked_now = set(locks_now.loc[locks_now["User"] == user, "GameID"].astype(str))
    if gids & locked_now:
        return "One of these bets was already locked. Refresh the page and try again."
    if any(not is_open(g) for g in wgames):
        return "Betting has closed on one of these games. Refresh the page and try again."

    old = bets_now[(bets_now["User"] == user) & bets_now["GameID"].isin(gids)
                   & (bets_now["Result"].astype(str).str.strip() == "")]
    row = users_now.index[users_now["User"] == user][0]
    available = num(users_now.at[row, "Credits"], 0) + sum(num(x, 0) for x in old["Stake"])
    total = sum(p[3] for p in new)
    if total > available:
        return f"These bets total {money(total)}, but you have {money(available)} credits."

    stamp = now().strftime("%Y-%m-%d %H:%M")
    ws_b = get_book().worksheet("Bets")
    for idx in sorted(old.index, reverse=True):  # bottom first so row numbers stay right
        ws_b.delete_rows(int(idx) + 2)
    if new:
        ws_b.append_rows([[g["Week"], user, g["GameID"], pk, odds, stake, "", "", stamp]
                          for g, pk, odds, stake in new], value_input_option="RAW")
    ws_u = get_book().worksheet("Users")
    col = ws_u.row_values(1).index("Credits") + 1
    ws_u.update_cell(int(row) + 2, col, round(available - total, 2))
    if lock and new:
        get_book().worksheet("BetLocks").append_rows(
            [[g["GameID"], user, stamp] for g, *_ in new], value_input_option="RAW")
    refresh()
    return None


# ---------------- DATA ----------------
if "user" not in st.session_state:
    st.session_state.user = None

games = load("Games", GAME_COLS)
users = load("Users", USER_COLS)
bets = load("Bets", BET_COLS)
locks = load("BetLocks", LOCK_COLS)
locks["GameID"] = locks["GameID"].astype(str)
games["GameID"] = games["GameID"].astype(str)
bets["GameID"] = bets["GameID"].astype(str)

# ---------------- AUTO-SETTLE ----------------
# Typing "Settle" in a game's Status column settles it the next time
# anyone opens the app. The app then changes the Status to "Settled".
to_settle = [g["GameID"] for _, g in games.iterrows()
             if str(g["Status"]).strip().lower() == "settle" and has_scores(g)]
if to_settle:
    for gid in to_settle:
        settle_game(gid)
    st.rerun()

user = st.session_state.user
me = users[users["User"] == user] if user else users.iloc[0:0]
credits = num(me.iloc[0]["Credits"], 0) if not me.empty else None

open_all = [g for _, g in games.iterrows() if is_open(g)]
weeks = [num(g["Week"]) for g in open_all if num(g["Week"]) is not None] or \
        [num(w) for w in games["Week"] if num(w) is not None]
week_txt = f"Week {int(max(weeks))}" if weeks else "No games yet"
open_txt = f"{len(open_all)} game{'s' if len(open_all) != 1 else ''} open for betting"

# ---------------- HEADER ----------------
right = (f'<div class="credits">{money(credits)}</div><div class="clabel">{esc(user)}’s credits</div>'
         if user else '<div class="clabel">Log in to play</div>')
st.markdown(f"""
<div class="hero">
  <div><div class="league">Betting League</div>
       <div class="sub">{week_txt}, {open_txt}</div></div>
  <div>{right}</div>
</div>""", unsafe_allow_html=True)

if user:
    _, c = st.columns([5, 1])
    if c.button("Log out", use_container_width=True):
        st.session_state.user = None
        st.rerun()

tab_bet, tab_all, tab_board, tab_mine, tab_admin = st.tabs(
    ["Place bets", "Everyone's picks", "Leaderboard", "My bets", "Admin"])
my_locked = set(locks.loc[locks["User"] == user, "GameID"]) if user else set()  # GameIDs

# ---------------- PLACE BETS ----------------
with tab_bet:
    if not user:
        with st.container(border=True):
            mode = st.radio("Account", ["Log in", "Register"], horizontal=True,
                            label_visibility="collapsed")
            name = st.text_input("Name").strip()
            pin = st.text_input("PIN", type="password")
            if st.button(mode) and name and pin:
                refresh()
                users_now = load("Users", USER_COLS)
                match = users_now[users_now["User"].astype(str).str.lower() == name.lower()]
                if mode == "Register":
                    if not match.empty:
                        st.error("That name is taken. Pick another one.")
                    else:
                        get_book().worksheet("Users").append_row(
                            [name, hash_pin(pin), START_CREDITS], value_input_option="RAW")
                        refresh()
                        st.session_state.user = name
                        st.rerun()
                else:
                    if not match.empty and match.iloc[0]["PIN"] == hash_pin(pin):
                        st.session_state.user = match.iloc[0]["User"]
                        st.rerun()
                    else:
                        st.error("Name or PIN is wrong. Check both and try again.")
    else:
        if st.session_state.get("flash"):
            st.success(st.session_state.pop("flash"))
        open_weeks = sorted({wk(g["Week"]) for g in open_all}, key=lambda w: num(w, 0))
        my_open = bets[(bets["User"] == user) & (bets["Result"].astype(str).str.strip() == "")]
        saved = {b["GameID"]: b for _, b in my_open.iterrows()}

        if not open_weeks:
            st.markdown('<div class="empty">No games to bet on right now. '
                        'Your bets are under My bets.</div>', unsafe_allow_html=True)

        for week in open_weeks:
            wgames = [g for g in open_all if wk(g["Week"]) == week]
            st.markdown(f'<h3 class="week-title">Week {esc(week)}</h3>', unsafe_allow_html=True)

            locked_games = [g for g in wgames if g["GameID"] in my_locked]
            wgames = [g for g in wgames if g["GameID"] not in my_locked]
            for g in locked_games:
                b = saved.get(g["GameID"])
                with st.container(border=True):
                    st.markdown(game_card_html(g) + (
                        f'<div class="lockbar">🔒 Locked: <b>{pick_html(g, b["Pick"])}</b>, '
                        f'{money(num(b["Stake"], 0))} credits. See what everyone picked under '
                        f'<b>Everyone\'s picks</b>.</div>' if b is not None else ""),
                        unsafe_allow_html=True)
            if not wgames:
                continue

            with st.form(f"bets_{week}"):
                new = []
                for g in wgames:
                    with st.container(border=True):
                        st.markdown(game_card_html(g), unsafe_allow_html=True)
                        opts = {"No bet": None}
                        if num(g["OddsA"]) is not None:
                            opts[f"{pick_label(g, 'A')} @ {num(g['OddsA']):g}"] = ("A", num(g["OddsA"]))
                        if num(g["OddsB"]) is not None and str(g["TeamB"]).strip():
                            opts[f"{pick_label(g, 'B')} @ {num(g['OddsB']):g}"] = ("B", num(g["OddsB"]))
                        if num(g["OddsDraw"]) is not None:
                            opts[f"Draw @ {num(g['OddsDraw']):g}"] = ("Draw", num(g["OddsDraw"]))

                        old = saved.get(g["GameID"])
                        idx = 0
                        if old is not None:
                            idx = next((i for i, v in enumerate(opts.values())
                                        if v and v[0] == old["Pick"]), 0)
                        c1, c2 = st.columns([3, 1])
                        choice = c1.radio("Your pick", list(opts), index=idx, key=f"p{g['GameID']}")
                        stake = c2.number_input("Credits", min_value=0, step=1,
                                                value=int(num(old["Stake"], 0)) if old is not None else 0,
                                                key=f"s{g['GameID']}")
                        if old is not None:
                            st.caption(f"Saved: {pick_label(g, old['Pick'])}, "
                                       f"{money(num(old['Stake'], 0))} credits")
                        if opts[choice] and stake > 0:
                            new.append((g, *opts[choice], stake))

                st.markdown('<div class="lockhelp">Save as often as you like until the games start. '
                            'Locking makes the bets you’ve picked final and shows you what everyone '
                            'else picked on those games. Games without a bet stay open.</div>',
                            unsafe_allow_html=True)
                sure = st.checkbox("I’m sure, lock these bets", key=f"sure{week}")
                b1, b2 = st.columns(2)
                save = b1.form_submit_button("Save picks", use_container_width=True)
                lock = b2.form_submit_button("Save and lock 🔒", use_container_width=True)

            if save or lock:
                if lock and not sure:
                    st.warning("Tick “I’m sure” first. Once locked, your bets can’t be changed.")
                elif lock and not new:
                    st.warning("Pick a team and enter credits first. Only games with a bet get locked.")
                else:
                    err = save_picks(user, wgames, new, lock)
                    if err:
                        st.error(err)
                    else:
                        st.session_state["flash"] = ("Bets saved and locked." if lock
                                                     else "Bets saved. You can still change them.")
                        st.rerun()

# ---------------- EVERYONE'S PICKS ----------------
with tab_all:
    if not user:
        st.markdown('<div class="empty">Log in on the Place bets tab to see everyone’s picks.</div>',
                    unsafe_allow_html=True)
    else:
        weeks_all = sorted({wk(g["Week"]) for _, g in games.iterrows()}, key=lambda w: num(w, 0),
                           reverse=True)
        shown = False
        for week in weeks_all:
            wgames = [g for _, g in games.iterrows() if wk(g["Week"]) == week]
            # A game's picks show once you've locked that week, or once betting on it has closed
            visible = [g for g in wgames if g["GameID"] in my_locked or not is_open(g)]
            hidden = len(wgames) - len(visible)
            if not visible and not hidden:
                continue
            st.markdown(f'<h3 class="week-title">Week {esc(week)}</h3>', unsafe_allow_html=True)
            shown = True
            if hidden:
                st.markdown(f'<div class="lockbar">🔒 {hidden} game{"s are" if hidden > 1 else " is"} '
                            f'hidden. Lock your bet on a game (Place bets tab) to see what everyone '
                            f'picked on it.</div>', unsafe_allow_html=True)
            for g in visible:
                gb = bets[bets["GameID"] == g["GameID"]]
                if gb.empty:
                    continue
                rows = ""
                for _, b in gb.iterrows():
                    res = str(b["Result"]).strip()
                    if res:
                        status_chip = f'<span class="chip {res}">{res}</span>'
                    elif ((locks["GameID"] == g["GameID"]) & (locks["User"] == b["User"])).any():
                        status_chip = '<span class="chip Locked">Locked</span>'
                    elif is_open(g):
                        status_chip = '<span class="chip Pending">Can still change</span>'
                    else:
                        status_chip = '<span class="chip Pending">Pending</span>'
                    rows += (f'<tr><td class="big">{esc(b["User"])}</td><td>{pick_html(g, b["Pick"])}</td>'
                             f'<td class="num">{money(num(b["Stake"], 0))}</td><td>{status_chip}</td></tr>')
                st.markdown(f"""<div class="game-head">{game_title_html(g)}</div>
<div class="tbl-wrap"><table class="board"><tr><th>Player</th><th>Pick</th>
<th class="num">Credits</th><th>Status</th></tr>{rows}</table></div>""", unsafe_allow_html=True)
        if not shown:
            st.markdown('<div class="empty">No picks to show yet.</div>', unsafe_allow_html=True)

# ---------------- LEADERBOARD ----------------
with tab_board:
    if users.empty:
        st.markdown('<div class="empty">No players yet. Register on the Place bets tab.</div>',
                    unsafe_allow_html=True)
    else:
        res = bets["Result"].astype(str).str.strip()
        rows = []
        for _, u in users.iterrows():
            ub = bets[bets["User"] == u["User"]]
            ur = res[ub.index]
            won, lost = int((ur == "Won").sum()), int((ur == "Lost").sum())
            in_play = sum(num(x, 0) for x in ub.loc[ur == "", "Stake"])
            rows.append({
                "name": u["User"], "credits": num(u["Credits"], 0),
                "bets": len(ub), "won": won,
                "pct": f"{won / (won + lost) * 100:.0f}%" if won + lost else "–",
                "in_play": in_play,
            })
        rows.sort(key=lambda r: r["credits"] + r["in_play"], reverse=True)

        body = ""
        for i, r in enumerate(rows, 1):
            diff = r["credits"] + r["in_play"] - START_CREDITS
            delta = (f'<span class="up">+{money(diff)}</span>' if diff > 0
                     else f'<span class="down">{money(diff)}</span>' if diff < 0 else "")
            body += f"""<tr class="{'lead' if i == 1 else ''}">
<td class="rank">{i}</td><td class="big">{esc(r['name'])}</td>
<td class="num"><span class="big">{money(r['credits'])}</span>{delta}</td>
<td class="num">{money(r['in_play'])}</td>
<td class="num">{r['bets']}</td><td class="num">{r['won']}</td>
<td class="num">{r['pct']}</td></tr>"""
        st.markdown(f"""<div class="tbl-wrap"><table class="board">
<tr><th></th><th>Player</th><th class="num">Credits</th><th class="num">In play</th>
<th class="num">Bets</th><th class="num">Won</th><th class="num">Win %</th></tr>
{body}</table></div>""", unsafe_allow_html=True)
        st.caption("Ranked by credits plus credits still in play. "
                   "Win % counts settled bets only; refunded bets are left out.")

# ---------------- MY BETS ----------------
with tab_mine:
    if not user:
        st.markdown('<div class="empty">Log in on the Place bets tab to see your bets.</div>',
                    unsafe_allow_html=True)
    else:
        mine = bets[bets["User"] == user].merge(
            games[["GameID", "Sport", "TeamA", "TeamB", "Handicap"]], on="GameID", how="left")
        if mine.empty:
            st.markdown('<div class="empty">You haven’t placed any bets yet.</div>',
                        unsafe_allow_html=True)
        else:
            body = ""
            for _, r in mine.iloc[::-1].iterrows():
                h = num(r["Handicap"], 0)
                pick = (f"{r['TeamA']}{hcp_txt(h)}" if r["Pick"] == "A"
                        else f"{r['TeamB']}{hcp_txt(-h)}" if r["Pick"] == "B" else "Draw")
                if is_multi(r):
                    game_cell = "Multi"
                    pick_cell = f"{esc(r['Pick'])}: " + multi_logos(pick) if r["Pick"] in ("A", "B") else esc(pick)
                else:
                    game_cell = (with_logos(r["TeamA"]) + (" v " + with_logos(r["TeamB"])
                                 if str(r["TeamB"]).strip() else ""))
                    pick_cell = with_logos(pick)
                result = str(r["Result"]).strip() or "Pending"
                pay = money(num(r["Payout"], 0)) if result != "Pending" else ""
                body += f"""<tr><td>{esc(r['Week'])}</td>
<td>{game_cell}</td><td class="big">{pick_cell}</td>
<td class="num">{num(r['Odds'], 0):g}</td><td class="num">{money(num(r['Stake'], 0))}</td>
<td><span class="chip {result}">{result}</span></td><td class="num">{pay}</td></tr>"""
            st.markdown(f"""<div class="tbl-wrap"><table class="board">
<tr><th>Week</th><th>Game</th><th>Your pick</th><th class="num">Odds</th>
<th class="num">Stake</th><th>Result</th><th class="num">Payout</th></tr>
{body}</table></div>""", unsafe_allow_html=True)

# ---------------- ADMIN ----------------
with tab_admin:
    pw = st.text_input("Admin password", type="password")
    if pw and pw == st.secrets.get("admin_password", ""):
        st.write(f"Current time (SA): {now().strftime('%Y-%m-%d %H:%M')}")
        view = games.copy()
        view["Betting"] = view.apply(
            lambda g: "Open" if is_open(g)
            else ("Bad cutoff format" if parse_cutoff(g["Cutoff"]) is None
                  and str(g["Status"]).strip().lower() not in ("settled", "void")
                  else "Closed"), axis=1)
        st.dataframe(view, hide_index=True, use_container_width=True)

        st.subheader("Settle a game")
        st.caption("Games appear here once they have both scores, a Winner (A, B, C or Draw), or Status set to Void.")
        unsettled = set(bets[bets["Result"].astype(str).str.strip() == ""]["GameID"])
        ready = [g for _, g in games.iterrows()
                 if (has_scores(g) and str(g["Status"]).strip().lower() not in ("settled", "void"))
                 or (str(g["Status"]).strip().lower() == "void" and g["GameID"] in unsettled)]
        if not ready:
            st.write("No games ready to settle.")
        else:
            labels = {f"#{g['GameID']} {g['TeamA']}" + (f" v {g['TeamB']}" if str(g['TeamB']).strip() else ""): g
                      for g in ready}
            pick = st.selectbox("Game", list(labels))
            g = labels[pick]
            n_bets = int((bets["GameID"] == g["GameID"]).sum())
            st.write(f"Result: **{result_text(g)}** ({n_bets} bets)")
            if st.button("Settle this game"):
                n = settle_game(g["GameID"])
                st.success(f"Settled {n} bets on game #{g['GameID']}.")
                st.rerun()

        if st.button("Reload data"):
            refresh()
            st.rerun()
    elif pw:
        st.error("Wrong password.")
