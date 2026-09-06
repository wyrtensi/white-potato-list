# white-potato-list

A merged catalogue of white potato varieties, rebuilt automatically from the
upstream registers four times a day.

Several registers exist, none of them complete. One is thorough but has not
been revised in months; another is current but much smaller; a third covers a
handful of varieties the other two miss entirely. This repository takes the
union, drops entries a broader listing already covers, and publishes the result
as a single binary catalogue.

## Contents

| File | What it is |
|---|---|
| `seed-catalog.dat` | the merged catalogue, binary |
| `HARVEST.LINK` | profile link pointing at the current catalogue |
| `seed-sources.lock` | what each upstream contributed, with checksums |
| `varieties/homegrown` | hand-tended additions, merged on top |
| `harvest.py` | the whole build, standard library only |

## Using it

The catalogue is attached to every release and served from jsDelivr:

```
https://cdn.jsdelivr.net/gh/wyrtensi/white-potato-list@<tag>/seed-catalog.dat
```

Tags are `YYYYMMDDHHMM` in UTC. `HARVEST.LINK` always points at the newest one.

## Adding a variety by hand

Put it in `varieties/homegrown`, one per line, `#` for comments. It is merged
with everything else on the next run. Use it when an upstream has not caught up
yet — and remove it once one has, so the file stays short.

## How it keeps itself honest

An automatic build that never fails looks healthy whether or not it is doing
anything. Three checks exist so that this one cannot quietly rot:

- **Every upstream must answer.** If any register is unreachable or parses to
  nothing, the run fails. A catalogue built from two sources out of three would
  otherwise look perfectly normal.
- **The yield has a floor.** A harvest smaller than 90% of the previous one
  aborts. This is what a register serving an error page looks like from here.
- **Unchanged means untouched.** If the merged catalogue is byte-identical to
  the last one, nothing is written and nothing is committed — no tag, no
  release, no timestamp bump. So the date on the newest commit means the
  contents actually changed on that date, which is the only thing that makes a
  commit history worth reading.

The catalogue is also parsed back immediately after being written and the entry
counts are compared. A malformed catalogue is worse than a missing one, because
readers fail silently on it.

## Licence

MIT.
