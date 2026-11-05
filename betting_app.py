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
             "OddsA", "OddsB", "OddsDraw", "Cutoff", "ScoreA", "ScoreB", "Status"]
USER_COLS = ["User", "PIN", "Credits"]
BET_COLS = ["Week", "User", "GameID", "Pick", "Odds", "Stake", "Result", "Payout", "Placed"]


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
    return get_book().worksheet(name).get_all_records()


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


def outcome(g):
    """Returns 'A', 'B' or 'Draw' after applying the handicap to TeamA."""
    margin = num(g["ScoreA"]) + num(g["Handicap"], 0) - num(g["ScoreB"])
    if margin > 0:
        return "A"
    if margin < 0:
        return "B"
    return "Draw"


# ---------------- SETTLEMENT ----------------
def has_scores(g):
    return num(g["ScoreA"]) is not None and num(g["ScoreB"]) is not None


def result_text(g):
    if str(g["Status"]).strip().lower() == "void":
        return "Void: all stakes refunded"
    h = num(g["Handicap"], 0)
    res = outcome(g)
    if res == "A":
        return f"{g['TeamA']}{hcp_txt(h)} wins"
    if res == "B":
        return f"{g['TeamB']}{hcp_txt(-h)} wins"
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


# ---------------- LOGIN ----------------
if "user" not in st.session_state:
    st.session_state.user = None

with st.sidebar:
    st.header("Account")
    if st.session_state.user:
        st.write(f"Logged in as **{st.session_state.user}**")
        if st.button("Log out"):
            st.session_state.user = None
            st.rerun()
    else:
        mode = st.radio("", ["Log in", "Register"], horizontal=True)
        name = st.text_input("Name").strip()
        pin = st.text_input("PIN", type="password")
        if st.button(mode) and name and pin:
            refresh()
            users = load("Users", USER_COLS)
            match = users[users["User"].str.lower() == name.lower()]
            if mode == "Register":
                if not match.empty:
                    st.error("That name is taken.")
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
                    st.error("Wrong name or PIN.")

# ---------------- DATA ----------------
games = load("Games", GAME_COLS)
users = load("Users", USER_COLS)
bets = load("Bets", BET_COLS)
games["GameID"] = games["GameID"].astype(str)
bets["GameID"] = bets["GameID"].astype(str)

st.title("🏉 Betting League")
tab_bet, tab_board, tab_mine, tab_admin = st.tabs(
    ["🎯 Place bets", "🏆 Leaderboard", "📜 My bets", "🔧 Admin"])

# ---------------- PLACE BETS ----------------
with tab_bet:
    user = st.session_state.user
    if not user:
        st.info("Log in or register in the sidebar to place bets.")
    else:
        me = users[users["User"] == user]
        credits = num(me.iloc[0]["Credits"], 0) if not me.empty else 0
        st.metric("Your credits", f"{credits:g}")

        already = set(bets[bets["User"] == user]["GameID"])
        open_games = [g for _, g in games.iterrows()
                      if is_open(g) and g["GameID"] not in already]

        if not open_games:
            st.write("No games open for betting right now.")
        else:
            with st.form("bets"):
                picks = []
                for g in open_games:
                    h = num(g["Handicap"], 0)
                    opts = {"No bet": None,
                            f"{g['TeamA']}{hcp_txt(h)} @ {num(g['OddsA'])}": ("A", num(g["OddsA"])),
                            f"{g['TeamB']}{hcp_txt(-h)} @ {num(g['OddsB'])}": ("B", num(g["OddsB"]))}
                    if num(g["OddsDraw"]) is not None:
                        opts[f"Draw @ {num(g['OddsDraw'])}"] = ("Draw", num(g["OddsDraw"]))

                    cut = parse_cutoff(g["Cutoff"]).strftime("%d %b %H:%M")
                    st.subheader(f"{g['Sport']}: {g['TeamA']} vs {g['TeamB']}")
                    st.caption(f"Betting closes {cut}")
                    c1, c2 = st.columns([3, 1])
                    choice = c1.radio("Pick", list(opts), key=f"p{g['GameID']}",
                                      horizontal=True, label_visibility="collapsed")
                    stake = c2.number_input("Stake", min_value=0, step=1,
                                            key=f"s{g['GameID']}")
                    if opts[choice] and stake > 0:
                        picks.append((g, *opts[choice], stake))
                    st.divider()
                submitted = st.form_submit_button("Place bets")

            if submitted:
                refresh()
                users_now = load("Users", USER_COLS)
                row = users_now.index[users_now["User"] == user][0]
                credits_now = num(users_now.at[row, "Credits"], 0)
                total = sum(p[3] for p in picks)

                if not picks:
                    st.warning("Pick a team and enter a stake first.")
                elif total > credits_now:
                    st.error(f"You only have {credits_now:g} credits.")
                elif any(not is_open(p[0]) for p in picks):
                    st.error("Betting has closed on one of these games.")
                else:
                    stamp = now().strftime("%Y-%m-%d %H:%M")
                    get_book().worksheet("Bets").append_rows(
                        [[p[0]["Week"], user, p[0]["GameID"], p[1], p[2], p[3], "", "", stamp]
                         for p in picks], value_input_option="RAW")
                    ws_u = get_book().worksheet("Users")
                    col = ws_u.row_values(1).index("Credits") + 1
                    ws_u.update_cell(row + 2, col, round(credits_now - total, 2))
                    refresh()
                    st.success(f"Bets placed: {total:g} credits.")
                    st.rerun()

# ---------------- LEADERBOARD ----------------
with tab_board:
    if users.empty:
        st.write("No players yet.")
    else:
        pending = bets[bets["Result"].astype(str).str.strip() == ""]
        in_play = pending.groupby("User")["Stake"].apply(lambda s: sum(num(x, 0) for x in s))
        board = pd.DataFrame({
            "Player": users["User"],
            "Credits": users["Credits"].apply(lambda x: num(x, 0)),
        })
        board["In play"] = board["Player"].map(in_play).fillna(0)
        board = board.sort_values("Credits", ascending=False)
        board.insert(0, "Rank", range(1, len(board) + 1))
        st.dataframe(board, hide_index=True, use_container_width=True)

# ---------------- MY BETS ----------------
with tab_mine:
    if not st.session_state.user:
        st.info("Log in to see your bets.")
    else:
        mine = bets[bets["User"] == st.session_state.user].merge(
            games[["GameID", "TeamA", "TeamB", "Handicap"]], on="GameID", how="left")
        if mine.empty:
            st.write("You haven't placed any bets yet.")
        else:
            def pick_name(r):
                h = num(r["Handicap"], 0)
                if r["Pick"] == "A":
                    return f"{r['TeamA']}{hcp_txt(h)}"
                if r["Pick"] == "B":
                    return f"{r['TeamB']}{hcp_txt(-h)}"
                return "Draw"
            mine["Game"] = mine["TeamA"] + " vs " + mine["TeamB"]
            mine["Your pick"] = mine.apply(pick_name, axis=1)
            mine["Result"] = mine["Result"].replace("", "Pending")
            st.dataframe(mine[["Week", "Game", "Your pick", "Odds", "Stake", "Result", "Payout"]]
                         .iloc[::-1], hide_index=True, use_container_width=True)

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
        st.caption("Only games with both scores entered (or Status set to Void) appear here.")
        unsettled = set(bets[bets["Result"].astype(str).str.strip() == ""]["GameID"])
        ready = [g for _, g in games.iterrows()
                 if (has_scores(g) and str(g["Status"]).strip().lower() not in ("settled", "void"))
                 or (str(g["Status"]).strip().lower() == "void" and g["GameID"] in unsettled)]
        if not ready:
            st.write("No games ready to settle.")
        else:
            labels = {f"#{g['GameID']} {g['TeamA']} {g['ScoreA']} - {g['ScoreB']} {g['TeamB']}": g
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
