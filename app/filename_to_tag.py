"""
Wipes all tags (keeping cover art) and re-derives Artist/Title from the
filename, splitting on the first " - ". Files with no " - " are skipped.
Requires: pip install mutagen
"""

import os

import audio_common as common

try:
    from mutagen.mp3 import MP3
    from mutagen.wave import WAVE
    from mutagen.id3 import TIT2, TPE1
except Exception:
    common.report_import_failure("mutagen")
    raise SystemExit(1)

AUDIO_EXTENSIONS = (".mp3", ".wav")


def load_audio(path):
    lower = path.lower()
    if lower.endswith(".mp3"):
        return MP3(path)
    if lower.endswith(".wav"):
        return WAVE(path)
    return None


def strip_tags_keep_cover(audio):
    """Delete every tag frame except APIC (cover art)."""
    if audio.tags is None:
        audio.add_tags()
        return

    covers = audio.tags.getall("APIC")
    audio.tags.clear()
    for cover in covers:
        audio.tags.add(cover)


def set_title_artist(audio, base_name):
    """Split base_name on the first ' - ' into artist/title tags. False if no ' - ' found."""
    artist, sep, title = base_name.partition(" - ")
    if sep == "":
        return False
    audio.tags.add(TPE1(encoding=3, text=[artist.strip()]))
    audio.tags.add(TIT2(encoding=3, text=[title.strip()]))
    return True


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
        set_title_artist(audio, base_name)
        audio.save()
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