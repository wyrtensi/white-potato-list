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

# The register: one 70 MB file carrying 1543 sections, of which we read about
# thirty.  Its predecessor, russia-blocked-geosite, stopped publishing on
# 2026-08-10; the sections we took from it were identical to these on the day
# we moved, so the move is for breadth, not for freshness.
REGISTER_URL = (
    "https://github.com/runetfreedom/russia-v2ray-rules-dat/releases/latest"
    "/download/geosite.dat"
)

# Russian services, by category.  Everything here is meant to reach the user
# without going abroad first: banks refuse foreign addresses, delivery and
# government sites geofence, and a foreign exit makes them slow or unusable.
REGISTER_SECTIONS = (
    "CATEGORY-RU", "CATEGORY-BANK-RU", "CATEGORY-ECOMMERCE-RU",
    "CATEGORY-MEDIA-RU", "CATEGORY-RETAIL-RU", "CATEGORY-GOV-RU",
    "CATEGORY-MEDICINE-RU", "CATEGORY-TRAVEL-RU", "CATEGORY-EDUCATION-RU",
    "CATEGORY-ENTERTAINMENT-RU", "CATEGORY-TECH-MEDIA-RU",
    "CATEGORY-FORUMS-RU", "CATEGORY-AI-RU", "CATEGORY-BETTING-RU",
    "RU-AVAILABLE-ONLY-INSIDE", "SBER", "YANDEX", "MAILRU", "TBANK-RU",
    "MTS-RU", "T2-RU", "AUTORU", "REGRU", "NIC-RU",
)
CELLAR_SECTION = "PRIVATE"

# Read, never published: the list of what is blocked inside Russia, used only
# to refuse entries that would route a blocked site around the tunnel.
BLOCKED_SECTION = "RU-BLOCKED"

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

# Fields the harvest overwrites on the inherited profile.  They change on
# every run by construction, so they are excluded when deciding whether
# the upstream profile actually moved.
OURS = ("Name", "Geositeurl", "LastUpdated")

CATALOGUE = "seed-catalog.dat"
LOCKFILE = "seed-sources.lock"
LINKFILE = "HARVEST.LINK"

# A harvest that loses more than a tenth of last season's yield is a failed
# harvest, not a small one -- almost always an upstream serving an error page.
YIELD_FLOOR = 0.90

# And one that doubles is just as suspect: that is what it looks like when an
# upstream tips a blocklist into a category by mistake.  The floor alone would
# wave that through.
YIELD_CEILING = 2.00

# How much of the previous direct list may stop being covered before the run
# is treated as a structural loss rather than upstream tidying.  Coverage is
# not the same as the entry count: pruning removes entries without removing
# what they matched, and a growing list can still cover less.
COVERAGE_SLACK = 0.01

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


def read_catalogue(blob, wanted=None):
    """Return {SECTION: [(kind, value)]} from a catalogue blob.

    `wanted` limits which sections are decoded.  The register carries 2.9
    million entries across 1543 sections and this harvest reads thirty of
    them; walking past the rest instead of building tuples for it turns a
    24-second parse into a couple of seconds.  The name is the first field
    of a section, so an unwanted one is abandoned before its entries are
    touched.
    """
    sections = {}
    for num, payload in _fields(blob, 0, len(blob)):
        if num != 1:
            continue
        name, entries = None, []
        for n2, p2 in _fields(payload, 0, len(payload)):
            if n2 == 1 and isinstance(p2, bytes):
                name = p2.decode()
                if wanted is not None and name.upper() not in wanted:
                    break
            elif n2 == 2 and isinstance(p2, bytes):
                kind, value = SUFFIX, None
                for n3, p3 in _fields(p2, 0, len(p2)):
                    if n3 == 1 and isinstance(p3, int):
                        kind = p3
                    elif n3 == 2 and isinstance(p3, bytes):
                        value = p3.decode()
                if value:
                    entries.append((kind, value))
        if name and not (wanted is not None and name.upper() not in wanted):
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


def blocked_index(blocked):
    """Map every suffix of every blocked domain to the domains under it.

    Built once so that asking "what would this suffix drag along" is a dict
    lookup rather than a scan of seventy thousand strings per candidate.
    """
    index = {}
    for kind, value in blocked:
        if kind not in (SUFFIX, EXACT):
            continue
        parts = value.split(".")
        for i in range(len(parts)):
            index.setdefault(".".join(parts[i:]), []).append(value)
    return index


def captures(entry, index, blocked_values):
    """Which blocked domains this one entry would route direct."""
    kind, value = entry
    if kind in (SUFFIX, EXACT):
        hit = index.get(value, ())
        return hit if kind == SUFFIX else [d for d in hit if d == value]
    if kind == PLAIN:
        return [d for d in blocked_values if value in d]
    return []   # a regex is not analysed; it is admitted as written


def covered_by(entry, suffixes, exact, keywords):
    """Would this pool of entries still route `entry`'s domains direct?"""
    kind, value = entry
    if kind in (SUFFIX, EXACT):
        parts = value.split(".")
        if value in exact and kind == EXACT:
            return True
        if any(".".join(parts[i:]) in suffixes for i in range(len(parts))):
            return True
    return any(w in value for w in keywords)


def admit(rows, prior, index, blocked_values):
    """Keep the rows that do not open a hole, and say what was refused.

    The rule is one line: a *new* entry may not capture a domain blocked in
    Russia that the previous list did not already capture.  That is what
    rejects the bare TLDs `ru`, `su` and `xn--p1ai` hiding inside a category
    of Russian services -- one of them alone would route nineteen thousand
    blocked domains around the tunnel -- and it rejects the free-hosting
    suffixes `at.ua` and `ucoz.*` for the same reason, without either being
    named anywhere.

    Entries the previous list already had are grandfathered.  Otherwise a
    single new blocked domain under, say, `spb.ru` would silently withdraw
    every `spb.ru` site from the direct route.
    """
    known = set(prior)
    already = set()
    for entry in prior:
        already.update(captures(entry, index, blocked_values))

    kept, refused = [], []
    for entry in rows:
        if entry in known:
            kept.append(entry)
            continue
        drags = [d for d in captures(entry, index, blocked_values)
                 if d not in already]
        if drags:
            refused.append((entry, len(drags)))
        else:
            kept.append(entry)
    return kept, refused, already


def source_fingerprint():
    """Hash of the source configuration.

    The yield guards exist to catch an upstream serving nonsense.  Changing
    which sources are read is not that, and the first run after such a change
    legitimately moves the yield a long way, so the guards stand down for
    exactly one run and come back with the new baseline.
    """
    config = {
        "register": [REGISTER_URL, sorted(REGISTER_SECTIONS),
                     CELLAR_SECTION, BLOCKED_SECTION],
        "plain": sorted(PLAIN_SOURCES.items()),
        "culled": sorted(CULLED.items()),
    }
    return hashlib.sha256(
        json.dumps(config, sort_keys=True).encode()).hexdigest()


def previous_whitelist():
    """The direct list as published, read back from the catalogue on disk."""
    if not os.path.exists(CATALOGUE):
        return []
    try:
        return read_catalogue(open(CATALOGUE, "rb").read(),
                              {"WHITELIST"}).get("WHITELIST", [])
    except Exception:
        return []


def last_season():
    """The previous lockfile, or an empty one."""
    if not os.path.exists(LOCKFILE):
        return {}
    try:
        return json.load(open(LOCKFILE))
    except Exception:
        return {}


def profile_fingerprint(profile):
    """Fingerprint the inherited half of the profile.

    Upstream pins the region register by tag and re-cuts it most days, so
    the profile moves even when no catalogue does.  Hashing everything
    except the fields we write ourselves catches that, and catches the next
    field they change without having to know its name in advance.
    """
    inherited = {k: v for k, v in profile.items() if k not in OURS}
    return hashlib.sha256(json.dumps(
        inherited, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def announce(grew, drifted):
    """Tell the workflow what moved: the catalogue, the profile, or neither.

    They are reported separately because only a new catalogue earns a tag
    and a release; a profile-only change is a commit and nothing more.
    """
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as fh:
            fh.write("changed=%s\n" % ("true" if grew else "false"))
            fh.write("relinked=%s\n" % ("true" if drifted else "false"))


def main():
    stamp = time.strftime("%Y%m%d%H%M", time.gmtime())
    provenance = {}

    register = fetch(REGISTER_URL, "variety register")
    provenance["register"] = {
        "url": REGISTER_URL,
        "bytes": len(register),
        "sha256": hashlib.sha256(register).hexdigest(),
    }
    wanted_sections = set(REGISTER_SECTIONS) | {CELLAR_SECTION, BLOCKED_SECTION}
    sections = read_catalogue(register, wanted_sections)

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

    blocked = [(k, v.lower().lstrip(".")) for k, v in
               sections.get(BLOCKED_SECTION, [])]
    if not blocked:
        sys.exit("harvest aborted: register section %s is empty" % BLOCKED_SECTION)

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

    prior = previous_whitelist()
    index = blocked_index(blocked)
    blocked_values = [v for k, v in blocked]
    # Admit before pruning, never after: pruning collapses a child into its
    # parent suffix, so pruning first and then refusing the parent would
    # take the children with it.
    rows, refused, already = admit(rows, prior, index, blocked_values)
    whitelist = prune(rows)
    private = prune([(k, v.lower().lstrip(".")) for k, v in cellar])

    provenance["refused"] = {
        "count": len(refused),
        "worst": ["%s (%d blocked)" % (v, n) for (k, v), n
                  in sorted(refused, key=lambda r: -r[1])[:10]],
    }

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

    prev = last_season()
    prev_cat = prev.get("catalogue") or {}
    before = int(prev_cat.get("whitelist") or 0)
    before_sha = prev_cat.get("sha256")
    before_profile = (prev.get("profile") or {}).get("sha256")
    before_stamp = prev.get("harvested")
    fingerprint_now = source_fingerprint()
    same_sources = (prev.get("sources") or {}).get("sha256") == fingerprint_now

    if before and same_sources:
        if len(whitelist) < before * YIELD_FLOOR:
            sys.exit(
                "harvest aborted: yield %d is below %.0f%% of last season's %d"
                % (len(whitelist), YIELD_FLOOR * 100, before)
            )
        if len(whitelist) > before * YIELD_CEILING:
            sys.exit(
                "harvest aborted: yield %d is above %.0fx last season's %d"
                % (len(whitelist), YIELD_CEILING, before)
            )
    elif before:
        print("source configuration changed; yield guards stand down for "
              "this run and resume from the new baseline")

    # Coverage, not count.  Pruning drops entries without dropping what they
    # matched, so a longer list can still route less; only this comparison
    # would notice.
    suffixes = {v for k, v in whitelist if k == SUFFIX}
    exact = {v for k, v in whitelist if k == EXACT}
    keywords = [v for k, v in whitelist if k == PLAIN]
    lost = [e for e in prior
            if e not in set(whitelist)
            and not covered_by(e, suffixes, exact, keywords)]
    allowance = max(1, int(len(prior) * COVERAGE_SLACK))
    if len(lost) > allowance:
        sys.exit("harvest aborted: %d entries of the previous direct list are "
                 "no longer covered (allowance %d): %s"
                 % (len(lost), allowance,
                    ", ".join(v for k, v in lost[:12])))

    reach = set()
    for entry in whitelist:
        reach.update(captures(entry, index, blocked_values))
    leaked = sorted(reach - already)
    if len(leaked) > allowance:
        sys.exit("harvest aborted: %d blocked domains would newly be routed "
                 "direct: %s" % (len(leaked), ", ".join(leaked[:12])))

    provenance["guards"] = {
        "coverage_lost": [v for k, v in lost],
        "blocked_direct": len(reach),
        "blocked_direct_new": leaked,
    }

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
    profile = load_profile()
    wanted = referenced_sections(profile)
    missing = sorted(w for w in wanted if w not in sections)
    if missing:
        sys.exit("harvest aborted: profile references section(s) the catalogue "
                 "does not carry: %s" % ", ".join(missing))

    digest = hashlib.sha256(blob).hexdigest()
    fingerprint = profile_fingerprint(profile)

    grew = digest != before_sha
    # The profile is inherited whole from upstream, including its pin on the
    # region register, which upstream re-cuts most days.  That pin reaches
    # subscribers only through our link, so a profile that moved has to be
    # rebuilt even on the many days when no catalogue does.
    drifted = fingerprint != before_profile

    if not grew and not drifted:
        # Leave every file untouched -- the link carries a timestamp that
        # would otherwise churn on its own and make a dead harvest look
        # like a live one.
        print("whitelist %d, unchanged since %s" % (len(whitelist), before_sha[:12]))
        announce(False, False)
        return

    if grew:
        open(CATALOGUE, "wb").write(blob)
        published = stamp
    else:
        # No new catalogue means no new release for the link to point at,
        # so the pin stays on the one that is actually published.
        published = before_stamp

    link = build_link(published, profile)

    provenance["catalogue"] = dict(
        {name.lower(): len(entries) for name, entries in sections.items()},
        bytes=len(blob), sha256=digest)
    provenance["profile"] = {"url": PROFILE_URL, "sha256": fingerprint,
                             "region_register": profile.get("Geoipurl")}
    provenance["sources"] = {"sha256": fingerprint_now}
    provenance["harvested"] = published
    provenance["relinked"] = stamp
    json.dump(provenance, open(LOCKFILE, "w"), indent=2, sort_keys=True)
    open(LOCKFILE, "a").write("\n")

    if grew:
        print("%s, %d bytes" % (", ".join("%s %d" % (n.lower(), len(v))
                                          for n, v in sorted(sections.items())),
                                len(blob)))
        if before:
            print("previous whitelist %d (%+d)" % (before, len(whitelist) - before))
        print("refused %d entries that would have dragged blocked domains "
              "direct%s" % (len(refused),
                            "; worst: " + ", ".join(
                                "%s (%d)" % (v, n) for (k, v), n in
                                sorted(refused, key=lambda r: -r[1])[:5])
                            if refused else ""))
        print("blocked domains routed direct: %d (was %d), coverage lost: %d"
              % (len(reach), len(already), len(lost)))
    else:
        print("catalogue unchanged (%s), upstream profile moved" % digest[:12])
        print("region register now %s" % provenance["profile"]["region_register"])
    print("link %d bytes, pinned to %s" % (len(link), published))
    print("::notice::%s %s -> %d varieties"
          % ("harvest" if grew else "relink", published, len(whitelist)))
    announce(grew, drifted)


_PROFILE = []


def load_profile():
    """The upstream profile, decoded, fetched at most once per run."""
    if not _PROFILE:
        raw = fetch(PROFILE_URL, "upstream profile").decode("utf-8", "replace").strip()
        marker = "happ://routing/onadd/"
        if not raw.startswith(marker):
            sys.exit("harvest aborted: upstream profile is not in the expected form")
        _PROFILE.append(json.loads(base64.b64decode(raw[len(marker):])))
    # A copy, so a caller that edits the profile cannot move the fingerprint.
    return json.loads(json.dumps(_PROFILE[0]))


def referenced_sections(profile):
    """Every catalogue section the profile names, upper-cased."""
    wanted = set()
    for key in ("DirectSites", "ProxySites", "BlockSites"):
        for entry in profile.get(key) or []:
            if isinstance(entry, str) and entry.startswith("geosite:"):
                wanted.add(entry.split(":", 1)[1].upper())
    return wanted


def build_link(stamp, profile):
    """Rebuild the field profile, keeping the upstream's region register pin.

    Only the variety-catalogue URL and the timestamp are ours; everything else
    is inherited, so upstream changes to the profile shape carry over.
    """
    marker = "happ://routing/onadd/"

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
