"""Turn a schedule sheet into the site's schedule JSON - no AI, nothing leaves the server.

Inputs, all reduced to "positioned words" {t, c, x0, x1, y0, y1}:
  - photo (jpg/png/webp/heic-as-jpg): tiled local OCR (RapidOCR, CPU)
  - PDF: the text layer (exact); a scanned PDF falls back to OCR of the rendered page
  - Word (.docx) / Excel (.xlsx): table cells laid out on a virtual grid (exact)

Then the layout engine:
  1. split the sheet into week blocks (posted week on top, "PROPOSED SCHEDULE" below)
  2. find the day headers (Sunday..Saturday), fit day column positions and the photo's tilt
  3. job rows = words in the label column, snapped to last week's job list (template)
  4. every other word is put in (day, shift, job) by position; names are snapped to the
     roster (GPID list + names seen in past schedules); anything unsure is flagged for review
"""
import difflib
import io
import re
import datetime

DAY_NAMES = ["SUNDAY", "MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY"]
MONTHS = {m: i + 1 for i, m in enumerate(["JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY",
                                         "AUGUST", "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER"])}
SHIFTS_8 = [{"key": "D", "label": "Days", "hours": "7a-3p"}, {"key": "A", "label": "Afternoons", "hours": "3p-11p"},
            {"key": "M", "label": "Midnights", "hours": "11p-7a"}]
SHIFTS_12 = [{"key": "D", "label": "Day crew", "hours": "7a-7p"}, {"key": "N", "label": "Night crew", "hours": "7p-7a"}]
NOTE_RE = re.compile(r"^\(?\d{1,2}\s*[AP]\s*-\s*\d{1,2}\s*[AP]\)?$", re.I)      # 7A-11P
SKIP_RE = re.compile(r"^(NS|N5|N\$|NB|MS|-|~|[\d\s-]+|6-\S*|CONVERT ?TO ?PDF|D|A|M|N|#REF!?)$", re.I)


def norm(s):
    return re.sub(r"[^A-Z0-9]", "", s.upper())


# --- input readers --------------------------------------------------------------------------------

def words_from_image(img, ocr=None):
    """Tiled OCR at 3x so the small print is readable; overlapping tiles, duplicates dropped."""
    import numpy as np
    from PIL import Image
    if ocr is None:
        from rapidocr_onnxruntime import RapidOCR
        ocr = RapidOCR(intra_op_num_threads=3)
    TW, TH, OV, SC = 520, 420, 90, 3
    W, H = img.size
    found = []
    for y in range(0, H, TH - OV):
        for x in range(0, W, TW - OV):
            box = (x, y, min(x + TW, W), min(y + TH, H))
            tile = img.crop(box).resize(((box[2] - box[0]) * SC, (box[3] - box[1]) * SC), Image.LANCZOS)
            res, _ = ocr(np.array(tile))
            for quad, text, conf in res or []:
                xs = [p[0] / SC + x for p in quad]; ys = [p[1] / SC + y for p in quad]
                found.append({"t": text, "c": float(conf), "x0": min(xs), "x1": max(xs), "y0": min(ys), "y1": max(ys)})
    found.sort(key=lambda w: -w["c"])
    kept = []
    for w in found:
        a = (w["x1"] - w["x0"]) * (w["y1"] - w["y0"])
        if not any(_overlap(w, k) > 0.4 * min(a, (k["x1"] - k["x0"]) * (k["y1"] - k["y0"])) for k in kept):
            kept.append(w)
    return kept


def _overlap(a, b):
    ix = min(a["x1"], b["x1"]) - max(a["x0"], b["x0"]); iy = min(a["y1"], b["y1"]) - max(a["y0"], b["y0"])
    return ix * iy if ix > 0 and iy > 0 else 0


def words_from_pdf(data):
    """Text layer of every page (stacked top to bottom). Returns (words, page_images or None)."""
    import pdfplumber
    words, off = [], 0
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            for w in page.extract_words(keep_blank_chars=True, x_tolerance=1.5, y_tolerance=2):
                words.append({"t": w["text"], "c": 1.0, "x0": w["x0"] * 4, "x1": w["x1"] * 4,
                              "y0": (w["top"] + off) * 4, "y1": (w["bottom"] + off) * 4})
            off += page.height + 20
    if len(words) >= 30:
        return words, None
    import pypdfium2 as pdfium                       # scanned PDF: render and OCR
    pdf = pdfium.PdfDocument(data)
    imgs = [pdf[i].render(scale=4).to_pil().convert("RGB") for i in range(len(pdf))]
    return None, imgs


def words_from_grid(rows):
    """rows = list of lists of cell text (Excel sheet / Word table) -> words on a virtual grid."""
    words = []
    for r, row in enumerate(rows):
        for c, cell in enumerate(row):
            for k, line in enumerate(str(cell or "").splitlines()):
                if line.strip():
                    y = r * 60 + k * 14
                    words.append({"t": line.strip(), "c": 1.0, "x0": c * 300, "x1": c * 300 + 280, "y0": y, "y1": y + 12})
    return words


def words_from_xlsx(data):
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    ws = wb.worksheets[0]
    return words_from_grid([[c.value for c in row] for row in ws.iter_rows()])


def words_from_docx(data):
    import docx
    d = docx.Document(io.BytesIO(data))
    rows = []
    for p in d.paragraphs:
        if p.text.strip():
            rows.append([p.text])
    for t in d.tables:
        for row in t.rows:
            rows.append([cell.text for cell in row.cells])
    return words_from_grid(rows)


def read_any(filename, data):
    """-> (words, image or None). image is used for yellow-highlight detection on photos."""
    name = filename.lower()
    if name.endswith(".pdf"):
        words, imgs = words_from_pdf(data)
        if words is not None:
            return words, None
        img = imgs[0]
        return words_from_image(img), img
    if name.endswith(".xlsx") or name.endswith(".xlsm"):
        return words_from_xlsx(data), None
    if name.endswith(".docx"):
        return words_from_docx(data), None
    from PIL import Image, ImageOps
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    return words_from_image(img), img


# --- layout engine ------------------------------------------------------------------------------

def _cx(w): return (w["x0"] + w["x1"]) / 2
def _cy(w): return (w["y0"] + w["y1"]) / 2


def _day_index(text):
    t = norm(text)
    for i, d in enumerate(DAY_NAMES):
        if len(t) >= 5 and (d.startswith(t[:5]) or difflib.SequenceMatcher(None, t, d).ratio() > 0.8):
            return i
    return None


def _fit(points):
    """least squares y = a + b*x"""
    n = len(points)
    if n == 1:
        return points[0][1], 0.0
    mx = sum(p[0] for p in points) / n; my = sum(p[1] for p in points) / n
    sxx = sum((p[0] - mx) ** 2 for p in points) or 1
    b = sum((p[0] - mx) * (p[1] - my) for p in points) / sxx
    return my - b * mx, b


def _date_under(heads, words):
    """Read 'October 25, 2026'-style dates printed just under day headers (OCR may garble letters)."""
    for i, h in heads:
        for w in words:
            if 0 < _cy(w) - _cy(h) < 60 and abs(_cx(w) - _cx(h)) < 150:
                m = re.search(r"([A-Za-z]{3,})\s*(\d{1,2})\s*,?\s*(\d{4})?", w["t"])
                if not m:
                    continue
                month = difflib.get_close_matches(m.group(1).upper(), MONTHS.keys(), n=1, cutoff=0.5)
                if not month:
                    continue
                # OCR misreads years (2026 -> 2025): take the year whose date falls on this header's weekday
                this = datetime.date.today().year
                years = ([int(m.group(3))] if m.group(3) else []) + [this, this + 1, this - 1]
                for year in years:
                    try:
                        d = datetime.date(year, MONTHS[month[0]], int(m.group(2)))
                    except ValueError:
                        continue
                    if (d.weekday() + 1) % 7 == i:
                        return d - datetime.timedelta(days=i)
    return None


def _week_start(words, block_words, fallback):
    """Sunday of the week: from a 'Month D, YYYY' date under a day header, or (MM/DD/YYYY) in the title."""
    text = " ".join(w["t"] for w in block_words)
    m = re.search(r"(JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER)\s*(\d{1,2})\s*,?\s*(20\d\d)", text.upper())
    if m:
        d = datetime.date(int(m.group(3)), MONTHS[m.group(1)], int(m.group(2)))
        return d - datetime.timedelta(days=(d.weekday() + 1) % 7)
    m = re.search(r"(\d{1,2})/(\d{1,2})/(20\d\d)", text)
    if m:
        d = datetime.date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
        return d - datetime.timedelta(days=(d.weekday() + 1) % 7)
    return fallback


def _blocks(words):
    """Split into [posted week, proposed week] by the 'PROPOSED' line if there is one."""
    prop = [w for w in words if "PROPOSED" in w["t"].upper()]
    if not prop:
        return [("posted", words)]
    y = min(_cy(w) for w in prop)
    return [("posted", [w for w in words if _cy(w) < y - 5]), ("proposed", [w for w in words if _cy(w) > y + 5])]


def _core(s):
    """'Baking Mix Relief (6-2-10)' -> 'BAKINGMIXRELIEF' (drop codes/hours in brackets or loose)"""
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(r"\b\d+\s*-\s*\d+(\s*-\s*\d+)?\b", " ", s)
    return norm(s)


class Snapper:
    """Snap OCR text to a known list (job labels or people) by closest spelling."""
    def __init__(self, known, cutoff, strip=False):
        self.key = _core if strip else norm
        self.by_key = {}
        for k in known:
            self.by_key.setdefault(self.key(k), k)
        self.cutoff = cutoff

    def __call__(self, text):
        q = self.key(text)
        if not q:
            return None, 0.0
        if q in self.by_key:
            return self.by_key[q], 1.0
        m = difflib.get_close_matches(q, self.by_key.keys(), n=1, cutoff=self.cutoff)
        if not m:
            return None, 0.0
        return self.by_key[m[0]], difflib.SequenceMatcher(None, q, m[0]).ratio()


class RuledLines:
    """Horizontal ruled lines of the table, per vertical strip. Used to correct each column's rows
    against the label column (photo angle, perspective and paper curl differ across the sheet)."""
    def __init__(self, img):
        import numpy as np, cv2
        g = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2GRAY)
        th = cv2.adaptiveThreshold(g, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 31, 12)
        self.mask = cv2.morphologyEx(th, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (45, 1)))
        self.vmask = cv2.morphologyEx(th, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, 45)))

    def xs(self, x0, x1, y0, y1):
        """x positions of vertical ruled lines crossing the band y0..y1"""
        x0, x1, y0, y1 = max(0, int(x0)), int(x1), max(0, int(y0)), int(y1)
        band = self.vmask[y0:y1, x0:x1]
        if band.size == 0:
            return []
        prof = (band > 0).sum(axis=0) / max(1, band.shape[0])
        out, run = [], []
        for i, v in enumerate(prof):
            if v > 0.5:
                run.append(i)
            elif run:
                out.append(x0 + sum(run) / len(run)); run = []
        if run:
            out.append(x0 + sum(run) / len(run))
        return out

    def ys(self, x0, x1, y0, y1):
        import numpy as np
        x0, x1, y0, y1 = max(0, int(x0)), int(x1), max(0, int(y0)), int(y1)
        strip = self.mask[y0:y1, x0:x1]
        if strip.size == 0:
            return []
        prof = (strip > 0).sum(axis=1) / max(1, strip.shape[1])
        out, run = [], []
        for i, v in enumerate(prof):
            if v > 0.55:
                run.append(i)
            elif run:
                out.append(y0 + sum(run) / len(run)); run = []
        if run:
            out.append(y0 + sum(run) / len(run))
        return out


def _offset_map(ref, col, guess, tol):
    """Pair the column's ruled lines with the label column's -> sorted [(y_in_column, shift)]."""
    pairs, used = [], set()
    for y in col:
        best = min(((abs((y - guess) - r), k) for k, r in enumerate(ref) if k not in used), default=None)
        if best and best[0] < tol:
            used.add(best[1]); pairs.append((y, y - ref[best[1]]))
    pairs.sort()
    # drop pairs whose shift jumps too far from their neighbours (wrong matches)
    clean = [p for i, p in enumerate(pairs)
             if not (0 < i < len(pairs) - 1 and abs(p[1] - pairs[i - 1][1]) > tol * 0.6 and abs(p[1] - pairs[i + 1][1]) > tol * 0.6)]
    return clean


def _shift_at(pairs, y, default):
    if not pairs:
        return default
    if y <= pairs[0][0]:
        return pairs[0][1]
    if y >= pairs[-1][0]:
        return pairs[-1][1]
    for (ya, sa), (yb, sb) in zip(pairs, pairs[1:]):
        if ya <= y <= yb:
            return sa + (sb - sa) * (y - ya) / ((yb - ya) or 1)
    return default


def _snap_grid(edges, start, step, n):
    """Best offset for an n-column grid of width `step` near `start`, then each edge snapped to a real line."""
    if not edges:
        return [start + k * step for k in range(n + 1)]
    best = (-1, 0)
    for off in range(int(-step * 1.5), int(step * 1.5) + 1, 2):
        hits = sum(1 for k in range(n + 1) if min(abs(e - (start + off + k * step)) for e in edges) < step * 0.12)
        if hits > best[0]:
            best = (hits, off)
    grid = []
    for k in range(n + 1):
        exp = start + best[1] + k * step
        near = min(edges, key=lambda e: abs(e - exp))
        grid.append(near if abs(near - exp) < step * 0.2 else exp)
    return grid


def _align(cand, order):
    """Match the label-column lines to last week's job list IN ORDER (OCR can turn L4 into L7, but the
    sheet's order doesn't change). A job may cover several lines (e.g. three 'L4 OPERATOR' rows), and a
    line may be noise (None). Returns one job name or None per line."""
    n, m = len(cand), len(order)
    if not n or not m:
        return [None] * n
    keys = [_core(o) for o in order]
    gain = [[difflib.SequenceMatcher(None, _core(c["t"]), k).ratio() - 0.6 for k in keys] for c in cand]
    NEG = float("-inf")
    # state j = index of the last job used (0..m-1), or m = nothing used yet
    dp = [0.0 if j == m else NEG for j in range(m + 1)]
    choice = []
    for i in range(n):
        new = dp[:]                                   # option: line i is noise, state unchanged
        pick = [None] * (m + 1)
        run_best, run_arg = dp[m], m                  # best over states <= k (state m = start counts as -1)
        for k in range(m):
            if dp[k] > run_best:
                run_best, run_arg = dp[k], k
            v = run_best + gain[i][k]
            if v > new[k]:
                new[k], pick[k] = v, run_arg
        dp = new
        choice.append(pick)
    out = [None] * n
    j = max(range(m + 1), key=lambda k: dp[k])
    for i in range(n - 1, -1, -1):
        if choice[i][j] is not None:
            out[i] = order[j]
            j = choice[i][j]
    return out


def _right_pairs(rows, body, day_edges_hint, order, skip_area):
    """Labels are printed on BOTH edges. Pair each left row with its right-edge copy and give it a
    slope k, so a row's height anywhere is yL + k*(x - xL): fixes tilt and perspective row by row."""
    right = []
    for w in sorted((w for w in body if w["x0"] > day_edges_hint - 10), key=_cy):
        t = w["t"].strip()
        t = re.sub(r"^6-\S*\s*", "", t)                     # '6-2- L11Oporator'
        if SKIP_RE.match(t) or len(_core(t)) < 3 or _day_index(t) is not None:
            continue
        if right and abs(_cy(w) - right[-1]["ry"]) < 4:
            if len(_core(t)) > len(_core(right[-1]["t"])):
                right[-1].update(t=t, x=_cx(w))
            continue
        right.append({"t": t, "x": _cx(w), "ry": _cy(w), "y": _cy(w)})
    names = _align(right, order)
    right = [dict(r, name=n) for r, n in zip(right, names) if n]
    if len(right) < len(rows) * 0.5:
        return None
    sm = difflib.SequenceMatcher(None, [r["name"] for r in rows], [r["name"] for r in right], autojunk=False)
    paired = {}
    for blk in sm.get_matching_blocks():
        for k in range(blk.size):
            paired[blk.a + k] = right[blk.b + k]
    if len(paired) < len(rows) * 0.5:
        return None
    for i, r in enumerate(rows):
        if i in paired:
            p = paired[i]
            r["k"] = (p["ry"] - r["yL"]) / ((p["x"] - r["xL"]) or 1)
    known = [i for i in range(len(rows)) if "k" in rows[i]]
    for i, r in enumerate(rows):                         # rows without a right copy: borrow neighbours' slope
        if "k" not in r:
            near = sorted(known, key=lambda j: abs(rows[j]["yL"] - r["yL"]))[:2]
            r["k"] = sum(rows[j]["k"] for j in near) / len(near)
    # one bad pairing gives a crazy slope: clamp to the median of its neighbours
    for i, r in enumerate(rows):
        nb = sorted(rows[j]["k"] for j in range(max(0, i - 4), min(len(rows), i + 5)))
        med = nb[len(nb) // 2]
        if abs(r["k"] - med) > 0.01:
            r["k"] = med
    return True


def _split_two(text, people):
    """'DOEJANE SMITHJOHN' -> [('DOE JANE', 7), ('SMITH JOHN', 9)] if both halves are known people."""
    q = norm(text)
    best = None
    for i in range(5, len(q) - 4):
        a, sa = people(q[:i]); b, sb = people(q[i:])
        if a and b and sa > 0.85 and sb > 0.85 and (best is None or sa + sb > best[0]):
            best = (sa + sb, (a, i), (b, len(q) - i))
    return best[1:] if best else None


def _is_yellow(img, x0, y0, x1, y1):
    if img is None:
        return False
    W, H = img.size
    pts = [(int(x0 + (x1 - x0) * f), int((y0 + y1) / 2)) for f in (0.05, 0.5, 0.95)] + [(int(x1 + 4), int((y0 + y1) / 2))]
    hits = 0
    for x, y in pts:
        if 0 <= x < W and 0 <= y < H:
            r, g, b = img.getpixel((x, y))
            hits += r > 170 and g > 150 and b < 120 and r - b > 80
    return hits >= 2


def parse(words, template=None, roster=(), img=None, fallback_week=None):
    """-> list of schedule dicts (one per week block) with a 'review' list of things to check."""
    template = template or {}
    t_areas = template.get("areas", [])
    label_area = {norm(j["name"]): a["name"] for a in t_areas for j in a["jobs"]}
    labels = Snapper([j["name"] for a in t_areas for j in a["jobs"]], 0.72, strip=True)
    area_names = Snapper([a["name"] for a in t_areas] + ["LINE", "SANITATION", "SHIPPING", "MILL", "UNION", "LAB", "ADDITIONAL"], 0.8)
    people = Snapper(list(roster), 0.78)
    sheet = "12hr" if any("CREW" in w["t"].upper() for w in words) else "8hr"
    shifts = SHIFTS_12 if sheet == "12hr" else SHIFTS_8
    out = []
    for status, bw in _blocks(words):
        heads = [(i, w) for w in bw if (i := _day_index(w["t"])) is not None]
        if len(heads) < 2:
            continue
        top = min(_cy(w) for _, w in heads)
        heads = [(i, w) for i, w in heads if _cy(w) < top + 60]          # header line only
        a, b = _fit([(i, _cx(w)) for i, w in heads])                       # day centre = a + b*i
        ty, slope = _fit([(_cx(w), _cy(w)) for _, w in heads])             # tilt of the photo
        ndays = 7 if any(i == 6 for i, _ in heads) or sheet == "12hr" else 6
        week = _date_under(heads, bw) or _week_start(words, bw, fallback_week)
        if week is not None and week.weekday() != 6:
            week = week - datetime.timedelta(days=(week.weekday() + 1) % 7)
        if week is None:
            continue
        day_lo = a - b / 2
        x_ref = day_lo - b * 0.5                                           # roughly the label column
        straight = lambda w: _cy(w) - slope * (_cx(w) - x_ref)
        header_bottom = max(straight(w) for _, w in heads) + abs(b) * 0.12
        body = [w for w in bw if straight(w) > header_bottom]
        day_lo = min(day_lo, min(_cx(w) for _, w in heads) - abs(b) * 0.5)

        # job rows from the label column (left of the first day)
        cand = []
        for w in sorted((w for w in body if w["x1"] < day_lo + b * 0.05), key=straight):
            t = w["t"].strip()
            if SKIP_RE.match(t) or len(_core(t)) < 3 or _day_index(t) is not None:
                continue
            if area_names(t)[0] and not labels(t)[0]:
                continue
            y = straight(w)
            if cand and abs(y - cand[-1]["y"]) < 4:            # same printed line: keep the longer piece
                if len(_core(t)) > len(_core(cand[-1]["t"])):
                    cand[-1] = {"t": t, "y": y, "x": _cx(w), "ry": _cy(w)}
                continue
            cand.append({"t": t, "y": y, "x": _cx(w), "ry": _cy(w)})
        rows, review = [], []
        if t_areas:
            order = [j["name"] for a_ in t_areas for j in a_["jobs"]]
            for c, name in zip(cand, _align(cand, order)):
                if name:
                    rows.append({"name": name, "y": c["y"], "xL": c["x"], "yL": c["ry"]})
                elif len(_core(c["t"])) >= 6:
                    review.append(f"Label column has '{c['t']}' that isn't a known job - new job? check it")
        else:
            rows = [{"name": c["t"], "y": c["y"]} for c in cand]
        if not rows:
            continue
        gap = sorted(rows[i + 1]["y"] - rows[i]["y"] for i in range(len(rows) - 1))
        row_h = gap[len(gap) // 2] if gap else 14
        # Edge-to-edge row lines (_right_pairs) tested worse on bowed paper (2026-10-29): column maps are used instead.
        row_lines = None

        dates = [(week + datetime.timedelta(days=i)).isoformat() for i in range(ndays)]
        # per-column correction from the ruled lines (photos only)
        maps = {}
        if img is not None:
            ruled = RuledLines(img) if not hasattr(img, "_ruled") else img._ruled
            lab_x0 = min(w["x0"] for w in body if w["x1"] < day_lo) if any(w["x1"] < day_lo for w in body) else day_lo - b
            y_top = min(_cy(w) for _, w in heads); y_bot = max(_cy(w) for w in body) + 20
            ref = ruled.ys(lab_x0, day_lo - 5, y_top, y_bot)
            for di in range(ndays):
                cx0 = day_lo + di * b
                guess = slope * (cx0 + b / 2 - x_ref)
                col = ruled.ys(cx0 + b * 0.1, cx0 + b * 0.9, y_top, y_bot + abs(guess))
                maps[di] = _offset_map(ref, col, guess, row_h * 2.5)
        # column edges from the header line itself (photos are wider at one end, so not an even grid)
        centres = {i: _cx(w) for i, w in heads}
        for i in range(ndays):
            if i not in centres:
                lo = max((k for k in centres if k < i), default=None); hi = min((k for k in centres if k > i), default=None)
                if lo is not None and hi is not None:
                    centres[i] = centres[lo] + (centres[hi] - centres[lo]) * (i - lo) / (hi - lo)
                else:
                    centres[i] = a + b * i
        cs = [centres[i] for i in range(ndays)]
        day_edges = [cs[0] - (cs[1] - cs[0]) / 2] + [(cs[i] + cs[i + 1]) / 2 for i in range(ndays - 1)] + [cs[-1] + (cs[-1] - cs[-2]) / 2]
        hdr_zone = [w for w in bw if top - 20 < _cy(w) < header_bottom + abs(b) * 0.25]
        crewish = lambda t: bool(re.search(r"CRE|DAYS|MID|NIGHT|NGHT", t.upper())) or t.strip().upper() in ("D", "A", "M", "N")
        sub_words = sorted((w for w in hdr_zone if crewish(w["t"]) and _day_index(w["t"]) is None), key=_cx)
        if sheet == "12hr":   # keep the DAYS/NIGHT pairs in order; drops legends like "MIDNIGHT SHIFT"
            seq, want_day = [], True
            for w in sub_words:
                is_day = "DAY" in w["t"].upper()
                if is_day == want_day:
                    seq.append(w); want_day = not want_day
            sub_words = seq[:ndays * len(shifts)]
        sub_edges = None
        if len(sub_words) == ndays * len(shifts):
            sc = [_cx(w) for w in sub_words]
            sub_edges = [sc[0] - (sc[1] - sc[0]) / 2] + [(sc[i] + sc[i + 1]) / 2 for i in range(len(sc) - 1)] + [sc[-1] + (sc[-1] - sc[-2]) / 2]
        sub_w = (day_edges[-1] - day_edges[0]) / (ndays * len(shifts))
        split_body = []
        for w in body:
            if people.by_key and (w["x1"] - w["x0"]) > sub_w * 1.15 and not people(w["t"])[0]:
                parts = _split_two(w["t"], people)
                if parts:
                    (p1, n1), (p2, n2) = parts
                    cut = w["x0"] + (w["x1"] - w["x0"]) * n1 / (n1 + n2)
                    split_body += [dict(w, t=p1, x1=cut - 2), dict(w, t=p2, x0=cut + 2)]
                    continue
            split_body.append(w)
        body = split_body
        cells = {}
        for w in body:
            x = _cx(w)
            # columns lean with the photo: compare at header height
            xh = x
            if not (day_edges[0] <= xh < day_edges[-1]):
                continue
            di = max(k for k in range(ndays) if day_edges[k] <= xh)
            if sub_edges and sub_edges[0] <= xh < sub_edges[-1]:
                col = max(k for k in range(len(sub_edges) - 1) if sub_edges[k] <= xh)
                si = col % len(shifts)
                di = min(col // len(shifts), ndays - 1) if len(sub_edges) - 1 == ndays * len(shifts) else di
            else:
                frac = (xh - day_edges[di]) / (day_edges[di + 1] - day_edges[di])
                si = min(int(frac * len(shifts)), len(shifts) - 1)
            if row_lines:
                ys = [r_["yL"] + r_["k"] * (x - r_["xL"]) for r_ in rows]
                r = min(range(len(rows)), key=lambda k: abs(ys[k] - _cy(w)))
                if abs(ys[r] - _cy(w)) > row_h * 0.75:
                    continue
                cells.setdefault((rows[r]["name"], dates[di], shifts[si]["key"]), []).append(w)
                continue
            if maps.get(di):
                y = _cy(w) - _shift_at(maps[di], _cy(w), slope * (x - x_ref))  # into label-column coords
            else:
                y = straight(w)
            r = min(range(len(rows)), key=lambda k: abs(rows[k]["y"] - y))
            if abs(rows[r]["y"] - y) > row_h * 0.75:
                continue
            cells.setdefault((rows[r]["name"], dates[di], shifts[si]["key"]), []).append(w)

        jobs = {}
        line_groups = []
        for (job, date, sk), ws in cells.items():
            ws.sort(key=lambda w: _cy(w))
            lines = []
            for w in ws:
                if lines and abs(_cy(w) - _cy(lines[-1][-1])) < row_h * 0.5:
                    lines[-1].append(w)
                else:
                    lines.append([w])
            for ln in lines:
                line_groups.append(((job, date, sk), sorted(ln, key=lambda w: w["x0"])))
        for (job, date, sk), ws in line_groups:
            texts = [w["t"].strip() for w in ws if not SKIP_RE.match(w["t"].strip())]
            if not texts:
                continue
            note = " ".join(t for t in texts if NOTE_RE.match(t.replace(" ", "")))
            raw = " ".join(t for t in texts if not NOTE_RE.match(t.replace(" ", "")))
            if not raw:
                continue
            person, score = people(raw) if people.by_key else (raw, 1.0)
            entry = {"name": person or raw.upper()}
            if note:
                entry["note"] = note
            if _is_yellow(img, ws[0]["x0"], ws[0]["y0"], ws[-1]["x1"], ws[-1]["y1"]):
                entry["hl"] = True
            if not person:
                entry["check"] = True
                review.append(f"{date} {sk} {job}: read '{raw}' - not a known name")
            jobs.setdefault(job, {}).setdefault(date, {}).setdefault(sk, []).append(entry)

        areas, order = {}, []
        for a_ in t_areas:
            for j in a_["jobs"]:
                if j["name"] in jobs:
                    areas.setdefault(a_["name"], []).append(j["name"])
                    if a_["name"] not in order:
                        order.append(a_["name"])
        for job in jobs:
            if norm(job) not in label_area:
                areas.setdefault("Other", []).append(job)
                if "Other" not in order:
                    order.append("Other")
        out.append({
            "sheet": sheet, "title": "", "week_start": week.isoformat(), "status": status,
            "shifts": shifts,
            "days": [{"date": d, "label": DAY_NAMES[i].capitalize()} for i, d in enumerate(dates)],
            "areas": [{"name": an, "jobs": [{"name": jn, "slots": jobs[jn]} for jn in areas[an]]} for an in order],
            "notes": "", "review": review,
        })
    return out
