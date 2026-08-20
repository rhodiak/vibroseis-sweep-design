"""
sweep_engine.py
================
Core signal-generation and analysis routines for Sweep Design. No GUI or
plotting code lives here on purpose, so it can be unit-tested / reused
headlessly.

Physics notes / conventions used (worth reviewing before trusting results
for real acquisition QC):

- Linear sweep: constant sweep rate in Hz/s.
      f(t) = f1 + (f2-f1) * t/T

- dB/Octave (nonlinear): the sweep dwells longer at frequencies you want
  boosted. Amplitude spectrum ~ 1 / (df/dt), so we specify a desired
  boost profile b(f) in dB, LINEAR IN LOG2(f) (i.e. per octave), and
  solve numerically for f(t) such that instantaneous rate satisfies
      df/dt(f)  ∝  10^(-b(f)/20)
  This reproduces the standard "equal energy per octave" sweep when
  boost_db = 0 (rate ~ f, i.e. exponential/log sweep), and biases the
  dwell time toward one end of the band when boost_db != 0.

- dB/Hz (nonlinear): identical mechanism, but the boost profile b(f) is
  specified LINEAR IN f (per Hz) instead of per octave. With boost_db=0
  this degenerates to the ordinary linear sweep.

- T-power: linear frequency law, with an overall amplitude envelope
  A(t) = (t/T)^p (p = "power" parameter) applied across the WHOLE sweep,
  independent of the start/end tapers. Used here to explore ground-force
  shaping independent of edge tapering.

- Random (pseudo-random) sweep: instantaneous frequency is a smoothed
  random walk confined to [f1, f2], integrated to phase. Useful for
  simultaneous-source decorrelation studies, not a "real" fixed design.

- Pulse: a short, broadband, impulsive source (Klauder-like / exponential
  envelope wavelet) centered in-band, included for comparison against
  swept sources even though it isn't a true Vibroseis sweep.

All sweeps are returned as (t, signal, inst_freq) at the requested sample
rate, drive-level-scaled, and with start/end tapers applied last.

- Vibrator force model: everything above is a dimensionless waveform. A
  real unit's peak ground force is capped by reaction-mass stroke at the
  bottom of the band, by hydraulic flow just above it, and by the
  hold-down weight everywhere else -- so the same sweep comes out of a
  heavy vibrator and a light one as two different signals. See the
  VIBRATOR FORCE MODEL block further down for the three limit laws, what
  they do and do not account for, and how far the absolute numbers can be
  trusted.

NORMALIZATION POLICY (important for comparing sweeps):
compute_spectrum() and compute_autocorrelation() return RAW, unnormalized
magnitudes -- they do NOT force each trace to its own peak of 1 / 0 dB.
Forcing per-trace normalization would hide real, physically meaningful
differences between sweeps: a longer sweep or a higher drive level puts
more energy into the ground and should show a taller autocorrelation
peak and a higher spectrum level than a shorter/quieter one. Any shared
display normalization (so an overlay plot is readable) should be applied
once, using a single common reference across all traces being compared --
that responsibility belongs to the caller/plotting layer, not here.

"Raw" does NOT mean "raw discrete sum", though. Both functions carry a
1/fs (i.e. dt) factor so they approximate the continuous-time integrals
they stand for -- amplitude spectral density in amplitude*s, and
correlation in amplitude^2*s. Without it, every result would scale with
the SAMPLE COUNT: the same 12 s sweep read at 1 ms instead of 2 ms would
show a spectrum and a correlation peak 6 dB higher, purely as a
bookkeeping artifact, and invite the reader to think the finer sample
rate had put more energy into the ground. It has not. With the dt factor
these quantities depend on the sweep -- its length, bandwidth and drive
level -- and not on how densely it happens to be sampled.
"""

from __future__ import annotations
from dataclasses import dataclass, field
import numpy as np
from scipy.signal import hilbert, correlate


SWEEP_TYPES = ["Linear", "dB/Octave", "dB/Hz", "T-power", "Random", "Pulse"]
TAPER_TYPES = ["Cosine", "Blackman"]


@dataclass
class SweepParams:
    label: str = "Sweep 1"
    sweep_type: str = "Linear"
    f1: float = 6.0            # Hz
    f2: float = 96.0           # Hz
    length: float = 12.0       # s (pulse width for "Pulse" type)
    start_phase_deg: float = 0.0
    taper_start_len: float = 0.25   # s
    taper_end_len: float = 0.25     # s
    taper_type: str = "Cosine"
    force_pct: float = 100.0   # drive level, 0-100 %
    dt_ms: float = 1.0         # sample interval in ms (1.0 ms -> 1000 Hz)

    # type-specific extras
    nonlin_boost_db: float = 6.0    # dB/Octave & dB/Hz types
    t_power_exp: float = 2.0        # T-power exponent
    random_seed: int = 0            # Random type
    random_smooth_s: float = 0.5    # Random type: smoothing window (s)
    pulse_cycles: float = 2.5       # Pulse type: cycles under envelope

    @property
    def fs(self) -> float:
        """Sample rate in Hz, derived from the sample interval in ms."""
        return 1000.0 / self.dt_ms

    @property
    def nyquist(self) -> float:
        return self.fs / 2.0

    @property
    def safe_freq(self) -> float:
        """0.8x Nyquist -- a conservative practical ceiling, not a hard limit."""
        return 0.8 * self.nyquist


def _taper_window(n: int, taper_type: str) -> np.ndarray:
    """One-sided ramp-up window of length n (0 -> 1), used for both ends."""
    if n <= 0:
        return np.array([])
    if n == 1:
        return np.array([1.0])
    x = np.linspace(0.0, 1.0, n)
    if taper_type == "Blackman":
        # half of a Blackman window, normalized to start at 0 and end at 1
        full = np.blackman(2 * n)
        w = full[:n]
        w = w - w.min()
        w = w / w.max()
        return w
    # Cosine (Hann-style) ramp
    return 0.5 - 0.5 * np.cos(np.pi * x)


def apply_tapers(sig: np.ndarray, fs: float, start_len: float, end_len: float,
                  taper_type: str) -> np.ndarray:
    out = sig.copy()
    n_start = int(round(start_len * fs))
    n_end = int(round(end_len * fs))
    n_start = min(n_start, len(out))
    n_end = min(n_end, len(out) - n_start) if n_start < len(out) else 0
    if n_start > 0:
        w = _taper_window(n_start, taper_type)
        out[:n_start] *= w
    if n_end > 0:
        w = _taper_window(n_end, taper_type)[::-1]
        out[-n_end:] *= w
    return out


def _nonlinear_freq_law(f1, f2, T, fs, boost_db, per_octave: bool):
    """
    Solve for f(t) on a uniform time grid given a desired dB boost profile
    b(f) that is linear either in log2(f) (per_octave=True) or linear in f
    (per_octave=False), such that df/dt ∝ 10^(-b(f)/20).
    Returns (t, f_of_t) both length int(T*fs)+1.
    """
    n_f = 4000
    f_grid = np.linspace(f1, f2, n_f)
    if per_octave:
        span = np.log2(f2 / f1)
        b = boost_db * (np.log2(f_grid / f1) / span) if span != 0 else np.zeros(n_f)
    else:
        span = (f2 - f1)
        b = boost_db * ((f_grid - f1) / span) if span != 0 else np.zeros(n_f)

    # dt/df ∝ 10^(+b/20)  (more boost -> slower sweep -> more dwell time ->
    # more energy deposited in that part of the band)
    dtdf = 10 ** (b / 20.0)
    # cumulative time as function of f (unnormalized), then scale to T
    t_of_f = np.concatenate(([0.0], np.cumsum(
        0.5 * (dtdf[1:] + dtdf[:-1]) * np.diff(f_grid))))
    t_of_f *= T / t_of_f[-1]

    n_samp = int(round(T * fs)) + 1
    t = np.linspace(0.0, T, n_samp)
    # invert t(f) -> f(t) via interpolation (t_of_f is monotonic increasing)
    f_of_t = np.interp(t, t_of_f, f_grid)
    return t, f_of_t


def generate_sweep(p: SweepParams):
    """
    Returns dict with keys: t, signal, inst_freq (Hz array, NaN where not
    meaningful e.g. Pulse), fs.
    """
    fs = p.fs
    phi0 = np.deg2rad(p.start_phase_deg)

    if p.sweep_type == "Linear":
        n = int(round(p.length * fs)) + 1
        t = np.linspace(0.0, p.length, n)
        k = (p.f2 - p.f1) / p.length if p.length > 0 else 0.0
        inst_f = p.f1 + k * t
        phase = 2 * np.pi * (p.f1 * t + 0.5 * k * t ** 2) + phi0
        sig = np.sin(phase)

    elif p.sweep_type in ("dB/Octave", "dB/Hz"):
        per_octave = (p.sweep_type == "dB/Octave")
        t, inst_f = _nonlinear_freq_law(p.f1, p.f2, p.length, fs,
                                         p.nonlin_boost_db, per_octave)
        phase = 2 * np.pi * np.concatenate(
            ([0.0], np.cumsum(0.5 * (inst_f[1:] + inst_f[:-1]) * np.diff(t)))) + phi0
        sig = np.sin(phase)

    elif p.sweep_type == "T-power":
        n = int(round(p.length * fs)) + 1
        t = np.linspace(0.0, p.length, n)
        k = (p.f2 - p.f1) / p.length if p.length > 0 else 0.0
        inst_f = p.f1 + k * t
        phase = 2 * np.pi * (p.f1 * t + 0.5 * k * t ** 2) + phi0
        sig = np.sin(phase)
        frac = np.clip(t / p.length if p.length > 0 else t, 0, 1)
        env = frac ** max(p.t_power_exp, 0.01)
        sig = sig * env

    elif p.sweep_type == "Random":
        n = int(round(p.length * fs)) + 1
        t = np.linspace(0.0, p.length, n)
        rng = np.random.default_rng(p.random_seed)
        raw = rng.uniform(p.f1, p.f2, size=n)
        # smooth via moving average to get a plausible band-limited walk
        win = max(int(round(p.random_smooth_s * fs)), 1)
        kernel = np.ones(win) / win
        smoothed = np.convolve(raw, kernel, mode="same")
        inst_f = np.clip(smoothed, p.f1, p.f2)
        phase = 2 * np.pi * np.concatenate(
            ([0.0], np.cumsum(0.5 * (inst_f[1:] + inst_f[:-1]) * np.diff(t)))) + phi0
        sig = np.sin(phase)

    elif p.sweep_type == "Pulse":
        n = int(round(p.length * fs)) + 1
        t = np.linspace(0.0, p.length, n)
        f0 = 0.5 * (p.f1 + p.f2)
        t0 = p.length / 2.0
        # Duration of the envelope set so ~pulse_cycles fit under it. The
        # floor only guards against an envelope too short to be sampled
        # (a few samples wide); it used to be p.length/12, which silently
        # overrode pulse_cycles for any normal sweep length -- a 12 s
        # "pulse" then came out as a 1 s Gaussian on f0, i.e. effectively a
        # monochromatic tone rather than the broadband burst intended.
        sigma_floor = 3.0 / fs
        sigma = max(p.pulse_cycles / (2 * f0), sigma_floor) if f0 > 0 else sigma_floor
        env = np.exp(-0.5 * ((t - t0) / sigma) ** 2)
        sig = env * np.sin(2 * np.pi * f0 * (t - t0) + phi0)
        inst_f = np.full_like(t, np.nan)
        inst_f[:] = f0  # nominal, shown flat on the freq-time plot

    else:
        raise ValueError(f"Unknown sweep type: {p.sweep_type}")

    sig = apply_tapers(sig, fs, p.taper_start_len, p.taper_end_len, p.taper_type)
    sig = sig * (p.force_pct / 100.0)

    return {"t": t, "signal": sig, "inst_freq": inst_f, "fs": fs, "params": p}


def compute_spectrum(sig: np.ndarray, fs: float):
    """
    Returns (freqs, mag_linear) up to Nyquist. mag_linear is the RAW
    amplitude spectral density -- NOT normalized to its own peak -- so
    that a longer sweep or a higher drive level correctly shows more
    spectral energy than a shorter/quieter one when several sweeps are
    compared. Convert to dB against a reference you choose (e.g. shared
    across an overlay set) at the display layer.

    Scaled by dt = 1/fs, so it is |integral s(t) exp(-j2*pi*f*t) dt| in
    amplitude*s rather than a bare bin sum. That makes the level a
    property of the sweep and not of its sample rate -- see the
    NORMALIZATION POLICY note at the top of this module.
    """
    n = len(sig)
    nfft = int(2 ** np.ceil(np.log2(max(n, 2))))
    spec = np.fft.rfft(sig, n=nfft)
    freqs = np.fft.rfftfreq(nfft, d=1.0 / fs)
    mag = np.abs(spec) / fs
    return freqs, mag


def compute_autocorrelation(sig: np.ndarray, fs: float):
    """
    Returns (lags, ac) with RAW (unnormalized) autocorrelation amplitude.
    The zero-lag peak equals the signal's energy, integral s(t)^2 dt, which
    scales with sweep duration and drive level -- do not normalize each
    trace to its own peak, or that comparison is lost. Normalize against a
    shared reference at the display layer if you need the overlay readable.
    """
    return correlate_signals(sig, sig, fs)


def correlate_signals(sig_a: np.ndarray, sig_b: np.ndarray, fs: float):
    """
    Cross-correlate sig_a against sig_b (same convention/units as
    compute_autocorrelation; sig_a=sig_b reduces to it exactly). Used for
    stacked/array signals, which must be correlated against the ORIGINAL
    single-unit reference sweep -- not against themselves -- to get the
    physically correct (linear, not squared) amplitude scaling. See
    array_response_complex() / stack_sweep() docstrings.

    Scaled by dt = 1/fs, so the result approximates the continuous
    integral s_a(t) s_b(t - tau) dt in amplitude^2*s and does not grow
    with the sample rate -- see the NORMALIZATION POLICY note at the top
    of this module.
    """
    ac = correlate(sig_a, sig_b, mode="full") / fs
    n = len(sig_a)
    lags = (np.arange(-(n - 1), n)) / fs
    return lags, ac


def compute_envelope(sig: np.ndarray):
    return np.abs(hilbert(sig))


def array_response_complex(freqs: np.ndarray, n: int, spacing_m: float,
                            velocity_mps) -> np.ndarray:
    """
    Complex array factor AF(f) for n equally-spaced, in-phase sources
    (spacing_m apart) as seen by a wavefield component with apparent
    (moveout) velocity velocity_mps. Standard linear-array beam pattern:

        AF(f) = (1/n) * sum_{k=0}^{n-1} exp(j * 2*pi*f * (k - (n-1)/2) * spacing_m / velocity_mps)

    Normalized so AF(0 Hz) = 1 and, for spacing_m=0 or n<=1, AF(f)=1 for
    all f (pure stationary stacking: no frequency-dependent filtering,
    only linear n-fold gain). For spacing_m != 0, |AF(f)| < 1 at
    frequencies where the array partially cancels -- this is exactly the
    mechanism source/receiver arrays use to attenuate low-apparent-
    velocity noise (e.g. ground roll) relative to higher-apparent-
    velocity reflections, PROVIDED velocity_mps is set to the apparent
    velocity of the energy you're trying to attenuate or preserve.
    """
    if n <= 1 or spacing_m == 0 or not velocity_mps or velocity_mps <= 0:
        return np.ones_like(freqs, dtype=complex)
    k = 2 * np.pi * freqs / velocity_mps
    m = np.arange(n) - (n - 1) / 2.0
    AF = np.mean(np.exp(1j * np.outer(k, m * spacing_m)), axis=1)
    return AF


def stack_sweep(sig: np.ndarray, fs: float, n: int, spacing_m: float = 0.0,
                 velocity_mps=None) -> np.ndarray:
    """
    Returns the THEORETICAL, noiseless composite time-domain signal for n
    identical, in-phase sources firing the same sweep -- e.g. n stationary
    VP repeats stacked in processing, or an n-element vibrator array with
    element spacing spacing_m, as it would appear to a wavefield component
    arriving at apparent velocity velocity_mps.

    - n=1, or spacing_m=0 / velocity_mps unset: pure stationary stack --
      composite = n * sig (scalar gain only, no shape change).
    - spacing_m != 0 with velocity_mps set: composite = n * AF(f) filtered
      version of sig -- frequency-dependent, per array_response_complex().

    No noise, no ground-coupling variability, no statics/NMO -- this is
    purely the array's coherent-sum response to isolate what geometry
    alone changes, as distinct from real-world SNR gain from stacking
    (which comes from averaging out incoherent noise, not modeled here).
    """
    if n <= 1:
        return sig.copy()
    freqs = np.fft.rfftfreq(len(sig), d=1.0 / fs)
    spec = np.fft.rfft(sig)
    AF = array_response_complex(freqs, n, spacing_m, velocity_mps)
    stacked_spec = n * AF * spec
    return np.fft.irfft(stacked_spec, n=len(sig))


# ====================================================================
# VIBRATOR FORCE MODEL
# ====================================================================
# Everything above this line describes a sweep as a dimensionless
# waveform: a drive level in percent, an amplitude of 1.0. That is the
# THEORETICAL sweep -- the reference signal a correlator would use.
#
# A real vibrator cannot deliver that waveform at constant amplitude
# across the band. Its peak ground force is bounded by three independent
# mechanical limits, and which one binds depends on frequency:
#
#   stroke limit      F = m_r * (2*pi*f)^2 * x_pk          (+12 dB/octave)
#       The reaction mass has to travel far enough to generate the force,
#       and it only has so much room. Binds at the very bottom of the
#       band.
#
#   flow limit        F = m_r * (2*pi*f) * v_pk            (+6 dB/octave)
#       The pump delivers a finite fluid volume per second, so the
#       reaction mass has a peak VELOCITY ceiling v_pk = Q/A_piston.
#       Displacement available at frequency f is then v_pk/(2*pi*f),
#       giving a force that rises only as f. Binds just above the stroke
#       region: the two cross at f = v_pk / (2*pi*x_pk), below which the
#       quadratic stroke curve is the lower of the two.
#
#   hold-down limit   F = (decouple_pct/100) * m_hd * g    (flat)
#       Peak ground force above the hold-down weight lifts the baseplate
#       off the ground -- it decouples, radiates a distorted wavelet and
#       hammers the pad. The manufacturer's usable-force rating is the
#       hold-down weight derated by a safety margin (70-80% in normal
#       practice). Drive level is a percentage OF THIS, and it is the
#       only one of the three limits the operator can move.
#
# The achievable ground force is the pointwise minimum, and that is the
# whole "heavy vibrator vs light vibrator" question in one curve: a
# bigger unit does not merely push harder in the middle of the band, it
# moves the +12 dB/octave knee DOWN, because it carries both a larger
# reaction mass and a longer stroke. That is what buys the low end.
#
# WHAT THIS MODEL DOES NOT INCLUDE, and should not be read as including:
# baseplate flexure and its resonance, the mass-decoupling nonlinearity
# near the limit, ground stiffness/coupling (which varies station to
# station by more than any of the effects modelled here), harmonic
# distortion, servo-valve dynamics and phase error, and the systematic
# difference between the weighted-sum ground force a vibrator REPORTS
# and the force actually entering the earth. Absolute levels from this
# model are order-of-magnitude. Differences BETWEEN two vibrators driven
# the same way are much more trustworthy than either one's absolute kN,
# and that comparison is what it is built for.

G0 = 9.80665            # standard gravity, m/s^2

# Unit conversions. Vibrator spec sheets are quoted in field units almost
# everywhere in the world that runs Vibroseis, but the physics above only
# works in SI, so SI is what is stored and these are applied at the edge.
KG_PER_LB = 0.45359237
M_PER_IN = 0.0254
LBF_PER_KN = 224.808943
UNIT_SYSTEMS = ("SI", "Field")


@dataclass
class VibratorParams:
    """One vibrator's mechanical limits. All fields SI; see UNIT_SYSTEMS."""
    label: str = "Heavy (26 t hold-down)"
    hold_down_kg: float = 26000.0      # mass bearing on the baseplate
    reaction_mass_kg: float = 3400.0   # the moving mass
    stroke_pp_m: float = 0.152         # peak-to-peak, as spec sheets quote it
    mass_vel_pk_mps: float = 2.0       # pump flow / piston area
    decouple_pct: float = 80.0         # usable fraction of the hold-down weight
    # Which unit system these numbers were TYPED in. Metadata only: no force
    # law reads it, and every field above stays SI regardless. It exists so a
    # saved vibrator can be written back to its library file in the units its
    # spec sheet used, which is how the person who entered it will recognise
    # it. See VIB_UNIT_FACTORS.
    entry_units: str = "SI"

    @property
    def stroke_pk_m(self) -> float:
        """Half the peak-to-peak stroke -- the amplitude the force law wants."""
        return 0.5 * self.stroke_pp_m

    @property
    def hold_down_force_n(self) -> float:
        return self.hold_down_kg * G0

    @property
    def rated_force_n(self) -> float:
        """Usable peak ground force at 100% drive, i.e. the flat ceiling."""
        return (self.decouple_pct / 100.0) * self.hold_down_force_n

    def target_force_n(self, drive_pct: float) -> float:
        return max(drive_pct, 0.0) / 100.0 * self.rated_force_n

    @property
    def f_stroke_flow_hz(self) -> float:
        """Where the stroke and flow limits cross: f = v_pk / (2*pi*x_pk).

        Below it the +12 dB/octave stroke curve is the binding one; above
        it the +6 dB/octave flow curve is."""
        x = self.stroke_pk_m
        if x <= 0:
            return float("inf")
        return self.mass_vel_pk_mps / (2.0 * np.pi * x)

    def f_full_force_hz(self, drive_pct: float) -> float:
        """Lowest frequency at which the target force is actually reachable.

        The single most useful number this model produces: below it the
        sweep is running at reduced force no matter what the drive level
        says, and no amount of sweep length recovers energy the machine
        never radiated."""
        m = self.reaction_mass_kg
        f_t = self.target_force_n(drive_pct)
        if m <= 0 or f_t <= 0:
            return 0.0
        cross = self.f_stroke_flow_hz
        x, v = self.stroke_pk_m, self.mass_vel_pk_mps
        # Try the stroke branch first; if its solution lies above the
        # crossover the flow limit was the binding one there instead.
        if x > 0:
            f_stroke = np.sqrt(f_t / (m * x)) / (2.0 * np.pi)
            if f_stroke <= cross:
                return float(f_stroke)
        if v > 0:
            return float(f_t / (m * 2.0 * np.pi * v))
        return float("inf")


# Generic classes, named by hold-down weight rather than by any
# manufacturer's force rating: the usable force follows from the weight
# and the decoupling margin, and is shown alongside so the number is
# derived in front of the user rather than asserted. These are plausible
# representative figures for each size class, not any specific product.
VIBRATOR_PRESETS = {
    "Light (11 t hold-down)":
        VibratorParams("Light (11 t hold-down)", 11000.0, 1400.0, 0.075, 1.4),
    "Medium (18 t hold-down)":
        VibratorParams("Medium (18 t hold-down)", 18000.0, 2200.0, 0.102, 1.7),
    "Heavy (26 t hold-down)":
        VibratorParams("Heavy (26 t hold-down)", 26000.0, 3400.0, 0.152, 2.0),
    "Extra heavy (32 t hold-down)":
        VibratorParams("Extra heavy (32 t hold-down)", 32000.0, 4200.0, 0.178, 2.2),
}
DEFAULT_PRESET = "Heavy (26 t hold-down)"

# The mechanical fields, in the order a spec sheet tends to list them.
# Saved vibrators are stored as these five and ALWAYS in SI, never in
# whatever the Vibrator tab's unit toggle happened to be set to: a library
# file has to mean the same thing on the next machine that opens it.
VIB_ATTRS = ("hold_down_kg", "reaction_mass_kg", "stroke_pp_m",
             "mass_vel_pk_mps", "decouple_pct")

# Keys used in the library file. The SI unit suffix is dropped on purpose:
# an entry saved in field units holds pounds, and a key called
# "hold_down_kg" carrying 48044 lb would be a lie in the one place a reader
# has nothing else to go on.
VIB_FILE_KEYS = {
    "hold_down_kg": "hold_down",
    "reaction_mass_kg": "reaction_mass",
    "stroke_pp_m": "stroke_pp",
    "mass_vel_pk_mps": "mass_vel_pk",
    "decouple_pct": "decouple_pct",
}

# Display units, as (factor, unit name) with display = SI * factor. The one
# table behind both the Vibrator tab's fields and the library file, so a
# value cannot be converted one way on screen and another way on disk.
# (vibrator_summary formats the readout panel separately -- it quotes mass
# in tonnes rather than kg, which is a prose choice, not a field edit.)
VIB_UNIT_FACTORS = {
    "SI": {
        "hold_down_kg": (1.0, "kg"),
        "reaction_mass_kg": (1.0, "kg"),
        "stroke_pp_m": (1000.0, "mm"),
        "mass_vel_pk_mps": (1.0, "m/s"),
        "decouple_pct": (1.0, "%"),
    },
    "Field": {
        "hold_down_kg": (1.0 / KG_PER_LB, "lb"),
        "reaction_mass_kg": (1.0 / KG_PER_LB, "lb"),
        "stroke_pp_m": (1.0 / M_PER_IN, "in"),
        "mass_vel_pk_mps": (1.0 / M_PER_IN, "in/s"),
        "decouple_pct": (1.0, "%"),
    },
}


def vib_unit_note(units: str) -> str:
    """'hold_down lb, reaction_mass lb, ...' -- a legend for the file."""
    table = VIB_UNIT_FACTORS[units]
    return ", ".join(f"{VIB_FILE_KEYS[a]} {table[a][1]}" for a in VIB_ATTRS)


def vibrator_to_dict(vib: VibratorParams) -> dict:
    """One vibrator as it should appear in the library file.

    Written in the units it was ENTERED in, tagged with which those were.
    Someone who typed a spec sheet in pounds and inches should find pounds
    and inches in the file; storing everything in SI made every entry need
    a conversion in the reader's head before it could be checked against
    the sheet it came from.

    The label is deliberately left out -- it is the key the vibrator is
    filed under, so storing it twice invites the two copies to disagree."""
    units = vib.entry_units if vib.entry_units in VIB_UNIT_FACTORS else "SI"
    table = VIB_UNIT_FACTORS[units]
    out = {"units": units}
    for attr in VIB_ATTRS:
        out[VIB_FILE_KEYS[attr]] = float(getattr(vib, attr)) * table[attr][0]
    return out


def vibrator_from_dict(label: str, data: dict) -> VibratorParams:
    """Inverse of vibrator_to_dict. Raises ValueError on anything unusable.

    Hand-editing the library file is expected -- it is plain JSON sitting
    beside the settings, and copying one between crews is the point -- so a
    missing or nonsensical field has to fail loudly here. Left to reach
    force_limits, a zero or a NaN produces an all-zero force curve, which
    looks like a modelling result rather than a typo.

    An entry with no "units" key is a version-1 file: those were written in
    raw SI under the attribute names, so they are read that way and get
    rewritten in the new form the next time the library is saved."""
    if not isinstance(data, dict):
        raise ValueError(f"{label}: entry is not a set of parameters")
    legacy = "units" not in data
    units = "SI" if legacy else data["units"]
    if units not in VIB_UNIT_FACTORS:
        raise ValueError(f"{label}: unknown units {units!r} "
                          f"(expected one of {', '.join(VIB_UNIT_FACTORS)})")
    table = VIB_UNIT_FACTORS[units]
    kwargs = {}
    for attr in VIB_ATTRS:
        # Legacy entries are keyed by the SI attribute name and need no
        # conversion; new ones use the neutral key and the units tag.
        key = attr if legacy else VIB_FILE_KEYS[attr]
        factor = 1.0 if legacy else table[attr][0]
        if key not in data:
            raise ValueError(f"{label}: missing {key}")
        try:
            val = float(data[key])
        except (TypeError, ValueError):
            raise ValueError(f"{label}: {key} is not a number")
        if not np.isfinite(val) or val <= 0:
            raise ValueError(f"{label}: {key} must be a positive number")
        kwargs[attr] = val / factor
    if kwargs["decouple_pct"] > 100:
        raise ValueError(f"{label}: decouple_pct cannot exceed 100")
    return VibratorParams(label=label, entry_units=units, **kwargs)


def force_limits(freqs: np.ndarray, vib: VibratorParams,
                  drive_pct: float = 100.0) -> dict:
    """The three force ceilings and their minimum, in newtons, vs frequency.

    Keys: 'stroke', 'flow', 'target', 'available' -- all arrays matching
    freqs, so they can be overlaid directly. 'target' is the flat
    hold-down ceiling after the drive level; 'available' is the pointwise
    minimum, i.e. what the machine can actually deliver."""
    f = np.asarray(freqs, dtype=float)
    w = 2.0 * np.pi * f
    m = vib.reaction_mass_kg
    stroke = m * w ** 2 * vib.stroke_pk_m
    flow = m * w * vib.mass_vel_pk_mps
    target = np.full_like(f, vib.target_force_n(drive_pct))
    available = np.minimum(np.minimum(stroke, flow), target)
    return {"stroke": stroke, "flow": flow, "target": target,
            "available": available}


def apply_force_model(sig: np.ndarray, inst_freq: np.ndarray, fs: float,
                       vib: VibratorParams, drive_pct: float = 100.0,
                       mode: str = "time") -> np.ndarray:
    """Convert a dimensionless sweep into ground force in NEWTONS.

    `sig` is what generate_sweep() returned, so the drive level is
    already baked into its amplitude; it is divided back out here and
    reapplied through the vibrator's force ceiling, which is where drive
    level physically acts. Passing the drive twice would square it.

    mode="time" evaluates the force ceiling at each sample's
    instantaneous frequency. That is not an approximation of a filter --
    it is what the vibrator's control loop actually does, tracking one
    frequency at a time and delivering whatever force is available there.
    Use it for every swept type.

    mode="spectral" applies the same ceiling as a zero-phase filter
    across the whole spectrum. Use it for Pulse, where the signal is
    broadband at every instant and "the frequency at time t" is a
    fiction.
    """
    s = np.asarray(sig, dtype=float)
    drive = max(drive_pct, 0.0) / 100.0
    if drive <= 0 or s.size == 0:
        return np.zeros_like(s)
    unit = s / drive          # back to the +/-1 waveform, tapers intact

    if mode == "spectral":
        freqs = np.fft.rfftfreq(len(unit), d=1.0 / fs)
        avail = force_limits(freqs, vib, drive_pct)["available"]
        return np.fft.irfft(np.fft.rfft(unit) * avail, n=len(unit))

    f_t = np.abs(np.asarray(inst_freq, dtype=float))
    # A NaN instantaneous frequency means the type has no meaningful
    # f(t); fall back to the flat ceiling there rather than zeroing the
    # sample, which would punch a hole in the waveform.
    f_t = np.where(np.isfinite(f_t), f_t, 0.0)
    avail = force_limits(f_t, vib, drive_pct)["available"]
    return unit * avail


def energy_per_octave(freqs: np.ndarray, mag: np.ndarray) -> np.ndarray:
    """Linear energy density per OCTAVE from an amplitude spectrum.

    Energy in a band is the integral of |S(f)|^2 df, so the density per
    unit log2(f) is |S(f)|^2 * f * ln2. Raw and unnormalized, like every
    other spectral quantity here -- the caller picks one shared reference
    across the overlay set. See energy_per_octave_db()."""
    f = np.asarray(freqs, dtype=float)
    m = np.asarray(mag, dtype=float)
    return (m ** 2) * f * np.log(2.0)


def energy_per_octave_db(freqs: np.ndarray, mag: np.ndarray,
                          ref: float = None) -> np.ndarray:
    """Energy per octave in dB -- energy_per_octave() against a reference.

    Plotting this instead of the amplitude spectrum answers a different
    and more practical question: not "how tall is the spectrum here" but
    "how much of the sweep's energy actually went into this octave" --
    which is the question a low-frequency force limit changes most.

    `ref` is a shared linear reference (as elsewhere in this module, the
    caller supplies one common value across the overlay set); if None the
    curve is referenced to its own maximum."""
    dens = energy_per_octave(freqs, mag)
    if ref is None:
        ref = float(dens.max()) if dens.size and dens.max() > 0 else 1.0
    ref = ref if ref > 0 else 1.0
    return 10.0 * np.log10(np.maximum(dens, ref * 1e-12) / ref)


def vibrator_summary(vib: VibratorParams, drive_pct: float = 100.0,
                      units: str = "SI") -> list:
    """(name, value) rows describing one vibrator, for a text panel."""
    field = (units == "Field")
    rated_kn = vib.rated_force_n / 1000.0
    target_kn = vib.target_force_n(drive_pct) / 1000.0

    def force(kn):
        return (f"{kn * LBF_PER_KN:,.0f} lbf" if field else f"{kn:,.1f} kN")

    def mass(kg):
        return (f"{kg / KG_PER_LB:,.0f} lb" if field else f"{kg / 1000.0:,.1f} t")

    def length(m):
        return (f"{m / M_PER_IN:.2f} in" if field else f"{m * 1000.0:.0f} mm")

    def speed(mps):
        return (f"{mps / M_PER_IN:.1f} in/s" if field else f"{mps:.2f} m/s")

    return [
        ("Hold-down weight", mass(vib.hold_down_kg)),
        ("Reaction mass", mass(vib.reaction_mass_kg)),
        ("Stroke (pk-pk)", length(vib.stroke_pp_m)),
        ("Peak mass velocity", speed(vib.mass_vel_pk_mps)),
        ("Decoupling margin", f"{vib.decouple_pct:g}%"),
        ("Usable force (100% drive)", force(rated_kn)),
        (f"Target force ({drive_pct:g}% drive)", force(target_kn)),
        ("Full force above", f"{vib.f_full_force_hz(drive_pct):.2f} Hz"),
        ("Stroke/flow crossover", f"{vib.f_stroke_flow_hz:.2f} Hz"),
    ]


def describe_vibrator(vib: VibratorParams, drive_pct: float = 100.0) -> str:
    """Compact one-line vibrator tag, for appending to a sweep's legend."""
    return (f"{vib.label}, {vib.target_force_n(drive_pct) / 1000.0:.0f} kN "
            f"@ {drive_pct:g}%, full >{vib.f_full_force_hz(drive_pct):.1f} Hz")


RING_FLOOR_DB = -40.0   # level (below the peak) at which correlation
                         # ringing is treated as no longer significant;
                         # sets what compute_sweep_metrics()'s ring_ms and
                         # the table's "T40dB" column measure.


def compute_sweep_metrics(lags: np.ndarray, ac: np.ndarray, env: np.ndarray,
                           freqs: np.ndarray, mag_lin: np.ndarray) -> dict:
    """
    Quantify a sweep's correlation wavelet so candidates can be compared by
    number, not just by eye. All of it is measured on the RAW (unnormalized)
    autocorrelation and spectrum, so the caller can still express the peak
    against whatever shared reference the plot is using.

    Returned keys:

    - ``ac_peak``     raw autocorrelation peak amplitude (the sweep's
                      energy, integral s(t)^2 dt: grows with sweep length,
                      drive level and stack count, but NOT with the sample
                      rate -- see this module's NORMALIZATION POLICY).
    - ``sll_db``      first side-lobe level, dB relative to the main peak.
                      Measured on the ENVELOPE, which is smooth, so "first
                      side lobe" means the first envelope maximum after the
                      first envelope minimum -- unambiguous, unlike picking
                      local maxima off the oscillating wavelet itself. More
                      negative is better: less correlation ringing to be
                      mistaken for (or to mask) a nearby reflection.
    - ``pt_db``       peak-to-trough: the deepest NEGATIVE excursion of the
                      wavelet, dB relative to its positive peak, measured on
                      the signed trace and over the whole of it. Distinct
                      from ``sll_db``, which is an envelope measurement and
                      therefore blind to the trough at the first carrier
                      half cycle -- the trough that sits inside the envelope
                      main lobe and is usually the deepest one there is.
                      This is the number that says whether the correlated
                      wavelet is a spike or a ringing cosine: it tracks
                      bandwidth in octaves almost alone (about -10 dB at 4
                      octaves, -1.5 dB at 1 octave, -0.1 dB at a quarter
                      octave, where the troughs are as deep as the peak),
                      and it prices long tapers, which narrow the effective
                      band and deepen the troughs while barely moving
                      ``sll_db``. Physically it is the strength of the
                      flanking side lobes an interpreter sees around every
                      reflector, so more negative is better. For reference a
                      Ricker wavelet sits at -7.0 dB (the textbook 0.4464).
    - ``mlw_s``       main-lobe width in seconds, full width of the envelope
                      at HALF amplitude (-6 dB). This is the practical
                      temporal resolution of the correlated wavelet;
                      narrower is better and is driven mainly by bandwidth.
    - ``islr_db``     integrated side-lobe ratio: energy outside the main
                      lobe over energy inside it, in dB. Complements
                      ``sll_db`` -- one tall side lobe and a long low tail
                      are very different defects and this separates them.
    - ``ring_ms``     ringing length: how far from the main peak, in ms, the
                      envelope is still above ``RING_FLOOR_DB``. Neither
                      ``sll_db`` nor ``islr_db`` sees this -- the first
                      describes one lobe and the second is an energy ratio,
                      so "tall but brief" and "low but endless" can score
                      the same. This is the one that says how long
                      correlation noise keeps interfering with later events.
    - ``decay_db_per_100ms``
                      how fast the side lobes die away: least-squares slope
                      of the lobe CRESTS (in dB) against lag, over the
                      side-lobe region. More negative = faster decay.

    Both are measured against the sweep's OWN peak. To compare two sweeps
    as the eye sees them on a shared-reference plot, read them together
    with ``ac_peak`` -- a sweep with 10 dB more peak also carries its whole
    side-lobe train 10 dB higher.

    - ``bw_lo_hz`` /
      ``bw_hi_hz``    achieved -6 dB band edges of the amplitude spectrum.
                      Worth comparing against the requested f1-f2: tapers,
                      nonlinear dwell shaping and array response all move
                      these away from what was asked for.

    Any metric that can't be measured on the given data (degenerate or
    flat input) comes back as float('nan') rather than raising.
    """
    nan = float("nan")
    out = {"ac_peak": nan, "sll_db": nan, "pt_db": nan, "mlw_s": nan,
           "islr_db": nan, "ring_ms": nan, "decay_db_per_100ms": nan,
           "bw_lo_hz": nan, "bw_hi_hz": nan}

    env = np.asarray(env, dtype=float)
    if env.size < 3 or not np.isfinite(env).all():
        return out
    i0 = int(np.argmax(env))
    peak = env[i0]
    if peak <= 0:
        return out
    out["ac_peak"] = float(np.max(np.abs(ac)))

    # Peak-to-trough, on the SIGNED wavelet rather than the envelope. The
    # deepest trough of a bandpass wavelet sits at the first carrier half
    # cycle, INSIDE the envelope's main lobe, so sll_db -- which only looks
    # between envelope lobes -- never sees it. Whole trace, no exclusion
    # zone: finding the trough next to the peak is the entire point.
    ac_arr = np.asarray(ac, dtype=float)
    ac_pos = float(np.max(ac_arr)) if ac_arr.size else nan
    ac_neg = float(np.min(ac_arr)) if ac_arr.size else nan
    if np.isfinite(ac_pos) and ac_pos > 0 and np.isfinite(ac_neg) and ac_neg < 0:
        out["pt_db"] = float(20.0 * np.log10(-ac_neg / ac_pos))

    dt = float(np.mean(np.diff(lags))) if len(lags) > 1 else nan

    def _half_crossing(side, direction):
        """Sub-sample position where `side` first falls to half the peak,
        walking away from the main lobe; returns an offset in samples."""
        half = 0.5 * peak
        below = np.nonzero(side < half)[0]
        if below.size == 0:
            return nan
        j = int(below[0])
        if j == 0:
            return 0.0
        y0, y1 = side[j - 1], side[j]
        frac = (y0 - half) / (y0 - y1) if y1 != y0 else 0.0
        return (j - 1 + frac) * direction

    right = env[i0:]
    left = env[:i0 + 1][::-1]
    r_half = _half_crossing(right, 1.0)
    l_half = _half_crossing(left, 1.0)
    if np.isfinite(r_half) and np.isfinite(l_half) and np.isfinite(dt):
        out["mlw_s"] = float((r_half + l_half) * dt)

    # First envelope minimum on the positive-lag side marks the end of the
    # main lobe; the next maximum after it is the first side lobe.
    d = np.diff(right)
    trough = None
    for k in range(len(d) - 1):
        if d[k] < 0 <= d[k + 1]:
            trough = k + 1
            break
    if trough is not None:
        rest = right[trough:]
        d2 = np.diff(rest)
        crest = None
        for k in range(len(d2) - 1):
            if d2[k] > 0 >= d2[k + 1]:
                crest = k + 1
                break
        if crest is None and rest.size:
            crest = int(np.argmax(rest))   # monotonic tail: take its max
        if crest is not None and rest[crest] > 0:
            sll = float(20.0 * np.log10(rest[crest] / peak))
            # Below this there is no side lobe in any meaningful sense (a
            # Gaussian burst, say) -- what's left is numerical noise, and
            # printing "-317 dB" would read as a spectacular result rather
            # than as "not applicable".
            if sll > -80.0:
                out["sll_db"] = sll

        # Main lobe = the symmetric span out to that first trough; anything
        # beyond it on either side counts as side-lobe energy.
        end = i0 + trough
        start = max(0, i0 - trough)
        e_main = float(np.sum(env[start:end + 1] ** 2))
        e_total = float(np.sum(env ** 2))
        e_side = max(e_total - e_main, 0.0)
        if e_main > 0 and e_side > 0:
            out["islr_db"] = float(10.0 * np.log10(e_side / e_main))

    # --- how long the ringing lasts, and how fast it dies -------------
    lags = np.asarray(lags, dtype=float)
    if lags.size == env.size and np.isfinite(dt) and dt > 0:
        t_abs = np.abs(lags - lags[i0])
        db = 20.0 * np.log10(np.maximum(env, 1e-16) / peak)

        above = np.nonzero(db >= RING_FLOOR_DB)[0]
        if above.size:
            out["ring_ms"] = float(t_abs[above].max() * 1000.0)

        # Fit the lobe crests, not every sample: the envelope dips into
        # deep nulls between lobes and those nulls would drag a raw fit
        # down and make the decay look faster than it is. One crest per
        # 5 ms bin, from the end of the main lobe out to where the ringing
        # has died (or 250 ms, whichever comes first).
        mlw = out["mlw_s"] if np.isfinite(out["mlw_s"]) else 0.010
        far = min(out["ring_ms"] / 1000.0 if np.isfinite(out["ring_ms"]) else 0.25, 0.25)
        sel = (t_abs > mlw) & (t_abs <= far) & np.isfinite(db)
        if sel.sum() > 8:
            bins = (t_abs[sel] / 0.005).astype(int)
            uniq = np.unique(bins)
            if uniq.size >= 4:
                crest_t = np.array([t_abs[sel][bins == b].mean() for b in uniq])
                crest_db = np.array([db[sel][bins == b].max() for b in uniq])
                slope = np.polyfit(crest_t, crest_db, 1)[0]   # dB per second
                out["decay_db_per_100ms"] = float(slope / 10.0)

    mag_lin = np.asarray(mag_lin, dtype=float)
    if mag_lin.size and mag_lin.max() > 0:
        above = np.nonzero(mag_lin >= 0.5 * mag_lin.max())[0]   # -6 dB
        if above.size:
            out["bw_lo_hz"] = float(freqs[above[0]])
            out["bw_hi_hz"] = float(freqs[above[-1]])
    return out


# Column order and units for the on-canvas metrics table, and the order
# format_metrics() prints in -- one source of truth, so the table's headers
# and any inline rendering can't drift apart. The unit rides in the header
# rather than in every cell; that is what keeps the columns narrow enough to
# tabulate a whole overlay set across the width of the figure.
METRIC_COLUMNS = [
    ("pk", "dB"),
    ("SLL", "dB"),
    ("P/T", "dB"),
    ("MLW", "ms"),
    ("ISLR", "dB"),
    (f"T{abs(RING_FLOOR_DB):.0f}dB", "ms"),
    ("decay", "dB/100ms"),
    ("BW", "Hz"),
]


def metric_values(m: dict, peak_ref: float = None) -> dict:
    """
    compute_sweep_metrics() output rendered as display strings, keyed by the
    abbreviation used in METRIC_COLUMNS: ``{"pk": ("-3.5", "dB"), ...}``.
    Value and unit are kept apart so a table can print the unit once in the
    header while a one-line rendering can join them back up.

    ``peak_ref`` is the shared amplitude reference the plot normalizes to
    (the largest peak among the sweeps on screen), so 'pk' reads as "this
    sweep's correlation peak relative to the strongest one here" -- 0 dB
    for the strongest, negative for the rest. Without it the raw peak is
    reported instead, and unitless (the one case where a value's unit does
    not match the METRIC_COLUMNS header).

    'pk' is converted with 20*log10, an AMPLITUDE ratio, even though
    ``ac_peak`` is an energy. That is deliberate and it is not a mixed
    convention by accident: the number exists to explain the autocorrelation
    panel drawn directly above the table, where the traces are divided by
    that same shared reference and read as amplitudes. A half-energy sweep
    genuinely sits at half height there, and half height is -6 dB. Convert
    with 10*log10 instead and the column would stop agreeing with the
    picture it annotates.

    The consequence worth knowing when reading it: doubling sweep length
    doubles the correlation peak, so 'pk' moves +6 dB -- but that is
    correlated SIGNAL amplitude, not signal-to-noise. Random noise
    correlates up as sqrt(T), so the S/N gain from doubling the sweep is
    about 3 dB, not 6.

    Metrics that couldn't be measured are absent from the dict rather than
    present-and-empty, so callers decide how to show a gap.
    """
    out = {}
    pk = m.get("ac_peak", float("nan"))
    if np.isfinite(pk):
        if peak_ref and peak_ref > 0:
            db = 20 * np.log10(max(pk, 1e-12) / peak_ref)
            # Two sweeps that differ only in sample rate now land within a
            # thousandth of a dB of each other; without this, the quieter
            # one prints "-0.0", which reads as a defect rather than a tie.
            out["pk"] = (f"{db if abs(db) >= 0.05 else 0.0:+.1f}", "dB")
        else:
            out["pk"] = (f"{pk:.3g}", "")
    if np.isfinite(m.get("sll_db", float("nan"))):
        out["SLL"] = (f"{m['sll_db']:.1f}", "dB")
    if np.isfinite(m.get("pt_db", float("nan"))):
        out["P/T"] = (f"{m['pt_db']:.1f}", "dB")
    if np.isfinite(m.get("mlw_s", float("nan"))):
        out["MLW"] = (f"{m['mlw_s'] * 1000.0:.3g}", "ms")
    if np.isfinite(m.get("islr_db", float("nan"))):
        out["ISLR"] = (f"{m['islr_db']:.1f}", "dB")
    if np.isfinite(m.get("ring_ms", float("nan"))):
        out[f"T{abs(RING_FLOOR_DB):.0f}dB"] = (f"{m['ring_ms']:.0f}", "ms")
    if np.isfinite(m.get("decay_db_per_100ms", float("nan"))):
        out["decay"] = (f"{m['decay_db_per_100ms']:.1f}", "dB/100ms")
    if np.isfinite(m.get("bw_lo_hz", float("nan"))) and \
            np.isfinite(m.get("bw_hi_hz", float("nan"))):
        out["BW"] = (f"{m['bw_lo_hz']:.0f}-{m['bw_hi_hz']:.0f}", "Hz")
    return out


def format_metrics(m: dict, peak_ref: float = None) -> str:
    """
    One-line rendering of compute_sweep_metrics() output, e.g.

        pk -3.5 dB, SLL -13.2 dB, MLW 11 ms, ISLR -6.8 dB, BW 7-93 Hz

    The GUI tabulates the same numbers under the plots instead (see
    METRIC_COLUMNS / metric_values); this stays for callers that want the
    whole set as a single string -- scripts, logs, a one-off print.
    Metrics that couldn't be measured are omitted.
    """
    vals = metric_values(m, peak_ref)
    return ", ".join(f"{abbr} {vals[abbr][0]} {vals[abbr][1]}".strip()
                     for abbr, _unit in METRIC_COLUMNS if abbr in vals)


def _fmt(x) -> str:
    """Compact numeric formatting for labels: trims trailing zeros (12.0 -> 12,
    0.25 -> 0.25) and a leading zero before the decimal point (0.25 -> .25,
    -0.5 -> -.5) to keep legend text as short as possible."""
    s = f"{x:g}"
    if s.startswith("0."):
        s = s[1:]
    elif s.startswith("-0."):
        s = "-" + s[2:]
    return s


def describe_params(p: SweepParams) -> str:
    """
    Full, legend-friendly description of a sweep's parameters, spelled out
    rather than abbreviated: type, frequency range, length, start/end taper
    (in ms, always both ends, named so it can't be confused with the other
    numbers) and taper type, drive level, sample interval, plus whichever
    type-specific extras actually apply to this sweep type. E.g.

        Linear, 6-96 Hz, 12 s, tapers 500/300 ms cosine, 70% DL, 1 ms
        dB/Octave, 6-96 Hz, 12 s, tapers 250/250 ms cosine, 100% DL, 1 ms, boost 6 dB

    A non-zero start phase is included only when set, so the common
    zero-phase case doesn't carry a redundant field.
    """
    parts = [
        p.sweep_type,
        f"{_fmt(p.f1)}-{_fmt(p.f2)} Hz",
        f"{_fmt(p.length)} s",
        f"tapers {_fmt(p.taper_start_len * 1000.0)}/{_fmt(p.taper_end_len * 1000.0)} ms"
        f" {p.taper_type.lower()}",
        f"{_fmt(p.force_pct)}% DL",   # DL = drive level
        f"{_fmt(p.dt_ms)} ms",
    ]
    if p.start_phase_deg:
        parts.append(f"phase {_fmt(p.start_phase_deg)}°")

    # Only the extras that this sweep type actually uses -- the same
    # visibility rule the parameter panel applies to its input rows.
    if p.sweep_type in ("dB/Octave", "dB/Hz"):
        parts.append(f"boost {_fmt(p.nonlin_boost_db)} dB")
    elif p.sweep_type == "T-power":
        parts.append(f"t-power {_fmt(p.t_power_exp)}")
    elif p.sweep_type == "Random":
        parts.append(f"seed {int(p.random_seed)}")
        parts.append(f"smooth {_fmt(p.random_smooth_s * 1000.0)} ms")
    elif p.sweep_type == "Pulse":
        parts.append(f"{_fmt(p.pulse_cycles)} cycles")

    return ", ".join(parts)
