# Vinyl Recorder

Record a record from your audio interface, look it up on Discogs, split each side into
tracks and save tagged FLAC files with cover art.

Open **Vinyl Recorder** from the Start menu or the desktop shortcut. Right-click it →
*Pin to taskbar* if you like.

On a new PC, double-click `launch.bat` once: it installs everything and creates the shortcuts.
To recreate or remove them later:

```
& "$env:USERPROFILE\Downloads\vinyl-recorder\.venv\Scripts\python.exe" "$env:USERPROFILE\Downloads\vinyl-recorder\tools\install_shortcuts.py"
& "$env:USERPROFILE\Downloads\vinyl-recorder\.venv\Scripts\python.exe" "$env:USERPROFILE\Downloads\vinyl-recorder\tools\install_shortcuts.py" --remove
```

(PowerShell, from any folder. Add `--no-desktop` to the first one to skip the desktop shortcut.)

The icon is drawn by `tools\make_icon.py`. Edit it and re-run to change the icon.

## Workflow

1. **Album**: **My collection** lists your Discogs collection (it loads once, then
   *Refresh from Discogs* when you've added records). Arrow down the list to preview
   each record, or press **Next not saved ↓**. Colour shows progress: green = every side
   saved, orange = some sides recorded or saved. *Hide saved* shows only what's left.
   After saving an album, **Next album →** takes you straight to the next one.
   **Search Discogs** is still there for records that aren't in your collection. Pasting
   a Discogs release URL works even without a token.
2. **Record**: pick the device (use *Focusrite USB ASIO* to reach inputs 3 & 4), set the
   left/right inputs and sample rate, set your mixer so peaks sit around -6 dB, then
   record each side. Files are named `Artist - Album (Year) - Side A.wav`, and side
   letters follow the album's sides. The clip counter counts every clipped moment per
   channel. It resets when a take starts, and the clips are marked red on the waveform.
   **Whole LP** (the default) records the entire record in one take: play side A, flip,
   play side B without stopping. The flip pause is found automatically, marked as
   "not a track" and left out of the saved files.
3. **Split & save**: the gaps between tracks are found using the Discogs track lengths.
   Each gap becomes its own grey "gap" part that isn't saved. A track ends on its last
   sound (plus 0.15 s of decay) and the next starts on its first beat, measured from
   the samples. Crackle in the gap is ignored. **Space** plays from the pink cursor and
   stops again; double-clicking a track plays it from its exact start.
   Scroll wheel zooms in (down to a quarter of a second across the screen, showing the
   real samples). Drag empty space to move along, and *Zoom to selected track's start*
   jumps straight to a split. Drag a line to move a split, double-click or
   *Split at cursor* to add one. Right-click for more: remove a split, mark a part as
   not a track, set the music start/end, play from there. Choose **FLAC**, **MP3 320 kbps** or **both**, then
   save. Each track gets full tags, the cover, its length (`TLEN`/`LENGTH`, in ms) and
   its detected **BPM** (`TBPM`/`BPM`). With both formats, MP3s go in `Album (Year) [MP3]`.

### Why record WAV and save MP3?

The take is always captured lossless, because splitting an MP3 means re-encoding it and
leaves small gaps at track boundaries. MP3s are made once, from the clean WAV, at
save time. 96 kHz recordings are converted to 48 kHz for MP3, since MP3 tops out at 48 kHz.

### BPM

BPM is detected from the middle 90 s of each track. As in Rekordbox, results fall in
a range you choose in Settings (default 78–155, which suits disco, funk and house). Use
88–175 for faster music, otherwise 174 BPM drum & bass shows up as 87.

## Discogs token

Searching needs a free personal access token: discogs.com → Settings → Developers →
*Generate new token*. Paste it in **File → Settings** (or set the `DISCOGS_TOKEN`
environment variable). It's stored in `%APPDATA%\VinylRecorder\settings.json`.

## Notes

- No RIAA EQ is applied. A DJ mixer's line output is already equalised.
- Raw recordings stay in `<output folder>\_recordings` as 24-bit WAV until you delete them.
- ASIO only lets one app use the interface at a time, so close VinylStudio, DAWs, etc.
  first. Focusrite Control can stay open.

## Sharing the app (standalone build)

`packaging\build.bat` builds `dist\Vinyl Recorder\Vinyl Recorder.exe` (no Python needed) and zips
it, with `Read me first.txt`, as `dist\Vinyl Recorder.zip`. Rebuild after changing the code.
Settings, the Discogs token, rip history and recordings live in each person's own
`%APPDATA%\VinylRecorder` and `Music` folders, so none of yours go into the zip.
To check a build on another PC: `"Vinyl Recorder.exe" --self-test report.txt`.

## Development

```
.venv\Scripts\python.exe -m pytest tests
.venv\Scripts\python.exe tests\smoke_ui.py %TEMP%\vr-smoke
```
