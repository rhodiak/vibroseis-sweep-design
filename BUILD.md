# Building a Windows .exe

Sweep Design is a plain Python + tkinter + matplotlib program, so freezing
it is straightforward. The one thing that is not straightforward is *where*
you run the build.

## You cannot build the .exe on Fedora

PyInstaller (and cx_Freeze, and Nuitka) bundle **the interpreter and the
compiled libraries of the machine they run on**. There is no cross-compile
mode. Running PyInstaller on Linux gives you a Linux binary no matter what
flags you pass. You need Windows — real, virtual, or emulated. Three ways,
best first.

---

## Option A — GitHub Actions (recommended)

No Windows machine required, reproducible, and you get a downloadable zip
every time you push. This project is public domain (The Unlicense), so
there is nothing to keep private.

The project is not currently a git repository. Set one up:

```bash
cd path/to/sweep_design
git init
printf '__pycache__/\n*.pyc\nbuild/\ndist/\nsweep_design_state.json\n' > .gitignore
git add -A && git commit -m "Sweep Design v1.0"
gh repo create vibroseis-sweep-design --public --source=. --push
```

Then create `.github/workflows/windows-build.yml`:

```yaml
name: Windows build

on:
  push:
    branches: [main]
  workflow_dispatch:

jobs:
  build:
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install numpy scipy matplotlib pyinstaller

      - name: Build
        run: pyinstaller --noconfirm --clean sweep_design.spec

      - name: Smoke-test the build
        run: |
          dir dist\SweepDesign
          if (-not (Test-Path dist\SweepDesign\SweepDesign.exe)) { exit 1 }

      - uses: actions/upload-artifact@v4
        with:
          name: SweepDesign-windows
          path: dist/SweepDesign/
```

Push, wait ~4 minutes, download the artifact from the Actions tab. Note
that `windows-latest` runners are x86-64, so the result will not run on ARM
Windows machines.

### Cutting a release

Artifacts expire after 90 days and need a GitHub login to download.
Releases do neither, so that is what to hand people. The workflow builds a
release whenever a `v*` tag is pushed — bump `APP_VERSION` in
`sweep_design.py` first, because the workflow refuses a tag that disagrees
with it:

```bash
# edit APP_VERSION in sweep_design.py, add a row to the version history
# table in README.md, then:
git commit -am "Release v1.1"
git tag -a v1.1 -m "Sweep Design v1.1"
git push && git push --tags
```

Add the README row *before* tagging, not after: the release is built from
the tagged commit, so a row added later is missing from the very build it
describes. Record the known problems of the version being superseded at
the same time — that table is the durable record, and it is the reason
old releases can be left in place instead of deleted.

That builds as usual, zips `dist/SweepDesign/` as
`SweepDesign-v1.1-windows-x64.zip`, and publishes a release with the zip
attached and notes generated from the commits since the last tag.

The version check runs *before* the build, so a mismatched tag costs
seconds rather than four minutes. If you tag by mistake, delete it with
`git push --delete origin v1.1` and delete the release on the repo page —
a published release is visible immediately, so check the tag before
pushing it rather than after.

The workflow declares `permissions: contents: write` for this. Without it
a repository whose default workflow permission is read-only would build
fine and then fail on the very last step.

---

## Option B — a Windows VM on your Fedora box

More setup up front, much faster to iterate afterwards, and you can
actually *use* the program while testing it, which Option A cannot do.

```bash
sudo dnf install virt-manager qemu-kvm libvirt
sudo systemctl enable --now libvirtd
```

Grab a Windows 11 Enterprise evaluation ISO from Microsoft (90 days, free,
no key) or use a licensed image. Give the VM 4 GB RAM and 40 GB disk.
Inside Windows:

1. Install Python 3.12 from python.org — **tick "Add python.exe to PATH"**.
   Do not use the Microsoft Store build; its install layout confuses
   PyInstaller.
2. Share the project folder into the VM (virt-manager → Add Hardware →
   Filesystem, or just use a shared folder / scp).
3. Then follow **The build itself** below.

---

## Option C — Wine (last resort)

Works, but it is the flakiest of the three and hard to debug when it isn't
working. Only worth it if you refuse a VM and won't use CI.

```bash
sudo dnf install wine winetricks
export WINEPREFIX=~/.wine-sweepdesign
export WINEARCH=win64
wineboot -i
# Install a real Windows Python into the prefix:
wget https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe
wine python-3.12.8-amd64.exe /quiet InstallAllUsers=1 PrependPath=1 Include_tcltk=1
wine python -m pip install numpy scipy matplotlib pyinstaller
wine python -m PyInstaller --noconfirm --clean sweep_design.spec
```

The `Include_tcltk=1` is not optional — the default silent install can skip
tkinter, and you will get an opaque failure at freeze time rather than a
clear one. Always test the resulting .exe on real Windows; a Wine-built
binary that runs under Wine can still fail on Windows.

---

## The build itself

Same on any of the three hosts, from the project directory:

```
pip install numpy scipy matplotlib pyinstaller
pyinstaller --noconfirm --clean sweep_design.spec
```

Output lands in `dist\SweepDesign\`. Zip that whole folder — the .exe alone
will not run.

### One folder or one file?

`sweep_design.spec` builds **one folder** by default. For a single .exe:

```
set SD_ONEFILE=1
pyinstaller --noconfirm --clean sweep_design.spec
```

| | one folder | one file |
|---|---|---|
| what you ship | a zip of ~40 files | one `SweepDesign.exe` |
| size | ~200 MB unpacked | ~90 MB |
| startup | under a second | 5–15 s, **every launch** |
| antivirus | occasional complaint | frequent complaint |

One file re-extracts the entire scipy/matplotlib payload to a temp
directory on every single launch. For a program someone opens repeatedly to
compare sweeps, that wait is the wrong trade. Ship the folder unless you
specifically need a single file to email.

---

## Windows-specific things that will bite you

**Settings file location.** `sweep_design.py` resolves this at startup
(`_app_dir()`): running from source it sits next to the script, frozen it
sits next to the .exe. This matters — under a one-file build the naive
`__file__` path points into PyInstaller's temp extraction directory, which
Windows deletes on exit, so every setting would silently vanish between
runs. If you unpack into `C:\Program Files\`, where standard users cannot
write, it falls back to `%APPDATA%\SweepDesign\`. The About box names the
files and says **which of the two folders** was used — "beside the
program" or "per-user config folder (program folder not writable)" — so
you can tell where they went.

It deliberately does **not** print the absolute path, and neither does any
status line or the `--selftest` report. This program gets shared,
screenshotted and demoed, and a full path describes the machine it is
running on rather than the program; the person sharing it did not choose
to publish their directory tree. Everything user-visible goes through
`display_path()`, which reduces a file in the program's folder to its
name, anything under the user's home to `~/...`, and anything else to its
name as well — that last case is where a roaming `%APPDATA%` on a network
share lands, and its real path would name the file server. `--selftest`
enforces this, so a new message that interpolates a raw path will fail the
build rather than reach a release.

**HiDPI displays.** Handled, but worth understanding if you ever touch the
layout code. On a display scaled to 125 % or 150 % — most Windows laptops
— an ordinary process is handed a fictitious smaller screen and the
compositor stretches its output, so plot lines and axis text come out
blurry. `_declare_dpi_aware()` turns that off at startup, before the first
window exists, because Windows fixes a process's awareness at the moment
it starts drawing.

Killing the blur alone would leave everything physically too small. The
figure dpi has to rise with the display scale so a 10 pt label occupies
`10 * dpi / 72` real pixels — but **matplotlib's Tk backend already does
that**, in `_update_device_pixel_ratio()`, which reads the scale out of Tk
and sets `figure.dpi = ratio * figure._original_dpi`. So the figure is
created at a flat `BASE_DPI` and the backend scales it.

Everything drawn on the figure then reads the scale back out of the figure
through `SweepDesignApp.fpx()`, which is `n * fig.dpi / BASE_DPI`, and the
margin budgets are refreshed by `_scale_margins()` on every layout pass —
the dpi changes when the canvas is first mapped, so a value computed at
construction is already stale.

That is the invariant: **a new figure pixel constant must go through
`fpx()`**, never through a separately detected scale factor. v1.0.2 got
this wrong. It multiplied the dpi by its own detected factor *as well*,
so a 150 % display ran at 225 dpi — text 1.5x larger than the margins
reserved for it, and axis labels overlapping the neighbouring panels.
Deriving from `fig.dpi` is self-consistent by construction and cannot
double-count. Point sizes need no help; they ride on the dpi already.

`px()` still exists and is for **Tk widget geometry only** — the window
size, a wraplength — where the figure's dpi is not the relevant quantity.

System DPI awareness is claimed, not per-monitor: Tk 8.6 does not rescale
itself when a window is dragged to a monitor with a different scale, so
claiming per-monitor would promise what the toolkit cannot deliver. The
matplotlib toolbar looks after its own icons once Tk scaling is right.

Two consequences worth knowing. Exported PNGs come out at the display's
dpi, so the same figure saved on a 150 % machine is 1.5x the pixels — same
physical size, more detail. SVG exports are vector and unaffected.

Scaling is auto-detected **only on Windows**, deliberately: an X11 server
can report any DPI it likes and honouring it would resize the program on
machines where nothing was wrong. Override anywhere with the environment
variable, which also forces the backend's own scaling hook so the scaled
*figure* path can be exercised from Linux — matplotlib derives its ratio
from the X server's reported DPI, which is 1.0 on an ordinary desktop, and
not being able to test that path is exactly how the v1.0.2 double-scaling
bug reached a release:

```bash
SWEEP_DESIGN_SCALE=1.5 python sweep_design.py   # force 150%
SWEEP_DESIGN_SCALE=1   SweepDesign.exe          # opt out on Windows
```

**Antivirus false positives.** Unsigned PyInstaller executables get flagged
by SmartScreen and by some scanners, purely for being unsigned and
self-extracting. Nothing you can do short of buying a code-signing
certificate. The spec deliberately does not use UPX compression, which
makes it markedly worse.

**Console window.** The spec sets `console=False`, so no black window
appears behind the GUI. The cost is that a crash before the Tk window opens
leaves no visible message. If the exe dies silently, rebuild with
`console=True` in `sweep_design.spec` and run it from `cmd` to see the
traceback.

---

## Testing the result

The workflow runs `SweepDesign.exe --selftest` against the frozen build and
fails the job if it does not pass. You can run the same check on any copy:

```
SweepDesign.exe --selftest report.txt
```

It writes a real file in every export format the program offers, does a
known-answer check on the engine, checks the vibrator force model by
its slopes (12 dB/octave stroke, 6 dB/octave flow, flat hold-down) and by
confirming drive level is applied exactly once, and round-trips the saved-
vibrator library through a temporary file (including that one malformed
entry is skipped rather than losing the rest), and renders the bundled
README into the HTML manual the Help button opens — checking every heading
survives, no in-page link is dead, no raw markdown reaches the reader and
every fenced code block comes out character for character. It also writes
a SEG-Y file in both sample formats and reads it back with its own reader,
comparing every sample and every header field it wrote, and does the same
for the ASCII table through `numpy.loadtxt` and for the Petrel wavelet
through its own parser — including that `WAVELET-TFS` plus the 0-based
offset column lands back on the same time axis as the SEG-Y delay field,
which is the one way that layout goes quietly wrong. A file of samples,
unlike a figure, carries no visible evidence that it is right, so nothing
short of decoding it again proves the bytes landed where the standard
says. Then it
exits 0 or 1. **The manual check is why `README.md` must stay in the spec's
`datas`**: in a frozen build the manual is read from the bundle
(`sys._MEIPASS`), and dropping it there would leave Help with nothing to
show while every other test still passed. That
specific export test
exists because v1.0 shipped unable to export SVG: matplotlib loads output
backends lazily inside `savefig()`, so PyInstaller never saw the import,
and every other check in this file passed on a build that could not save
the format it defaults to. **A new export format therefore needs adding to
`EXPORT_FORMATS` in `sweep_design.py` and to `hiddenimports` in the spec** --
the self-test enforces the second from the first.

Then, by hand, beyond "it opens":

1. Add three sweeps with different bandwidths — the metrics table should
   show 8 columns and the grid should be centred.
2. Export both a PNG and an SVG. The footer, the metrics key and the table
   must all appear in the exported file, not just on screen.
3. Resize the window from small to maximised. Text must not collide or
   overflow at any size. **Do this in both views** — they share one layout
   solver but not one set of axis labels, and the field view's log-scaled
   force panel carries a second, in-panel legend the sweep view does not.
4. Switch to **Field model** with a heavy and a light vibrator on the plot.
   The two achievable-force curves must separate, each one's knee must sit
   at the vertical marker, and the vibrator readout panel must show the
   settings currently on the Vibrator tab.
5. Toggle the Vibrator tab between SI and field units. The numbers must
   convert and the labels must change together; a value that changes
   without its label (or the reverse) is the bug that turns a 26 t vibrator
   into a 26,000 lb one.
6. Enter a real machine's figures **in field units**, click **Save
   vibrator**, then close and reopen. It must be in the Preset list, and
   `sweep_design_vibrators.json` must hold the pounds and inches you typed,
   under `"units": "Field"`. Then save a second machine with the tab set to
   SI and confirm that entry says `"units": "SI"` with the stroke in
   millimetres. Two entries in different units in one file, both reading
   back to the same forces, is the property that matters here — a value
   written in one system and read in the other turns a 26 t vibrator into a
   26,000 lb one.
7. Close the program, reopen it, and confirm your sweep parameters, the
   vibrator fields, the chosen units and the chosen view came back. This is
   the check that catches a broken state path.
8. Delete `sweep_design_state.json` and reopen. The saved vibrators must
   still be there — that separation is the whole reason they are in their
   own file.
9. Click **Help**. The manual must open in the default browser with its
   contents sidebar, tables and code blocks intact — this is the check
   that catches `README.md` having fallen out of the bundle, which nothing
   about the program's own behaviour would reveal. Then **Print** it and
   confirm the preview drops the sidebar and does not split a table across
   pages.
10. Open **About**. It should be a short box — name, version, file paths,
    licence — not a wall of text. If technical detail has crept back in,
    it belongs in the README instead, where Help will pick it up.
11. Click **Traces (data)...** with two sweeps of different lengths on the
    plot, tick everything, and export. Eight files must appear beside each
    other — four tables plus a `.wlt` per sweep per content, since the
    Petrel layout holds one wavelet each. Then load `..._wavelet.sgy` into
    whatever package you actually use: the zero-lag peak must sit at time
    **zero**, not halfway along
    the trace — that is the check that the negative delay recording time
    survived the trip, and the one thing no amount of testing here can
    settle, because it depends on whether the reader honours the field.
    Load the ASCII file too; every line that is not a `#` comment must be
    the same number of columns.
12. Import `..._wavelet_1.wlt` into the package that wanted the Petrel
    layout. The zero-lag peak must again land at time **zero**, which is
    the check that `WAVELET-TFS` was read — the time column in the file
    starts at 0 either way, so a package that ignores `WAVELET-TFS` shows
    the peak half a wavelet late and looks perfectly plausible doing it.
14. Repeat that export with **Half, from zero lag** and half the sample
    count. The file must be half the size and start at the peak.
15. Put two sweeps at *different sample intervals* on the plot and try to
    export. It must refuse with a message naming both intervals, and it
    must refuse **before** asking where to put the file.
16. Test on a machine that has never had Python installed. A missing
    runtime DLL will only show up there.
