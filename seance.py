#!/usr/bin/env python3
"""Séance: a local Gemma 4 medium for things that died.

  python3 seance.py find code REPO [--skip GLOB ...]   deleted files and functions in a git repo
  python3 seance.py find site DOMAIN [--page TS/URL]   a dead website, from the Wayback Machine
  python3 seance.py find apps [--days N]               Mac apps not opened in N days
  python3 seance.py list                               every grave found so far
  python3 seance.py tombstone NAME                     Gemma writes the tombstone
  python3 seance.py summon NAME ["question"]           ask the dead (a chat if no question)
  python3 seance.py selftest

Code gathers the facts (dates, commits, authors, snapshots) and numbers each piece of evidence.
Gemma only writes the words, and may only cite evidence that exists. When the
evidence doesn't say, the answer is "The spirits are silent."

Python standard library only. Gemma 4 runs on this machine through Ollama.
"""
import argparse, fnmatch, json, os, plistlib, re, subprocess, sys, tempfile, time, urllib.request
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path

MODEL = os.environ.get("SEANCE_MODEL", "gemma4:e4b")
OLLAMA = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
NUM_CTX = 4096  # what Ollama's MLX runner loads gemma4:e4b with; fixed, because changing it reloads the model
GRAVES = Path(__file__).resolve().parent / "graves"
SILENT = "The spirits are silent."
LAST_WORDS_LINES = 60
BOILERPLATE = re.compile(r"\s*(import\s|package\s|from\s+\S+\s+import\s|#include\s|using\s)")


# ---------- Gemma ----------

def ask(messages, schema):
    if sum(len(m["content"]) for m in messages) / 3 > NUM_CTX - 400 - 64:  # ~3+ characters a token, erring long
        sys.exit("The evidence is too long for Gemma's context window.")
    for seed in (7, 8):  # a reply that loops until the 400-token cap is cut off; one retry with a new seed
        body = {"model": MODEL, "messages": messages, "stream": False, "think": False,
                "format": schema, "keep_alive": "30m",
                "options": {"num_ctx": NUM_CTX, "num_predict": 400, "temperature": 0.2, "seed": seed}}
        req = urllib.request.Request(OLLAMA + "/api/chat", json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=1800) as r:  # a cold load, or a Mac short on memory, takes minutes
            reply = json.load(r)
        if reply.get("prompt_eval_count", 0) >= NUM_CTX - 400 - 64:  # leave room for the reply
            sys.exit("The evidence is too long for Gemma's context window.")
        try:
            return json.loads(reply["message"]["content"])
        except json.JSONDecodeError:
            pass
    return None  # cut off twice: Gemma got stuck


def gemma_up():
    try:
        with urllib.request.urlopen(OLLAMA + "/api/tags", timeout=3) as r:
            return any(m["name"] == MODEL for m in json.load(r)["models"])
    except OSError:
        return False


# ---------- evidence ----------

def evidence_block(grave):
    return "\n".join(f"[{e['id']}] {e['text']}" for e in grave["evidence"])


def cited(grave, ids):
    """Keep only ids that exist in this grave's evidence, in order, once each."""
    real = {e["id"] for e in grave["evidence"]}
    return list(dict.fromkeys(i for i in ids if i in real))


def numbers_ok(text, grave, cites):
    """Every number in TEXT appears in the evidence it cites (04 and 4 count as the same)."""
    said = {int(n) for n in re.findall(r"\d+", text)}
    known = {int(n) for e in grave["evidence"] if e["id"] in cites for n in re.findall(r"\d+", e["text"])}
    return said <= known


def guard(grave, reply):
    """Gemma's summon reply -> (answer, cites). No real evidence cited, or a number the cited
    evidence doesn't contain, means silence."""
    cites = cited(grave, reply.get("cites", []))
    answer = re.sub(r"\s*\[[^\]]*\]", "", reply.get("answer", "")).strip()  # ids are shown from `cites`
    if reply.get("silent") or not cites or not answer or not numbers_ok(answer, grave, cites):
        return SILENT, []
    return answer, cites


# ---------- code graveyard ----------

# A line that defines a function, method or class. A heuristic for common languages, not a parser.
DEF = re.compile(r"\s*(?:export\s+)?(?:async\s+)?(?:def|function|func|fun|fn|class|interface|enum|struct|trait)\s+"
                 r"(?:\([^)]*\)\s*)?([A-Za-z_]\w*)")
METHOD = re.compile(r"\s*(?:@\w+\s+)*(?:(?:public|private|protected|static|final|synchronized|abstract|native|"
                    r"default|override|open|internal|suspend)\s+)*([\w<>\[\].?,]+)\s+([A-Za-z_]\w*)\s*\([^;=]*$")
NOT_TYPES = {"return", "new", "else", "if", "for", "while", "switch", "catch", "throw", "case", "await", "yield",
             "do", "try", "in", "of", "print", "assert", "not", "and", "or", "is", "sizeof", "typeof", "delete"}
LOG_FORMAT = "--format=%x01%h%x1f%aI%x1f%an%x1f%s"  # hash, ISO date, author name (never email), subject


def def_name(line):
    if m := DEF.match(line):
        return m.group(1)
    m = METHOD.match(line)
    return m.group(2) if m and m.group(1) not in NOT_TYPES else None


def git(repo, *args, raw=False, check=True):
    out = subprocess.run(["git", "-C", str(repo), "-c", "core.quotePath=false", *args],
                         capture_output=True, check=check).stdout
    return out if raw else out.decode("utf-8", "replace")


def skipped(path, skip):
    return any(fnmatch.fnmatch(path, g) for g in skip)


def when(iso):
    return iso[:16].replace("T", " ")


def plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def lifespan(a, b):
    mins = int((datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() // 60)
    if mins < 60:
        return plural(mins, "minute")
    if mins < 48 * 60:
        return f"{plural(mins // 60, 'hour')} {plural(mins % 60, 'minute')}"
    return plural(mins // 1440, "day")


def mentions(repo, word, skip):
    """Lines in today's files (HEAD) that contain WORD as a whole word: 'path:line:text'."""
    hits = git(repo, "grep", "-I", "-n", "-w", "-F", word, "HEAD", check=False).splitlines()
    return [h.removeprefix("HEAD:") for h in hits if not skipped(h.removeprefix("HEAD:").split(":", 1)[0], skip)]


def last_words(lines, label):
    body = [l[:160] for l in lines if l.strip() and not BOILERPLATE.match(l)]
    cut = f" (the first {LAST_WORDS_LINES} lines shown)" if len(body) > LAST_WORDS_LINES else ""
    return f"{label}, without blank, import and package lines{cut}:\n```\n" + "\n".join(body[:LAST_WORDS_LINES]) + "\n```"


def haunts(repo, names, skip, words=False):
    out = []
    for n in names:
        if not words and n.isalpha() and n.islower():
            continue  # a function called "mark" turns up in prose; only identifier-like names are searched
        hits = [h for h in mentions(repo, n, skip) if def_name(h.split(":", 2)[-1]) != n]
        out.append(f"The name \"{n}\" still appears {plural(len(hits), 'time')} in the repository's files today:\n" +
                   "\n".join(h[:200] for h in hits[:5]) if hits
                   else f"The name \"{n}\" no longer appears anywhere in the repository's files today.")
    return out


class Evidence(list):
    def add(self, text):
        self.append({"id": f"E{len(self) + 1}", "text": text})
        return self[-1]["id"]


def commit_text(c, what):
    h, iso, author, subject = c
    return f'{when(iso)}: commit {h} by {author} {what}. Message: "{subject}"'


def grave(kind, name, where, path, about, facts, line, ev):
    return {"kind": kind, "name": name, "where": where, "path": path, "about": about,
            "facts": facts, "line": line, "evidence": ev}


def code_grave(name, repo, path, about, born, died, died_cite, makers, ev):
    f = {"born": when(born[1]), "born_commit": born[0], "died": when(died[1]), "died_commit": died[0],
         "died_cite": died_cite, "lifespan": lifespan(born[1], died[1]), "makers": list(dict.fromkeys(makers))}
    return grave("code", name, repo.name, path, about, f,
                 f"{repo.name}/{path.split('#')[0]}: born {f['born']} in {f['born_commit']}, died {f['died']} in "
                 f"{f['died_commit']}, lived {f['lifespan']}, made by {', '.join(f['makers'])}", ev)


def find_code(repo, skip=()):
    """Everything deleted from REPO's history that is still gone today: whole files, then functions."""
    repo = Path(repo).expanduser().resolve()
    return dead_files(repo, skip) + dead_functions(repo, skip)


def dead_files(repo, skip):
    deaths = {}  # path -> latest commit that deleted it
    for chunk in git(repo, "log", "-M", "--diff-filter=D", "--name-only", "--format=%x01%H").split("\x01")[1:]:
        commit, *paths = chunk.split("\n")
        for p in filter(None, paths):
            deaths.setdefault(p, commit)
    graves = []
    for path, death in deaths.items():
        if skipped(path, skip) or subprocess.run(["git", "-C", str(repo), "cat-file", "-e", f"HEAD:{path}"],
                                                 capture_output=True).returncode == 0:
            continue  # skipped, or came back to life
        body = git(repo, "show", f"{death}^:{path}", raw=True)
        if b"\0" in body[:8000]:
            continue  # binary: nothing for Gemma to read
        graves.append(dead_file(repo, path, death, body.decode("utf-8", "replace"), skip))
    return graves


def dead_file(repo, path, death, last, skip):
    ev = Evidence()
    commits = []
    log = git(repo, "log", "--follow", "-M", "--name-status", LOG_FORMAT, death, "--", path)
    for chunk in reversed(log.split("\x01")[1:]):  # oldest first
        head, *rest = chunk.strip().split("\n")
        c = tuple(head.split("\x1f"))
        status = next((l.split("\t") for l in rest if l and l[0] in "AMDRC"), ["M"])
        what = {"A": "created this file", "M": "changed this file", "D": "deleted this file"}.get(status[0][0])
        if status[0][0] in "RC":
            what = f"{'renamed' if status[0][0] == 'R' else 'copied'} it from {status[1]} to {status[2]}"
        commits.append((c, ev.add(commit_text(c, what))))
    (born, _), (died, died_cite) = commits[0], commits[-1]
    ev.add(f"It lived {lifespan(born[1], died[1])}, from {when(born[1])} to {when(died[1])}.")
    message = git(repo, "log", "-1", "--format=%B", death).strip()
    if "\n" in message:
        ev.add(f"The full message of the commit that deleted it ({died[0]}):\n{message}")
    added = [l.split("\t")[-1] for l in git(repo, "show", "-M", "--name-status", "--format=", death).splitlines()
             if l and l[0] in "AR" and not skipped(l.split("\t")[-1], skip)]
    ev.add(f"The same commit ({died[0]}) added: {', '.join(added[:10])}." if added
           else f"The same commit ({died[0]}) added no new files.")
    ev.add(last_words(last.splitlines(), f"Its last version had {len(last.splitlines())} lines. Here it is"))
    for h in haunts(repo, [Path(path).stem], skip, words=True):
        ev.add(h)
    return code_grave(Path(path).name, repo, path,
                 f"the file {path}, deleted from the git repository \"{repo.name}\"",
                 born, died, died_cite, [c[0][2] for c in commits], ev)


def dead_functions(repo, skip):
    """Functions and classes removed from files that live on, grouped by the commit and file they died in."""
    # ponytail: reads the whole history's diff into memory; fine for side projects, stream it for huge repos
    born, died, hunks = {}, {}, {}  # name -> commit; name -> (commit, path); (hash, path) -> removed blocks
    log = git(repo, "log", "-p", "-M", "--unified=0", "--no-color", LOG_FORMAT)
    for chunk in reversed(log.split("\x01")[1:]):  # oldest first
        head, _, diff = chunk.partition("\n")
        c = tuple(head.split("\x1f"))
        added, removed = set(), []
        for fdiff in re.split(r"^diff --git ", diff, flags=re.M)[1:]:
            lines = fdiff.split("\n")
            path = lines[0].rsplit(" b/", 1)[-1]
            if skipped(path, skip) or any(l.startswith("deleted file mode") for l in lines[1:6]):
                continue  # a deleted file is a grave of its own
            blocks = []  # per hunk: [removed lines, (name, def line) removed, def lines added; names as \0]
            for l in lines[1:]:
                if l.startswith("@@"):
                    blocks.append([[], [], []])
                elif l.startswith("+") and not l.startswith("+++ ") and blocks:
                    if n := def_name(l[1:]):
                        added.add(n)
                        born.setdefault(n, c)
                        blocks[-1][2].append(l[1:].replace(n, "\0").strip())
                elif l.startswith("-") and not l.startswith("--- ") and blocks:
                    blocks[-1][0].append(l[1:])
                    if n := def_name(l[1:]):
                        blocks[-1][1].append((n, l[1:].replace(n, "\0").strip()))
            # a def line that comes back in the same hunk with only its name changed is a rename, not a death
            defs = [n for gone, rm, add in blocks for n, shape in rm if shape not in add]
            if defs:
                hunks[(c[0], path)] = [gone for gone, rm, add in blocks]
                removed += [(n, path) for n in defs]
        for n, path in removed:
            if n not in added:  # removed and added in one commit = changed or moved, not dead
                died[n] = (c, path)
    groups = {}
    for n, (c, path) in died.items():
        if n in born and not any(def_name(h.split(":", 2)[-1]) == n for h in mentions(repo, n, skip)):
            groups.setdefault((c, path), []).append(n)
    return [dead_group(repo, c, path, names, born, hunks[(c[0], path)], skip) for (c, path), names in groups.items()]


def dead_group(repo, died, path, names, born, blocks, skip):
    ev = Evidence()
    births = sorted({born[n] for n in names}, key=lambda c: c[1])
    for b in births:
        mine = [n for n in names if born[n] == b]
        ev.add(commit_text(b, f"added {', '.join(mine)}"))
    died_cite = ev.add(commit_text(died, f"removed {', '.join(names)} from {path}, while {path} itself lived on"))
    ev.add(f"It lived {lifespan(births[0][1], died[1])}, from {when(births[0][1])} to {when(died[1])}.")
    message = git(repo, "log", "-1", "--format=%B", died[0]).strip()
    if "\n" in message:
        ev.add(f"The full message of the commit that removed it ({died[0]}):\n{message}")
    gone = [l for b in blocks if any(def_name(x) in names for x in b) for l in b]
    ev.add(last_words(gone, f"The code removed from {path} in commit {died[0]}"))
    for h in haunts(repo, names, skip):
        ev.add(h)
    return code_grave(f"{Path(path).name}: {', '.join(names)}", repo, f"{path}#{names[0]}",
                 f"the code {', '.join(names)}, removed from {path} in the git repository \"{repo.name}\"",
                 births[0], died, died_cite, [b[2] for b in births] + [died[2]], ev)


# ---------- website graveyard ----------

WAYBACK = "https://web.archive.org"
SITE_BUDGET = 9000  # characters of page text, so the whole prompt fits in NUM_CTX tokens
PAGE_CHARS = 1800
BLOCK_TAGS = {"p", "div", "br", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6", "title", "section",
              "article", "header", "footer", "table", "ul", "ol", "dd", "dt", "blockquote", "pre", "form", "center"}


class PageText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.lines, self.hidden, self.title, self.in_title = [""], 0, "", False

    def handle_starttag(self, tag, attrs):
        self.hidden += tag in ("script", "style", "noscript")
        self.in_title = tag == "title"
        if tag in BLOCK_TAGS:
            self.lines.append("")

    def handle_endtag(self, tag):
        self.hidden -= tag in ("script", "style", "noscript") and self.hidden > 0
        if tag in BLOCK_TAGS:
            self.lines.append("")

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        elif not self.hidden:
            self.lines[-1] += data


def prose(line):
    """True for a line that reads like writing, not a menu, a date list or a link bar."""
    words = line.split()
    numeric = sum(any(ch.isdigit() for ch in w) for w in words)
    lower = sum(w[:1].islower() for w in words)
    return len(words) >= 4 and numeric <= len(words) / 5 and (re.search(r"[.!?…]", line) or lower >= len(words) / 2)


def page_text(html):
    """A page's title and its readable lines, so menus and link lists drop out."""
    p = PageText()
    p.feed(html)
    title = " ".join(p.title.split())
    body = "\n".join(l for l in (" ".join(x.split()) for x in p.lines) if prose(l))
    return f"[title: {title}]\n{body}" if title else body


def fetch(url, cache):
    """GET with curl, cached on disk. curl unzips gzip captures and uses the system's certificates
    (Python's own certificate store fails against web.archive.org on some Macs)."""
    if not cache.exists():
        time.sleep(2.5)  # the Wayback Machine rate-limits
        r = subprocess.run(["curl", "-sSfL", "--compressed", "--retry", "2", "-m", "90", url], capture_output=True)
        if r.returncode:
            raise OSError(f"Could not fetch {url}: {r.stderr.decode().strip()}")
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(r.stdout)
    raw = cache.read_bytes()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", "replace")  # older pages


def stamp(ts):
    return f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}"


def spread(rows, n):
    """Up to n rows spaced evenly through the list, always keeping the first and the last."""
    if len(rows) <= n:
        return rows
    return [rows[round(i * (len(rows) - 1) / (n - 1))] for i in range(n)]


def find_site(domain, pages=(), yearly=4):
    """A dead website from the Wayback Machine: home pages through the years, plus any snapshots
    passed as TIMESTAMP/URL (e.g. the page that announced its death)."""
    domain = re.sub(r"^https?://", "", domain).strip("/")
    cache = GRAVES / "site" / domain / "cache"
    rows = json.loads(fetch(f"{WAYBACK}/cdx/search/cdx?url={domain}/&output=json&collapse=timestamp:4"
                            "&filter=statuscode:200&filter=mimetype:text/html&fl=timestamp,original,length",
                            cache / "home-by-year.json") or "[[]]")[1:]
    if not rows:
        sys.exit(f"The Wayback Machine has no saved home page for {domain}.")
    real = [r for r in rows if int(r[2]) > 600]  # under ~600 bytes is a redirect or an empty page
    picks = [p.split("/", 1) for p in pages] + [r[:2] for r in spread(real, yearly)]
    texts, used = {}, 0
    for ts, url in picks:  # the pages asked for first, then the yearly home pages, until the budget runs out
        if (ts, url) in texts or used >= SITE_BUDGET:
            continue
        slug = re.sub(r"\W+", "_", url)[:80]
        text = page_text(fetch(f"{WAYBACK}/web/{ts}id_/{url}", cache / f"{ts}_{slug}.html"))
        if text.strip():
            texts[(ts, url)] = text[:min(PAGE_CHARS, SITE_BUDGET - used)]
            used += len(texts[(ts, url)])
    ev = Evidence()
    ev.add(f"The Wayback Machine holds working copies of the {domain} home page from {stamp(rows[0][0])} "
           f"(the earliest) to {rows[-1][0][:4]} (the latest year).")
    for (ts, url), text in sorted(texts.items()):
        ev.add(f"The Wayback Machine's copy of {url}, saved {stamp(ts)}, reads:\n{text}")
    r = subprocess.run(["curl", "-sSL", "-o", "/dev/null", "-m", "20", "-w", "%{http_code} %{url_effective}",
                        f"http://{domain}/"], capture_output=True, text=True)
    code, _, final = r.stdout.partition(" ")
    ev.add(f"Checked live on {date.today()}: http://{domain}/ " +
           (f"answers with HTTP {code}, ending at {final}." if code not in ("", "000") else "does not answer."))
    facts = {"first_saved": stamp(rows[0][0]), "last_year": rows[-1][0][:4], "pages": len(texts)}
    return grave("site", domain, domain, domain,
                 f"the website {domain}, as its pages were saved by the Internet Archive's Wayback Machine", facts,
                 f"saved by the Wayback Machine from {facts['first_saved']} to {facts['last_year']}; "
                 f"{plural(len(texts), 'page')} read", ev)


# ---------- apps graveyard ----------

MDLS = sorted(["kMDItemLastUsedDate", "kMDItemDateAdded", "kMDItemUseCount", "kMDItemPhysicalSize"])  # mdls -raw answers in name order


def mdls(app):
    out = subprocess.run(["mdls", "-raw", *[x for n in MDLS for x in ("-name", n)], str(app)],
                         capture_output=True, text=True).stdout.split("\0")
    return {n: (None if v == "(null)" else v) for n, v in zip(MDLS, out)}


def md_date(v):
    return datetime.strptime(v, "%Y-%m-%d %H:%M:%S %z") if v else None


def find_apps(days=365, folder="/Applications", unrecorded=False):
    """Apps in FOLDER that macOS last saw opened more than DAYS days ago (with UNRECORDED, also apps
    it has no record of opening: menu-bar and command-line-launched apps often have none). Apps only."""
    now = datetime.now().astimezone()  # local time, so "today" is the user's today
    graves = []
    for app in sorted(Path(folder).glob("*.app")):
        md = mdls(app)
        used, added = md_date(md["kMDItemLastUsedDate"]), md_date(md["kMDItemDateAdded"])
        if (used and (now - used).days < days) or (not used and not unrecorded):
            continue  # still alive, or no record to judge by
        try:
            info = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
        except (OSError, plistlib.InvalidFileException):
            info = {}
        ev = Evidence()
        if added:
            ev.add(f"It was added to {folder} on {added:%Y-%m-%d}.")
        died_cite = ev.add(f"macOS last saw it opened on {used:%Y-%m-%d}, {plural((now - used).days, 'day')} before "
                           f"today ({now:%Y-%m-%d})." if used else
                           f"macOS has no record of it ever being opened (checked {now:%Y-%m-%d}).")
        if md["kMDItemUseCount"]:
            ev.add(f"macOS counted it being opened {plural(int(md['kMDItemUseCount']), 'time')}.")
        version = info.get("CFBundleShortVersionString") or info.get("CFBundleVersion")
        if version:
            ev.add(f"Its version is {version}; its bundle id is {info.get('CFBundleIdentifier', 'unknown')}.")
        if info.get("NSHumanReadableCopyright"):
            ev.add(f"Its copyright line reads: {info['NSHumanReadableCopyright'].strip()}")
        if info.get("LSMinimumSystemVersion"):
            ev.add(f"It needs macOS {info['LSMinimumSystemVersion']} or later.")
        if info.get("LSApplicationCategoryType"):
            ev.add(f"Its category is {info['LSApplicationCategoryType'].split('.')[-1].replace('-', ' ')}.")
        if md["kMDItemPhysicalSize"]:
            ev.add(f"It takes {int(md['kMDItemPhysicalSize']) / 1e6:.0f} MB on disk.")
        if (app / "Contents" / "_MASReceipt").exists():
            ev.add("It came from the Mac App Store.")
        facts = {"added": f"{added:%Y-%m-%d}" if added else None, "last_used": f"{used:%Y-%m-%d}" if used else None,
                 "idle_days": (now - used).days if used else None, "died_cite": died_cite}
        graves.append(grave("app", app.stem, "applications", app.stem, f"the Mac app {app.stem}", facts,
                            f"last opened {facts['last_used']} ({plural(facts['idle_days'], 'day')} ago)" if used else
                            "no record of ever being opened" + (f", added {facts['added']}" if added else ""), ev))
    return graves


# ---------- graves on disk ----------

def grave_file(g):
    return GRAVES / g["kind"] / g["where"] / (g["path"].replace("/", "__") + ".json")


def save(g):
    f = grave_file(g)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(g, indent=1, ensure_ascii=False))


def all_graves():
    return sorted(GRAVES.glob("*/*/*.json"))


def load(name):
    files = all_graves()
    match = [f for f in files if f.stem.lower() == name.lower().replace("/", "__")] or \
            [f for f in files if name.lower() in f.stem.lower()]
    if len(match) != 1:
        sys.exit(f"No grave called {name!r}." if not match else
                 f"{name!r} matches {len(match)} graves: " + ", ".join(f.stem for f in match))
    return json.loads(match[0].read_text())


def describe(g):
    return f"{g['name']}\n  {g.get('line', '')}"


# ---------- the medium ----------

RULES = ("You are the medium at a séance. You speak for {about}.\n"
         "Use ONLY the numbered evidence below. Never add a fact, date, name, number or reason that the "
         "evidence does not state. List in `cites` every evidence id you used.\n\nEvidence:\n{evidence}")


def tombstone(g):
    ids = [e["id"] for e in g["evidence"]]
    schema = {"type": "object", "required": ["cause_of_death", "epitaph", "cites"], "properties": {
        "cause_of_death": {"type": "string"}, "epitaph": {"type": "string"},
        "cites": {"type": "array", "items": {"type": "string", "enum": ids}}}}
    reply = ask([{"role": "system", "content": RULES.format(about=g["about"], evidence=evidence_block(g))},
                 {"role": "user", "content":
                  "Write its tombstone.\n"
                  "cause_of_death: one plain sentence giving the REASON it died (was deleted, shut down or stopped "
                  "being used), as the evidence explains it: a commit message, a page, a code comment or a record. "
                  "Don't just name the commit or date that killed it. If the evidence gives no reason, say the "
                  "record does not say why.\n"
                  "epitaph: one short line it would say about itself, under 15 words, in its own voice. Poetic, "
                  "but no new facts, and don't copy a commit message or a page title."}],
                schema)
    if reply is None:
        sys.exit("Gemma got stuck writing this tombstone twice. Try again.")
    cites = cited(g, reply["cites"])
    cause = reply["cause_of_death"].strip()
    if not cites or not numbers_ok(cause, g, cites):
        cause, cites = "The record does not say why.", []
    g["tombstone"] = {"cause_of_death": cause, "epitaph": reply["epitaph"].strip(), "cites": cites}
    save(g)
    return g["tombstone"]


def summon(g, question, history=()):
    ids = [e["id"] for e in g["evidence"]]
    schema = {"type": "object", "required": ["answer", "silent", "cites"], "properties": {
        "answer": {"type": "string"}, "silent": {"type": "boolean"},
        "cites": {"type": "array", "items": {"type": "string", "enum": ids}}}}
    system = RULES.format(about=g["about"], evidence=evidence_block(g)) + (
        "\n\nAnswer as the spirit, in the first person, in one to three short sentences. "
        "Answer every part of the question that the evidence covers. Only if the evidence covers none of it, "
        "set silent to true, leave cites empty, and do not guess.")
    reply = ask([{"role": "system", "content": system}, *history, {"role": "user", "content": question}], schema)
    return guard(g, reply) if reply else (SILENT, [])  # a reply that loops to the cap has nothing to stand on


def receipt(text, answer):
    """The sentence of a piece of evidence that shares the most words with the answer."""
    said = set(re.findall(r"\w{4,}", answer.lower()))
    sentences = [x.strip() for x in re.split(r"(?<=[.!?])\s+|\n", text) if x.strip()]
    return max(sentences, key=lambda x: sum(map(len, said & set(re.findall(r"\w{4,}", x.lower())))))


def show_answer(g, answer, cites):
    print(f"\n  {answer}" + (f"  [{', '.join(cites)}]" if cites else ""))
    for e in g["evidence"]:
        if e["id"] in cites:
            print(f"    {e['id']}: {receipt(e['text'], answer)[:200]}")


# ---------- self-test ----------

def selftest():
    global GRAVES
    with tempfile.TemporaryDirectory() as tmp:
        GRAVES, repo = Path(tmp) / "graves", Path(tmp) / "haunted"
        repo.mkdir()
        env = {**os.environ, "GIT_AUTHOR_NAME": "Ada", "GIT_AUTHOR_EMAIL": "ada@example.com",
               "GIT_COMMITTER_NAME": "Ada", "GIT_COMMITTER_EMAIL": "ada@example.com"}

        def commit(day, msg, files):
            for f, text in files.items():
                p = repo / f
                if text is None:
                    p.unlink()
                else:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(text) if isinstance(text, bytes) else p.write_text(text)
            date = f"2026-10-{day:02d}T12:00:00"
            subprocess.run(["git", "-C", repo, "add", "-A"], check=True)
            subprocess.run(["git", "-C", repo, "commit", "-q", "-m", msg], check=True,
                           env={**env, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date})

        subprocess.run(["git", "-C", repo, "init", "-q"], check=True)
        commit(1, "Add the ghost", {"ghost.py": "def boo():\n    return 'boo'\n", "keep.py": "x = 1\n",
               "logo.png": b"\x89PNG\0\0binary", "old.py": "y = 2\n", "gen/a.md": "generated\n",
               "tricks.py": "def stay():\n    return 1\n\n\ndef rename_me():\n    return 2\n"})
        commit(2, "Make the ghost louder", {"ghost.py": "def boo():\n    return 'BOO'\n"})
        commit(3, "Rename old.py; teach a trick", {"old.py": None, "new.py": "y = 2\n",
               "tricks.py": "def stay():\n    return 1\n\n\ndef renamed():\n    return 2\n\n\n"
                            "def float_away():\n    return 'whoosh'\n"})
        commit(4, "Remove ghost.py: spirit.py does it better\n\nThe ghost scared nobody.",
               {"ghost.py": None, "spirit.py": "def boo():\n    return 'BOO!'\n", "logo.png": None, "gen/a.md": None})
        commit(5, "Call the spirit; nobody floats", {"keep.py": "from spirit import boo  # was ghost\n",
               "tricks.py": "def stay():\n    return 1\n\n\ndef renamed():\n    return 2\n"})

        found = find_code(repo, skip=["gen/*"])
        # renamed, binary and skipped files are not dead; nor is a function renamed in place (rename_me)
        assert [g["path"] for g in found] == ["ghost.py", "tricks.py#float_away"], [g["path"] for g in found]
        g, trick = found
        save(g)
        f = g["facts"]
        assert (f["born"], f["died"], f["makers"], f["lifespan"]) == \
            ("2026-10-01 12:00", "2026-10-04 12:00", ["Ada"], "3 days"), f
        t = trick["facts"]
        assert (t["born"], t["died"], t["lifespan"]) == ("2026-10-03 12:00", "2026-10-05 12:00", "2 days"), t
        trick_text = "\n".join(e["text"] for e in trick["evidence"])
        for must in ("added float_away", "removed float_away from tricks.py", "return 'whoosh'", "nobody floats"):
            assert must in trick_text, must
        texts = "\n".join(e["text"] for e in g["evidence"])
        for must in ("created this file", "deleted this file", "The ghost scared nobody", "added: spirit.py",
                     "return 'BOO'", 'still appears 1 time ', "keep.py:1:"):
            assert must in texts, must
        assert load("ghost")["path"] == "ghost.py"
        save(grave("site", "example.org", "example.org", "example.org", "a site", {}, "", []))
        assert load("example")["kind"] == "site"  # every kind of grave can be summoned by name

        # the guard: invented ids are dropped, and no real citation means silence
        assert guard(g, {"answer": "I was deleted [E99].", "silent": False, "cites": ["E99"]}) == (SILENT, [])
        assert guard(g, {"answer": "I was deleted.", "silent": False, "cites": []}) == (SILENT, [])
        assert guard(g, {"answer": "Who knows.", "silent": True, "cites": ["E1"]}) == (SILENT, [])
        assert guard(g, {"answer": "I died in 1999.", "silent": False, "cites": ["E3"]}) == (SILENT, [])
        assert guard(g, {"answer": "I died on 4 Oct [E3].", "silent": False, "cites": ["E3", "E99", "E3"]}) \
            == ("I died on 4 Oct.", ["E3"])
        assert receipt("Saved in 2014. We will shut down on 30 September.\nBye.", "It shut down in September") \
            == "We will shut down on 30 September."
        # websites: menus and date lists drop out, prose and the title stay
        html = ("<html><head><title>Bye Bye Site</title><script>var x = 1;</script></head><body>"
                "<ul><li>June 2014 (1)</li></ul><div>Archives June 2014 (1) May 2014 (3) April 2014 (2)</div>"
                "<div>Home About Blog Contact Us Now</div><p>We will shut down the site on 30 September 2014.</p>"
                "</body></html>")
        assert page_text(html) == "[title: Bye Bye Site]\nWe will shut down the site on 30 September 2014.", \
            page_text(html)
        assert spread(list(range(10)), 4) == [0, 3, 6, 9] and spread([1, 2], 4) == [1, 2]

        # apps: mdls answers in name order; a date must never land in a number field
        apps = sorted(Path("/Applications").glob("*.app"))[:5] if sys.platform == "darwin" else []
        for app in apps:
            md = mdls(app)
            assert all(v is None or v.isdigit() for v in (md["kMDItemPhysicalSize"], md["kMDItemUseCount"])), md
            md_date(md["kMDItemLastUsedDate"]), md_date(md["kMDItemDateAdded"])
        print("offline checks passed")

        if not gemma_up():
            print(f"live checks skipped: Ollama is not serving {MODEL}")
            return
        t = tombstone(g)
        assert t["epitaph"] and t["cause_of_death"] and set(t["cites"]) <= {e["id"] for e in g["evidence"]}, t
        print(f"tombstone: {t['cause_of_death']} / \"{t['epitaph']}\" {t['cites']}")
        answer, cites = summon(g, "On what date were you deleted, and in which commit?")
        print("deleted when:", answer, cites)
        assert f["died_cite"] in cites and answer != SILENT, (answer, cites)
        answer, cites = summon(g, "What was your favourite colour?")
        print("favourite colour:", answer, cites)
        assert answer == SILENT and cites == [], (answer, cites)
        print("live checks passed")


# ---------- command line ----------

def main():
    ap = argparse.ArgumentParser(description="Séance: talk to dead code with a local Gemma 4.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    fp = sub.add_parser("find", help="find the dead and save their evidence")
    fp.add_argument("graveyard", choices=["code", "site", "apps"])
    fp.add_argument("target", nargs="?", help="code: a git repository; site: a domain; apps: a folder "
                                              "(default /Applications)")
    fp.add_argument("--skip", action="append", default=[], metavar="GLOB", help="code: ignore deleted paths like this")
    fp.add_argument("--page", action="append", default=[], metavar="TIMESTAMP/URL",
                    help="site: also read this Wayback snapshot, e.g. 20140630142637/http://example.com/bye.html")
    fp.add_argument("--days", type=int, default=365, help="apps: unused for at least this many days")
    fp.add_argument("--unrecorded", action="store_true", help="apps: also list apps macOS has no record of opening")
    sub.add_parser("list", help="every grave found so far")
    tp = sub.add_parser("tombstone", help="Gemma writes the tombstone")
    tp.add_argument("name")
    sp = sub.add_parser("summon", help="ask the dead a question")
    sp.add_argument("name")
    sp.add_argument("question", nargs="?")
    sub.add_parser("selftest", help="check the finder and the citation guard")
    a = ap.parse_args()

    if a.cmd == "selftest":
        return selftest()
    if a.cmd == "find":
        if a.graveyard != "apps" and not a.target:
            sys.exit(f"find {a.graveyard} needs a target.")
        kind, where = {"code": ("code", lambda: Path(a.target).expanduser().resolve().name),
                       "site": ("site", lambda: re.sub(r"^https?://", "", a.target).strip("/")),
                       "apps": ("app", lambda: "applications")}[a.graveyard]
        for old in (GRAVES / kind / where()).glob("*.json"):
            old.unlink()  # a fresh dig replaces the last one; stale graves and tombstones go
        graves = (find_code(a.target, a.skip) if a.graveyard == "code" else
                  [find_site(a.target, a.page)] if a.graveyard == "site" else
                  find_apps(a.days, a.target or "/Applications", a.unrecorded))
        for g in graves:
            save(g)
        print(f"{plural(len(graves), 'grave')} dug:\n")
        print("\n\n".join(describe(g) for g in graves) or "  none (or all skipped)")
        return
    if a.cmd == "list":
        print("\n\n".join(describe(json.loads(f.read_text())) for f in all_graves()) or "No graves yet. Run find.")
        return
    g = load(a.name)
    if a.cmd == "tombstone":
        t = tombstone(g)
        print(describe(g))
        print(f"\n  Cause of death: {t['cause_of_death']}" + (f"  [{', '.join(t['cites'])}]" if t["cites"] else ""))
        print(f"  \"{t['epitaph']}\"")
        return
    if a.question:
        return show_answer(g, *summon(g, a.question))
    print(f"The medium reaches for {g['name']}. Ask it something (empty line to leave).")
    history = []
    while (q := input("\n> ").strip()):
        answer, cites = summon(g, q, history[-6:])
        show_answer(g, answer, cites)
        history += [{"role": "user", "content": q}, {"role": "assistant", "content": answer}]


if __name__ == "__main__":
    main()
