# Spindle — smart CD ripper

Spindle is a modern HandBrake for CDs. It's built for the shoebox of home-burnt discs: it reads the disc, works out what each track actually is, evens out the volume, names and tags everything, and adds album art.

## What it does

- **Reads both kinds of burnt CD.** It handles audio CDs (rips the tracks) and data CDs full of MP3/WMA/etc. (reads the files). You can also point it at any folder of messy music.
- **Copes with scratched discs.** Bad reads are retried, then retried sector by sector. If a few sectors still won't come off, Spindle fills them with silence and tells you which track was hit and how many seconds were lost.
- **Identifies tracks four ways:**
  1. Disc ID lookup, for copies of retail CDs.
  2. By sound with AcoustID. This works on a disc named "Track01, Track02…" with no names at all.
  3. MusicBrainz, which double-checks and corrects names from tags or filenames.
  4. Filename parsing (`03 - artist - title (www.junk.ru).mp3`).
- **Cleans up names.** It strips website junk, bitrate tags and "(Official Video)". It fixes SHOUTING or all-lowercase titles but leaves deliberate styling alone (iPhone, McCartney, AC/DC). It also fixes garbled accents (`BeyoncÃ©` → `Beyoncé`) and standardizes "ft / featuring" to "feat."
- **Spots mixes.** When a disc has lots of different artists, Spindle tags it as a compilation ("Various Artists"). It names the mix from the disc or folder label and builds a 2×2 cover mosaic from the songs' own album covers.
- **Spots duplicates.** A song burnt twice is unticked automatically.
- **Levels the volume** using EBU R128 loudness measurement:
  - Smart (the default): albums get album gain, so quiet songs stay quiet. Mixes get track gain, so a 1999 MP3 doesn't sound half as loud as a 2009 one.
  - Peaks are limited so nothing clips.
  - MP3s from data discs aren't re-encoded, because that only loses quality. They get ReplayGain tags instead.
- **Writes FLAC, MP3, AAC or Opus.** Each track gets full tags and embedded art, plus a `cover.jpg` and an `.m3u8` playlist in each folder.
- **Shows a review screen before anything is written.** Every track has a confidence dot. You can click any name to fix it, preview tracks, and swap the art.
- **Auto mode.** Insert a disc and walk away. If Spindle is confident about every track, it finishes and ejects the disc on its own. If not, it waits for you.

## Windows

1. Unzip anywhere.
2. Double-click **Spindle.bat**. On the first run it installs Python (if needed), ffmpeg and fpcalc. If ffmpeg was just installed, close the window and run Spindle again.
3. Your browser opens at http://127.0.0.1:8484.

## Raspberry Pi

```bash
cd Spindle
bash setup-pi.sh       # one time
./spindle.sh           # or use the Desktop / menu icon
```

USB CD drives work fine. On Raspberry Pi OS desktop, data discs mount by themselves. Run `./spindle.sh --lan` to use the dashboard from another computer at `http://<pi-address>:8484`.

## Recognising songs by sound

Get a free key at https://acoustid.org/new-application and paste it into **Settings → AcoustID key**. Without a key, Spindle still uses tags, filenames, disc IDs and MusicBrainz.

## Folder layout

The defaults (you can change them in Settings):

- Albums: `{albumartist}/{album} ({year})/{disc}{track:02} - {title}`
- Mixes:  `Mixes/{album} ({year})/{track:02} - {artist} - {title}`

Finished music goes to `Music/Spindle` in your home folder. Settings, ripped audio and art are kept in `~/.spindle`. Ripped audio is cached there, so re-finishing a disc doesn't need the disc again.
