# mid2seqp.py

Convert a Standard MIDI File to a PlayStation Psy-Q/libsnd `.SEQ` file. The script is standalone and uses only the Python standard library.

## Requirements

- Python 3
- An input Standard MIDI File (`.mid`)
- For a matching sound bank, `sf2vab.py` and a compatible SoundFont or DLS file

The reader accepts SMF formats 0, 1, and 2, including RIFF/RMID-wrapped MIDI. SMPTE time division is unsupported. Multi-port MIDI is merged into the same 16 channels.

## Basic use

```text
python mid2seqp.py song.mid
```

This writes `song.seq`. By default, the converter assigns SEQ program numbers to the melodic instruments and drum-note groups used by the song, then appends a `VMAP` instrument table after the SEQ end-of-track event. The table lets `sf2vab.py --seq` build the corresponding VAB without a separate mapping file.

Specify an output path explicitly:

```text
python mid2seqp.py song.mid output.seq
```

## Choose a program layout

The SEQ and VAB must use the same mapping mode.

### Song-specific mapping (default)

```text
python mid2seqp.py song.mid song.seq
python sf2vab.py soundfont.sf2 --seq song.seq
```

Only instruments and notes used by the MIDI are assigned programs. Melodic programs are numbered first; drum notes are grouped into ranges of up to 16 notes. If more than 128 programs/groups are needed, the last ones are dropped with a warning.

### SoundFont voice map

Create a VAB and voice map from all SoundFont banks, then convert the MIDI using that map:

```text
python sf2vab.py soundfont.sf2 --all-banks -o bank.vab
python mid2seqp.py --voice-map bank.vmap song.mid song.seq
```

The `--voice-map` (`-m`) input is the `.vmap` written by `sf2vab.py --all-banks`.

### Fixed GM layout

```text
python sf2vab.py soundfont.sf2 --gm -o gm.vab
python mid2seqp.py --gm song.mid song.seq
```

This maps GM melodic programs to 0–123 and drum-note groups to 124–127. MIDI melodic programs 124–127 fall back to 120–123 in this layout. Drum notes outside 24–87 are dropped.

### Pass program numbers through

```text
python mid2seqp.py --plain song.mid song.seq
```

`--plain` keeps MIDI program numbers unchanged, like Psy-Q SMF2SEQ. It does not resolve bank selections. When pairing this output with `sf2vab.py --seq`, SEQs without the appended instrument table are interpreted as GM, with MIDI channel 10 treated as drums.

## Options

```text
python mid2seqp.py [options] input.mid [output.seq]
```

- `-m, --voice-map VMAP`: map MIDI banks/programs using an `sf2vab.py --all-banks` voice map.
- `--gm`: use the fixed GM layout.
- `--plain`: pass program numbers through unchanged.
- `--no-table`: do not append the `VMAP` instrument table. Use only when the receiving workflow expects GM program interpretation.
- `--keep-nrpn`: preserve NRPN controllers for MIDI authored for libsnd. By default NRPNs are removed because libsnd treats them as VAB attribute changes.
- `--loop`: loop forever between recognized loop markers, or over the whole song if no usable end marker is found. Recognized marker text includes `loopEnd` or `]`; start markers include `loopStart`, `loop`, or `[`. CC111 is also recognized as a start marker.
- `--resolution PPQ`: rescale MIDI ticks to the requested ticks-per-quarter-note value. Default is the MIDI file's resolution.

Run `python mid2seqp.py --help` for the current argparse help.

## Conversion behavior and limitations

- Bank-select MSB/LSB events are removed from the output; bank selection is resolved into SEQ program numbers where the chosen mapping supports it.
- Supported output includes notes, program changes, pitch bend, tempo changes, and libsnd-supported controllers. Unsupported controllers, aftertouch, and SysEx are dropped. All Notes Off and All Sound Off are converted to note-offs.
- NRPNs and associated data entry are dropped unless `--keep-nrpn` is specified.
- Drum parts are detected on MIDI channel 10 or from bank MSB 120 (GM2) / 127 (XG). GS SysEx drum-part assignments are not handled.
- MIDI files without a tempo event use 120 BPM. The first tempo and time signature at tick zero are written to the header; later tempo events are retained.
- `--loop` writes libsnd NRPN loop markers and repeats indefinitely.
- libsnd playback tick rate is determined by the runtime. Very close events can quantize together regardless of SEQ resolution.

## Output format

The output is a Psy-Q/libsnd SEQ beginning with `pQES`, version 1. Its instrument table, when enabled, is appended after the end-of-track event as `VMAP`, a little-endian 32-bit byte length, and text mapping rows. libsnd stops reading the event stream at end-of-track, so the appended table is for companion tools such as `sf2vab.py`.
