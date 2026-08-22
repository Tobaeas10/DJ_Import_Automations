"""
Batch-cleans song filenames in your work folder into a consistent
"Artist - Title (Remix info)" style, and auto-converts lossless audio
(flac, aiff, alac, ape, wv, dsf, dff) to .wav via ffmpeg. Lossy formats
(m4a, aac, ogg, opus, wma, mid) are renamed but left in their original
codec. Existing float-PCM .wav files are also re-encoded to standard PCM.
"""

import os
import re
import struct
import subprocess

import audio_common as common

"""Junk tags to be stripped. Whitespace/parens optional, even glued
to another word ("NAMEv15" -> "NAME"). Longest/most specific first.

CAREFUL_JUNK_TAGS: kept (and reported) instead of removed if more real text
follows -- e.g. "Original Dubplate" -- since these words can plausibly be
part of a real title/phrase.

STRICT_JUNK_TAGS: always removed wherever found, no matter what follows."""
CAREFUL_JUNK_TAGS = [
    r"Original\s*Mix", r"Extended\s*Mix", r"Radio\s*Edit", r"Radio\s*Mix", r"Radio\s*Version",
    r"Club\s*Mix", r"Dub\s*Mix", r"Main\s*Mix", r"Album\s*Version", r"Single\s*Version",
    r"Bonus\s*Track", r"Work\s*In\s*Progress", r"Final\s*Mix", r"Playable", r"V\d+",
    r"Original", r"Final", r"Full", r"Free", r"Remastered", r"Remaster", r"Instrumental",
    r"Acapella", r"Acappella", r"Clean", r"Dirty", r"Explicit", r"V\d+\s*Final", r"Demo",
]

STRICT_JUNK_TAGS = [
    r"!!!+", r"™", r"®", r"©", r"Free\s*Dl", r"Free\s*Download", r"Snippet", r"Preview",
    r"Unreleased", r"WIP", r"HQ", r"HD",
]

JUNK_TAGS = CAREFUL_JUNK_TAGS + STRICT_JUNK_TAGS

"""Word swaps: Need non-alphanumerical symbols around the word/phrase in order to match"""
TEXT_FIXES = ("Vip Vip -> VIP2, Vip -> VIP, Bootleg -> Remix, X -> &, Vs -> &, "
              "Akira -> Akirah, Subz -> Subfiltronik, Tw!sty -> Twisty, Ember. -> Ember, "
              "Yunit. -> Yunit")

"""Word swaps but only to prevent title-casing to mess up special names"""
NAME_ALLCAPS_FIXES = (
    "NB, LV, AD, NT, IITYX, TEOTI, IP, x87, DGE, DRS, LSB, MPH, JG, ID, DJ, AJ, JJD, FD, R28D"
)



# Non-User-intended lists
LOWERCASE_WORDS = ["Is", "To", "It", "Of", "By", "A", "An", "Out", "The", "From", "My"]

PROCESSED_EXTENSIONS = (".wav", ".mp3")
LOSSLESS_EXTENSIONS = (".flac", ".aiff", ".aif", ".alac", ".ape", ".wv", ".dsf", ".dff")
LOSSY_EXTENSIONS = (".m4a", ".aac", ".ogg", ".opus", ".wma", ".mid", ".midi")
AUDIO_EXTENSIONS = PROCESSED_EXTENSIONS + LOSSLESS_EXTENSIONS + LOSSY_EXTENSIONS


def title_case_preserving_dots(file_name):
    """Title-case file_name up to (but not including) the extension dot,
    capitalizing after each space/paren/hyphen."""
    ext_dot_index = file_name.rfind(".")
    result = ""
    capitalize_next = True
    for i, letter in enumerate(file_name):
        if i == ext_dot_index:
            result += file_name[ext_dot_index:]
            break
        result += letter.capitalize() if capitalize_next else letter.lower()
        capitalize_next = letter in " ([-"
    return result


def strip_junk_tags(name_part):
    """Remove STRICT_JUNK_TAGS unconditionally (simple replace, wherever
    found). Remove CAREFUL_JUNK_TAGS too, but kept (and reported) instead of
    removed if more real text follows -- e.g. "Original Dubplate". Runs to a
    fixed point first so ordering between tags doesn't cause false keeps.
    Punctuation-only tags skip the \\b word boundary, since \\b can't match
    next to punctuation."""
    def is_safe_tail(match):
        rest = match.string[match.end():].lstrip()
        return not rest or rest[0] in "-()"

    def tag_pattern(tag):
        boundary = r"\b" if re.search(r"\w", tag) else ""
        # Consume whitespace *inside* an optional wrapping paren pair (so
        # "( Snippet )" -> "") but never outer whitespace -- eating outer
        # whitespace on a tag glued to one side (e.g. "Word!!!" with no
        # space before the "!!!") would swallow the one real space that
        # follows it and glue the surrounding words together.
        return rf"\(\s*{tag}{boundary}\s*\)|{tag}{boundary}"

    # Strict tags: always removed, no fixed-point loop needed (no keep-check
    # to interact with), but run before the careful loop so e.g. "!!!"
    # glued next to a careful tag doesn't interfere with its tail check.
    for tag in STRICT_JUNK_TAGS:
        name_part = re.sub(tag_pattern(tag), "", name_part, flags=re.IGNORECASE)

    changed = True
    while changed:
        changed = False
        for tag in CAREFUL_JUNK_TAGS:
            pattern = tag_pattern(tag)
            new_name_part = re.sub(
                pattern, lambda m: "" if is_safe_tail(m) else m.group(0),
                name_part, flags=re.IGNORECASE,
            )
            if new_name_part != name_part:
                changed = True
                name_part = new_name_part

    kept = []
    for tag in CAREFUL_JUNK_TAGS:
        pattern = tag_pattern(tag)
        kept += [m.group(0).strip() for m in re.finditer(pattern, name_part, re.IGNORECASE) if not is_safe_tail(m)]

    return name_part, kept


def parse_fix_pairs(fix_string):
    """Parse a 'Wrong -> Right, Wrong2 -> Right2' string into (wrong, right)
    tuples, used by both TEXT_FIXES and NAME_TEXT_FIXES."""
    pairs = []
    for chunk in fix_string.split(","):
        if "->" not in chunk:
            continue
        wrong, right = chunk.split("->", 1)
        pairs.append((wrong.strip(), right.strip()))
    return pairs


def replace_with_word_boundaries(name_part, wrong, right):
    """Replace wrong -> right, requiring a word boundary on any letter/digit
    edge of `wrong` (so "VIP" won't match inside "AVIP", but "VIP)" can
    still match against the parenthesis)."""
    lead = r"\b" if wrong[:1].isalnum() else ""
    trail = r"\b" if wrong[-1:].isalnum() else ""
    return re.sub(lead + re.escape(wrong) + trail, right, name_part)


_name_case_fixes = None


def get_name_case_fixes():
    """Build (once) the artist-name-casing dict from NAME_ALLCAPS_FIXES: maps
    the title-cased mangled form (e.g. "Dj") back to the intended form
    (e.g. "DJ")."""
    global _name_case_fixes
    if _name_case_fixes is None:
        _name_case_fixes = {name.strip().capitalize(): name.strip() for name in NAME_ALLCAPS_FIXES.split(",")}
    return _name_case_fixes


def apply_name_fixes(name_part):
    for wrong, right in get_name_case_fixes().items():
        name_part = re.sub(rf"\b{re.escape(wrong)}\b", right, name_part)
    return name_part


def apply_text_fixes(name_part):
    for wrong, right in parse_fix_pairs(TEXT_FIXES):
        name_part = replace_with_word_boundaries(name_part, wrong, right)
    return name_part


def convert_named_vip_to_remix(name_part):
    """"(Someone VIP)" -> "(Someone Remix)" -- a VIP credited to a specific
    remixer is really a remix. A bare "(VIP)" with nothing preceding it
    inside the parens (e.g. "Kaycin - Kano (VIP)") is a true VIP and is
    left untouched, since there's no remixer name to attribute it to."""
    return re.sub(
        r"\(([^()]+?)\s+VIP\)",
        lambda m: f"({m.group(1)} Remix)",
        name_part,
        flags=re.IGNORECASE,
    )


def wrap_bare_vip(name_part):
    """"Block VIP" -> "Block (VIP)". Only wraps a VIP that isn't already
    inside parens -- run after convert_named_vip_to_remix, so any "(VIP)"
    or "(Someone VIP)" that survives to this point is left alone rather
    than double-wrapped into "((VIP))"."""
    return re.sub(r"(?<!\()\bVIP\b(?!\))", "(VIP)", name_part, flags=re.IGNORECASE)


def lowercase_small_words(name_part):
    """Lowercase LOWERCASE_WORDS, except as first/last word of a dash segment."""
    segments = name_part.split(" - ")
    for i, segment in enumerate(segments):
        words = segment.split(" ")
        for j in range(1, len(words) - 1):
            if words[j] in LOWERCASE_WORDS:
                words[j] = words[j].lower()
        segments[i] = " ".join(words)
    return " - ".join(segments)


def merge_remixer_credit(name_part):
    """'Artist & Remixer - Title - Remixer Remix' -> 'Artist - Title (Remixer Remix)'."""
    segments = name_part.split(" - ")
    if len(segments) < 2:
        return name_part

    remix_match = re.match(r"^(.*)\s+Remix$", segments[-1].strip(), re.IGNORECASE)
    if not remix_match:
        return name_part

    remixer = remix_match.group(1).strip()
    lead_segments = segments[:-1]
    artists = [a.strip() for a in lead_segments[0].split(" & ")]
    lead_segments[0] = " & ".join(a for a in artists if a.lower() != remixer.lower())
    return " - ".join(lead_segments) + f" ({remixer} Remix)"


def clean_name_part(name_part):
    """Regex cleanup of name_part: track numbers, empty parens, trailing
    dash, and dash spacing left uneven by tag removal (glued dashes like
    "Laz-R" are untouched since they have no space on either side)."""
    name_part = re.sub(r"^\d{2}\s*-?\s*", "", name_part)
    name_part = re.sub(r"\s*\(\s*\d+\s*\)\s*$", "", name_part)
    name_part = re.sub(r"\s+\d+\s*$", "", name_part)
    name_part = re.sub(r"\(\s*\)", "", name_part)
    name_part = re.sub(r"\[\s*\]", "", name_part)
    name_part = re.sub(r"\s*-\s*$", "", name_part)
    name_part = re.sub(r"(\S)-(\s)", r"\1 -\2", name_part)
    name_part = re.sub(r"(\s)-(\S)", r"\1- \2", name_part)
    name_part = re.sub(r"(\w)\(", r"\1 (", name_part)
    return re.sub(r"\s+", " ", name_part)


def clean_song_filename(file_name):
    """Parse artist/title out of file_name and clean it into "Artist - Title
    (Remix info)" style."""
    if "-" not in file_name:
        print("------------------------- Warning -------------------------")
        print("------------------ Couldn't find Artist -------------------")
        print("-------------------- in the song below --------------------")

    file_name = file_name.replace("_", " ")
    file_name = file_name.replace(" [www.slider.kz]", "")
    file_name = file_name.replace("[", "(").replace("]", ")")

    new_file_name = title_case_preserving_dots(file_name)

    name_part, dot, ext_part = new_file_name.rpartition(".")
    if dot == "":
        name_part, ext_part = new_file_name, ""

    name_part, kept_tags = strip_junk_tags(name_part)
    name_part = clean_name_part(name_part)
    name_part = apply_text_fixes(name_part)
    name_part = convert_named_vip_to_remix(name_part)
    name_part = wrap_bare_vip(name_part)
    name_part = name_part.replace(", ", " & ")  # "Artist1, Artist2" -> "Artist1 & Artist2"
    name_part = apply_name_fixes(name_part)
    name_part = lowercase_small_words(name_part)
    name_part = name_part.replace(" .", ".")
    name_part = merge_remixer_credit(name_part)
    name_part = re.sub(r"\s+", " ", name_part).strip()

    final_segments = [s for s in name_part.split(" - ") if s.strip() != ""]
    dash_issue = len(final_segments) != 2
    if dash_issue:
        print("------------------------- Warning -------------------------")
        print(f"---------- Ambiguous segments ({len(final_segments)}) in title ----------")
        print("--------------------", name_part, "--------------------")

    return name_part + ("." + ext_part if ext_part else ""), dash_issue, kept_tags


def transcode_to_wav(folder_path, file_name):
    """Convert to .wav in place, preserving metadata. Returns new filename, or None on failure."""
    original_path = os.path.join(folder_path, file_name)
    base_name, _, _ = file_name.rpartition(".")
    output_filename = base_name + ".wav"
    output_path = os.path.join(folder_path, output_filename)

    temp_path = os.path.join(folder_path, file_name + "_to_be_converted")
    os.rename(original_path, temp_path)

    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", temp_path, "-map_metadata", "0", output_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW, check=True,
        )
    except subprocess.CalledProcessError:
        if os.path.exists(temp_path):
            os.rename(temp_path, original_path)
        return None

    if os.path.exists(temp_path):
        os.remove(temp_path)
    return output_filename


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
                        sub_format = struct.unpack("<H", fmt_data[24:26])[0]
                        return sub_format == 0x0003
                    return audio_format == 0x0003  # WAVE_FORMAT_IEEE_FLOAT
                f.seek(chunk_size + (chunk_size % 2), 1)
    except Exception:
        return False


def convert_if_needed(folder_path, file_name, ffmpeg_ok, stats):
    """Convert to WAV if lossless or float-PCM. Updates stats in place."""
    lower_name = file_name.lower()

    needs_conversion = False
    if lower_name.endswith(LOSSLESS_EXTENSIONS):
        needs_conversion = True
    elif lower_name.endswith(".wav") and is_float_wav(os.path.join(folder_path, file_name)):
        needs_conversion = True

    if not needs_conversion:
        return

    if not ffmpeg_ok:
        print("---- Skipping conversion (ffmpeg not found on PATH):", file_name)
        stats["ignored"].append(file_name)
        return

    converted_name = transcode_to_wav(folder_path, file_name)
    if converted_name:
        print("Converted to WAV:", file_name, "->", converted_name)
        stats["converted"] += 1
    else:
        print("---- ffmpeg conversion failed for:", file_name)
        stats["ignored"].append(file_name)


def process_file(folder_path, file_name, ffmpeg_ok, stats):
    try:
        new_file_name, dash_issue, kept_tags = clean_song_filename(file_name)
    except Exception as e:
        print("---- Ignoring (error while processing):", file_name, "--", e)
        stats["ignored"].append(file_name)
        return

    if dash_issue:
        stats["dash_warnings"].append(new_file_name)

    if kept_tags:
        print(f"     (kept possibly-junk tag(s), followed by more text: {', '.join(kept_tags)})")

    if new_file_name != file_name:
        os.rename(os.path.join(folder_path, file_name), os.path.join(folder_path, new_file_name))
        print("Renamed:")
        print("Old:", file_name)
        print("New:", new_file_name)
        stats["renamed"] += 1
        stats["rename_log"].append((file_name, new_file_name))
    else:
        print("----No Rename Needed:", file_name)
        stats["no_rename_needed"] += 1

    if new_file_name.lower().endswith(LOSSY_EXTENSIONS):
        stats["reencode_reminders"].append(new_file_name)
    else:
        convert_if_needed(folder_path, new_file_name, ffmpeg_ok, stats)


def revert_renames(folder_path, rename_log):
    """Undo filename renames in reverse order (LIFO), so any chained renames
    unwind correctly. Does NOT undo WAV conversions -- only renames. Skips
    (with a warning) any step where the expected file isn't there anymore
    or where reverting would overwrite an existing file, instead of failing
    the whole revert."""
    reverted, skipped = 0, 0
    for old_name, new_name in reversed(rename_log):
        current_path = os.path.join(folder_path, new_name)
        original_path = os.path.join(folder_path, old_name)

        if not os.path.isfile(current_path):
            print(f"---- Skipping revert (file not found): {new_name}")
            skipped += 1
            continue
        if os.path.exists(original_path):
            print(f"---- Skipping revert (a file already exists at the original name): {old_name}")
            skipped += 1
            continue

        os.rename(current_path, original_path)
        print(f"Reverted: {new_name} -> {old_name}")
        reverted += 1

    print(f"\nRevert complete. Reverted: {reverted}, Skipped: {skipped}")


def print_summary(stats, total_found):
    print("\n\n")
    print("Found:", total_found)
    print("Renamed:", stats["renamed"])
    print("No Rename Needed:", stats["no_rename_needed"])
    print("Converted to WAV:", stats["converted"])
    print("__________________")
    misc_extensions = f" ({', '.join(sorted(stats['ignored_misc_extensions']))})" if stats["ignored_misc_extensions"] else ""
    print("\nMisc. Ignored:", stats["ignored_misc"], misc_extensions)

    all_ignored_songs = stats["ignored"] + stats["reencode_reminders"]
    print("Ignored Songs:", len(all_ignored_songs))
    for song in all_ignored_songs:
        print(song)

    if stats["reencode_reminders"]:
        print("\nSome songs need to be re-encoded!")
    if stats["dash_warnings"]:
        print("\nMissing or Multiple dashes:")
    for song in stats["dash_warnings"]:
        print(song)


def main():
    folder_path = common.work_folder(__file__)

    ffmpeg_ok = common.check_ffmpeg_available(
        "convert lossless files (flac/aiff/alac/ape/wv/dsf/dff) and float-PCM wavs"
    )

    stats = {
        "renamed": 0, "no_rename_needed": 0, "converted": 0, "ignored_misc": 0,
        "ignored_misc_extensions": set(), "ignored": [], "reencode_reminders": [],
        "dash_warnings": [], "rename_log": [],
    }

    for file_name in os.listdir(folder_path):
        path_to_file = os.path.join(folder_path, file_name)
        if not os.path.isfile(path_to_file):
            continue

        if not file_name.lower().endswith(AUDIO_EXTENSIONS):
            stats["ignored_misc"] += 1
            stats["ignored_misc_extensions"].add(os.path.splitext(file_name)[1].lower() or "(no ext)")
            continue

        process_file(folder_path, file_name, ffmpeg_ok, stats)

    total_found = (
        stats["renamed"] + stats["no_rename_needed"] + stats["converted"]
        + stats["ignored_misc"] + len(stats["ignored"]) + len(stats["reencode_reminders"])
    )
    print_summary(stats, total_found)

    print("\n\n\nPress ENTER to exit, or type R and press ENTER to undo all the renames done just now")
    choice = input().strip().lower()
    if choice == "r":
        revert_renames(folder_path, stats["rename_log"])
        input("\n\nPress ENTER to exit")


if __name__ == "__main__":
    common.run_main(main, ffmpeg_hint=True)