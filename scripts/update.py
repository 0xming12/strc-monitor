#!/usr/bin/env python3
"""STRC 监控后台。

由 GitHub Actions 定时运行：读取 Strategy 在 SEC 的文件列表和 8-K 正文、STRC 价格、BTC 和 USDT 行情，
生成网页用的 data/latest.json；出现新信号时开 GitHub Issue 提醒（GitHub 会发邮件或 App 推送）。

只用 Python 标准库，本地也能运行：python3 scripts/update.py
没有 GITHUB_TOKEN 时不会开 Issue，只在终端打印要提醒的内容。
"""
import datetime as dt
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
STATE_PATH = os.path.join(DATA_DIR, "state.json")
LATEST_PATH = os.path.join(DATA_DIR, "latest.json")

CIK = "0001050446"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK%s.json" % CIK
DOC_BASE = "https://www.sec.gov/Archives/edgar/data/1050446/"
LOOKBACK_START = "2026-10-01"   # 只解析这天之后的 8-K
SEC_BASELINE = "2026-11-16"     # 这天及以前的事项类文件已经看过，不再提醒

# 2026-11-08 各优先股的股数，之后按每周 8-K 里的增发和回购累加
BASELINE_ASOF = "2026-11-08"
BASELINE_SHARES = {"STRF": 12840000, "STRC": 79930000, "STRE": 7750000, "STRK": 14021000, "STRD": 14024000}  # STRC 来自 11/08 投资者简报，其余来自 Q2 10-Q
STATED_USD = {"STRF": 100.0, "STRC": 100.0, "STRE": 114.08, "STRK": 100.0, "STRD": 100.0}  # STRE 面值 100 欧元，按 6 月底的 8.841 亿美元 / 775 万股折算
PARSER_VERSION = 4            # 改了解析规则就加 1，旧的解析缓存会作废重算
FIXED_RATES = {"STRF": 10.0, "STRE": 10.0, "STRK": 8.0, "STRD": 10.0}  # STRC 用公告里的利率
CONVERTS_YI = 67.137          # 可转债合计（亿美元），有变动时改这里
CONVERT_INTEREST_YI = 0.346   # 可转债每年利息（亿美元）

TIER_ORDER = {"green": 0, "info": 0, "yellow": 1, "orange": 2, "red": 3}
TIER_LABEL = {"green": "安全", "info": "提示", "yellow": "黄色", "orange": "橙色", "red": "红色"}
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"

# 8-K 事项编号：auto 直接触发；没有 auto 的等你在网页上确认；info 只提示
ITEM_RULES = {
    "1.03": {"tier": "red", "auto": True, "ev": "r_restructure", "text": "破产或接管"},
    "2.04": {"tier": "red", "auto": True, "ev": "r_default", "text": "触发债务加速到期或增加的事件，可能是可转债违约"},
    "4.02": {"tier": "red", "auto": True, "ev": "r_audit", "text": "之前的财报不能再依赖，需要重述"},
    "4.01": {"tier": "red", "ev": "r_audit", "text": "更换审计师。如果是审计师辞任，按红色处理", "confirm": "确认是审计师辞任"},
    "1.05": {"tier": "red", "ev": "r_custody", "text": "重大网络安全事件。如果涉及 BTC 托管，按红色处理", "confirm": "确认涉及 BTC 托管"},
    "2.03": {"tier": "orange", "ev": "o_pledge", "text": "新增直接债务，排在 STRC 前面。用 BTC 抵押的按橙色处理；不管是不是抵押，都把金额填进“新增排在 STRC 前面的债权”", "confirm": "确认是 BTC 抵押借款"},
    "3.03": {"tier": "orange", "ev": "o_terms", "text": "证券持有人的权利有变更，看是不是改了 STRC 的条款", "confirm": "确认对 STRC 不利"},
    "5.03": {"tier": "orange", "ev": "o_terms", "text": "修改公司章程或优先股指定证书，看是不是改了 STRC 条款，或者新设了排在 STRC 前面的优先股", "confirm": "确认对 STRC 不利"},
    "3.01": {"tier": "info", "text": "退市或不符合上市规则的通知"},
    "2.01": {"tier": "info", "text": "完成重大资产收购或出售，可能是大额卖币或并购"},
    "5.01": {"tier": "info", "text": "控制权变更。STRC 持有人可能有权按 100 美元加欠息要求回购"},
}
FORM_RULES = {
    "PRE 14A": {"tier": "orange", "ev": "o_terms", "text": "股东大会初步代理声明，看议案是否涉及修改 STRC 条款", "confirm": "确认对 STRC 不利"},
    "DEF 14A": {"tier": "orange", "ev": "o_terms", "text": "股东大会正式代理声明，看议案是否涉及修改 STRC 条款", "confirm": "确认对 STRC 不利"},
    "PREM14A": {"tier": "info", "text": "并购相关的代理声明"},
    "DEFM14A": {"tier": "info", "text": "并购相关的代理声明"},
    "25-NSE": {"tier": "info", "text": "交易所提交的退市通知"},
    "15-12B": {"tier": "info", "text": "终止证券注册"},
}
DIVIDEND_RULES = {  # 某只优先股过了已宣告的期限还没有新宣告时。STRC 自己走网页的“STRC 分红”指标，不在这里
    "STRF": {"tier": "red", "ev": None, "text": "STRF 没有按时宣告分红。STRF 排在 STRC 前面，它停了 STRC 也很难照付"},
    "STRK": {"tier": "orange", "ev": "o_strk", "text": "STRK 延付股息（没有按时宣告）"},
    "STRD": {"tier": "yellow", "ev": "y_strd", "text": "STRD 停发股息（没有按时宣告）"},
}

warnings = []


def warn(msg):
    warnings.append(msg)
    print("警告：" + msg, file=sys.stderr)


# ---------- 网络 ----------
def sec_contact():
    """SEC 要求自动程序在 User-Agent 里留真实联系邮箱，GitHub 的 noreply 邮箱会被拒绝（403）。"""
    c = os.environ.get("SEC_CONTACT", "").strip()
    return c if re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", c) and "noreply" not in c.lower() else ""


def sec_user_agent():
    return "STRC-Monitor/1.0 (%s)" % sec_contact()


def http_get(url, ua=None, timeout=30, tries=3, as_json=False):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": ua or BROWSER_UA, "Accept": "*/*", "Accept-Encoding": "identity"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read().decode("utf-8", "replace")
            return json.loads(body) if as_json else body
        except Exception as e:  # 网络偶尔失败，重试几次
            last = e
            time.sleep(2 * (i + 1))
    raise last


def sec_get(url, as_json=False):
    time.sleep(0.25)  # SEC 要求每秒不超过 10 次请求
    return http_get(url, ua=sec_user_agent(), as_json=as_json)


# ---------- 解析工具 ----------
class TableParser(HTMLParser):
    """把 HTML 表格转成行列表，每行只保留非空、非“$”的单元格。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables, self._stack, self._row, self._cell = [], [], None, None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._stack.append([])
        elif tag == "tr" and self._stack:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(re.sub(r"\s+", " ", "".join(self._cell)).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None and self._stack:
            cells = [c for c in self._row if c and c not in ("$", "€")]
            if cells:
                self._stack[-1].append(cells)
            self._row = None
        elif tag == "table" and self._stack:
            t = self._stack.pop()
            if t:
                self.tables.append(t)

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def html_to_text(h):
    h = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", h)
    h = re.sub(r"<[^>]+>", " ", h)
    h = html.unescape(h).replace("\u2019", "'").replace("\u201c", '"').replace("\u201d", '"').replace("\xa0", " ")
    return re.sub(r"\s+", " ", h).strip()


def to_num(s):
    if s is None:
        return None
    s = re.sub(r"\(\d+\)", "", str(s)).replace("$", "").replace("€", "").replace(",", "").strip()
    if s in ("-", "—", "–", ""):
        return 0.0
    return float(s) if re.match(r"^-?\d+(\.\d+)?$", s) else None


def money_yi(amount, unit):
    """“$4.75 billion” → 47.5（亿美元）"""
    v = float(amount.replace(",", ""))
    return round(v * (10 if unit.lower().startswith("b") else 0.01), 4)


def iso(date_text):
    return dt.datetime.strptime(date_text.strip(), "%B %d, %Y").date().isoformat()


def clean_label(s):
    return re.sub(r"\s*\(\d+\)", "", s).strip()


SERIES_NAMES = {"Strife": "STRF", "Stretch": "STRC", "Stream": "STRE", "Strike": "STRK", "Stride": "STRD"}


def tickers_in(s):
    """从 “STRF Stock, STRE Stock ...”、“STRC” 或 “Perpetual Strife Preferred Stock” 这类写法里找出代码。"""
    found = set(re.findall(r"\b(STRF|STRC|STRK|STRD|STRE)\b", s))
    for name, tk in SERIES_NAMES.items():
        if re.search(r"Perpetual %s Preferred" % name, s):
            found.add(tk)
    return sorted(found)


def snippet(text, pos, width=160):
    return text[max(0, pos - width // 2): pos + width].strip()


# ---------- 解析单份 8-K ----------
def parse_security_table(tables, first_col_label):
    for t in tables:
        for i, row in enumerate(t):
            if not row or not row[0].startswith("Security") or not any(c.startswith(first_col_label) for c in row):
                continue
            out = {}
            for r in t[i + 1:]:
                m = re.match(r"^(STRF|STRC|STRK|STRD|STRE|MSTR) Stock", r[0])
                if not m or len(r) != len(row):
                    continue
                out[m.group(1)] = {clean_label(lab): to_num(val) for lab, val in zip(row[1:], r[1:])}
            return out
    return None


def parse_filing(h):
    tp = TableParser()
    tp.feed(h)
    text = html_to_text(h)
    rec = {}

    atm = parse_security_table(tp.tables, "Shares Sold")
    if atm is not None:
        rec["atm"] = {k: {"shares": v.get("Shares Sold") or 0, "notional": v.get("Notional Value (in millions)") or 0,
                          "net": v.get("Net Proceeds (in millions)") or 0} for k, v in atm.items()}
    elif re.search(r"did not sell any shares under its at-the-market offering program", text):
        rec["atm"] = {}

    rep = parse_security_table(tp.tables, "Shares Repurchased")
    if rep is not None:
        rec["repurchases"] = {k: {"shares": v.get("Shares Repurchased") or 0, "amount": v.get("Aggregate Purchase Price (in millions)") or 0}
                              for k, v in rep.items()}

    for t in tp.tables:
        for i, row in enumerate(t):
            if any(c.startswith("Aggregate BTC Holdings") for c in row) and i + 1 < len(t) and len(t[i + 1]) == len(row):
                d = {}
                for lab, val in zip(row, t[i + 1]):
                    d.setdefault(clean_label(lab), to_num(val))
                rec["btc"] = {"holdings": d.get("Aggregate BTC Holdings"),
                              "purchased": (d.get("BTC Purchased") or 0) + (d.get("BTC Acquired") or 0),
                              "sold": d.get("BTC Sold") or 0}
    if "btc" not in rec and re.search(r"did not purchase or sell any bitcoin", text):
        rec["btc"] = {"holdings": None, "purchased": 0, "sold": 0}

    # 周报的标志。用来发现“看起来是周报、却没读出数据”的情况
    rec["weeklyMarkers"] = bool(re.search(r"ATM Update|BTC Update|ATM and BTC Update|Repurchase Program Updates|USD Reserve and USD Cash Updates", text))

    # 措辞会变，比如 11/23 的 8-K 在中间插了一句 “after giving effect to the reallocation,”。
    # 所以先找余额那句话，再往前找最近的 “As of 日期”
    m = (re.search(r"balances? of the USD Reserve and USD Cash (?:were|was) \$([\d.,]+) (billion|million),? and \$([\d.,]+) (billion|million)", text)
         or re.search(r"USD Reserve (?:balance )?(?:was|of|totaled) \$([\d.,]+) (billion|million)[^$]{0,120}?USD Cash (?:balance )?(?:was|of|totaled) \$([\d.,]+) (billion|million)", text))
    if m:
        rec["usdReserve"] = money_yi(m.group(1), m.group(2))
        rec["usdCash"] = money_yi(m.group(3), m.group(4))
        dates = re.findall(r"As of (\w+ \d{1,2}, \d{4})", text[max(0, m.start() - 300): m.start()])
        if dates:
            rec["usdAsOf"] = iso(dates[-1])

    # 数据截止日期：优先用余额那句话的日期，没有就用 “During Period A to B” 的 B
    p = re.search(r"(?:During Period|During the period from) (\w+ \d{1,2}, \d{4}) (?:to|through) (\w+ \d{1,2}, \d{4})", text, re.I)
    period_end = iso(p.group(2)) if p else None
    as_of = rec.get("usdAsOf") or period_end
    if as_of and any(k in rec for k in ("atm", "btc", "repurchases", "usdReserve")):
        rec["asOf"] = as_of

    # 例：“used $154.1 million of USD Cash to fund repurchases of STRC Stock and $13.0 million of USD Cash to fund purchases of bitcoin.”
    for pool, key in (("Reserve", "reserveUses"), ("Cash", "cashUses")):
        found = [{"amount": money_yi(u.group(1), u.group(2)), "purpose": u.group(3).strip()}
                 for u in re.finditer(r"\$([\d.,]+) (million|billion) of (?:the )?USD %s to fund (.+?)(?= and \$|\.(?:\s|$)|;)" % pool, text)]
        if found:
            rec[key] = found
    # 储备政策的调整：转到美元现金、降低下限、暂停等。逐句找，关键词在 “USD Reserve” 前后都算
    for sentence in re.split(r"(?<=[a-z0-9)])\.\s+(?=[A-Z(])", text):
        if "USD Reserve" in sentence and re.search(r"realloc|transfer|reduc|amend|modif|suspend|terminat|eliminat|withdraw|release|repurpos|below (?:the )?12", sentence, re.I) \
                and not re.search(r"after giving effect to the realloc", sentence, re.I):
            rec["reservePolicyText"] = sentence.strip()[:400]
            break

    m = re.search(r"\$([\d.,]+) (million|billion) aggregate purchase price (?:of Strategy's preferred stock )?remain(?:s|ed) available under (?:Strategy's |its |the )?digital credit", text, re.I)
    if m:
        rec["buybackRemaining"] = money_yi(m.group(1), m.group(2))

    # 措辞会变：“...rate per annum on STRC Stock for dividends with record dates from A through B at 12.00%”
    # 或 “...rate per annum on the Company's Variable Rate ... Stretch Preferred Stock ("STRC") for dividends ... will be 12.00%”
    for m in re.finditer(r"regular dividend rate per annum on (?:the Company's )?(?:STRC\b|Variable Rate Series A Perpetual Stretch Preferred Stock)", text):
        seg = text[m.start(): m.start() + 700]
        period = re.search(r"record dates from (\w+ \d{1,2}, \d{4}) through (\w+ \d{1,2}, \d{4})", seg)
        if not period:
            continue
        rates = re.findall(r"(\d{1,2}(?:\.\d+)?)%", seg[: period.end() + 120])
        if rates:
            rec["strcRate"] = {"from": iso(period.group(1)), "through": iso(period.group(2)), "rate": float(rates[-1])}
            break

    decl = {}
    for m in re.finditer(r"declared ([a-z\- ]*?)cash dividends on (.{0,200}?)(?: \(| for each| for the| each payable| payable)", text):
        tickers = tickers_in(m.group(2))
        window = text[m.start(): m.start() + 700]
        through = None
        p = re.search(r"from (\w+ \d{1,2}, \d{4}) through (\w+ \d{1,2}, \d{4})", window)
        if p:
            through = iso(p.group(2))
        else:
            p = re.search(r"payable on (\w+ \d{1,2}, \d{4})", window)
            if p:
                through = iso(p.group(1))
        for tk in tickers:
            if through:
                decl[tk] = max(decl.get(tk, through), through)
    for t in tp.tables:  # 季度分红表：Ticker | Period（Quarter ending ...）
        for r in t:
            tk = next((c for c in r if c in ("STRF", "STRC", "STRK", "STRD", "STRE")), None)
            per = next((c for c in r if c.startswith("Quarter ending")), None)
            if tk and per:
                try:
                    d = iso(per.replace("Quarter ending", ""))
                    decl[tk] = max(decl.get(tk, d), d)
                except ValueError:
                    pass
    if decl:
        rec["declared"] = decl

    m = re.search(r"collateral|pledg|secured by|security interest", text, re.I)
    if m:
        rec["collateralText"] = snippet(text, m.start())
    for m in re.finditer(r"Convertible Senior Notes due 20\d\d", text):
        win = text[max(0, m.start() - 250): m.end() + 250]
        if re.search(r"repurchas|redeem|redemption|exchang|settle|convert(?:ed|ing)? into|conversion", win, re.I):
            rec["notesText"] = win.strip()[:400]
            break
    return rec


# ---------- 行情 ----------
def strc_closes():
    """返回 [(日期, 收盘价)]，只含已经收盘的交易日。"""
    j = http_get("https://query1.finance.yahoo.com/v8/finance/chart/STRC?range=1mo&interval=1d", as_json=True)
    r = j["chart"]["result"][0]
    ny = ZoneInfo("America/New_York")
    now = dt.datetime.now(ny)
    out = {}
    for ts, c in zip(r.get("timestamp") or [], r["indicators"]["quote"][0].get("close") or []):
        if c is None:
            continue
        d = dt.datetime.fromtimestamp(ts, ny).date()
        if d == now.date() and (now.hour, now.minute) < (16, 15):
            continue  # 今天还没收盘
        out[d.isoformat()] = round(float(c), 2)
    # Yahoo 有时把最新一根日线的收盘价留空（2026-11-25 收盘后就是这样），这时用 meta 里的收盘价补上。
    # regularMarketTime 在 16:00 以后才算收盘价，盘中的价格不用
    meta = r.get("meta") or {}
    px, t = meta.get("regularMarketPrice"), meta.get("regularMarketTime")
    if px and t:
        mt = dt.datetime.fromtimestamp(t, ny)
        if (mt.hour, mt.minute) >= (16, 0):
            out.setdefault(mt.date().isoformat(), round(float(px), 2))
    return sorted(out.items())


def btc_weekly_closes():
    rows = http_get("https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=86400", as_json=True)
    now = time.time()
    rows = sorted(rows, key=lambda r: -r[0])
    utc = lambda ts: dt.datetime.fromtimestamp(ts, dt.timezone.utc)
    sundays = [r for r in rows if utc(r[0]).weekday() == 6 and r[0] + 86400 <= now]
    return [(utc(r[0]).date().isoformat(), float(r[4])) for r in sundays[:2]], float(rows[0][4])


def usdt_48h_max():
    rows = http_get("https://api.exchange.coinbase.com/products/USDT-USD/candles?granularity=3600", as_json=True)
    since = time.time() - 48 * 3600
    recent = [float(r[4]) for r in rows if r[0] >= since]
    return max(recent) if len(recent) >= 40 else None


# ---------- 推导 ----------
def build(filings_meta, parsed, prices, today):
    rows = sorted([dict(parsed[f["acc"]], acc=f["acc"], filingDate=f["date"], url=f["url"])
                   for f in filings_meta if f["acc"] in parsed and parsed[f["acc"]].get("asOf")],
                  key=lambda r: (r["asOf"], r["filingDate"]))
    by_asof = {}
    for r in rows:  # 同一周有多份（比如 8-K/A 更正）时只用最后一份，避免重复计算
        by_asof[r["asOf"]] = r
    weekly = [by_asof[k] for k in sorted(by_asof)]
    # BTC 持仓：没写持仓的一周沿用上周
    last_hold = None
    for w in weekly:
        b = w.get("btc") or {}
        if b.get("holdings"):
            last_hold = b["holdings"]
        elif last_hold is not None:
            w.setdefault("btc", {})["holdings"] = last_hold
    latest = weekly[-1] if weekly else None

    # 各优先股股数 = 基准 + 之后每周增发 − 回购
    shares = dict(BASELINE_SHARES)
    for w in weekly:
        if w["asOf"] <= BASELINE_ASOF:
            continue
        for k in shares:
            shares[k] += int((w.get("atm") or {}).get(k, {}).get("shares") or 0)
            shares[k] -= int((w.get("repurchases") or {}).get(k, {}).get("shares") or 0)
    prefs = {k: {"shares": v, "notional": round(v * STATED_USD[k] / 1e8, 2)} for k, v in shares.items()}

    # STRC 利率公告和各优先股的分红宣告
    rate_events = sorted([dict(parsed[f["acc"]]["strcRate"], filingDate=f["date"], url=f["url"])
                          for f in filings_meta if parsed.get(f["acc"], {}).get("strcRate")], key=lambda r: (r["from"], r["filingDate"]))
    declared = {}
    for f in filings_meta:
        for k, d in (parsed.get(f["acc"], {}).get("declared") or {}).items():
            declared[k] = max(declared.get(k, d), d)

    # 回购额度低于 0.5 亿美元从哪周开始
    low_since = None
    for w in weekly:
        rem = w.get("buybackRemaining")
        if rem is None:
            continue
        if rem < 0.5:
            low_since = low_since or w["asOf"]
        else:
            low_since = None

    closes = prices.get("strc") or []
    strc_price = closes[-1] if closes else None
    close_on = lambda day: next((c for d, c in reversed(closes) if d <= day), None)

    items = []  # 网页里“自动检测”列表的条目

    def add(key, date, title, rules, url, extra=None):
        it = {"acc": key, "date": date, "form": title, "items": "", "url": url, "rules": rules}
        if extra:
            it.update(extra)
        items.append(it)

    # 1) 8-K 事项编号和代理声明
    for f in filings_meta:
        if f["date"] <= SEC_BASELINE:
            continue
        rules = []
        if f["form"].startswith("8-K"):
            for it in [x.strip() for x in f["items"].split(",") if x.strip()]:
                if it in ITEM_RULES:
                    rules.append(dict(ITEM_RULES[it], code="Item " + it))
        if f["form"] in FORM_RULES:
            rules.append(dict(FORM_RULES[f["form"]], code=f["form"]))
        p = parsed.get(f["acc"], {})
        if any(r["code"] == "Item 2.03" for r in rules) and p.get("collateralText"):
            rules.append({"tier": "orange", "ev": "o_pledge", "code": "正文", "text": "正文里提到抵押或担保：" + p["collateralText"], "confirm": "确认是 BTC 抵押借款"})
        if rules:
            add(f["acc"], f["date"], f["form"], rules, f["url"], {"items": f["items"]})
        # 2) 正文推导出的信号
        if f["date"] > SEC_BASELINE and p.get("notesText"):
            add("notes-" + f["acc"], f["date"], "可转债变动", [{"tier": "info", "code": "正文", "text": "可转债有回购、赎回、交换或转股的描述，请核对资本结构里的可转债金额：" + p["notesText"][:240]}], f["url"])

    for w in weekly:
        if w["filingDate"] <= SEC_BASELINE:
            continue
        stc = (w.get("atm") or {}).get("STRC") or {}
        if stc.get("shares"):
            net_px = stc["net"] * 1e6 / stc["shares"] if stc["shares"] else 0
            tier_txt = "每股净得约 $%.2f" % net_px
            if net_px < 98.5:
                add("atm-" + w["acc"], w["filingDate"], "STRC 增发", [{"tier": "orange", "ev": "o_issue", "code": "ATM", "confirm": "确认是 100 美元以下增发",
                    "text": "这周通过 ATM 卖了 %s 股 STRC，%s，可能低于 100 美元" % (format(int(stc["shares"]), ","), tier_txt)}], w["url"])
        for u in w.get("reserveUses") or []:
            if re.search(r"repurchas|redeem|repay|principal|bitcoin|MSTR|acqui", u["purpose"], re.I):
                add("reserve-" + w["acc"], w["filingDate"], "美元储备用途", [{"tier": "orange", "ev": "o_reserve", "code": "正文", "confirm": "确认储备被挪用",
                    "text": "美元储备用了 %.2f 亿美元，用途：%s" % (u["amount"], u["purpose"][:160])}], w["url"])
                break
        # 11/23 起美元现金里有从美元储备转过来的钱。一周内用在付息和回购优先股以外的地方（买 BTC、回购普通股、还可转债等）
        # 合计超过 0.5 亿美元（约两周的股息）时要你确认；以前也有用美元现金零星买 BTC 的情况，金额小的不提醒
        other = [u for u in (w.get("cashUses") or []) if not re.search(r"dividend|interest|preferred|STR[CFKDE]\b", u["purpose"], re.I)]
        if sum(u["amount"] for u in other) >= 0.5:
            desc = "；".join("%.2f 亿美元用于 %s" % (u["amount"], u["purpose"][:120]) for u in other)
            add("cash-" + w["acc"], w["filingDate"], "美元现金用途", [{"tier": "orange", "ev": "o_reserve", "code": "正文", "confirm": "确认储备被挪用",
                "text": "美元现金这周%s。美元现金里有从美元储备转过来的钱，用在付息和回购优先股以外的地方，可以按“美元储备被挪用”处理" % desc}], w["url"])
        if w.get("reservePolicyText"):
            i = weekly.index(w)
            prev = weekly[i - 1] if i > 0 else {}
            bal = ""
            if w.get("usdReserve") is not None and prev.get("usdReserve") is not None:
                bal = "本周美元储备 %.1f 亿、美元现金 %.1f 亿美元（上周 %.1f 亿、%.1f 亿），两项合计 %.1f 亿（上周 %.1f 亿）。" % (
                    w["usdReserve"], w.get("usdCash") or 0, prev["usdReserve"], prev.get("usdCash") or 0,
                    w["usdReserve"] + (w.get("usdCash") or 0), prev["usdReserve"] + (prev.get("usdCash") or 0))
            add("reserve-policy-" + w["acc"], w["filingDate"], "储备政策", [{"tier": "orange", "ev": "o_reserve", "code": "正文", "confirm": "确认储备被挪用",
                "text": "8-K 提到美元储备政策的调整。" + bal + "原文：" + w["reservePolicyText"][:240]}], w["url"])

    for prev, cur in zip(weekly, weekly[1:]):
        if cur["filingDate"] <= SEC_BASELINE:
            continue
        ph, ch = (prev.get("btc") or {}).get("holdings"), (cur.get("btc") or {}).get("holdings")
        sold = (cur.get("btc") or {}).get("sold") or 0
        if ph and ch and ch < ph and (ph - ch) > sold + 1:
            add("holdings-" + cur["acc"], cur["filingDate"], "BTC 持仓变少", [{"tier": "red", "ev": "r_custody", "code": "正文", "confirm": "确认不是正常卖币",
                "text": "BTC 持仓从 %s 枚降到 %s 枚，比披露的卖出量多少了 %s 枚" % (format(int(ph), ","), format(int(ch), ","), format(int(ph - ch - sold), ","))}], cur["url"])

    for prev, cur in zip(rate_events, rate_events[1:]):
        if cur["filingDate"] <= SEC_BASELINE or cur["rate"] >= prev["rate"]:
            continue
        px = close_on(cur["filingDate"])
        if px is not None and px < 100:
            add("ratecut-" + cur["from"], cur["filingDate"], "STRC 降息", [{"tier": "orange", "ev": "o_ratecut", "auto": True, "code": "利率公告",
                "text": "STRC 利率从 %.2f%% 降到 %.2f%%（%s 起），当时 STRC 收盘 $%.2f，低于 100 美元" % (prev["rate"], cur["rate"], cur["from"], px)}], cur["url"])

    if low_since and strc_price:
        days = (today - dt.date.fromisoformat(low_since)).days
        if days >= 30 and strc_price[1] < 95:
            add("buyback-low-" + low_since, today.isoformat(), "回购额度", [{"tier": "yellow", "ev": "y_buyback", "auto": True, "code": "回购",
                "text": "回购额度从 %s 起低于 0.5 亿美元，已经 %d 天没有追加，STRC 收盘 $%.2f" % (low_since, days, strc_price[1])}], latest["url"] if latest else "")

    div_status = {}
    for k in ("STRC", "STRF", "STRK", "STRD", "STRE"):
        thr = declared.get(k)
        status = "unknown" if not thr else ("late" if today > dt.date.fromisoformat(thr) else ("due" if (dt.date.fromisoformat(thr) - today).days <= 7 else "ok"))
        div_status[k] = {"declaredThrough": thr, "status": status}
        rule = DIVIDEND_RULES.get(k)
        if rule and status == "late":
            add("div-%s-%s" % (k, thr), today.isoformat(), "%s 分红" % k, [dict(rule, auto=True, code="分红宣告",
                text=rule["text"] + "：已宣告到 %s，之后没有新的宣告" % thr)], latest["url"] if latest else "")
    if div_status["STRC"]["status"] == "unknown":
        warn("没有在 8-K 里找到 STRC 的分红宣告")

    strc_rate = None
    if rate_events:
        cur = [r for r in rate_events if r["from"] <= today.isoformat() <= r["through"]]
        strc_rate = (cur or rate_events)[-1]

    return {
        "weekly": latest, "weeklyHistory": weekly[-12:], "prefs": prefs, "strcRate": strc_rate, "rateEvents": rate_events[-6:],
        "dividends": div_status, "buybackLowSince": low_since, "strcPrice": strc_price, "items": items,
    }


def weekly_problems(scan, parsed, w, today):
    """检查周报有没有读全。返回（网页顶部横幅显示的问题 {text, url} 或 None, 警告列表）。"""
    problem, msgs = None, []
    if not w:
        msgs.append("没有解析到每周 8-K 的数据")
    # 比最新周报更晚、看起来也是周报、却没读出数据的 8-K：说明措辞变了，数字会停在旧的一周
    for f in scan:
        p = parsed.get(f["acc"], {})
        if f["form"].startswith("8-K") and p.get("weeklyMarkers") and not p.get("asOf") and (not w or f["date"] > w["filingDate"]):
            msg = "%s 的 8-K 看起来是周报，但后台没读出里面的数字，页面上的数字还停在 %s。请打开这份 8-K 核对。" % (f["date"], w["asOf"] if w else "旧数据")
            msgs.append(msg)
            problem = problem or {"text": msg, "url": f["url"]}
    if w and not problem:
        names = {"btc": "BTC 持仓", "usdReserve": "美元储备和现金", "buybackRemaining": "回购剩余额度"}
        missing = [names[k] for k in ("btc", "usdReserve", "buybackRemaining") if w.get(k) is None]
        if missing:
            msg = "%s 的周报 8-K 里没读出%s，这几项还是上一周的数字。请打开这份 8-K 核对。" % (w["filingDate"], "、".join(missing))
            msgs.append(msg)
            problem = {"text": msg, "url": w["url"]}
        elif (today - dt.date.fromisoformat(w["asOf"])).days > 10:
            msgs.append("最新一份周报 8-K 的数据截至 %s，已经超过 10 天，Strategy 之后没有发新的周报" % w["asOf"])
    return problem, msgs


# ---------- GitHub Issue 提醒 ----------
def gh(method, path, body=None):
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    req = urllib.request.Request("https://api.github.com/repos/%s%s" % (repo, path), method=method,
                                 data=json.dumps(body).encode("utf-8") if body is not None else None,
                                 headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                                          "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "strc-monitor"})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read().decode("utf-8")
        return json.loads(raw) if raw else None


def page_url():
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" not in repo:
        return ""
    owner, name = repo.split("/", 1)
    return "https://%s.github.io/%s/" % (owner.lower(), name)


def notify(notes, state):
    """notes: [(key, tier, title, body)]；同一个 key 只提醒一次。"""
    sent = state.setdefault("notified", {})
    new = [n for n in notes if n[0] not in sent]
    if not new:
        return
    can_post = bool(os.environ.get("GITHUB_TOKEN") and os.environ.get("GITHUB_REPOSITORY"))
    if can_post:
        try:
            gh("POST", "/labels", {"name": "strc-signal", "color": "d73a4a", "description": "STRC 监控提醒"})
        except urllib.error.HTTPError:
            pass  # 标签已存在
    for key, tier, title, body in new:
        full_title = "[%s] %s" % (TIER_LABEL.get(tier, tier), title)
        full_body = body + ("\n\n打开监控页面：" + page_url() if page_url() else "") + "\n\n<!-- strc-key: %s -->" % key
        if can_post:
            try:
                gh("POST", "/issues", {"title": full_title, "body": full_body, "labels": ["strc-signal"]})
            except Exception as e:
                warn("开 Issue 失败：%s" % e)
                continue
        else:
            print("（本地运行，不开 Issue）要提醒：%s" % full_title)
        sent[key] = dt.date.today().isoformat()


def quant_notes(out, prices, state):
    """覆盖倍数、美元储备、USDT 变差时提醒（网页自己也会算，这里只负责推送）。"""
    notes = []
    w, prefs = out.get("weekly"), out["prefs"]
    rate = (out.get("strcRate") or {}).get("rate", 12.0)
    if w and w.get("usdReserve") is not None:
        usd = w["usdReserve"] + (w.get("usdCash") or 0)
        annual = CONVERT_INTEREST_YI + prefs["STRC"]["notional"] * rate / 100 + sum(prefs[k]["notional"] * r / 100 for k, r in FIXED_RATES.items())
        months = usd / annual * 12 if annual else 99
        tier = "orange" if months < 12 else "yellow" if months < 24 else "green"
        last = state.get("reserveTier", "green")
        if TIER_ORDER[tier] > TIER_ORDER[last]:
            notes.append(("reserve-%s-%s" % (tier, w["asOf"]), tier, "美元资产只够付 %.1f 个月的股息和利息" % months,
                          "按 %s 的 8-K，美元储备加现金 %.1f 亿美元，每年股息和利息约 %.1f 亿美元。" % (w["asOf"], usd, annual)))
        state["reserveTier"] = tier
        weekly = prices.get("btcWeekly") or []
        hold = (w.get("btc") or {}).get("holdings")
        if len(weekly) >= 2 and hold:
            claims = CONVERTS_YI + prefs["STRF"]["notional"] + prefs["STRC"]["notional"] - usd
            confirmed = max(weekly[0][1], weekly[1][1])
            cov = hold * confirmed / (claims * 1e8) if claims > 0 else 99
            ctier = "red" if cov < 1.5 else "orange" if cov < 2 else "yellow" if cov < 3 else "green"
            last = state.get("coverageTier", "green")
            if TIER_ORDER[ctier] > TIER_ORDER[last]:
                notes.append(("coverage-%s-%s" % (ctier, weekly[0][0]), ctier, "BTC 覆盖倍数降到 %.2fx" % cov,
                              "最近两个周线收盘 $%s 和 $%s，按较高的一个计算。" % (format(round(weekly[0][1]), ","), format(round(weekly[1][1]), ","))))
            state["coverageTier"] = ctier
    usdt = prices.get("usdtMax48")
    if usdt is not None:
        bad = usdt < 0.98
        if bad and not state.get("usdtBad"):
            notes.append(("usdt-%s" % dt.date.today().isoformat(), "orange", "USDT 连续 48 小时低于 0.98", "48 小时内最高 %.4f。" % usdt))
        state["usdtBad"] = bad
    return notes


# ---------- 主流程 ----------
def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    state = load_json(STATE_PATH, {})
    if not sec_contact():
        msg = ("还没有设置 SEC_CONTACT。SEC 要求自动程序留一个真实的联系邮箱，GitHub 的 noreply 邮箱会被拒绝。"
               "请在仓库 Settings > Secrets and variables > Actions 里新建名为 SEC_CONTACT 的 secret，值填你的邮箱。")
        notify([("setup-sec-contact", "info", "需要设置 SEC_CONTACT 才能读取 SEC 文件", msg)], state)
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)
        print(msg)
        return  # 不算失败，免得每 30 分钟收到一封失败邮件；Issue 只开一次
    if state.get("parserVersion") != PARSER_VERSION:
        state["parsed"] = {}
        state["parserVersion"] = PARSER_VERSION
    parsed = state.setdefault("parsed", {})
    today = dt.datetime.now(ZoneInfo("America/New_York")).date()

    sub = sec_get(SUBMISSIONS_URL, as_json=True)["filings"]["recent"]
    filings = []
    for i, form in enumerate(sub["form"]):
        acc, doc = sub["accessionNumber"][i], sub["primaryDocument"][i]
        folder = DOC_BASE + acc.replace("-", "") + "/"
        filings.append({"acc": acc, "date": sub["filingDate"][i], "form": form, "items": sub["items"][i] or "",
                        "url": folder + (doc or acc + "-index.htm")})

    for f in filings:
        if f["date"] < LOOKBACK_START or f["acc"] in parsed or not f["form"].startswith("8-K"):
            continue
        try:
            h = sec_get(f["url"])
            rec = parse_filing(h)
            # 有的周报放在附件 99.1（新闻稿）里；财报 8-K（Item 2.02）的附件是季度数据，不读，免得重复计算回购
            items = [x.strip() for x in f["items"].split(",")]
            if "2.02" not in items and not rec.get("asOf") and re.search(r"Exhibit 99", h):
                folder = f["url"].rsplit("/", 1)[0] + "/"
                for it in sec_get(folder + "index.json", as_json=True)["directory"]["item"]:
                    name = it.get("name", "")
                    if re.search(r"ex[-_]?99", name, re.I) and name.lower().endswith((".htm", ".html")):
                        ex = parse_filing(sec_get(folder + name))
                        markers = rec.get("weeklyMarkers") or ex.get("weeklyMarkers")
                        for k, v in ex.items():
                            rec.setdefault(k, v)
                        rec["weeklyMarkers"] = bool(markers)
            parsed[f["acc"]] = rec
        except Exception as e:
            warn("解析 %s（%s）失败：%s" % (f["date"], f["acc"], e))
    scan = [f for f in filings if f["date"] >= LOOKBACK_START]

    prices = {}
    try:
        prices["strc"] = strc_closes()
    except Exception as e:
        warn("STRC 价格获取失败：%s" % e)
        old = load_json(LATEST_PATH, {}).get("strc", {})
        if old.get("price"):
            prices["strc"] = [(old["priceDate"], old["price"])]
    try:
        prices["btcWeekly"], _ = btc_weekly_closes()
    except Exception as e:
        warn("BTC 周线获取失败：%s" % e)
    try:
        prices["usdtMax48"] = usdt_48h_max()
    except Exception as e:
        warn("USDT 价格获取失败：%s" % e)

    out = build(scan, parsed, prices, today)
    w = out["weekly"]
    weekly_problem, msgs = weekly_problems(scan, parsed, w, today)
    for msg in msgs:
        warn(msg)

    latest8k = next((f for f in filings if f["form"].startswith("8-K")), None)
    payload = {
        "weekly": {k: w.get(k) for k in ("acc", "filingDate", "asOf", "url", "usdReserve", "usdCash", "buybackRemaining")} if w else None,
        "btcHoldings": ((w or {}).get("btc") or {}).get("holdings"),
        "prefs": out["prefs"],
        "strc": {"rate": (out["strcRate"] or {}).get("rate"), "rateFrom": (out["strcRate"] or {}).get("from"),
                 "rateThrough": (out["strcRate"] or {}).get("through"),
                 "price": out["strcPrice"][1] if out["strcPrice"] else None, "priceDate": out["strcPrice"][0] if out["strcPrice"] else None},
        "dividends": out["dividends"],
        "buybackLowSince": out["buybackLowSince"],
        "rateEvents": out["rateEvents"],
        "history": [{"asOf": h["asOf"], "btcHoldings": (h.get("btc") or {}).get("holdings"), "usdReserve": h.get("usdReserve"),
                     "usdCash": h.get("usdCash"), "buybackRemaining": h.get("buybackRemaining"),
                     "strcRepurchased": ((h.get("repurchases") or {}).get("STRC") or {}).get("shares"),
                     "strcSold": ((h.get("atm") or {}).get("STRC") or {}).get("shares")} for h in out["weeklyHistory"]],
        "latest8K": {k: latest8k[k] for k in ("acc", "date", "form", "items", "url")} if latest8k else None,
        "recentFilings": [{k: f[k] for k in ("acc", "date", "form", "items", "url")} for f in filings[:10]],
        "items": out["items"],
        "secBaseline": SEC_BASELINE,
        "weeklyProblem": weekly_problem,
        "warnings": warnings,
    }
    payload["version"] = hashlib.sha1(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:12]
    payload["checkedDate"] = today.isoformat()
    payload["generatedAt"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    old = load_json(LATEST_PATH, {})
    if old.get("version") != payload["version"] or old.get("checkedDate") != payload["checkedDate"]:
        with open(LATEST_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        print("已更新 data/latest.json（版本 %s）" % payload["version"])
    else:
        print("数据没有变化")

    notes = []
    for it in out["items"]:
        top = max((r["tier"] for r in it["rules"]), key=lambda t: TIER_ORDER.get(t, 0))
        if TIER_ORDER.get(top, 0) == 0:
            continue
        auto = any(r.get("auto") for r in it["rules"])
        body = "\n".join("- %s：%s" % (r["code"], r["text"]) for r in it["rules"])
        body += "\n\n" + ("页面已经自动按%s处理。" % TIER_LABEL[top] if auto else "需要你在页面上确认：点“%s”按%s处理，或者点“看过了，没影响”。" % (
            next((r.get("confirm") for r in it["rules"] if r.get("confirm")), "确认触发"), TIER_LABEL[top])) + ("\n\n文件：" + it["url"] if it.get("url") else "")
        title = "%s %s" % (it["date"], it["form"]) if auto else "待确认：%s %s" % (it["date"], it["form"])
        notes.append((it["acc"], top, title, body))
    notes += quant_notes(out, prices, state)
    sd = out["dividends"].get("STRC", {})
    if sd.get("status") == "late":
        notes.append(("div-STRC-late-%s" % sd["declaredThrough"], "red", "STRC 没有按时宣告分红",
                      "STRC 的分红只宣告到 %s，之后没有看到新的宣告。按规则这是红色信号：全部卖出，不等反弹。请先打开最新的 8-K 核实。" % sd["declaredThrough"]))
    elif sd.get("status") == "due":
        notes.append(("div-STRC-due-%s" % sd["declaredThrough"], "info", "还没看到 STRC 下个月的分红宣告",
                      "STRC 的分红宣告到 %s，离到期不到 7 天。通常每月 14–15 日宣告下个月，过了期限还没有就是红色信号。" % sd["declaredThrough"]))
    lasting = [x for x in warnings if "获取失败" not in x]  # 行情接口偶尔失败不发提醒，网页上会显示
    if lasting:
        key = "warn-" + hashlib.sha1("|".join(sorted(lasting)).encode("utf-8")).hexdigest()[:10]
        notes.append((key, "info", "后台解析有问题，部分数据可能要手动核对", "\n".join("- " + x for x in lasting)))
    notify(notes, state)

    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)


if __name__ == "__main__":
    main()
