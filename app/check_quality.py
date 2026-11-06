"""
Flags audio files that are probably low quality (nothing above ~16kHz) or
probably faked (high-freq noise not tracking the music). Report only, no
renaming. Run it BEFORE the clean/convert step, while lossless files are still
lossless (a spectrum check on an AAC encode says nothing). Requires: pip install librosa numpy scipy mutagen (+ ffmpeg).
"""

import os

# Persistent numba cache so librosa's JIT cost isn't paid on every run.
os.environ.setdefault(
    "NUMBA_CACHE_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".numba_cache"),
)

import audio_common as common

try:
    import concurrent.futures
    import numpy as np
    import librosa
    import mutagen
    from scipy.signal import stft
except Exception:
    common.report_import_failure("librosa numpy scipy mutagen", needs_ffmpeg=True)
    raise SystemExit(1)

# Below this many files, run serially (avoids paying librosa's import/JIT
# cost once per worker process).
SMALL_BATCH_THRESHOLD = 150

HIGH_BAND = (16500, 20000)   # the band we're checking for real content
REF_BAND = (32, 16000)       # tracks the music's actual dynamics

RATIO_DB_HARD_CUTOFF = -45    # below this -> almost certainly low quality
RATIO_DB_REVIEW_CUTOFF = -35  # below this (but above hard cutoff) -> flag for manual review
CORR_SUSPICIOUS = 0.2         # below this -> high band doesn't track the music
SILENCE_GAP_SUSPICIOUS = 6    # below this (dB) -> high band doesn't fade in quiet parts

SEGMENT_RANGE = (0.20, 0.50)   # only analyze this portion of each file (fraction of duration)
MIN_DURATION_FOR_SEGMENT = 60  # seconds; shorter files are analyzed in full

AUDIO_EXTENSIONS = (
    ".wav", ".mp3", ".flac", ".aiff", ".aif", ".alac", ".ape", ".wv",
    ".dsf", ".dff", ".m4a", ".aac", ".ogg", ".opus", ".wma",
)


def get_duration(path):
    try:
        audio = mutagen.File(path)
        if audio is not None and audio.info is not None and audio.info.length:
            return audio.info.length
    except Exception:
        pass
    return None


def load_segment(path):
    duration = get_duration(path)
    if duration and duration >= MIN_DURATION_FOR_SEGMENT:
        offset = duration * SEGMENT_RANGE[0]
        load_duration = duration * (SEGMENT_RANGE[1] - SEGMENT_RANGE[0])
        return librosa.load(path, sr=None, mono=True, offset=offset, duration=load_duration)
    return librosa.load(path, sr=None, mono=True)


def band_energies(y, sr):
    """Per-frame mean energy in the high band, reference band, and overall."""
    f, _, Z = stft(y, fs=sr, nperseg=4096, noverlap=4096 - 2048)
    mag = np.abs(Z)

    nyquist = sr / 2
    high_mask = (f >= HIGH_BAND[0]) & (f <= min(HIGH_BAND[1], nyquist - 200))
    ref_mask = (f >= REF_BAND[0]) & (f <= REF_BAND[1])

    high_e = mag[high_mask].mean(axis=0)
    ref_e = mag[ref_mask].mean(axis=0)
    overall_e = mag.mean(axis=0)
    return high_e, ref_e, overall_e


def silence_gap_db(high_e, overall_e):
    """dB drop of the high band during quiet passages, or None if no clear quiet/loud split."""
    quiet = overall_e < np.percentile(overall_e, 15)
    if quiet.sum() <= 5 or (~quiet).sum() <= 5:
        return None
    high_db_self = 20 * np.log10(high_e / (high_e.max() + 1e-12) + 1e-12)
    return np.mean(high_db_self[~quiet]) - np.mean(high_db_self[quiet])


def analyze_file(path):
    y, sr = load_segment(path)

    nyquist = sr / 2
    if nyquist <= HIGH_BAND[0] + 500:
        return "LOW_QUALITY", f"sample rate too low ({sr}Hz, nothing above {HIGH_BAND[0]}Hz is even possible)"

    high_e, ref_e, overall_e = band_energies(y, sr)

    ratio_db = 20 * np.log10(high_e.mean() / (ref_e.mean() + 1e-12) + 1e-12)
    if ratio_db < RATIO_DB_HARD_CUTOFF:
        return "LOW_QUALITY", f"high band is {ratio_db:.1f}dB below reference -- almost certainly low quality"
    if ratio_db < RATIO_DB_REVIEW_CUTOFF:
        return "REVIEW", f"high band is {ratio_db:.1f}dB below reference -- borderline, could just be a quiet track"

    corr = np.corrcoef(high_e, ref_e)[0, 1]
    gap = silence_gap_db(high_e, overall_e)

    if corr < CORR_SUSPICIOUS and (gap is None or gap < SILENCE_GAP_SUSPICIOUS):
        gap_str = f"{gap:.1f}dB" if gap is not None else "n/a"
        return "SUSPICIOUS", f"high band doesn't track the music (corr={corr:.2f}, silence_gap={gap_str}) -- possible pasted-on noise"

    return "OK", f"ratio_db={ratio_db:.1f}, corr={corr:.2f}"


def record(results, i, total, name, verdict, detail):
    """Print one file's outcome and file it into `results` by verdict."""
    print(f"[{i}/{total}] {name}")
    if verdict is None:
        print("  ---- couldn't analyze:", detail)
    else:
        print("  ", verdict, "-", detail)
    results[verdict if verdict else "ERROR"].append((name, detail))


def analyze_all(folder_path, files):
    results = {k: [] for k in ("OK", "LOW_QUALITY", "REVIEW", "SUSPICIOUS", "ERROR")}

    if len(files) <= SMALL_BATCH_THRESHOLD:
        for i, name in enumerate(files, 1):
            try:
                verdict, detail = analyze_file(os.path.join(folder_path, name))
            except Exception as e:
                verdict, detail = None, e
            record(results, i, len(files), name, verdict, detail)
        return results

    print("Large batch detected, expect a longer startup before results appear.\n")
    max_workers = min(os.cpu_count() or 4, len(files))
    with concurrent.futures.ProcessPoolExecutor(max_workers=max_workers) as executor:
        future_to_name = {
            executor.submit(analyze_file, os.path.join(folder_path, name)): name
            for name in files
        }
        for i, future in enumerate(concurrent.futures.as_completed(future_to_name), 1):
            try:
                verdict, detail = future.result()
            except Exception as e:
                verdict, detail = None, e
            record(results, i, len(files), future_to_name[future], verdict, detail)
    return results


def print_summary(results):
    print("\n\n==================== SUMMARY ====================")
    print("OK:", len(results["OK"]))

    for label, key in (("LOW QUALITY", "LOW_QUALITY"), ("MAYBE LOW QUALITY", "REVIEW")):
        print(f"\n{label}:", len(results[key]))
        for name, _ in results[key]:
            print(name)

    print("\nSUSPICIOUS:", len(results["SUSPICIOUS"]))
    for name, detail in results["SUSPICIOUS"]:
        print(name, "  |  ", detail)

    if results["ERROR"]:
        print("\nCouldn't analyze:", len(results["ERROR"]))
        for name, _ in results["ERROR"]:
            print(name)


def main():
    folder_path = common.work_folder(__file__)
    common.check_ffmpeg_available("decode most audio formats (mp3, m4a, aac, etc)")

    files = common.list_audio_files(folder_path, AUDIO_EXTENSIONS)
    print(f"Analyzing {len(files)} audio file(s)...\n")

    results = analyze_all(folder_path, files)
    print_summary(results)
    common.pause_exit()


if __name__ == "__main__":
    common.run_main(main, ffmpeg_hint=True)