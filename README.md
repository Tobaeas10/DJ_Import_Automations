# Riddim Automation

Three small tools that take the manual, repetitive parts out of prepping a
folder of tracks: cleaning up filenames, flagging low-quality/faked files,
and writing artist/title tags from the filename.

## Installation

1. Install Python **3.10 or newer** from https://www.python.org/downloads/
   (tested on 3.14.6 — anything 3.10+ should work). On Windows, tick
   "Add python.exe to PATH" during install.
2. Download the entire repository from GitHub. Extract it inside the folder you want to
   work in (e.g. a folder on your Desktop or in Downloads — anywhere is
   fine, songs go in that same parent folder).
4. Run `SETUP.py` once (double-click it, or run
   `python SETUP.py` from a terminal inside `_riddim_automation`).
   It will:
   - check your Python version,
   - install `ffmpeg` if it's missing (asks before doing anything),
   - install the required Python packages (`mutagen`, `librosa`, `numpy`,
     `scipy`) via pip (asks before doing anything),
   - check that `qaac64` is available (needed for the m4a conversion; put
     it on PATH or set `QAAC_EXE` at the top of `app/audio_common.py`),
   - create three launchers directly in your work folder — "___Step
     1___ Check Audio Quality", "___Step 2___ Clean File Names and Convert
     to M4A", "___Step 3___ Convert File Names to Tag".
5. From then on, drop songs into your work folder and double-click the
   launchers in order (Step 1, 2, 3) as needed.

Your folder should end up looking like this:

```
your work folder/
├── DJ_Import_Automations-master/
│   ├── SETUP.py                  <- the only thing you ever run directly
│   └── app/
│       ├── audio_common.py
│       ├── preprocessing.py
│       ├── clean_file_names.py
│       ├── check_quality.py
│       └── filename_to_tag.py
├── ___Step 1___ Check Audio Quality.bat/.command/.sh              <- created by SETUP.py
├── ___Step 2___ Clean File Names and Convert to M4A.bat/.command/.sh  <- created by SETUP.py
├── ___Step 3___ Convert File Names to Tag.bat/.command/.sh        <- created by SETUP.py
│
│   songs to be analyzed go here
├── example artist - cool song.mp3
└── artist2 - cooler song.wav

```

Only files directly inside your work folder are processed — sub-folders
are ignored by design, so you can use them to archive already-processed
tracks.

## Features

### Step 1 — Check Audio Quality
Flags files that are probably low quality (no real content above ~16kHz,
suggesting a lossy source dressed up as lossless) or probably faked (high
frequencies present but not tracking the music, suggesting pasted-on
noise to fake a higher bitrate). Only decodes a middle segment of each
file for speed, and runs across all CPU cores for large batches. This is
a report only — nothing gets renamed or deleted. It runs first because
the check only means something on the original lossless files, before
they are encoded to AAC.

### Step 2 — Clean File Names and Convert to M4A
Renames files into a consistent `Artist - Title (Remix info)` style:
fixes capitalization, strips junk tags like `(Original Mix)` or `(Radio
Edit)`, merges remixer credits into a trailing `(X Remix)`, and normalizes
things like `VIP`/`Bootleg`. Every lossless file (wav, flac, aiff, alac,
ape, wv, dsf, dff) is encoded to `.m4a` with qaac (AAC TVBR, quality set
by `QAAC_TVBR` in `app/preprocessing.py`) and the original is deleted once
the encode succeeded. mp3, m4a and aac files are kept as they are. Formats
Pioneer players can't play (ogg, opus, wma, mid) are renamed but not
converted, and are listed at the end so you can handle them manually.

The list of artist names excluded from title-casing (`NAME_ALLCAPS_FIXES`)
and the manual name corrections (`NAME_TEXT_FIXES`) at the top of
`app/clean_file_names.py` are **just the ones we happened to need** — this
list is almost certainly incomplete for your library. Feel free to edit it
directly, or open a pull request if you're working from a shared repo, so
the fix benefits everyone else too.

### Step 3 — Convert File Names to Tag
Wipes all tags except embedded cover art (mp3, wav and m4a), then re-derives Artist and
Title tags from the filename (splitting on the first ` - `). Handy after
running "Clean File Names" so your tags match your filenames exactly.

Run the tools in that order (1 → 2 → 3) for the best results. Quality
checking has to come before the m4a conversion, and tagging assumes
filenames are already clean.