"""
Wipes all tags (keeping cover art, and the m4a gapless iTunSMPB atom) and re-derives Artist/Title from the
filename, splitting on the first " - ". Files with no " - " are skipped.
Requires: pip install mutagen
"""

import os

import audio_common as common

try:
    from mutagen.mp3 import MP3
    from mutagen.wave import WAVE
    from mutagen.mp4 import MP4
    from mutagen.id3 import TIT2, TPE1
except Exception:
    common.report_import_failure("mutagen")
    raise SystemExit(1)

AUDIO_EXTENSIONS = (".mp3", ".wav", ".m4a")
MP4_KEEP_KEYS = ("covr", "----:com.apple.iTunes:iTunSMPB")


def load_audio(path):
    lower = path.lower()
    if lower.endswith(".mp3"):
        return MP3(path)
    if lower.endswith(".wav"):
        return WAVE(path)
    if lower.endswith(".m4a"):
        return MP4(path)
    return None


def strip_wav_list_chunks(path):
    """
    Remove any RIFF 'LIST' chunks (e.g. INFO: IART/INAM/ICMT/etc.) from a WAV file.

    mutagen's WAVE class only reads/writes the 'id3 ' chunk. Many WAV files
    (from DAWs, Explorer, Rekordbox, etc.) instead store metadata in an older
    RIFF LIST/INFO chunk, which mutagen never touches -- so clearing ID3 tags
    alone leaves that old metadata fully intact. WAV files essentially never
    carry cover art in a LIST chunk, so it's safe to drop entirely here.
    """
    with open(path, "rb") as f:
        data = f.read()

    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return  # not a RIFF/WAVE file we understand; leave untouched

    body = data[12:]
    kept = bytearray()
    removed_any = False
    i = 0
    while i + 8 <= len(body):
        chunk_id = body[i:i + 4]
        size = int.from_bytes(body[i + 4:i + 8], "little")
        padded_size = size + (size & 1)  # chunks are word-aligned
        chunk_total = 8 + padded_size
        if i + chunk_total > len(body):
            # malformed/truncated chunk size; keep the rest as-is rather than guess
            kept.extend(body[i:])
            break
        if chunk_id == b"LIST":
            removed_any = True
        else:
            kept.extend(body[i:i + chunk_total])
        i += chunk_total

    if not removed_any:
        return

    new_riff_size = 4 + len(kept)  # 'WAVE' + chunks
    new_data = b"RIFF" + new_riff_size.to_bytes(4, "little") + b"WAVE" + bytes(kept)
    with open(path, "wb") as f:
        f.write(new_data)


def write_wav_info_chunk(path, artist, title):
    """
    Write a RIFF LIST/INFO chunk containing IART (artist) and INAM (title).

    Explorer and Rekordbox read WAV metadata from this legacy INFO chunk, not
    from the ID3 chunk mutagen writes -- so after stripping the old INFO
    chunk, we need to add a fresh one with the corrected values or those
    tools will show blank/derived-from-filename metadata.
    """
    def info_subchunk(chunk_id, text):
        data = text.encode("utf-8") + b"\x00"
        if len(data) % 2:
            data += b"\x00"  # word-align
        return chunk_id + len(data).to_bytes(4, "little") + data

    info_body = b"INFO" + info_subchunk(b"IART", artist) + info_subchunk(b"INAM", title)
    list_chunk = b"LIST" + len(info_body).to_bytes(4, "little") + info_body

    with open(path, "rb") as f:
        data = f.read()

    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return  # not a RIFF/WAVE file we understand; leave untouched

    body = data[12:] + list_chunk
    new_riff_size = 4 + len(body)  # 'WAVE' + chunks
    new_data = b"RIFF" + new_riff_size.to_bytes(4, "little") + b"WAVE" + body
    with open(path, "wb") as f:
        f.write(new_data)


def strip_tags_keep_cover(audio):
    """Delete every tag frame except cover art (APIC / covr)."""
    if isinstance(audio, MP4):
        if audio.tags is None:
            audio.add_tags()
        for key in [k for k in audio.tags.keys() if k not in MP4_KEEP_KEYS]:
            del audio.tags[key]
        return

    if audio.tags is None:
        audio.add_tags()
        return

    covers = audio.tags.getall("APIC")
    audio.tags.clear()
    for cover in covers:
        audio.tags.add(cover)


def set_title_artist(audio, base_name):
    """Split base_name on the first ' - ' into artist/title tags. Returns (artist, title) or None if no ' - ' found."""
    artist, sep, title = base_name.partition(" - ")
    if sep == "":
        return None
    artist, title = artist.strip(), title.strip()
    if isinstance(audio, MP4):
        audio.tags["\xa9ART"] = [artist]
        audio.tags["\xa9nam"] = [title]
        return artist, title
    audio.tags.add(TPE1(encoding=3, text=[artist]))
    audio.tags.add(TIT2(encoding=3, text=[title]))
    return artist, title


def retag_file(folder_path, name):
    """Process one file. Returns 'tagged', 'skipped', or 'failed'."""
    base_name, _, _ = name.rpartition(".")
    if " - " not in base_name:
        print("---- Skipping (no ' - ' to derive artist/title):", name)
        return "skipped"

    path = os.path.join(folder_path, name)
    try:
        audio = load_audio(path)
        strip_tags_keep_cover(audio)
        artist_title = set_title_artist(audio, base_name)
        # v1=2 forces mutagen to also remove any legacy ID3v1 tag (the trailing
        # 128 bytes some tools still read as a fallback) instead of rewriting it.
        if isinstance(audio, MP4):
            audio.save()
        else:
            audio.save(v1=2)
        if path.lower().endswith(".wav"):
            strip_wav_list_chunks(path)
            if artist_title is not None:
                artist, title = artist_title
                write_wav_info_chunk(path, artist, title)
    except Exception as e:
        print("---- Failed:", name, "--", e)
        return "failed"

    print(f"Tagged: {name}")
    return "tagged"


def print_summary(results):
    print("\n\n==================== SUMMARY ====================")
    for label, key in (("Tagged", "tagged"), ("Skipped (no ' - ' in name)", "skipped"), ("Failed", "failed")):
        names = results[key]
        print(f"{label}: {len(names)}")
        for name in names:
            print(" -", name)


def main():
    folder_path = common.work_folder(__file__)
    files = common.list_audio_files(folder_path, AUDIO_EXTENSIONS)
    print(f"Processing {len(files)} audio file(s)...\n")

    results = {"tagged": [], "skipped": [], "failed": []}
    for name in files:
        results[retag_file(folder_path, name)].append(name)

    print_summary(results)
    common.pause_exit()


if __name__ == "__main__":
    common.run_main(main)