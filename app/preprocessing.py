"""
Per-file preprocessing that runs before any filename cleanup:
  1) reorder a tag-confirmed "Title - Artist" filename to "Artist - Title"
  2) convert every lossless file (wav/flac/aiff/...) to m4a via qaac (TVBR)

mp3, m4a and aac are left alone. Formats Pioneer players can't play (ogg, opus,
wma, mid) are flagged, not converted.

Exposes check_dependencies() and preprocess_file(); the caller only has to
apply the returned rename log and counters.
"""

import os
import re
import struct
import subprocess
import threading

import audio_common as common

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

try:
    # Optional: only needed for the "Title - Artist" reorder, which reads the
    # file's own tags. Conversion and filename cleanup work without it.
    import mutagen
except Exception:
    mutagen = None

QAAC_TVBR = 109
ENCODE_WORKERS = 7  # parallel encodes (each one runs its own ffmpeg + qaac process)
_finalize_lock = threading.Lock()

KEPT_EXTENSIONS = (".mp3", ".m4a", ".aac")
LOSSLESS_EXTENSIONS = (".wav", ".flac", ".aiff", ".aif", ".alac", ".ape", ".wv", ".dsf", ".dff")
PIONEER_UNSUPPORTED_EXTENSIONS = (".ogg", ".opus", ".wma", ".mid", ".midi")
AUDIO_EXTENSIONS = KEPT_EXTENSIONS + LOSSLESS_EXTENSIONS + PIONEER_UNSUPPORTED_EXTENSIONS


def check_dependencies():
    """Report on the tools and return {"qaac": path or None, "ffmpeg": bool}."""
    qaac = common.find_qaac()
    if qaac is None:
        print("---- qaac64 was not found (not on PATH, QAAC_EXE in audio_common.py not a valid file) ----")
        print("     Lossless files can't be converted to m4a until it is.\n")
    ffmpeg_ok = common.check_ffmpeg_available(
        "convert flac/aiff/alac/ape/wv/dsf/dff and float-PCM wavs for qaac"
    )
    if mutagen is None:
        print("---- mutagen not installed (pip install mutagen): can't read tags, so")
        print('     "Title - Artist" filenames won\'t be reordered to "Artist - Title".\n')
    return {"qaac": qaac, "ffmpeg": ffmpeg_ok}


# ----------------------------- tag-based reorder -----------------------------

def normalize_for_tag_match(text):
    """Squash text to bare lowercase alphanumerics so a filename segment can be
    compared to a tag value despite underscores, spacing, punctuation or casing
    ("CRAZIER_THAN_YOUR_GRANNY" matches the title tag "Crazier Than Your Granny")."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def read_artist_title_tags(path):
    """Return (artist, title) from the file's own tags, or None.

    Uses mutagen's format-agnostic File(..., easy=True), so flac (Vorbis
    comments), mp3 (ID3) and m4a/ogg/opus all expose the same "artist"/"title"
    keys -- unlike filename_to_tag.py, which needs the per-format classes
    because it writes raw ID3 frames."""
    if mutagen is None:
        return None
    try:
        audio = mutagen.File(path, easy=True)
    except Exception:
        return None
    if audio is None or not audio.tags:
        return None

    def first(key):
        try:
            value = audio.tags.get(key)
        except Exception:
            return ""
        if not value:
            return ""
        return (value if isinstance(value, str) else str(value[0])).strip()

    artist, title = first("artist"), first("title")
    return (artist, title) if artist and title else None


def swapped_name_from_tags(folder_path, file_name):
    """If file_name is "Title - Artist.ext" according to the file's own
    title/artist tags, return the "Artist - Title.ext" version, else None
    (missing tags, not exactly two dash-segments, already artist-first, or no
    clean match -- in which case we don't guess)."""
    base_name, dot, ext_part = file_name.rpartition(".")
    if dot == "":
        return None

    tags = read_artist_title_tags(os.path.join(folder_path, file_name))
    if tags is None:
        return None
    artist, title = tags

    # Underscores become spaces during cleanup anyway, so match the spaced form.
    segments = [s.strip() for s in base_name.replace("_", " ").split(" - ")]
    if len(segments) != 2 or not all(segments):
        return None

    artist_key, title_key = normalize_for_tag_match(artist), normalize_for_tag_match(title)
    first_seg, second_seg = (normalize_for_tag_match(s) for s in segments)
    if not artist_key or not title_key or artist_key == title_key:
        return None
    if first_seg == artist_key or first_seg != title_key or second_seg != artist_key:
        return None

    return f"{segments[1]} - {segments[0]}.{ext_part}"


def reorder_from_tags(folder_path, file_name):
    """Rename a tag-confirmed "Title - Artist" file to "Artist - Title".
    Returns the (possibly unchanged) filename."""
    try:
        swapped = swapped_name_from_tags(folder_path, file_name)
    except Exception as e:
        print("---- Couldn't check tags for artist/title order:", file_name, "--", e)
        return file_name

    if not swapped or swapped == file_name:
        return file_name
    if os.path.exists(os.path.join(folder_path, swapped)):
        print("---- Skipping reorder (target name already exists):", swapped)
        return file_name

    os.rename(os.path.join(folder_path, file_name), os.path.join(folder_path, swapped))
    print("Reordered to Artist - Title (confirmed by tags):")
    print("Old:", file_name)
    print("New:", swapped)
    return swapped


# ------------------------------- wav conversion ------------------------------

def is_float_wav(path):
    """True if the wav's fmt chunk declares IEEE-float samples."""
    try:
        with open(path, "rb") as f:
            riff = f.read(12)
            if len(riff) < 12 or riff[0:4] != b"RIFF" or riff[8:12] != b"WAVE":
                return False
            while True:
                header = f.read(8)
                if len(header) < 8:
                    return False
                chunk_id, chunk_size = struct.unpack("<4sI", header)
                if chunk_id == b"fmt ":
                    fmt_data = f.read(chunk_size)
                    audio_format = struct.unpack("<H", fmt_data[0:2])[0]
                    if audio_format == 0xFFFE:  # WAVE_FORMAT_EXTENSIBLE
                        return struct.unpack("<H", fmt_data[24:26])[0] == 0x0003
                    return audio_format == 0x0003  # WAVE_FORMAT_IEEE_FLOAT
                f.seek(chunk_size + (chunk_size % 2), 1)
    except Exception:
        return False


def needs_ffmpeg_pipe(path):
    """qaac reads int-PCM wav itself; everything else goes through ffmpeg first."""
    return not path.lower().endswith(".wav") or is_float_wav(path)


def extract_cover(src, cover_path):
    """Dump the first embedded picture to cover_path. True if there was one."""
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", src, "-an", "-c:v", "copy", "-frames:v", "1", "-f", "image2", cover_path],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW,
    )
    return os.path.isfile(cover_path) and os.path.getsize(cover_path) > 0


def run_qaac(qaac, src, out_path):
    """Encode src to out_path. Returns True on success."""
    cmd = [qaac, "--tvbr", str(QAAC_TVBR)]

    if not needs_ffmpeg_pipe(src):
        p = subprocess.run(cmd + [src, "-o", out_path], capture_output=True, creationflags=NO_WINDOW)
        return p.returncode == 0

    cover_path = out_path + ".cover"
    tags = read_artist_title_tags(src)
    if tags:
        cmd += ["--artist", tags[0], "--title", tags[1]]
    if extract_cover(src, cover_path):
        cmd += ["--artwork", cover_path]

    ffmpeg_cmd = ["ffmpeg", "-v", "error", "-i", src, "-vn"]
    if src.lower().endswith((".dsf", ".dff")):
        ffmpeg_cmd += ["-ar", "44100"]  # DSD decodes to a rate AAC can't take
    ffmpeg_cmd += ["-c:a", "pcm_s32le", "-f", "wav", "-"]

    try:
        ffmpeg = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
        q = subprocess.run(cmd + ["--ignorelength", "-", "-o", out_path], stdin=ffmpeg.stdout,
                           capture_output=True, creationflags=NO_WINDOW)
        ffmpeg.stdout.close()
        return q.returncode == 0 and ffmpeg.wait() == 0
    finally:
        if os.path.exists(cover_path):
            os.remove(cover_path)


def transcode_to_m4a(folder_path, file_name, qaac):
    """Convert to .m4a next to the source and delete the source on success.
    Returns the new filename, or None on failure (source left untouched)."""
    src = os.path.join(folder_path, file_name)
    output_filename = file_name.rpartition(".")[0] + ".m4a"
    output_path = os.path.join(folder_path, output_filename)
    if os.path.exists(output_path):
        print("---- Skipping conversion (target name already exists):", output_filename)
        return None

    temp_path = os.path.join(folder_path, "converting_" + file_name + ".m4a")  # unique per source file
    try:
        ok = run_qaac(qaac, src, temp_path)
    except Exception as e:
        print("---- qaac/ffmpeg error:", e)
        ok = False

    if not ok or not os.path.isfile(temp_path) or os.path.getsize(temp_path) == 0:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        return None

    with _finalize_lock:  # two sources (a.wav + a.flac) can map to the same m4a name
        if os.path.exists(output_path):
            os.remove(temp_path)
            print("---- Skipping conversion (target name already exists):", output_filename)
            return None
        os.replace(temp_path, output_path)
    os.remove(src)
    return output_filename


# -------------------------------- entry point --------------------------------

def preprocess_file(folder_path, file_name, tools):
    """Run every pre-rename step on one file. `tools` comes from check_dependencies().

    Returns (file_name, result) where file_name is the current name on disk and
    result is a dict of:
      renames     -- [(old, new)] rename steps, in the order they happened
      reordered   -- True if the name was flipped to "Artist - Title"
      converted   -- True if the file was encoded to m4a
      failed      -- True if a needed conversion couldn't be done
      unsupported -- True for formats Pioneer players can't play (left as-is)
    """
    result = {"renames": [], "reordered": False, "converted": False, "failed": False, "unsupported": False}

    new_name = reorder_from_tags(folder_path, file_name)
    if new_name != file_name:
        result["renames"].append((file_name, new_name))
        file_name = new_name
        result["reordered"] = True

    lower_name = file_name.lower()
    if lower_name.endswith(PIONEER_UNSUPPORTED_EXTENSIONS):
        result["unsupported"] = True
        return file_name, result

    if not lower_name.endswith(LOSSLESS_EXTENSIONS):
        return file_name, result

    if not tools["qaac"]:
        print("---- Skipping conversion (qaac not found):", file_name)
        result["failed"] = True
        return file_name, result

    if not tools["ffmpeg"] and needs_ffmpeg_pipe(os.path.join(folder_path, file_name)):
        print("---- Skipping conversion (ffmpeg not found on PATH):", file_name)
        result["failed"] = True
        return file_name, result

    converted_name = transcode_to_m4a(folder_path, file_name, tools["qaac"])
    if converted_name:
        print("Converted to M4A:", file_name, "->", converted_name)
        result["converted"] = True
        file_name = converted_name  # not logged as a rename, so revert never relabels an m4a as its old extension
    else:
        print("---- Conversion failed for:", file_name)
        result["failed"] = True

    return file_name, result