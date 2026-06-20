# Contributing to hpgl-buddy

Thanks for your interest! hpgl-buddy aims to plot HP-GL files **safely and
observably** - it validates, paces the device buffer so an inked pen never
stalls, watches for faults, and logs every wire exchange. Contributions that
keep that bar are very welcome.

The authoritative design, with HP manual citations, lives in
[`DESIGN.md`](DESIGN.md). Read it before non-trivial protocol or execution work.

## Getting set up

Requires Python 3.13.

```bash
git clone https://github.com/hpgl-buddy/hpgl-buddy
cd hpgl-buddy
python -m venv .venv && source .venv/bin/activate
pip install -e . -r requirements-dev.txt   # editable install + pinned dev tooling
```

## Day-to-day

```bash
tox                 # the canonical check: tests on Python 3.13
pytest -q           # tests directly (faster loop)
pyflakes src tests  # must be clean
tox -e build        # build the sdist + wheel
```

No hardware is needed for most work: `check` (offline syntax check), the planner,
flow control, the executor, and the demo generators are all covered by tests with
a fake device. Please add or update tests for behavior changes - the executor and
status layers especially reward a focused regression test.

## House conventions

These are load-bearing, not style preferences:

- **Logging, never `print`.** Everything goes through the standard `logging` module
  so a run is reconstructable from the log alone. Wire traffic is logged at DEBUG.
- **ASCII-only output.** No emoji or non-ASCII in logs or user-facing text.
- **Descriptive names.** Prefer `buffer_free_bytes` over `n`. Match the surrounding code.
- **Errors state what, where, and why** - include the offending command, its file
  sequence index, and source line where relevant.
- **Immediate vs. buffered commands matter.** `ESC.` device-control queries are safe
  mid-plot; HP-GL output instructions (`OS`, `OE`, ...) are buffered and only answered
  at pen-up. See DESIGN.md before adding device traffic.

## Adding a device

Most plotters need only a declarative profile - drop a `<model>.toml` into
`src/hpgl_buddy/devices/profiles/` (buffer size, pen count, serial defaults,
capabilities such as `pen_sensing`). A device with unusual behavior can subclass
`Device`. Please open a [device support issue](https://github.com/hpgl-buddy/hpgl-buddy/issues/new?template=device_support.yml)
first so the field values can be confirmed against the manual, and test on real
hardware if you have it.

## Dependencies

Direct dependencies are hand-maintained as loose ranges and frozen to pinned files
by CI - do not edit the pinned files by hand:

- runtime: `requirements-rough.txt` (kept in sync with `[project.dependencies]`) -> `requirements.txt`
- dev / CI: `requirements-dev-rough.txt` -> `requirements-dev.txt`

Edit the `*-rough.txt` file; the `dependencies_update` workflow regenerates the pins.

## Submitting changes

1. Branch off `master`.
2. Keep commits focused with clear messages - **the release changelog is generated
   from commit messages**, so write them for a reader.
3. Run `tox` and `pyflakes`; update docs (README / `--help` / DESIGN.md) if user-facing.
4. Open a PR using the template. Reference any issue it closes.

Releases are automated: pushing a bumped `src/hpgl_buddy/version.py` to `master` tags
the version, builds artifacts, renders the changelog, and publishes to PyPI.

## Code of conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md). By participating
you agree to uphold it.
