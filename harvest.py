#!/usr/bin/env python3
"""Harvest the seed catalogue.

Pulls the upstream variety registers, merges them into a single catalogue and
writes it out in the binary format the field readers expect.  Standard library
only, on purpose: this file is short enough to audit in one sitting and nothing
it depends on can be swapped underneath it.
"""

import base64
import hashlib
import json
import os
import ssl
import sys
import time
import urllib.request

# --- Where the varieties come from -----------------------------------------

# One 5 MB register carries three of the four inputs, so it is fetched once and
# read three times rather than pulled per category.
REGISTER_URL = (
    "https://github.com/runetfreedom/russia-blocked-geosite/releases/latest"
    "/download/geosite-ru-only.dat"
)
REGISTER_SECTIONS = ("RU-AVAILABLE-ONLY-INSIDE", "CATEGORY-GOV-RU")
CELLAR_SECTION = "PRIVATE"

PLAIN_SOURCES = {
    "orchard": "https://raw.githubusercontent.com/hydraponique/roscomvpn-geosite/master/data/whitelist",
    "allotment": "https://raw.githubusercontent.com/itdoginfo/allow-domains/main/Russia/outside-raw.lst",
}

# The profile does not only route around things, it also drops some.  Those
# sections have to exist in the catalogue too, or the core refuses to start
# with "section ... is missing" and the client is dead on arrival.
CULLED = {
    "WIN-SPY": "https://raw.githubusercontent.com/hydraponique/roscomvpn-geosite/master/data/win-spy",
    "TORRENT": "https://raw.githubusercontent.com/hydraponique/roscomvpn-geosite/master/data/torrent",
    "CATEGORY-ADS": "https://raw.githubusercontent.com/hydraponique/roscomvpn-geosite/master/data/category-ads",
}

# The upstream profile we inherit the region register pin from.
PROFILE_URL = (
    "https://raw.githubusercontent.com/hydraponique/roscomvpn-routing/main"
    "/HAPP/WHITELIST.DEEPLINK"
)

# Hand-tended rows, merged on top of everything above.  Same syntax as the
# plain registers: one entry per line, "#" starts a comment.
HOMEGROWN = os.path.join("varieties", "homegrown")

CATALOGUE = "seed-catalog.dat"
LOCKFILE = "seed-sources.lock"
LINKFILE = "HARVEST.LINK"

# A harvest that loses more than a tenth of last season's yield is a failed
# harvest, not a small one -- almost always an upstream serving an error page.
YIELD_FLOOR = 0.90

# Entry kinds, in the order the binary format numbers them.
PLAIN, REGEX, SUFFIX, EXACT = 0, 1, 2, 3
PREFIXES = {"full:": EXACT, "keyword:": PLAIN, "regexp:": REGEX, "domain:": SUFFIX}


# --- Binary format ----------------------------------------------------------
#
#   Catalogue { repeated Section section = 1 }
#   Section   { string name = 1; repeated Entry entry = 2 }
#   Entry     { Kind kind = 1; string value = 2 }


def _varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _read_varint(buf, i):
    r = s = 0
    while True:
        x = buf[i]
        i += 1
        r |= (x & 0x7F) << s
        if not x & 0x80:
            return r, i
        s += 7


def _tagged(field, payload):
    return _varint(field << 3 | 2) + _varint(len(payload)) + payload


def _fields(buf, start, end):
    i = start
    while i < end:
        key, i = _read_varint(buf, i)
        num, wire = key >> 3, key & 7
        if wire == 0:
            val, i = _read_varint(buf, i)
            yield num, val
        elif wire == 2:
            size, i = _read_varint(buf, i)
            chunk = buf[i:i + size]
            i += size
            yield num, chunk
        else:
            raise ValueError("unsupported wire type %d" % wire)


def read_catalogue(blob):
    """Return {SECTION: [(kind, value)]} from a catalogue blob."""
    sections = {}
    for num, payload in _fields(blob, 0, len(blob)):
        if num != 1:
            continue
        name, entries = None, []
        for n2, p2 in _fields(payload, 0, len(payload)):
            if n2 == 1 and isinstance(p2, bytes):
                name = p2.decode()
            elif n2 == 2 and isinstance(p2, bytes):
                kind, value = SUFFIX, None
                for n3, p3 in _fields(p2, 0, len(p2)):
                    if n3 == 1 and isinstance(p3, int):
                        kind = p3
                    elif n3 == 2 and isinstance(p3, bytes):
                        value = p3.decode()
                if value:
                    entries.append((kind, value))
        if name:
            sections[name.upper()] = entries
    return sections


def write_catalogue(sections):
    """Serialise {SECTION: [(kind, value)]} back into a catalogue blob."""
    out = bytearray()
    for name in sorted(sections):
        body = bytearray(_tagged(1, name.encode()))
        for kind, value in sections[name]:
            entry = bytearray()
            if kind:  # kind 0 is the default and is left implicit
                entry += _varint(1 << 3 | 0) + _varint(kind)
            entry += _tagged(2, value.encode())
            body += _tagged(2, bytes(entry))
        out += _tagged(1, bytes(body))
    return bytes(out)


# --- Harvesting -------------------------------------------------------------


def fetch(url, what):
    """Fetch a source, or die.  Never harvest from a partial set of fields."""
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"User-Agent": "seed-catalogue/1"})
    try:
        with urllib.request.urlopen(req, timeout=120, context=ctx) as r:
            data = r.read()
    except Exception as exc:
        sys.exit("harvest aborted: %s unreachable (%s)" % (what, exc))
    if not data:
        sys.exit("harvest aborted: %s returned nothing" % what)
    return data


def parse_lines(text):
    """Read a plain register.  v2fly-style prefixes and comments are honoured."""
    rows = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        line = line.split()[0]
        if line.startswith("include:"):
            continue  # registers we pull are flat; nothing to recurse into
        kind = SUFFIX
        for prefix, k in PREFIXES.items():
            if line.startswith(prefix):
                kind, line = k, line[len(prefix):]
                break
        line = line.strip().lower().lstrip(".")
        if line:
            rows.append((kind, line))
    return rows


def prune(rows):
    """Drop entries a broader suffix already covers, and duplicates."""
    suffixes = {v for k, v in rows if k == SUFFIX}
    kept, seen = [], set()
    for kind, value in sorted(rows, key=lambda r: (r[0], r[1])):
        if (kind, value) in seen:
            continue
        if kind == SUFFIX:
            parts = value.split(".")
            if any(".".join(parts[i:]) in suffixes for i in range(1, len(parts))):
                continue  # a parent suffix already matches this
        seen.add((kind, value))
        kept.append((kind, value))
    return kept


def last_season():
    """Yield and fingerprint of the previous harvest, or (0, None)."""
    if not os.path.exists(LOCKFILE):
        return 0, None
    try:
        prev = json.load(open(LOCKFILE))["catalogue"]
        return int(prev["whitelist"]), prev.get("sha256")
    except Exception:
        return 0, None


def announce(changed):
    """Tell the workflow whether anything actually moved."""
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as fh:
            fh.write("changed=%s\n" % ("true" if changed else "false"))


def main():
    stamp = time.strftime("%Y%m%d%H%M", time.gmtime())
    provenance = {}

    register = fetch(REGISTER_URL, "variety register")
    provenance["register"] = {
        "url": REGISTER_URL,
        "bytes": len(register),
        "sha256": hashlib.sha256(register).hexdigest(),
    }
    sections = read_catalogue(register)

    rows = []
    for name in REGISTER_SECTIONS:
        got = sections.get(name, [])
        if not got:
            sys.exit("harvest aborted: register section %s is empty" % name)
        provenance.setdefault("register", {}).setdefault("sections", {})[name] = len(got)
        rows += [(k, v.lower().lstrip(".")) for k, v in got]

    cellar = sections.get(CELLAR_SECTION, [])
    if not cellar:
        sys.exit("harvest aborted: register section %s is empty" % CELLAR_SECTION)

    for label, url in PLAIN_SOURCES.items():
        blob = fetch(url, label)
        got = parse_lines(blob.decode("utf-8", "replace"))
        if not got:
            sys.exit("harvest aborted: %s parsed to nothing" % label)
        provenance[label] = {
            "url": url,
            "entries": len(got),
            "sha256": hashlib.sha256(blob).hexdigest(),
        }
        rows += got

    # Anything grown here at home, for the days an upstream has not caught up.
    if os.path.exists(HOMEGROWN):
        got = parse_lines(open(HOMEGROWN, encoding="utf-8").read())
        provenance["homegrown"] = {"path": HOMEGROWN, "entries": len(got)}
        rows += got

    whitelist = prune(rows)
    private = prune([(k, v.lower().lstrip(".")) for k, v in cellar])

    sections = {"WHITELIST": whitelist, "PRIVATE": private}
    for name, url in CULLED.items():
        blob = fetch(url, name.lower())
        got = parse_lines(blob.decode("utf-8", "replace"))
        if not got:
            sys.exit("harvest aborted: %s parsed to nothing" % name)
        provenance[name.lower()] = {
            "url": url,
            "entries": len(got),
            "sha256": hashlib.sha256(blob).hexdigest(),
        }
        sections[name] = prune(got)

    before, before_sha = last_season()
    if before and len(whitelist) < before * YIELD_FLOOR:
        sys.exit(
            "harvest aborted: yield %d is below %.0f%% of last season's %d"
            % (len(whitelist), YIELD_FLOOR * 100, before)
        )

    blob = write_catalogue(sections)
    # Round-trip what we just wrote; a catalogue that cannot be read back is
    # worse than no catalogue, because the field reader fails silently.
    back = read_catalogue(blob)
    for name, entries in sections.items():
        if len(back.get(name, [])) != len(entries):
            sys.exit("harvest aborted: catalogue failed its own round-trip check")

    # The profile names the sections it expects.  A catalogue missing even one
    # of them does not degrade -- the core refuses to start at all -- so this
    # is checked before anything is published, not after.
    wanted = referenced_sections()
    missing = sorted(w for w in wanted if w not in sections)
    if missing:
        sys.exit("harvest aborted: profile references section(s) the catalogue "
                 "does not carry: %s" % ", ".join(missing))

    digest = hashlib.sha256(blob).hexdigest()
    if digest == before_sha:
        # Nothing grew.  Leave every file untouched -- including the profile,
        # whose timestamp would otherwise churn on its own and make a dead
        # harvest look like a live one.
        print("whitelist %d, unchanged since %s" % (len(whitelist), before_sha[:12]))
        announce(False)
        return

    open(CATALOGUE, "wb").write(blob)
    link = build_link(stamp)

    provenance["catalogue"] = dict(
        {name.lower(): len(entries) for name, entries in sections.items()},
        bytes=len(blob), sha256=digest)
    provenance["harvested"] = stamp
    json.dump(provenance, open(LOCKFILE, "w"), indent=2, sort_keys=True)
    open(LOCKFILE, "a").write("\n")

    print("%s, %d bytes" % (", ".join("%s %d" % (n.lower(), len(v))
                                      for n, v in sorted(sections.items())), len(blob)))
    if before:
        print("previous whitelist %d (%+d)" % (before, len(whitelist) - before))
    print("link %d bytes" % len(link))
    print("::notice::harvest %s -> %d varieties" % (stamp, len(whitelist)))
    announce(True)


def load_profile():
    """The upstream profile, decoded."""
    raw = fetch(PROFILE_URL, "upstream profile").decode("utf-8", "replace").strip()
    marker = "happ://routing/onadd/"
    if not raw.startswith(marker):
        sys.exit("harvest aborted: upstream profile is not in the expected form")
    return json.loads(base64.b64decode(raw[len(marker):]))


def referenced_sections():
    """Every catalogue section the profile names, upper-cased."""
    profile = load_profile()
    wanted = set()
    for key in ("DirectSites", "ProxySites", "BlockSites"):
        for entry in profile.get(key) or []:
            if isinstance(entry, str) and entry.startswith("geosite:"):
                wanted.add(entry.split(":", 1)[1].upper())
    return wanted


def build_link(stamp):
    """Rebuild the field profile, keeping the upstream's region register pin.

    Only the variety-catalogue URL and the timestamp are ours; everything else
    is inherited, so upstream changes to the profile shape carry over.
    """
    marker = "happ://routing/onadd/"
    profile = load_profile()

    repo = os.environ.get("GITHUB_REPOSITORY", "wyrtensi/white-potato-list")
    profile["Name"] = "White Potato"
    profile["Geositeurl"] = (
        "https://cdn.jsdelivr.net/gh/%s@%s/%s" % (repo, stamp, CATALOGUE)
    )
    profile["LastUpdated"] = str(int(time.time()))

    packed = base64.b64encode(
        json.dumps(profile, ensure_ascii=False, separators=(",", ":")).encode()
    ).decode()
    link = marker + packed
    open(LINKFILE, "w").write(link + "\n")
    return link


if __name__ == "__main__":
    main()
