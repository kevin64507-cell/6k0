"""QRify - Telegram QR code bot.
Single file, Python standard library only (no pip install needed).
Needs one environment variable: BOT_TOKEN
"""
import json
import logging
import os
import re
import sqlite3
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

logging.basicConfig(
    level=logging.INFO, stream=sys.stdout, format="%(asctime)s %(levelname)s %(message)s"
)

TOKEN = os.environ.get("BOT_TOKEN", "").strip()
DB_PATH = os.environ.get("DB_PATH", "qrify.db")


# =====================================================================
#  QR CODE ENCODER (pure Python, byte mode, versions 1-40)
# =====================================================================
ECC_ORDER = {"L": 0, "M": 1, "Q": 2, "H": 3}
ECC_FORMAT_BITS = {"L": 1, "M": 0, "Q": 3, "H": 2}

ECC_CODEWORDS_PER_BLOCK = (
    (-1, 7, 10, 15, 20, 26, 18, 20, 24, 30, 18, 20, 24, 26, 30, 22, 24, 28, 30, 28, 28, 28, 28, 30, 30, 26, 28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
    (-1, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24, 24, 28, 28, 26, 26, 26, 26, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28),
    (-1, 13, 22, 18, 26, 18, 24, 18, 22, 20, 24, 28, 26, 24, 20, 30, 24, 28, 28, 26, 30, 28, 30, 30, 30, 30, 28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
    (-1, 17, 28, 22, 16, 22, 28, 26, 26, 24, 28, 24, 28, 22, 24, 24, 30, 28, 28, 26, 28, 30, 24, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
)
NUM_ERROR_CORRECTION_BLOCKS = (
    (-1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 4, 4, 4, 4, 4, 6, 6, 6, 6, 7, 8, 8, 9, 9, 10, 12, 12, 12, 13, 14, 15, 16, 17, 18, 19, 19, 20, 21, 22, 24, 25),
    (-1, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5, 5, 8, 9, 9, 10, 10, 11, 13, 14, 16, 17, 17, 18, 20, 21, 23, 25, 26, 28, 29, 31, 33, 35, 37, 38, 40, 43, 45, 47, 49),
    (-1, 1, 1, 2, 2, 4, 4, 6, 6, 8, 8, 8, 10, 12, 16, 12, 17, 16, 18, 21, 20, 23, 23, 25, 27, 29, 34, 34, 35, 38, 40, 43, 45, 48, 51, 53, 56, 59, 62, 65, 68),
    (-1, 1, 1, 2, 4, 4, 4, 5, 6, 8, 8, 11, 11, 16, 16, 18, 16, 19, 21, 25, 25, 25, 34, 30, 32, 35, 37, 40, 42, 45, 48, 51, 54, 57, 60, 63, 66, 70, 74, 77, 81),
)


def _raw_modules(ver):
    result = (16 * ver + 128) * ver + 64
    if ver >= 2:
        n = ver // 7 + 2
        result -= (25 * n - 10) * n - 55
        if ver >= 7:
            result -= 36
    return result


def _data_codewords(ver, ecc):
    e = ECC_ORDER[ecc]
    return _raw_modules(ver) // 8 - ECC_CODEWORDS_PER_BLOCK[e][ver] * NUM_ERROR_CORRECTION_BLOCKS[e][ver]


def _gf_mul(x, y):
    z = 0
    for i in range(7, -1, -1):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree):
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 2)
    return result


def _rs_remainder(data, divisor):
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_mul(coef, factor)
    return result


def _add_ecc_and_interleave(data, ver, ecc):
    e = ECC_ORDER[ecc]
    num_blocks = NUM_ERROR_CORRECTION_BLOCKS[e][ver]
    block_ecc_len = ECC_CODEWORDS_PER_BLOCK[e][ver]
    raw_cw = _raw_modules(ver) // 8
    num_short = num_blocks - raw_cw % num_blocks
    short_len = raw_cw // num_blocks
    divisor = _rs_divisor(block_ecc_len)
    blocks = []
    k = 0
    for i in range(num_blocks):
        dat = data[k:k + short_len - block_ecc_len + (0 if i < num_short else 1)]
        k += len(dat)
        ecc_bytes = _rs_remainder(dat, divisor)
        if i < num_short:
            dat = dat + [0]
        blocks.append(dat + ecc_bytes)
    result = []
    for i in range(len(blocks[0])):
        for j, blk in enumerate(blocks):
            if i != short_len - block_ecc_len or j >= num_short:
                result.append(blk[i])
    return result


def _alignment_positions(ver):
    if ver == 1:
        return []
    num = ver // 7 + 2
    step = 26 if ver == 32 else (ver * 4 + num * 2 + 1) // (num * 2 - 2) * 2
    result = [ver * 4 + 17 - 7 - i * step for i in range(num - 1)] + [6]
    result.reverse()
    return result


def _format_cells(size, ecc, mask):
    data = ECC_FORMAT_BITS[ecc] << 3 | mask
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    bits = (data << 10 | rem) ^ 0x5412

    def g(i):
        return ((bits >> i) & 1) == 1

    cells = []
    for i in range(6):
        cells.append((8, i, g(i)))
    cells.append((8, 7, g(6)))
    cells.append((8, 8, g(7)))
    cells.append((7, 8, g(8)))
    for i in range(9, 15):
        cells.append((14 - i, 8, g(i)))
    for i in range(8):
        cells.append((size - 1 - i, 8, g(i)))
    for i in range(8, 15):
        cells.append((8, size - 15 + i, g(i)))
    cells.append((8, size - 8, True))
    return cells


def _mask_cond(mask, x, y):
    if mask == 0:
        return (x + y) % 2 == 0
    if mask == 1:
        return y % 2 == 0
    if mask == 2:
        return x % 3 == 0
    if mask == 3:
        return (x + y) % 3 == 0
    if mask == 4:
        return (x // 3 + y // 2) % 2 == 0
    if mask == 5:
        return x * y % 2 + x * y % 3 == 0
    if mask == 6:
        return (x * y % 2 + x * y % 3) % 2 == 0
    return ((x + y) % 2 + x * y % 3) % 2 == 0


def _penalty(mod, size):
    score = 0
    rows = ["".join("1" if c else "0" for c in r) for r in mod]
    cols = ["".join("1" if mod[y][x] else "0" for y in range(size)) for x in range(size)]
    for line in rows + cols:
        run = 1
        for i in range(1, size):
            if line[i] == line[i - 1]:
                run += 1
            else:
                if run >= 5:
                    score += 3 + run - 5
                run = 1
        if run >= 5:
            score += 3 + run - 5
        score += 40 * (line.count("10111010000") + line.count("00001011101"))
    for y in range(size - 1):
        r0, r1 = rows[y], rows[y + 1]
        for x in range(size - 1):
            if r0[x] == r0[x + 1] == r1[x] == r1[x + 1]:
                score += 3
    dark = sum(r.count("1") for r in rows)
    total = size * size
    k = (abs(dark * 20 - total * 10) + total - 1) // total - 1
    return score + k * 10


def make_qr(data, ecc="M"):
    """Encode bytes into a QR code. Returns a square list of lists of bool."""
    ver = None
    for v in range(1, 41):
        cc_bits = 8 if v < 10 else 16
        if len(data) < (1 << cc_bits) and 4 + cc_bits + 8 * len(data) <= _data_codewords(v, ecc) * 8:
            ver = v
            break
    if ver is None:
        raise ValueError("too long")
    cap_bits = _data_codewords(ver, ecc) * 8
    cc_bits = 8 if ver < 10 else 16

    bits = []

    def put(val, n):
        for i in range(n - 1, -1, -1):
            bits.append((val >> i) & 1)

    put(0b0100, 4)
    put(len(data), cc_bits)
    for b in data:
        put(b, 8)
    put(0, min(4, cap_bits - len(bits)))
    put(0, -len(bits) % 8)
    pad = 0xEC
    while len(bits) < cap_bits:
        put(pad, 8)
        pad ^= 0xEC ^ 0x11
    codewords = [0] * (len(bits) // 8)
    for i, b in enumerate(bits):
        codewords[i >> 3] |= b << (7 - (i & 7))
    all_cw = _add_ecc_and_interleave(codewords, ver, ecc)

    size = ver * 4 + 17
    mod = [[False] * size for _ in range(size)]
    fn = [[False] * size for _ in range(size)]

    def setf(x, y, dark):
        mod[y][x] = dark
        fn[y][x] = True

    for i in range(size):
        setf(6, i, i % 2 == 0)
        setf(i, 6, i % 2 == 0)
    for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                dist = max(abs(dx), abs(dy))
                xx, yy = cx + dx, cy + dy
                if 0 <= xx < size and 0 <= yy < size:
                    setf(xx, yy, dist not in (2, 4))
    pos = _alignment_positions(ver)
    n = len(pos)
    for i, ax in enumerate(pos):
        for j, ay in enumerate(pos):
            if (i == 0 and j == 0) or (i == 0 and j == n - 1) or (i == n - 1 and j == 0):
                continue
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    setf(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)
    for x, y, bit in _format_cells(size, ecc, 0):
        setf(x, y, bit)
    if ver >= 7:
        rem = ver
        for _ in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
        vbits = ver << 12 | rem
        for i in range(18):
            bit = ((vbits >> i) & 1) == 1
            a = size - 11 + i % 3
            b = i // 3
            setf(a, b, bit)
            setf(b, a, bit)

    # place data bits in the zigzag order
    i = 0
    total_bits = len(all_cw) * 8
    for right in range(size - 1, 0, -2):
        if right <= 6:
            right -= 1
        for vert in range(size):
            for j in range(2):
                x = right - j
                upward = (right + 1) & 2 == 0
                y = (size - 1 - vert) if upward else vert
                if not fn[y][x] and i < total_bits:
                    mod[y][x] = ((all_cw[i >> 3] >> (7 - (i & 7))) & 1) == 1
                    i += 1

    # pick the best mask
    best = None
    best_score = None
    for m in range(8):
        grid = [row[:] for row in mod]
        for y in range(size):
            for x in range(size):
                if not fn[y][x] and _mask_cond(m, x, y):
                    grid[y][x] = not grid[y][x]
        for x, y, bit in _format_cells(size, ecc, m):
            grid[y][x] = bit
        score = _penalty(grid, size)
        if best_score is None or score < best_score:
            best, best_score = grid, score
    return best


def png_bytes(modules, scale, fg, bg, border=4):
    """Render modules to an indexed-color PNG using only zlib."""
    n = len(modules) + 2 * border
    raw = bytearray()

    def add_row(cells):
        line = b"\x00" + b"".join((b"\x01" if c else b"\x00") * scale for c in cells)
        raw.extend(line * scale)

    for _ in range(border):
        add_row([0] * n)
    for row in modules:
        add_row([0] * border + [1 if m else 0 for m in row] + [0] * border)
    for _ in range(border):
        add_row([0] * n)

    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    px = n * scale
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", px, px, 8, 3, 0, 0, 0))
        + chunk(b"PLTE", bytes(bg) + bytes(fg))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


# =====================================================================
#  SETTINGS / CONTENT BUILDERS
# =====================================================================
COLORS = {
    "black": ("⚫ Classic", (0, 0, 0), (255, 255, 255)),
    "blue": ("🔵 Blue", (29, 78, 216), (255, 255, 255)),
    "green": ("🟢 Green", (21, 128, 61), (255, 255, 255)),
    "red": ("🔴 Red", (185, 28, 28), (255, 255, 255)),
    "purple": ("🟣 Purple", (126, 34, 206), (255, 255, 255)),
}
SIZES = {"small": ("Small", 400), "medium": ("Medium", 700), "large": ("Large", 1000)}
ECC_LEVELS = ["L", "M", "Q", "H"]
DEFAULTS = {"color": "black", "size": "medium", "ecc": "M"}

TLDS = ("com|net|org|io|co|app|dev|me|info|ly|gl|to|ai|xyz|ng|uk|us|edu|gov|biz|tv|shop|site|online|link|store|tech")
URL_RE = re.compile(r"^(?:www\.)?[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\.(?:%s)(?:[/?#]\S*)?$" % TLDS, re.I)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*:", re.I)
COORD_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*[,; ]\s*(-?\d+(?:\.\d+)?)\s*$")


def smart(text):
    """Turn bare links and email addresses into proper scannable links."""
    t = text.strip()
    if SCHEME_RE.match(t) or " " in t or "\n" in t:
        return t
    if EMAIL_RE.match(t):
        return "mailto:" + t
    if URL_RE.match(t):
        return "https://" + t
    return t


def wifi_esc(s):
    for ch in '\\;,:"':
        s = s.replace(ch, "\\" + ch)
    return s


def vesc(s):
    return s.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", " ")


def build_wifi(d):
    out = "WIFI:T:%s;S:%s;" % (d["security"], wifi_esc(d["ssid"]))
    if d["password"] and d["security"] != "nopass":
        out += "P:%s;" % wifi_esc(d["password"])
    return out + ";"


def build_contact(d):
    lines = ["BEGIN:VCARD", "VERSION:3.0", "N:;%s;;;" % vesc(d["name"]), "FN:%s" % vesc(d["name"])]
    if d["company"]:
        lines.append("ORG:%s" % vesc(d["company"]))
    if d["phone"]:
        lines.append("TEL:%s" % d["phone"])
    if d["email"]:
        lines.append("EMAIL:%s" % d["email"])
    if d["website"]:
        lines.append("URL:%s" % d["website"])
    lines.append("END:VCARD")
    return "\n".join(lines)


def build_email(d):
    params = []
    if d["subject"]:
        params.append("subject=" + urllib.parse.quote(d["subject"], safe=""))
    if d["body"]:
        params.append("body=" + urllib.parse.quote(d["body"], safe=""))
    return "mailto:%s%s" % (d["to"], ("?" + "&".join(params)) if params else "")


def build_phone(d):
    return "tel:%s" % d["number"]


def build_sms(d):
    return "SMSTO:%s:%s" % (d["number"], d["message"])


def build_whatsapp(d):
    digits = re.sub(r"\D", "", d["number"])
    url = "https://wa.me/%s" % digits
    if d["message"]:
        url += "?text=" + urllib.parse.quote(d["message"], safe="")
    return url


def build_location(d):
    return "geo:%s,%s" % (d["lat"], d["lon"])


# step = (key, prompt, kind[, choices]); kinds: text, opt, phone, email, coords, choice
FLOWS = {
    "wifi": {"build": build_wifi, "steps": [
        ("ssid", "📶 Send the Wi-Fi network name (SSID):", "text"),
        ("password", "🔑 Send the Wi-Fi password.", "opt"),
        ("security", "🔒 Choose the security type:", "choice", [("WPA / WPA2", "WPA"), ("WEP", "WEP"), ("No password", "nopass")]),
    ]},
    "contact": {"build": build_contact, "steps": [
        ("name", "👤 Send the contact's full name:", "text"),
        ("phone", "📞 Send the phone number.", "opt"),
        ("email", "✉️ Send the email address.", "opt"),
        ("website", "🌐 Send the website.", "opt"),
        ("company", "🏢 Send the company name.", "opt"),
    ]},
    "email": {"build": build_email, "steps": [
        ("to", "✉️ Send the email address:", "email"),
        ("subject", "📝 Send the subject.", "opt"),
        ("body", "💬 Send the message.", "opt"),
    ]},
    "phone": {"build": build_phone, "steps": [
        ("number", "📞 Send the phone number (with country code, e.g. +2348012345678):", "phone"),
    ]},
    "sms": {"build": build_sms, "steps": [
        ("number", "📱 Send the phone number (with country code):", "phone"),
        ("message", "💬 Send the text message.", "opt"),
    ]},
    "whatsapp": {"build": build_whatsapp, "steps": [
        ("number", "🟢 Send the WhatsApp number (with country code, e.g. +2348012345678):", "phone"),
        ("message", "💬 Send a pre-filled message.", "opt"),
    ]},
    "location": {"build": build_location, "steps": [
        ("coords", "📍 Share a location (📎 → Location) or type coordinates like: 4.8156, 7.0498", "coords"),
    ]},
}

# =====================================================================
#  TELEGRAM API (standard library only)
# =====================================================================
def _call(url, data, headers):
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8")).get("result")
    except urllib.error.HTTPError as e:
        logging.error("Telegram error %s: %s", e.code, e.read().decode("utf-8", "ignore"))
        return None


def api(method, **params):
    return _call(
        "https://api.telegram.org/bot%s/%s" % (TOKEN, method),
        json.dumps(params).encode("utf-8"),
        {"Content-Type": "application/json"},
    )


def api_upload(method, fields, file_field, filename, content, mime):
    boundary = "----qrify" + os.urandom(8).hex()
    body = bytearray()
    for k, v in fields.items():
        body += ('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n' % (boundary, k, v)).encode("utf-8")
    body += (
        '--%s\r\nContent-Disposition: form-data; name="%s"; filename="%s"\r\nContent-Type: %s\r\n\r\n'
        % (boundary, file_field, filename, mime)
    ).encode("utf-8")
    body += content + b"\r\n"
    body += ("--%s--\r\n" % boundary).encode("utf-8")
    return _call(
        "https://api.telegram.org/bot%s/%s" % (TOKEN, method),
        bytes(body),
        {"Content-Type": "multipart/form-data; boundary=%s" % boundary},
    )


def btn(text, data):
    return {"text": text, "callback_data": data}


def markup(rows):
    return {"inline_keyboard": rows}


def send(chat_id, text, reply_markup=None):
    params = {"chat_id": chat_id, "text": text}
    if reply_markup:
        params["reply_markup"] = reply_markup
    return api("sendMessage", **params)


# =====================================================================
#  STORAGE
# =====================================================================
def db():
    return sqlite3.connect(DB_PATH)


def init_db():
    folder = os.path.dirname(DB_PATH)
    if folder:
        os.makedirs(folder, exist_ok=True)
    con = db()
    try:
        with con:
            con.execute(
                "CREATE TABLE IF NOT EXISTS settings (user_id INTEGER PRIMARY KEY, color TEXT, size TEXT, ecc TEXT)"
            )
            con.execute(
                "CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, content TEXT)"
            )
    finally:
        con.close()


def get_settings(uid):
    con = db()
    try:
        row = con.execute("SELECT color, size, ecc FROM settings WHERE user_id=?", (uid,)).fetchone()
    finally:
        con.close()
    s = dict(DEFAULTS)
    if row:
        s.update({"color": row[0], "size": row[1], "ecc": row[2]})
    if s["color"] not in COLORS:
        s["color"] = DEFAULTS["color"]
    if s["size"] not in SIZES:
        s["size"] = DEFAULTS["size"]
    if s["ecc"] not in ECC_ORDER:
        s["ecc"] = DEFAULTS["ecc"]
    return s


def set_setting(uid, key, value):
    s = get_settings(uid)
    s[key] = value
    con = db()
    try:
        with con:
            con.execute(
                "INSERT OR REPLACE INTO settings (user_id, color, size, ecc) VALUES (?, ?, ?, ?)",
                (uid, s["color"], s["size"], s["ecc"]),
            )
    finally:
        con.close()


def add_history(uid, content):
    con = db()
    try:
        with con:
            cur = con.execute("INSERT INTO history (user_id, content) VALUES (?, ?)", (uid, content))
            new_id = cur.lastrowid
            con.execute(
                "DELETE FROM history WHERE user_id=? AND id NOT IN "
                "(SELECT id FROM history WHERE user_id=? ORDER BY id DESC LIMIT 20)",
                (uid, uid),
            )
        return new_id
    finally:
        con.close()


def get_history(uid, limit=5):
    con = db()
    try:
        return con.execute(
            "SELECT id, content FROM history WHERE user_id=? ORDER BY id DESC LIMIT ?", (uid, limit)
        ).fetchall()
    finally:
        con.close()


def get_history_item(uid, item_id):
    con = db()
    try:
        row = con.execute("SELECT content FROM history WHERE id=? AND user_id=?", (item_id, uid)).fetchone()
    finally:
        con.close()
    return row[0] if row else None


# =====================================================================
#  QR GENERATION + SENDING
# =====================================================================
def render_png(uid, content):
    """Return (png_bytes, note). Raises ValueError if content is too long."""
    s = get_settings(uid)
    data = content.encode("utf-8")
    levels = ECC_LEVELS[:ECC_LEVELS.index(s["ecc"]) + 1][::-1]  # chosen level, then lower ones
    modules = None
    used = None
    for lvl in levels:
        try:
            modules = make_qr(data, lvl)
            used = lvl
            break
        except ValueError:
            continue
    if modules is None:
        raise ValueError("too long")
    note = "" if used == s["ecc"] else "\nℹ️ Error correction lowered to %s to fit your text." % used
    scale = max(2, SIZES[s["size"]][1] // (len(modules) + 8))
    png = png_bytes(modules, scale, COLORS[s["color"]][1], COLORS[s["color"]][2])
    return png, note


def send_qr(chat_id, uid, content):
    content = content.strip()
    if not content:
        return
    try:
        png, note = render_png(uid, content)
    except ValueError:
        send(chat_id, "😕 That's too long for one QR code (limit is about 2,900 characters). Try something shorter.")
        return
    item_id = add_history(uid, content)
    preview = content if len(content) <= 300 else content[:300] + "…"
    caption = "✅ Your QR code is ready!\n\n%s%s" % (preview, note)
    api_upload(
        "sendPhoto",
        {
            "chat_id": chat_id,
            "caption": caption[:1000],
            "reply_markup": json.dumps(markup([[btn("📁 Send as file", "asfile_%d" % item_id), btn("🎨 Style", "style")]])),
        },
        "photo", "qr.png", png, "image/png",
    )


def send_qr_file(chat_id, uid, item_id):
    content = get_history_item(uid, item_id)
    if content is None:
        send(chat_id, "I couldn't find that QR code anymore. Send the text again.")
        return
    try:
        png, _ = render_png(uid, content)
    except ValueError:
        return
    api_upload("sendDocument", {"chat_id": chat_id, "caption": "📁 QR code (full quality)"},
               "document", "qrcode.png", png, "image/png")


# =====================================================================
#  CONVERSATION FLOWS
# =====================================================================
STATE = {}  # user_id -> {"flow": name, "i": step index, "data": {}}


def ask(chat_id, uid):
    st = STATE[uid]
    step = FLOWS[st["flow"]]["steps"][st["i"]]
    if step[2] == "choice":
        send(chat_id, step[1], markup([[btn(label, "c_" + val) for label, val in step[3]]]))
    elif step[2] == "opt":
        send(chat_id, step[1] + "\n\nSend - to skip.")
    else:
        send(chat_id, step[1])


def begin_flow(chat_id, uid, name):
    STATE[uid] = {"flow": name, "i": 0, "data": {}}
    ask(chat_id, uid)


def advance(chat_id, uid, values):
    """Store the answer(s) for the current step and move on."""
    st = STATE[uid]
    flow = FLOWS[st["flow"]]
    st["data"].update(values)
    st["i"] += 1
    if st["i"] >= len(flow["steps"]):
        data = st["data"]
        del STATE[uid]
        send_qr(chat_id, uid, flow["build"](data))
    else:
        ask(chat_id, uid)


def flow_input(chat_id, uid, text):
    st = STATE[uid]
    step = FLOWS[st["flow"]]["steps"][st["i"]]
    key, kind = step[0], step[2]
    value = text.strip()
    if kind == "choice":
        send(chat_id, "Please tap one of the buttons above, or /cancel.")
    elif kind == "opt":
        advance(chat_id, uid, {key: "" if value == "-" else value})
    elif kind == "phone":
        cleaned = re.sub(r"[^\d+]", "", value)
        if len(re.sub(r"\D", "", cleaned)) < 5:
            send(chat_id, "That doesn't look like a phone number. Try again, e.g. +2348012345678")
        else:
            advance(chat_id, uid, {key: cleaned})
    elif kind == "email":
        if EMAIL_RE.match(value):
            advance(chat_id, uid, {key: value})
        else:
            send(chat_id, "That doesn't look like an email address. Try again.")
    elif kind == "coords":
        m = COORD_RE.match(value)
        if m and abs(float(m.group(1))) <= 90 and abs(float(m.group(2))) <= 180:
            advance(chat_id, uid, {"lat": m.group(1), "lon": m.group(2)})
        else:
            send(chat_id, "Please send coordinates like: 4.8156, 7.0498")
    else:
        if not value:
            return
        advance(chat_id, uid, {key: value})


# =====================================================================
#  COMMANDS
# =====================================================================
HELP_TEXT = (
    "⚡ QRify - instant QR codes\n\n"
    "Just send me any text or link and I'll turn it into a QR code.\n\n"
    "More options:\n"
    "/qr <text> - QR from text (or reply to a message with /qr)\n"
    "/wifi - Wi-Fi login QR\n"
    "/contact - contact card QR\n"
    "/email - email QR\n"
    "/phone - phone call QR\n"
    "/sms - text message QR\n"
    "/whatsapp - WhatsApp chat QR\n"
    "/location - map location QR (or just share a location)\n"
    "/style - colors, size and error correction\n"
    "/history - your recent QR codes\n"
    "/cancel - stop what you're doing"
)


def cmd_start(msg, arg):
    STATE.pop(msg["from"]["id"], None)
    send(msg["chat"]["id"], "👋 Welcome to QRify!\n\n" + HELP_TEXT)


def cmd_qr(msg, arg):
    text = arg
    if not text and msg.get("reply_to_message"):
        text = msg["reply_to_message"].get("text") or msg["reply_to_message"].get("caption") or ""
    if not text:
        send(msg["chat"]["id"], "Send /qr followed by your text, e.g.\n/qr https://example.com\n\nOr reply to any message with /qr.")
        return
    send_qr(msg["chat"]["id"], msg["from"]["id"], smart(text))


def make_flow_cmd(name):
    def handler(msg, arg):
        begin_flow(msg["chat"]["id"], msg["from"]["id"], name)
    return handler


def cmd_cancel(msg, arg):
    if STATE.pop(msg["from"]["id"], None):
        send(msg["chat"]["id"], "Cancelled. Send me any text to make a QR code.")
    else:
        send(msg["chat"]["id"], "Nothing to cancel. Send me any text to make a QR code.")


STYLE_TEXT = (
    "🎨 QR style\n\n"
    "Color and size change how the QR looks.\n"
    "Error correction: higher levels still scan when the code is damaged or has a logo on it, "
    "but hold less text. L = 7%, M = 15%, Q = 25%, H = 30%."
)


def style_markup(uid):
    s = get_settings(uid)

    def mark(cond, label):
        return ("✅ " if cond else "") + label

    colors = [btn(mark(s["color"] == k, v[0]), "set_color_" + k) for k, v in COLORS.items()]
    sizes = [btn(mark(s["size"] == k, v[0]), "set_size_" + k) for k, v in SIZES.items()]
    eccs = [btn(mark(s["ecc"] == k, k), "set_ecc_" + k) for k in ECC_LEVELS]
    return markup([colors[:3], colors[3:], sizes, eccs])


def cmd_style(msg, arg):
    send(msg["chat"]["id"], STYLE_TEXT, style_markup(msg["from"]["id"]))


def cmd_history(msg, arg):
    rows = get_history(msg["from"]["id"], 5)
    if not rows:
        send(msg["chat"]["id"], "No QR codes yet. Send me any text to make one!")
        return
    lines = []
    buttons = []
    for n, (item_id, content) in enumerate(rows, 1):
        short = content.replace("\n", " ")
        lines.append("%d. %s" % (n, short if len(short) <= 50 else short[:50] + "…"))
        buttons.append(btn("🔁 %d" % n, "h_%d" % item_id))
    send(msg["chat"]["id"], "🕘 Your recent QR codes (tap to make again):\n\n" + "\n".join(lines), markup([buttons]))


COMMANDS = {
    "start": cmd_start, "help": cmd_start, "qr": cmd_qr, "cancel": cmd_cancel,
    "style": cmd_style, "history": cmd_history,
    "wifi": make_flow_cmd("wifi"), "contact": make_flow_cmd("contact"),
    "email": make_flow_cmd("email"), "phone": make_flow_cmd("phone"),
    "sms": make_flow_cmd("sms"), "whatsapp": make_flow_cmd("whatsapp"),
    "location": make_flow_cmd("location"),
}

BOT_COMMANDS = [
    ("qr", "QR code from text or a link"),
    ("wifi", "Wi-Fi login QR"),
    ("contact", "Contact card QR"),
    ("email", "Email QR"),
    ("phone", "Phone call QR"),
    ("sms", "Text message QR"),
    ("whatsapp", "WhatsApp chat QR"),
    ("location", "Map location QR"),
    ("style", "Colors, size, error correction"),
    ("history", "Your recent QR codes"),
    ("help", "How to use QRify"),
    ("cancel", "Cancel current action"),
]

DESCRIPTION = (
    "Generate QR codes instantly from text, links, and messages. "
    "Fast, simple, and easy to use anytime. 📱⚡️\n\n"
    "Send any text or link, or make Wi-Fi, contact, email, phone, SMS, WhatsApp and location QR codes. "
    "Choose colors and size. Tap Start!"
)
SHORT_DESCRIPTION = "Instant QR codes from text, links, Wi-Fi, contacts & more. Fast and simple. 📱⚡️"


# =====================================================================
#  UPDATE HANDLERS
# =====================================================================
def on_message(msg):
    if "from" not in msg:
        return
    chat = msg["chat"]
    chat_id = chat["id"]
    uid = msg["from"]["id"]
    private = chat.get("type") == "private"

    if "location" in msg and private:
        STATE.pop(uid, None)
        loc = msg["location"]
        send_qr(chat_id, uid, "geo:%s,%s" % (loc["latitude"], loc["longitude"]))
        return

    text = (msg.get("text") or "").strip()
    if not text:
        return
    if text.startswith("/"):
        parts = text.split(None, 1)
        cmd = parts[0][1:].split("@")[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        handler = COMMANDS.get(cmd)
        if handler:
            handler(msg, arg)
        return
    if not private:
        return  # in groups only commands are handled
    if uid in STATE:
        flow_input(chat_id, uid, text)
    else:
        send_qr(chat_id, uid, smart(text))


def on_callback(cb):
    api("answerCallbackQuery", callback_query_id=cb["id"])
    data = cb.get("data", "")
    message = cb.get("message")
    if not message:
        return
    chat_id = message["chat"]["id"]
    message_id = message["message_id"]
    uid = cb["from"]["id"]

    if data.startswith("c_"):
        st = STATE.get(uid)
        if st:
            step = FLOWS[st["flow"]]["steps"][st["i"]]
            if step[2] == "choice":
                advance(chat_id, uid, {step[0]: data[2:]})
    elif data.startswith("set_"):
        _, key, value = data.split("_", 2)
        valid = {"color": COLORS, "size": SIZES, "ecc": ECC_ORDER}.get(key)
        if valid is not None and value in valid:
            set_setting(uid, key, value)
            api("editMessageText", chat_id=chat_id, message_id=message_id,
                text=STYLE_TEXT, reply_markup=style_markup(uid))
    elif data == "style":
        send(chat_id, STYLE_TEXT, style_markup(uid))
    elif data.startswith("asfile_"):
        send_qr_file(chat_id, uid, int(data[7:]))
    elif data.startswith("h_"):
        content = get_history_item(uid, int(data[2:]))
        if content:
            send_qr(chat_id, uid, content)


def dispatch(update):
    if "message" in update:
        on_message(update["message"])
    elif "callback_query" in update:
        on_callback(update["callback_query"])


def main():
    logging.info("QRify starting (standard library only, no pip needed)")
    if not TOKEN:
        sys.exit("Set the BOT_TOKEN environment variable first.")
    init_db()
    api("deleteWebhook")
    me = api("getMe")
    if me:
        logging.info("Logged in as @%s", me.get("username"))
    else:
        logging.error("Could not log in. Check that BOT_TOKEN is correct.")
    # set the command menu and descriptions automatically (no BotFather steps needed)
    api("setMyCommands", commands=[{"command": c, "description": d} for c, d in BOT_COMMANDS])
    api("setMyDescription", description=DESCRIPTION)
    api("setMyShortDescription", short_description=SHORT_DESCRIPTION)
    logging.info("QRify is running...")

    offset = None
    while True:
        params = {"timeout": 30, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            params["offset"] = offset
        try:
            updates = api("getUpdates", **params)
        except Exception as e:
            logging.error("Network error: %s", e)
            time.sleep(5)
            continue
        if updates is None:
            time.sleep(5)
            continue
        for update in updates:
            offset = update["update_id"] + 1
            try:
                dispatch(update)
            except Exception:
                logging.exception("Error while handling update")


if __name__ == "__main__":
    main()
