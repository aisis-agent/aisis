"""A short, narrow-band tone is a chime, not a request (tickets #158 and #59).

The station's own activation chime leaks into its microphone as a 0.2-0.5 s tone with a spectral peak at 310-320 Hz. The
endpointer's VAD calls it speech, the 200 ms gate passes it, and recognisers answer with words: Whisper wrote
«Громкость.» or a subtitle caption, GigaAM and T-one write «да», «а», «у». In the always-on archive (2026-11-09 .. 11-20) one
clip in about twenty of those that reached a recogniser is this tone.

What tells it from a word: nearly all of its energy sits in one or two peaks 40 Hz wide. Speech spreads over many pitch
harmonics and moves; the loudest 20 Hz-wide pair of spectral peaks of a spoken word holds well under 0.6 of the energy below
1.9 kHz (synthetic Piper commands: at most 0.59; the archive's long sent clips: at most 0.90), a station chime holds 0.95 or
more. The rule needs all of: the audible part of the cut (every frame above -30 dB of the loudest, so a quiet request after the
chime is part of it) is at most MAX_TONE_MS long; at least
MIN_CONCENTRATION of its energy is in the two strongest peaks; and every audible window of it (the windows tile the whole cut, at most
100 ms each) holds at least MIN_SEGMENT_CONCENTRATION of its energy at those same two peaks (the chime holds the energy of a cut,
so a request spoken under it shows only window by window, and a voiced word is quasi-periodic too, but at its own pitch). A request
run into the chime is longer than MAX_TONE_MS or has a spread spectrum, so it is never dropped; so is a long steady note. The price:
a chime whose decay merges into noise (a last window below 0.75) is let through, as is a request 30 dB under the chime, and a
voiced reply of 150 ms or less 20-30 dB under a TWO-tone earcon can be taken for the earcon (the station's own chime is one tone:
a 300 ms single-tone chime with a 100-300 ms reply is never dropped in the unit sweep).

Pure functions, standard library only (the gateway's CI has no numpy).
"""
from __future__ import annotations

import array
import cmath
import math

FRAME_MS = 20
ACTIVE_OF_PEAK = 0.03       # frames above this share of the loudest frame's RMS (-30 dB) belong to the cut's sound

MAX_TONE_MS = 600           # a longer audible stretch is a word or a note, never the chime (0.2-0.5 s)
MIN_TONE_MS = 60            # shorter than three frames cannot be told from a click
DECIMATE = 4                # 16 kHz -> 4 kHz: the chime and its partial are below 1.4 kHz, and the FFT stays small
BAND_HZ = (80.0, 1900.0)
PEAKS = 2                   # a tone and one partial (the two-tone earcons are a fifth apart)
PEAK_HALF_HZ = 20.0
MIN_CONCENTRATION = 0.90
SEGMENT_MS = 100            # a quiet request after the chime hides in the whole cut's spectrum (the chime holds the energy), not in its own 100 ms
MIN_SEGMENT_CONCENTRATION = 0.75
SEGMENT_HALF_HZ = 35.0     # a 60-100 ms window resolves a tone to about ±30 Hz (Hann main lobe), wider than the whole cut's 20 Hz
MIN_FFT = 1024

MODES = {"0": "off", "off": "off", "1": "enforce", "on": "enforce", "enforce": "enforce", "shadow": "shadow"}
DEFAULT_MODE = "shadow"     # log what would be dropped, drop nothing: a request that is nearly a pure tone is measured before it can be lost


def parse_mode(raw, default):
    """The gateway's AISIS_STT_TONE_GUARD: 0/off, 1/on/enforce or shadow; unset or unknown keeps `default`."""
    return MODES.get(str(raw).strip().lower(), default) if raw is not None else default


def active_span(samples, rate):
    """(first, last_exclusive, threshold): sample indices from the first to the last AUDIBLE frame, and the RMS that made a frame
    audible: above ACTIVE_OF_PEAK of the loudest frame. No room floor is estimated: the cut's own quiet frames may be a request the VAD
    missed, and a floor taken from them would drop that request from the span; with only the -30 dB rule a noisy room makes the span
    longer than MAX_TONE_MS and the cut is left to the other gates. Audible, not merely loud: a request spoken 20 dB under the chime
    that precedes it must stay inside the span."""
    frame = rate * FRAME_MS // 1000
    levels = []
    for start in range(0, len(samples) - frame + 1, frame):
        chunk = samples[start:start + frame]
        levels.append(math.sqrt(sum(s * s for s in chunk) / frame))
    if not levels or max(levels) <= 0:
        return None
    threshold = ACTIVE_OF_PEAK * max(levels)
    active = [index for index, level in enumerate(levels) if level >= threshold]
    return active[0] * frame, (active[-1] + 1) * frame, threshold


def fft(values):
    """In-place iterative radix-2 FFT of a power-of-two list of complex numbers."""
    size = len(values)
    j = 0
    for i in range(1, size):
        bit = size >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j |= bit
        if i < j:
            values[i], values[j] = values[j], values[i]
    length = 2
    while length <= size:
        step = cmath.exp(-2j * math.pi / length)
        half = length >> 1
        twiddles = [1 + 0j]
        for _ in range(half - 1):
            twiddles.append(twiddles[-1] * step)
        for start in range(0, size, length):
            for k in range(half):
                even = values[start + k]
                odd = values[start + k + half] * twiddles[k]
                values[start + k] = even + odd
                values[start + k + half] = even - odd
        length <<= 1
    return values


def band_power(samples, rate):
    """(power per FFT bin inside BAND_HZ, Hz per bin) of the cut, or None when it is too short to analyse."""
    reduced = [sum(samples[i:i + DECIMATE]) / DECIMATE for i in range(0, len(samples) - DECIMATE + 1, DECIMATE)]
    count = len(reduced)
    if count < 8:
        return None
    mean = sum(reduced) / count
    window = [0.5 - 0.5 * math.cos(2 * math.pi * i / (count - 1)) for i in range(count)]
    size = MIN_FFT
    while size < count:
        size <<= 1
    values = [complex((reduced[i] - mean) * window[i]) for i in range(count)] + [0j] * (size - count)
    fft(values)
    hz = rate / DECIMATE / size
    low, high = int(BAND_HZ[0] / hz), min(int(BAND_HZ[1] / hz), size // 2)
    power = [0.0] * (size // 2 + 1)
    for k in range(low, high + 1):
        power[k] = values[k].real ** 2 + values[k].imag ** 2
    return power, hz


def strongest_peaks(power, hz):
    """(share of the band's energy in the PEAKS strongest PEAK_HALF_HZ-wide peaks, their centre frequencies in Hz); (0.0, []) for silence."""
    total = sum(power)
    if total <= 0:
        return 0.0, []
    power = list(power)
    half = max(1, int(PEAK_HALF_HZ / hz))
    held = 0.0
    centres = []
    for _ in range(PEAKS):
        peak = max(range(len(power)), key=power.__getitem__)
        lo, hi = max(0, peak - half), min(len(power), peak + half + 1)
        held += sum(power[lo:hi])
        centres.append(peak * hz)
        for k in range(lo, hi):
            power[k] = 0.0
    return held / total, centres


def concentration(samples, rate):
    """Share of the energy in BAND_HZ that lies in the PEAKS strongest PEAK_HALF_HZ-wide peaks (1.0 = pure tones, ~0.05 = noise)."""
    spectrum = band_power(samples, rate)
    return strongest_peaks(*spectrum)[0] if spectrum else 0.0


def share_at(samples, rate, centres, half_hz):
    """Share of the energy in BAND_HZ within `half_hz` of any of the `centres` (Hz): how much of a window is the whole cut's own tones."""
    spectrum = band_power(samples, rate)
    if not spectrum:
        return 0.0
    power, hz = spectrum
    total = sum(power)
    if total <= 0:
        return 0.0
    near = {k for centre in centres for k in range(max(0, int((centre - half_hz) / hz)), min(len(power), int((centre + half_hz) / hz) + 2))}
    return sum(power[k] for k in near) / total


def is_tone(pcm, rate):
    """True when a 16-bit mono PCM cut is a chime-like tone (see the module doc); anything unusual answers False (never drops speech)."""
    if rate % DECIMATE or len(pcm) < 2 * rate * MIN_TONE_MS // 1000:
        return False
    samples = array.array("h")
    samples.frombytes(pcm[:len(pcm) // 2 * 2])
    span = active_span(samples, rate)
    if span is None:
        return False
    first, last, threshold = span
    length_ms = (last - first) * 1000 // rate
    if not MIN_TONE_MS <= length_ms <= MAX_TONE_MS:
        return False
    cut = samples[first:last]
    spectrum = band_power(cut, rate)
    if not spectrum:
        return False
    share, centres = strongest_peaks(*spectrum)
    if share < MIN_CONCENTRATION:
        return False
    # Energy is dominated by the chime: a request under it must be heard window by window. The windows tile the whole cut (equal length,
    # at most SEGMENT_MS), so no suffix of it is left unexamined; a window counts when any of its frames is audible (a request that
    # starts mid-window is half quiet, and its window must not be skipped for the sake of its average). An audible window must be the
    # cut's own two tones: a voiced word is quasi-periodic too (a 100 ms vowel concentrates in two peaks of its own) but its peaks are
    # not the chime's.
    frame = rate * FRAME_MS // 1000
    count = max(1, -(-len(cut) // (rate * SEGMENT_MS // 1000)))
    size = len(cut) // count
    for index in range(count):
        part = cut[index * size:(index + 1) * size if index < count - 1 else len(cut)]
        audible = any(math.sqrt(sum(s * s for s in part[start:start + frame]) / frame) >= threshold for start in range(0, len(part) - frame + 1, frame))
        if audible and share_at(part, rate, centres, SEGMENT_HALF_HZ) < MIN_SEGMENT_CONCENTRATION:
            return False
    return True
