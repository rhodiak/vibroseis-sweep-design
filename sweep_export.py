"""Write sweeps and correlation wavelets out as ASCII tables or SEG-Y files.

Kept separate from sweep_engine.py, which is physics with no file I/O, and
from sweep_design.py, which is the GUI: everything here is byte layout, and
byte layout is the part that has to be exactly right and is worth testing on
its own. Nothing beyond numpy is imported, so a frozen build gains no new
dependency -- writing SEG-Y rev 1 by hand is a few hundred lines of struct
packing, and a third-party reader/writer would be a much larger thing to
bundle for the one direction we need.

What comes out:

- ASCII: one whitespace-delimited table, a `#` comment block naming every
  trace and the conventions above it, then a time column followed by one
  amplitude column per trace. Loads with numpy.loadtxt, awk, Excel or a
  text editor without anything having to know the format.

- Petrel ASCII wavelet: the keyword-block layout Petrel and its relatives
  read -- WAVELET-NAME / WAVELET-TFS / SAMPLE-RATE / WAVELET-DESC, then
  EOH, a two-column table, EOD. ONE wavelet per file, and the time column
  is a 0-based offset with the real start time carried in WAVELET-TFS.
  This is a separate writer, not a mode of the ASCII one: the ASCII table
  exists so several sweeps compare in one file, which this layout cannot
  express.

- SEG-Y: revision 1, big-endian, fixed trace length, IBM or IEEE 32-bit
  floats, one trace per sweep. The sweep-description fields in the trace
  header (bytes 127-140: start/end frequency, sweep length, sweep type,
  taper lengths and taper type) are filled in from the sweep's own
  parameters, and `corr` (125-126) marks a correlation wavelet as
  correlated, so a reader that shows those fields shows the real design.
  A wavelet's negative lags are carried in `delrt` (109-110), which is a
  SIGNED value, so the zero-lag peak lands at time zero rather than half a
  wavelet into the trace.

SEG-Y sizes: the sample count and interval live in 16-bit header fields.
The interval is in microseconds, so anything up to 65.535 ms is fine. The
sample count is read as unsigned by most software but as SIGNED by some,
so SEGY_SAFE_SAMPLES marks where that disagreement starts; the caller is
expected to warn rather than to refuse.
"""

import struct
import numpy as np

__all__ = [
    "SEGY_SAMPLE_FORMATS", "SEGY_MAX_SAMPLES", "SEGY_SAFE_SAMPLES",
    "ieee_to_ibm", "ibm_to_ieee", "textual_header", "sweep_type_code",
    "taper_type_code", "write_segy", "write_ascii", "read_segy",
    "write_petrel_wavelet", "read_petrel_wavelet", "PETREL_KEY_WIDTH",
]

# (name, SEG-Y format code, tooltip). Order is the order the radio buttons
# appear in; the first is the default.
SEGY_SAMPLE_FORMATS = (
    ("IBM", 1,
     "IBM 32-bit float (format code 1) -- the historical SEG-Y default. "
     "Read by everything, including older acquisition and processing "
     "packages. Slightly lossy: about 7 significant digits."),
    ("IEEE", 5,
     "IEEE 32-bit float (format code 5) -- exact for the values this "
     "program computes, and standard since SEG-Y revision 1. Choose IBM "
     "instead if your software is old enough to reject it."),
)

TEXT_HEADER_LINES = 40
TEXT_HEADER_WIDTH = 80
SEGY_MAX_SAMPLES = 65535     # widest a 16-bit sample count can express
SEGY_SAFE_SAMPLES = 32767    # widest that is safe if a reader treats it signed

# Trace-header fields this module writes, as {name: (0-based offset, code)}.
# Only the fields that are actually filled are listed; everything else in
# the 240-byte header stays zero, which is what a reader expects for
# information that was never recorded.
TRACE_FIELDS = {
    "tracl": (0, ">i"),      # trace sequence number within line
    "tracr": (4, ">i"),      # trace sequence number within file
    "fldr": (8, ">i"),       # original field record number
    "tracf": (12, ">i"),     # trace number within the field record
    "ep": (16, ">i"),        # energy source point number
    "cdp": (20, ">i"),
    "cdpt": (24, ">i"),
    "trid": (28, ">h"),      # 1 = seismic data, 6 = sweep
    "nvs": (30, ">h"),       # number of vertically summed traces
    "nhs": (32, ">h"),       # number of horizontally stacked traces
    "duse": (34, ">h"),      # 1 = production
    "scalel": (68, ">h"),
    "scalco": (70, ">h"),
    "delrt": (108, ">h"),    # delay recording time, ms -- SIGNED
    "ns": (114, ">H"),       # samples in this trace
    "dt": (116, ">H"),       # sample interval, microseconds
    "corr": (124, ">h"),     # 1 = no, 2 = yes
    "sfs": (126, ">h"),      # sweep frequency at start, Hz
    "sfe": (128, ">h"),      # sweep frequency at end, Hz
    "slen": (130, ">h"),     # sweep length, ms
    "styp": (132, ">h"),     # 1 linear, 2 parabolic, 3 exponential, 4 other
    "stas": (134, ">h"),     # sweep trace taper length at start, ms
    "stae": (136, ">h"),     # taper length at end, ms
    "tatyp": (138, ">h"),    # 1 linear, 2 cos^2, 3 other
    "tvmu": (202, ">h"),     # trace value measurement unit (8 = newton)
}

# Binary-header fields, same convention, offsets 0-based inside the 400-byte
# block (i.e. subtract 3201 from the byte numbers in the standard).
BINARY_FIELDS = {
    "jobid": (0, ">i"), "lino": (4, ">i"), "reno": (8, ">i"),
    "ntrpr": (12, ">h"), "nart": (14, ">h"),
    "hdt": (16, ">H"), "dto": (18, ">H"),
    "hns": (20, ">H"), "nso": (22, ">H"),
    "format": (24, ">h"), "fold": (26, ">h"), "tsort": (28, ">h"),
    "hsfs": (32, ">h"), "hsfe": (34, ">h"), "hslen": (36, ">h"),
    "hstyp": (38, ">h"), "hstas": (42, ">h"), "hstae": (44, ">h"),
    "htatyp": (46, ">h"), "hcorr": (48, ">h"),
    "rcvm": (52, ">h"),      # measurement system, 1 = metres
    "rev": (300, ">H"),      # 0x0100 = revision 1
    "fixed": (302, ">h"),    # 1 = every trace the same length
    "extfh": (304, ">h"),    # number of extended textual headers
}

# What SEG-Y's sweep-type field can say, against what this program offers.
# Only Linear has an exact counterpart; the dB/octave and dB/Hz laws are
# exponential-family (the frequency rate falls off with frequency), and the
# rest have no entry at all, so they go to "other" rather than being
# misdescribed as something a reader might act on.
SWEEP_TYPE_CODES = {
    "Linear": 1, "dB/Octave": 3, "dB/Hz": 3,
    "T-power": 4, "Random": 4, "Pulse": 4,
}
# The Cosine taper here is 0.5 - 0.5*cos, i.e. sin^2 -- the same shape the
# standard calls cos^2, just measured from the other end.
TAPER_TYPE_CODES = {"Cosine": 2, "Blackman": 3}


def sweep_type_code(sweep_type: str) -> int:
    return SWEEP_TYPE_CODES.get(sweep_type, 4)


def taper_type_code(taper_type: str) -> int:
    return TAPER_TYPE_CODES.get(taper_type, 3)


# ------------------------------------------------------------- IBM floats
def ieee_to_ibm(values) -> np.ndarray:
    """Encode floats as IBM System/360 32-bit words (uint32, host order).

    IBM's format is sign / 7-bit base-16 exponent / 24-bit fraction, with
    the fraction in [1/16, 1). Because the exponent steps by a factor of 16
    the mantissa carries between 21 and 24 significant bits depending on
    the value, so the round trip is good to about 5e-7 relative -- fine for
    a sweep, and the reason the IEEE option exists for anyone who wants the
    exact numbers back.

    Non-finite values and zeros become the all-zero word, which is IBM's
    true zero. Values too large to represent saturate at the largest
    magnitude rather than wrapping; values too small underflow to zero.
    """
    a = np.asarray(values, dtype=np.float64)
    shape = a.shape
    a = a.ravel()
    out = np.zeros(a.shape, dtype=np.uint32)
    good = np.isfinite(a) & (a != 0.0)
    if not good.any():
        return out.reshape(shape)

    v = a[good]
    sign = np.where(v < 0.0, np.uint32(1) << 31, np.uint32(0)).astype(np.uint32)
    m = np.abs(v)

    # Base-16 exponent e with m = frac * 16**e, frac in [1/16, 1).
    e = np.floor(np.log2(m) / 4.0).astype(np.int64) + 1
    frac = m / np.power(16.0, e.astype(np.float64))
    # log2 is not exact at the boundaries, so nudge the one-step cases back
    # in range rather than trusting it.
    low = frac < 0.0625
    frac = np.where(low, frac * 16.0, frac)
    e = np.where(low, e - 1, e)
    high = frac >= 1.0
    frac = np.where(high, frac / 16.0, frac)
    e = np.where(high, e + 1, e)

    mant = np.rint(frac * 16777216.0).astype(np.int64)   # 2**24
    carry = mant >= 16777216            # rounded up to a full fraction
    mant = np.where(carry, mant // 16, mant)
    e = np.where(carry, e + 1, e)

    biased = e + 64
    over = biased > 127
    mant = np.where(over, 16777215, mant)
    biased = np.where(over, 127, biased)

    word = (sign
            | (biased.astype(np.uint32) << np.uint32(24))
            | mant.astype(np.uint32))
    out[good] = np.where(biased < 0, np.uint32(0), word)
    return out.reshape(shape)


def ibm_to_ieee(words) -> np.ndarray:
    """Decode IBM 32-bit float words back to float64. Inverse of the above."""
    w = np.asarray(words, dtype=np.uint32)
    sign = np.where((w >> np.uint32(31)) & np.uint32(1), -1.0, 1.0)
    exp = ((w >> np.uint32(24)) & np.uint32(0x7F)).astype(np.int64) - 64
    mant = (w & np.uint32(0x00FFFFFF)).astype(np.float64) / 16777216.0
    return sign * mant * np.power(16.0, exp.astype(np.float64))


# ------------------------------------------------------------- SEG-Y write
def textual_header(lines) -> bytes:
    """Pack up to 40 lines into the 3200-byte EBCDIC textual header.

    Each line is prefixed "C nn " as the standard asks, uppercased (the
    convention, and the only thing some old readers display legibly),
    padded to 80 characters and encoded as EBCDIC cp500. Anything that
    cannot be represented there becomes a space rather than raising -- a
    stray degree sign in a sweep label must not be able to fail an export.
    """
    out = []
    for i in range(TEXT_HEADER_LINES):
        text = lines[i] if i < len(lines) else ""
        row = f"C{i + 1:2d} {str(text).upper()}"[:TEXT_HEADER_WIDTH]
        row = row.ljust(TEXT_HEADER_WIDTH)
        out.append(row.encode("cp500", errors="replace"))
    return b"".join(out)


def _pack_fields(size: int, table: dict, values: dict) -> bytes:
    buf = bytearray(size)
    for name, val in values.items():
        if val is None or name not in table:
            continue
        off, code = table[name]
        lo, hi = (0, 65535) if code.endswith("H") else (
            (-32768, 32767) if code.endswith("h") else (-2 ** 31, 2 ** 31 - 1))
        struct.pack_into(code, buf, off, int(np.clip(round(val), lo, hi)))
    return bytes(buf)


def write_segy(path, traces, dt_us, *, format_code=1, delay_ms=0,
               text_lines=(), trace_fields=None, extra_binary=None):
    """Write one SEG-Y rev 1 file.

    traces        2-D array-like, one row per trace, all the same length.
    dt_us         sample interval in microseconds (integer).
    format_code   1 = IBM float, 5 = IEEE float. See SEGY_SAMPLE_FORMATS.
    delay_ms      delay recording time for every trace; negative for a
                  correlation wavelet, whose first sample precedes zero lag.
    trace_fields  optional list of per-trace {field: value} overrides,
                  keyed by TRACE_FIELDS names.
    """
    data = np.atleast_2d(np.asarray(traces, dtype=np.float64))
    n_traces, n_samples = data.shape
    if n_traces < 1 or n_samples < 1:
        raise ValueError("nothing to write: no traces or no samples")
    if n_samples > SEGY_MAX_SAMPLES:
        raise ValueError(
            f"{n_samples} samples per trace exceeds SEG-Y's 16-bit limit of "
            f"{SEGY_MAX_SAMPLES}; shorten the sweep, widen the sample "
            f"interval, or narrow the wavelet window")
    if format_code not in (1, 5):
        raise ValueError(f"unsupported SEG-Y sample format code {format_code}")

    binary = dict(jobid=1, lino=1, reno=1, ntrpr=n_traces, nart=0,
                  hdt=dt_us, dto=dt_us, hns=n_samples, nso=n_samples,
                  format=format_code, fold=1, tsort=1, rcvm=1,
                  rev=0x0100, fixed=1, extfh=0)
    if extra_binary:
        binary.update(extra_binary)

    with open(path, "wb") as f:
        f.write(textual_header(text_lines))
        f.write(_pack_fields(400, BINARY_FIELDS, binary))
        for i in range(n_traces):
            hdr = dict(tracl=i + 1, tracr=i + 1, fldr=1, tracf=i + 1,
                       ep=1, cdp=1, cdpt=i + 1, trid=1, nvs=1, nhs=1,
                       duse=1, scalel=1, scalco=1,
                       delrt=delay_ms, ns=n_samples, dt=dt_us)
            if trace_fields and i < len(trace_fields) and trace_fields[i]:
                hdr.update(trace_fields[i])
            f.write(_pack_fields(240, TRACE_FIELDS, hdr))
            row = data[i]
            if format_code == 1:
                f.write(ieee_to_ibm(row).astype(">u4").tobytes())
            else:
                f.write(row.astype(">f4").tobytes())
    return n_traces, n_samples


def read_segy(path):
    """Read back a file written by write_segy. For verification, not general use.

    Returns (text, binary_dict, [(trace_header_dict, samples), ...]). It
    assumes fixed-length traces and one of the two sample formats above,
    which is exactly what write_segy produces -- this exists so the
    self-test can prove the bytes on disk decode to the numbers that went
    in, not to be a general SEG-Y reader.
    """
    with open(path, "rb") as f:
        raw = f.read()
    if len(raw) < 3600:
        raise ValueError("file is too short to be SEG-Y")
    text = raw[:3200].decode("cp500", errors="replace")
    binary = {}
    for name, (off, code) in BINARY_FIELDS.items():
        binary[name] = struct.unpack_from(code, raw, 3200 + off)[0]
    n_samples, fmt = binary["hns"], binary["format"]
    if fmt not in (1, 5):
        raise ValueError(f"unsupported sample format code {fmt}")
    stride = 240 + 4 * n_samples
    traces = []
    pos = 3600
    while pos + stride <= len(raw):
        hdr = {name: struct.unpack_from(code, raw, pos + off)[0]
               for name, (off, code) in TRACE_FIELDS.items()}
        body = raw[pos + 240:pos + stride]
        if fmt == 1:
            samples = ibm_to_ieee(np.frombuffer(body, dtype=">u4"))
        else:
            samples = np.frombuffer(body, dtype=">f4").astype(np.float64)
        traces.append((hdr, samples))
        pos += stride
    return text, binary, traces


# ------------------------------------------------------------- ASCII write
def write_ascii(path, traces, dt_ms, t0_ms, comment_lines=(),
                time_fmt="%.6f", amp_fmt="% .6e"):
    """Write one whitespace-delimited table: time column, then one per trace.

    The comment block carries what the columns cannot: which sweep each
    column came from, the units, and the conventions. Trace labels are long
    and contain spaces and commas, so the column headings are trace_1..N and
    the labels are listed above them -- a heading row that numpy.loadtxt can
    skip as a comment and a human can still read.

    TIME IS IN MILLISECONDS, and the column is named `time_ms` to say so.
    Everything else about a sweep is quoted in ms -- the sample interval,
    the taper lengths, the wavelet window, the SEG-Y delay field -- and a
    table whose header says "1 ms" over a column counting in seconds is
    the kind of mismatch someone eventually divides by 1000 twice.
    """
    data = np.atleast_2d(np.asarray(traces, dtype=np.float64))
    n_traces, n_samples = data.shape
    t = t0_ms + np.arange(n_samples) * dt_ms
    names = [f"trace_{i + 1}" for i in range(n_traces)]
    width = max(len(amp_fmt % -1.0), max(len(n) for n in names)) + 2

    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for line in comment_lines:
            f.write(f"# {line}\n".replace("# \n", "#\n"))
        f.write("#\n")
        f.write("# " + "time_ms".ljust(width - 2))
        f.write("".join(n.rjust(width) for n in names) + "\n")
        for i in range(n_samples):
            row = (time_fmt % t[i]).ljust(width)
            row += "".join((amp_fmt % v).rjust(width) for v in data[:, i])
            f.write(row.rstrip() + "\n")
    return n_traces, n_samples


# ------------------------------------------------- Petrel ASCII wavelet
# Every value starts in the same column, and a continuation line is that
# column's worth of spaces followed by more text -- which is how a reader
# tells "this line begins a new keyword" from "this line continues the
# last one" without a list of keywords to check against.
PETREL_KEY_WIDTH = 14

# Times are quoted to 2 decimals and amplitudes to 8, in columns 16 and 17
# wide, matching what the packages that read this layout write themselves.
# Both widen if a number does not fit rather than running the two columns
# together -- an unnormalized correlation wavelet is around 1e11.
PETREL_TIME_FMT = "%.2f"
PETREL_AMP_FMT = "%.8f"
PETREL_TIME_WIDTH = 16
PETREL_AMP_WIDTH = 17

# How much of an 80-column line is left for a wavelet name once the keyword
# column is spent. A name is a name, not a description: this program's
# auto-generated labels are a full parameter list and run past it, so they
# are trimmed here and written out in full in WAVELET-DESC, where length
# costs nothing.
PETREL_NAME_WIDTH = 80 - PETREL_KEY_WIDTH


def _petrel_text(value) -> str:
    """Flatten anything going into a header field to one safe line.

    A tab or a newline inside a name or a description would break the
    block structure -- the reader would take the remainder as a new
    keyword, or as a continuation of the wrong one. Leading spaces are
    kept, because the description block indents its detail lines and that
    indent is the only thing separating them from the headings.
    """
    return " ".join(str(value).replace("\t", " ").splitlines()).rstrip()


def _petrel_block(key, lines) -> list:
    """One keyword and its value lines, continuations indented to the column.

    Blank lines are dropped rather than written through: a line that is
    empty is neither a new keyword nor recognizably a continuation, and a
    strict reader is entitled to stop at one.
    """
    pad = " " * PETREL_KEY_WIDTH
    out = []
    for line in lines:
        text = _petrel_text(line)
        if not text.strip():
            continue
        head = key.ljust(PETREL_KEY_WIDTH) if not out else pad
        out.append((head + text).rstrip())
    return out


def _petrel_name(value) -> str:
    """One line, trimmed to the name column, broken on a word if it can be.

    Trimming rather than wrapping: a continuation line would make the rest
    of the parameter list part of the name in whatever imports it, which
    is worse than a name that stops early. The whole label is written to
    WAVELET-DESC regardless, so nothing is actually lost.

    A trailing `[tag]` survives the trim. It is the caller's way of saying
    which of several files from one sweep this is, so it is the last thing
    that should be cut -- trimming it away is how two files end up sharing
    a name inside whatever imports them.
    """
    text = _petrel_text(value)
    if len(text) <= PETREL_NAME_WIDTH:
        return text
    tag = ""
    if text.endswith("]") and " [" in text:
        head, tag = text.rsplit(" [", 1)
        tag, text = " [" + tag, head
    room = PETREL_NAME_WIDTH - len(tag)
    if len(text) <= room:
        return text + tag
    cut = text[:max(1, room - 3)]
    space = cut.rfind(" ")
    if space > room // 2:
        cut = cut[:space]
    return cut.rstrip(" ,;-") + "..." + tag


def write_petrel_wavelet(path, samples, dt_ms, t0_ms, name,
                         description=(), history=()):
    """Write ONE wavelet in the Petrel ASCII wavelet layout.

    The layout is a block of `KEYWORD       value` lines, the marker EOH, a
    two-column table, and the marker EOD:

        WAVELET-NAME  Klauder (6-96Hz/20s)
        WAVELET-TFS   -64.00000000
        SAMPLE-RATE   1.00000000
        WAVELET-DESC  ...free text, continued on indented lines...
        HISTORY       ...
        EOH
              0.00       0.05455646
              1.00      -0.08540388
        ...
        EOD

    Two things about it are easy to get wrong and are the reason this
    function exists rather than a flag on write_ascii():

    THE TIME COLUMN IS A 0-BASED OFFSET, not the wavelet's real time axis.
    It counts 0, dt, 2*dt ... regardless of where the wavelet actually
    starts; the true time of the first sample is carried once, in
    WAVELET-TFS. So a symmetric correlation wavelet has TFS = -500 and a
    time column running 0 to +1000, and a reader that ignored TFS would
    place the zero-lag peak half a wavelet late.

    EVERYTHING IS IN MILLISECONDS -- TFS and SAMPLE-RATE both -- which
    agrees with the rest of this program and with the SEG-Y delay field.

    `samples` is one trace: this layout holds a single wavelet, with a
    single name, so a set of sweeps becomes a set of files. A name longer
    than the 80-column line allows is trimmed (see _petrel_name); pass the
    full version in `description` if it matters, which is what the caller
    here does.
    """
    data = np.asarray(samples, dtype=np.float64).ravel()
    n = data.size
    if n == 0:
        raise ValueError("a Petrel wavelet needs at least one sample")

    # Fixed-point, because that is what the layout uses and what the
    # packages reading it expect -- but 8 decimals only carry a number
    # whose peak is around 1 or larger. A set exported "as computed" can
    # be anywhere, so the decimals follow the peak downward and a small
    # wavelet is not written out as a column of 0.00000000.
    peak = float(np.max(np.abs(data)))
    decimals = 8
    if 0.0 < peak < 1e-2:
        decimals = min(20, 8 + int(np.ceil(-np.log10(peak))))
    amp_fmt = f"%.{decimals}f" if decimals != 8 else PETREL_AMP_FMT

    times = [PETREL_TIME_FMT % (i * dt_ms) for i in range(n)]
    amps = [amp_fmt % v for v in data]
    t_w = max(PETREL_TIME_WIDTH, max(len(s) for s in times) + 2)
    a_w = max(PETREL_AMP_WIDTH, max(len(s) for s in amps) + 2)

    head = _petrel_block("WAVELET-NAME",
                         [_petrel_name(name) or "wavelet"])
    head.append("WAVELET-TFS".ljust(PETREL_KEY_WIDTH) + "%.8f" % float(t0_ms))
    head.append("SAMPLE-RATE".ljust(PETREL_KEY_WIDTH) + "%.8f" % float(dt_ms))
    if description:
        head += _petrel_block("WAVELET-DESC", list(description))
    if history:
        head += _petrel_block("HISTORY", list(history))

    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(head) + "\n")
        f.write("EOH\n")
        for i in range(n):
            f.write(times[i].rjust(t_w) + amps[i].rjust(a_w) + "\n")
        f.write("EOD\n")
    return n


def read_petrel_wavelet(path):
    """Read one back: ({keyword: [lines]}, offsets_ms, samples).

    Here for the same reason read_segy() is -- so --selftest can prove the
    file it just wrote parses to the numbers that went into it. The times
    returned are the 0-based offsets as written; WAVELET-TFS is in the
    header dict, where the format puts it.
    """
    header, key, in_data = {}, None, False
    times, amps = [], []
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.rstrip("\n")
            if not in_data:
                if line.strip() == "EOH":
                    in_data = True
                elif line[:PETREL_KEY_WIDTH].strip():
                    key = line[:PETREL_KEY_WIDTH].strip()
                    header[key] = [line[PETREL_KEY_WIDTH:].rstrip()]
                elif key is not None and line.strip():
                    header[key].append(line[PETREL_KEY_WIDTH:].rstrip())
                continue
            if line.strip() == "EOD":
                break
            parts = line.split()
            if len(parts) >= 2:
                times.append(float(parts[0]))
                amps.append(float(parts[1]))
    return (header, np.asarray(times, dtype=np.float64),
            np.asarray(amps, dtype=np.float64))
