"""
Batch-cleans song filenames in your work folder into a consistent
"Artist - Title (Remix info)" style. Per-file preprocessing (tag-based
"Title - Artist" reordering, and lossless conversion to m4a via qaac)
lives in preprocessing.py and runs before the renaming logic here.
"""

import os
import re
from concurrent.futures import ThreadPoolExecutor

PARALLEL_MIN_FILES = 10  # only encode in parallel when at least this many files need converting

import audio_common as common
import preprocessing as pre

AUDIO_EXTENSIONS = pre.AUDIO_EXTENSIONS

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
    r"Final\s*Master", r"Mastered", r"Master",
]

STRICT_JUNK_TAGS = [
    r"!!!+", r"™", r"®", r"©", r"Free\s*Dl", r"Free\s*Download", r"Snippet", r"Preview",
    r"Unreleased", r"WIP", r"HQ", r"HD", r"Freebie", r"\d+k", r"Bday", r"Birthday", r"\d+\s*bpm",
    r"\b\d+\s*-?\s*bits?",  # bit-depth tags: "16bit", "24 Bit", "32-bit"
]

JUNK_TAGS = CAREFUL_JUNK_TAGS + STRICT_JUNK_TAGS

"""Word swaps: Need non-alphanumerical symbols around the word/phrase in order to match"""
TEXT_FIXES = ("Vip Vip -> VIP2, Vip -> VIP, Bootleg -> Remix, X -> &, Vs -> &, "
              "Akira -> Akirah, Subz -> Subfiltronik, Tw!sty -> Twisty, Ember. -> Ember, "
              "Yunit. -> Yunit")

"""Word swaps but only to prevent title-casing to mess up special names"""
NAME_ALLCAPS_FIXES = (
    "NB, LV, AD, NT, IITYX, TEOTI, IP, x87, DGE, DRS, LSB, MPH, JG, ID, DJ, AJ, JJD, FD, R28D, CMF"
)



# Non-User-intended lists
LOWERCASE_WORDS = ["Is", "To", "It", "Of", "By", "A", "An", "Out", "The", "From", "My"]

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


def convert_named_flip_to_remix(name_part):
    """"(Someone Flip)" -> "(Someone Remix)". Only inside parens and only with a
    name before it, so a title word like "Flip The Switch" is never touched."""
    return re.sub(r"\(([^()]+?)\s+Flip\)", lambda m: f"({m.group(1)} Remix)", name_part, flags=re.IGNORECASE)


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
    name_part = convert_named_flip_to_remix(name_part)
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


def process_file(folder_path, file_name, tools, stats, prepped=None):
    file_name, prep = prepped or pre.preprocess_file(folder_path, file_name, tools)
    stats["rename_log"] += prep["renames"]
    stats["reordered"] += int(prep["reordered"])
    stats["converted"] += int(prep["converted"])

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
        src_p = os.path.join(folder_path, file_name)
        dst_p = os.path.join(folder_path, new_file_name)
        if os.path.exists(dst_p) and not os.path.samefile(src_p, dst_p):
            print("---- Skipping rename (a file with the cleaned name already exists):", file_name, "->", new_file_name)
            stats["ignored"].append(file_name)
            return
        os.rename(os.path.join(folder_path, file_name), os.path.join(folder_path, new_file_name))
        print("Renamed:")
        print("Old:", file_name)
        print("New:", new_file_name)
        stats["renamed"] += 1
        stats["rename_log"].append((file_name, new_file_name))
    else:
        print("----No Rename Needed:", file_name)
        stats["no_rename_needed"] += 1

    if prep["unsupported"]:
        stats["unsupported"].append(new_file_name)
    elif prep["failed"]:
        stats["ignored"].append(new_file_name)


def revert_renames(folder_path, rename_log):
    """Undo filename renames in reverse order (LIFO), so any chained renames
    unwind correctly. Does NOT undo m4a conversions -- only renames. Skips
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
    print("Reordered to Artist - Title (from tags):", stats["reordered"])
    print("Converted to M4A:", stats["converted"])
    print("__________________")
    misc_extensions = f" ({', '.join(sorted(stats['ignored_misc_extensions']))})" if stats["ignored_misc_extensions"] else ""
    print("\nMisc. Ignored:", stats["ignored_misc"], misc_extensions)

    print("Ignored Songs:", len(stats["ignored"]))
    for song in stats["ignored"]:
        print(song)

    if stats["unsupported"]:
        print("\nNot playable on Pioneer hardware, convert these manually:")
        for song in stats["unsupported"]:
            print(song)
    if stats["dash_warnings"]:
        print("\nMissing or Multiple dashes:")
    for song in stats["dash_warnings"]:
        print(song)


def main():
    folder_path = common.work_folder(__file__)

    tools = pre.check_dependencies()

    stats = {
        "renamed": 0, "no_rename_needed": 0, "converted": 0, "ignored_misc": 0,
        "ignored_misc_extensions": set(), "ignored": [], "unsupported": [],
        "dash_warnings": [], "rename_log": [], "reordered": 0,
    }

    audio_files = []
    for file_name in os.listdir(folder_path):
        path_to_file = os.path.join(folder_path, file_name)
        if not os.path.isfile(path_to_file):
            continue

        if not file_name.lower().endswith(AUDIO_EXTENSIONS):
            stats["ignored_misc"] += 1
            stats["ignored_misc_extensions"].add(os.path.splitext(file_name)[1].lower() or "(no ext)")
            continue

        audio_files.append(file_name)

    # Phase 1: tag reordering + m4a encoding, in parallel (subprocess-bound, so threads are enough).
    # Phase 2: renaming/stats, sequential, so output and collision handling stay deterministic.
    def prep_one(name):
        try:
            return pre.preprocess_file(folder_path, name, tools)
        except Exception as e:
            print("---- Error while preprocessing:", name, "--", e)
            return name, {"renames": [], "reordered": False, "converted": False, "failed": True, "unsupported": False}

    to_convert = sum(1 for f in audio_files if f.lower().endswith(pre.LOSSLESS_EXTENSIONS))
    if to_convert >= PARALLEL_MIN_FILES:
        print(f"Encoding {to_convert} file(s) with {pre.ENCODE_WORKERS} parallel workers...")
        with ThreadPoolExecutor(max_workers=pre.ENCODE_WORKERS) as pool:
            prepped_all = list(pool.map(prep_one, audio_files))
    else:
        prepped_all = [prep_one(f) for f in audio_files]  # too few to be worth the parallel overhead

    for original_name, prepped in zip(audio_files, prepped_all):
        process_file(folder_path, original_name, tools, stats, prepped)

    total_found = (
        stats["renamed"] + stats["no_rename_needed"]
        + stats["ignored_misc"] + len(stats["ignored"])
    )
    print_summary(stats, total_found)

    print("\n\n\nPress ENTER to exit, or type R and press ENTER to undo all the renames done just now")
    choice = input().strip().lower()
    if choice == "r":
        revert_renames(folder_path, stats["rename_log"])
        input("\n\nPress ENTER to exit")


if __name__ == "__main__":
    common.run_main(main, ffmpeg_hint=True)