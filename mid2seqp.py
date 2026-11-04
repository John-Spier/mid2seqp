#!/usr/bin/env python3
"""
mid2seq.py — Convert a Standard MIDI File to a PlayStation libsnd .SEQ.

Standalone (no imports from the other tools).  Program numbers are mapped to
VAB programs in one of four ways:

  song (default)  Every instrument the song actually plays gets its own SEQ
                  program, numbered from 0: melodic (bank, program) pairs
                  first, then drum kits, each kit split into groups of at most
                  16 used notes.  The numbering is stored in an instrument
                  table appended after the SEQ's end-of-track, so
                  `sf2vab.py font.sf2 --seq song.seq` can build the matching
                  VAB without a separate voice map.
  -m X.vmap       Use the voice map written by `sf2vab.py --all-banks`
                  (<sf2_bank> <program> <vab_program> [keys=lo-hi] <name>).
  --gm            Target `sf2vab.py --gm`: programs 0-123 = GM programs (GM
                  124-127 fall back to 120-123), drum note n -> program
                  124 + (n - 24) // 16 (notes 24-87).
  --plain         Pass program numbers through unchanged (like Psy-Q SMF2SEQ).

Usage:
  python mid2seq.py song.mid song.seq                      # + sf2vab --seq
  python mid2seq.py -m BGM.vmap song.mid song.seq          # sf2vab --all-banks
  python mid2seq.py --gm song.mid song.seq                 # sf2vab --gm
  python mid2seq.py --loop song.mid song.seq               # loop markers -> libsnd loop

SEQ format (Psy-Q "File Formats" / "Run-Time Library Overview", libsnd):
  'pQES', version 1 (u32 BE), quarter-note resolution (u16 BE), tempo in
  microseconds per quarter (24-bit BE), rhythm (numerator, log2 denominator),
  then one merged MIDI event stream with running status, note-offs written as
  note-on velocity 0, tempo changes as FF 51 tt tt tt, ending in FF 2F 00.

libsnd understands note on/off, program change, pitch bend and controllers 0
(VAB switch), 6, 7, 10, 11, 64, 91, 98/99 (NRPN: VAB attributes, loops, marks),
100/101 (RPN) and 121.  So:
  - CC0/CC32 bank select is removed (CC0 would switch VABs in libsnd); banks
    are resolved into program numbers instead.
  - NRPNs (and their data entry) are removed: libsnd would read them as VAB
    attribute changes.  --keep-nrpn keeps them (files authored for libsnd).
  - Unsupported controllers, aftertouch and SysEx are dropped.  All Notes Off /
    All Sound Off (CC123/CC120) become note-offs for the sounding notes.
  - When a channel changes SEQ program while notes are held (drum groups,
    program changes), a program change to the note's program is written before
    its note-off, so libsnd releases the right voice.
  - --loop: a loopStart/loopEnd marker (or CC111 loop start, RPG Maker style)
    becomes libsnd's NRPN loop (CC99=20 / CC99=30) that repeats forever.

Drum parts: MIDI channel 10, or bank MSB 120 (GM2) / 127 (XG) on any channel.
GS SysEx drum-part assignments are not handled.
"""

import argparse
import os
import struct
import sys
from typing import Dict, List, Optional, Tuple

DRUM_CHANNEL = 9
DRUM_BANK = 128
MAX_PROGRAMS = 128
MAX_TONES = 16          # VAB tones per program = drum notes per group
GM_DRUM_PROG = 124      # must match sf2vab.py --gm
GM_DRUM_FIRST_NOTE = 24
TABLE_MAGIC = b'VMAP'   # instrument table after the end-of-track

# Controllers libsnd uses (Run-Time Library Overview, libsnd "Score Data")
CC_DATA_MSB, CC_DATA_LSB = 6, 38
CC_NRPN_LSB, CC_NRPN_MSB, CC_RPN_LSB, CC_RPN_MSB = 98, 99, 100, 101
PASS_CCS = {7, 10, 11, 64, 91, 121}
CC_BANK_MSB, CC_BANK_LSB = 0, 32
CC_ALL_SOUND_OFF, CC_ALL_NOTES_OFF = 120, 123
CC_LOOP_START_RPGM = 111
NRPN_LOOP_START, NRPN_LOOP_END = 20, 30

# ── MIDI reading ────────────────────────────────────────────────────

# Event: (tick, track, index, kind, args); kinds:
#   on (ch, note, vel)  off (ch, note)  cc (ch, num, val)  pc (ch, prog)
#   pb (ch, lsb, msb)   tempo (us)      timesig (nn, dd)   marker (text)   eot ()


def _vlq(d: bytes, i: int, end: int) -> Tuple[int, int]:
    v = 0
    for _ in range(4):
        if i >= end:
            raise EOFError
        b = d[i]
        i += 1
        v = (v << 7) | (b & 0x7F)
        if not b & 0x80:
            return v, i
    return v, i


def read_midi(data: bytes, warn):
    """-> (division, events).  Handles SMF 0/1/2, RIFF RMID wrappers, junk
    around chunks, running status and truncated tracks."""
    start = data.find(b'MThd')
    if start < 0:
        raise ValueError("not a MIDI file (no MThd)")
    hlen = struct.unpack_from('>I', data, start + 4)[0]
    fmt, ntracks, division = struct.unpack_from('>HHH', data, start + 8)
    if division & 0x8000:
        raise ValueError("SMPTE time division is not supported")
    pos = start + 8 + hlen
    events = []
    ports = set()
    track = 0
    offset = 0  # format 2: tracks play one after another
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        ln = struct.unpack_from('>I', data, pos + 4)[0]
        if cid != b'MTrk':
            nxt = data.find(b'MTrk', pos + 1)
            if nxt < 0:
                break
            pos = nxt
            continue
        p, end = pos + 8, min(len(data), pos + 8 + ln)
        tick, status, idx, port = offset, 0, 0, 0
        try:
            while p < end:
                dt, p = _vlq(data, p, end)
                tick += dt
                if p >= end:
                    break
                b = data[p]
                if b & 0x80:
                    p += 1
                    if b < 0xF0:
                        status = b
                elif status == 0:
                    p += 1  # data byte with no status: skip
                    continue
                else:
                    b = status
                ev = None
                if b == 0xFF:
                    mtype = data[p]
                    mlen, p = _vlq(data, p + 1, end)
                    m = data[p:p + mlen]
                    p += mlen
                    if mtype == 0x51 and len(m) == 3:
                        ev = ('tempo', (int.from_bytes(m, 'big'),))
                    elif mtype == 0x58 and len(m) >= 2:
                        ev = ('timesig', (m[0], m[1]))
                    elif mtype in (0x06, 0x01) and m:
                        ev = ('marker', (m.decode('latin1').strip(),))
                    elif mtype == 0x21 and m:
                        port = m[0]
                    elif mtype == 0x2F:
                        events.append((tick, track, idx, 'eot', ()))
                        break
                elif b in (0xF0, 0xF7):
                    slen, p = _vlq(data, p, end)
                    p += slen
                else:
                    hi, ch = b & 0xF0, b & 0x0F
                    n = 1 if hi in (0xC0, 0xD0) else 2
                    if p + n > end:
                        break
                    a = data[p:p + n]
                    p += n
                    if any(x & 0x80 for x in a):
                        continue
                    if port:
                        ports.add(port)
                    if hi == 0x90 and a[1]:
                        ev = ('on', (ch, a[0], a[1]))
                    elif hi in (0x80, 0x90):
                        ev = ('off', (ch, a[0]))
                    elif hi == 0xB0:
                        ev = ('cc', (ch, a[0], a[1]))
                    elif hi == 0xC0:
                        ev = ('pc', (ch, a[0]))
                    elif hi == 0xE0:
                        ev = ('pb', (ch, a[0], a[1]))
                if ev:
                    events.append((tick, track, idx, ev[0], ev[1]))
                    idx += 1
        except (EOFError, IndexError):
            warn(f"track {track} is truncated")
        if fmt == 2:
            offset = tick
        track += 1
        pos = end
    if ports:
        warn(f"multi-port MIDI (ports {sorted(ports | {0})}): all ports are merged into one "
             f"set of 16 channels")
    if track != ntracks:
        warn(f"header says {ntracks} tracks, found {track}")
    return division, events

# ── Program mapping ─────────────────────────────────────────────────


class Mapper:
    """Maps (channel state, note) -> SEQ program.  `drum` is the part type."""

    def __init__(self, warn):
        self.warn = warn

    def map(self, bank: int, prog: int, drum: bool, note: int) -> Optional[int]:
        raise NotImplementedError

    def table(self) -> List[Tuple[int, int, int, Optional[Tuple[int, int]], str]]:
        return []


class PlainMapper(Mapper):
    def map(self, bank, prog, drum, note):
        return prog


class GMMapper(Mapper):
    def __init__(self, warn):
        super().__init__(warn)
        self.used = {}

    def map(self, bank, prog, drum, note):
        if drum:
            if not GM_DRUM_FIRST_NOTE <= note < GM_DRUM_FIRST_NOTE + 16 * (128 - GM_DRUM_PROG):
                self.warn(f"drum notes outside {GM_DRUM_FIRST_NOTE}-"
                          f"{GM_DRUM_FIRST_NOTE + 16 * (128 - GM_DRUM_PROG) - 1} dropped")
                return None
            g = (note - GM_DRUM_FIRST_NOTE) // 16
            lo = GM_DRUM_FIRST_NOTE + 16 * g
            self.used[(DRUM_BANK, prog, lo)] = GM_DRUM_PROG + g
            return GM_DRUM_PROG + g
        if prog >= GM_DRUM_PROG:
            self.warn(f"GM programs {GM_DRUM_PROG}-127 are drums in the --gm layout; "
                      f"they play programs {GM_DRUM_PROG - 4}-123 instead")
            prog -= 4
        self.used[(0, prog, None)] = prog
        return prog

    def table(self):
        out = []
        for (bank, prog, lo), num in sorted(self.used.items(), key=lambda kv: kv[1]):
            keys = (lo, lo + 15) if lo is not None else None
            out.append((bank, prog, num, keys, 'drums' if keys else 'melodic'))
        return out


class VmapMapper(Mapper):
    """sf2vab --all-banks voice map, with mid2seq.c's fallbacks."""

    def __init__(self, path, warn):
        super().__init__(warn)
        self.mel: Dict[Tuple[int, int], int] = {}
        self.kits: Dict[int, List[Tuple[int, int, int]]] = {}
        with open(path, encoding='latin1') as f:
            for line in f:
                parts = line.split()
                if not parts or parts[0].startswith('#') or len(parts) < 3:
                    continue
                try:
                    bank, prog, num = int(parts[0]), int(parts[1]), int(parts[2])
                except ValueError:
                    continue
                if not (0 <= prog <= 127 and 0 <= num <= 127 and bank >= 0):
                    continue
                keys = (0, 127)
                if len(parts) > 3 and parts[3].startswith('keys='):
                    lo, _, hi = parts[3][5:].partition('-')
                    keys = (int(lo), int(hi))
                if bank >= DRUM_BANK:
                    self.kits.setdefault(prog, []).append((keys[0], keys[1], num))
                else:
                    self.mel.setdefault((bank, prog), num)
        if not self.mel and not self.kits:
            raise ValueError(f"voice map {path} has no entries")
        print(f"[vmap] {path}: {len(self.mel)} melodic, {len(self.kits)} kits")

    def melodic(self, bank, prog):
        for key in ((bank, prog), (0, prog)):
            if key in self.mel:
                return self.mel[key]
        for (b, p), n in sorted(self.mel.items()):
            if p == prog:
                return n
        if (0, 0) in self.mel:
            return self.mel[(0, 0)]
        return min(self.mel.values()) if self.mel else 0

    def map(self, bank, prog, drum, note):
        if drum and self.kits:
            kit = self.kits.get(prog) or self.kits.get(0) or self.kits[min(self.kits)]
            for lo, hi, num in kit:
                if lo <= note <= hi:
                    return num
            return None
        return self.melodic(bank, prog)


class SongMapper(Mapper):
    """Numbers what the song plays.  Pass 1 (collect) records usage, then
    finish() assigns numbers; pass 2 maps."""

    def __init__(self, warn):
        super().__init__(warn)
        self.mel_used = set()
        self.kit_notes: Dict[int, set] = {}
        self.mel_num: Dict[Tuple[int, int], int] = {}
        self.kit_groups: Dict[int, List[Tuple[int, int, int]]] = {}
        self.collecting = True

    def map(self, bank, prog, drum, note):
        if self.collecting:
            if drum:
                self.kit_notes.setdefault(prog, set()).add(note)
            else:
                self.mel_used.add((bank, prog))
            return 0
        if drum:
            for lo, hi, num in self.kit_groups.get(prog, ()):
                if lo <= note <= hi:
                    return num
            return None
        return self.mel_num.get((bank, prog))

    def finish(self):
        self.collecting = False
        num = 0
        for key in sorted(self.mel_used):
            self.mel_num[key] = num
            num += 1
        for kit in sorted(self.kit_notes):
            notes = sorted(self.kit_notes[kit])
            groups = [notes[i:i + MAX_TONES] for i in range(0, len(notes), MAX_TONES)]
            ranges = []
            for gi, g in enumerate(groups):
                lo = 0 if gi == 0 else g[0]
                hi = 127 if gi == len(groups) - 1 else groups[gi + 1][0] - 1
                ranges.append((lo, hi, num))
                num += 1
            self.kit_groups[kit] = ranges
        if num > MAX_PROGRAMS:
            over = num - MAX_PROGRAMS
            self.warn(f"the song plays {num} instruments / drum groups, more than the "
                      f"{MAX_PROGRAMS} VAB programs; the last {over} are dropped")
            self.mel_num = {k: v for k, v in self.mel_num.items() if v < MAX_PROGRAMS}
            self.kit_groups = {k: [r for r in v if r[2] < MAX_PROGRAMS]
                               for k, v in self.kit_groups.items()}
        print(f"[song] {len(self.mel_num)} melodic instruments, "
              f"{sum(len(v) for v in self.kit_groups.values())} drum programs "
              f"({len(self.kit_groups)} kits) -> {min(num, MAX_PROGRAMS)} SEQ programs")

    def table(self):
        out = [(bank, prog, n, None, 'melodic') for (bank, prog), n in self.mel_num.items()]
        for kit, ranges in self.kit_groups.items():
            out += [(DRUM_BANK, kit, n, (lo, hi), 'drums') for lo, hi, n in ranges]
        return sorted(out, key=lambda e: e[2])

# ── SEQ writing ─────────────────────────────────────────────────────


class SeqWriter:
    def __init__(self):
        self.out = bytearray()
        self.tick = 0
        self.status = None

    def _delta(self, tick):
        dt = max(0, tick - self.tick)
        self.tick = max(self.tick, tick)
        b = [dt & 0x7F]
        dt >>= 7
        while dt:
            b.append(0x80 | (dt & 0x7F))
            dt >>= 7
        self.out += bytes(reversed(b))

    def chan(self, tick, status, *data):
        self._delta(tick)
        if status != self.status:  # running status
            self.out.append(status)
            self.status = status
        self.out += bytes(data)

    def tempo(self, tick, us):
        self._delta(tick)
        self.out += b'\xFF\x51' + min(us, 0xFFFFFF).to_bytes(3, 'big')
        self.status = None

    def end(self, tick):
        self._delta(tick)
        self.out += b'\xFF\x2F\x00'
        self.status = None


def _drum_part(ch: int, msb: int) -> bool:
    return ch == DRUM_CHANNEL or msb in (120, 127)


def convert(events, mapper: Mapper, writer: Optional[SeqWriter], warn, keep_nrpn=False,
            loop: Optional[Tuple[int, int]] = None, stats=None):
    """Run the event stream through the channel state.  writer=None only
    collects (song mode pass 1)."""
    msb = [0] * 16
    prog = [0] * 16
    seq_prog: List[Optional[int]] = [None] * 16
    param = [None] * 16       # 'rpn' / 'nrpn' / None: what CC6 currently edits
    rpn_sel = [None] * 16     # (msb, lsb) of the selected RPN, to restore after loops
    active: Dict[Tuple[int, int], List[int]] = {}
    unmapped = 0

    def emit(tick, status, *data):
        if writer:
            writer.chan(tick, status, *data)

    def set_prog(tick, ch, p):
        if seq_prog[ch] != p:
            emit(tick, 0xC0 | ch, p)
            seq_prog[ch] = p

    def note_off(tick, ch, note):
        stack = active.get((ch, note))
        if not stack:
            return False
        p = stack.pop(0)
        set_prog(tick, ch, p)
        emit(tick, 0x90 | ch, note, 0)
        return True

    # Note-offs are sorted in front of note-ons at the same tick; a zero-length
    # note's off then comes before its on.  Remember offs that found no note
    # (with their file position) so a note-on they originally followed still
    # gets released.
    orphan_off = {}

    loop_marks = []
    if loop:
        loop_marks = [(loop[0], NRPN_LOOP_START, 127), (loop[1], NRPN_LOOP_END, 0)]

    def put_loop_marks(upto):
        while loop_marks and loop_marks[0][0] <= upto:
            t, kind, val = loop_marks.pop(0)
            if kind == NRPN_LOOP_END:  # notes held across the jump would never end
                for (c, n), stack in list(active.items()):
                    while stack:
                        note_off(t, c, n)
            emit(t, 0xB0, CC_NRPN_MSB, kind)
            emit(t, 0xB0, CC_DATA_MSB, val)
            if rpn_sel[0] is not None:  # channel 0's data entry must edit its RPN again
                emit(t, 0xB0, CC_RPN_MSB, rpn_sel[0][0])
                emit(t, 0xB0, CC_RPN_LSB, rpn_sel[0][1])

    for tick, _tr, _idx, kind, a in events:
        put_loop_marks(tick)
        if kind == 'on':
            ch, note, vel = a
            p = mapper.map(msb[ch], prog[ch], _drum_part(ch, msb[ch]), note)
            if p is None:
                unmapped += 1
                continue
            set_prog(tick, ch, p)
            emit(tick, 0x90 | ch, note, vel)
            active.setdefault((ch, note), []).append(p)
            pos = orphan_off.get((tick, ch, note))
            if pos is not None and pos > (_tr, _idx):  # its off came after it in the file
                del orphan_off[(tick, ch, note)]
                note_off(tick, ch, note)
        elif kind == 'off':
            if not note_off(tick, a[0], a[1]):
                orphan_off[(tick, a[0], a[1])] = (_tr, _idx)
        elif kind == 'pc':
            prog[a[0]] = a[1]
        elif kind == 'pb':
            emit(tick, 0xE0 | a[0], a[1], a[2])
        elif kind == 'cc':
            ch, num, val = a
            if num == CC_BANK_MSB:
                msb[ch] = val
            elif num in (CC_ALL_NOTES_OFF, CC_ALL_SOUND_OFF):
                for (c, n) in [k for k, v in active.items() if k[0] == ch and v]:
                    while active.get((c, n)):
                        note_off(tick, c, n)
            elif num in (CC_RPN_MSB, CC_RPN_LSB):
                param[ch] = 'rpn'
                sel = list(rpn_sel[ch] or (127, 127))
                sel[0 if num == CC_RPN_MSB else 1] = val
                rpn_sel[ch] = tuple(sel)
                emit(tick, 0xB0 | ch, num, val)
            elif num in (CC_NRPN_MSB, CC_NRPN_LSB):
                param[ch] = 'nrpn'
                if keep_nrpn:
                    emit(tick, 0xB0 | ch, num, val)
            elif num == CC_DATA_MSB:
                if param[ch] == 'rpn' or (param[ch] == 'nrpn' and keep_nrpn):
                    emit(tick, 0xB0 | ch, num, val)
            elif num in PASS_CCS:
                if num == 121:
                    param[ch] = None
                emit(tick, 0xB0 | ch, num, val)
            elif num == CC_LOOP_START_RPGM:
                pass
            if stats is not None and num not in PASS_CCS and num not in (
                    CC_DATA_MSB, CC_RPN_MSB, CC_RPN_LSB):
                stats[num] = stats.get(num, 0) + 1
        elif kind == 'tempo':
            if writer and tick > 0:
                writer.tempo(tick, a[0])
    # Release notes the MIDI never turns off (before a loop end, so they don't
    # hang across the jump).
    last = events[-1][0] if events else 0
    for (ch, note), stack in list(active.items()):
        while stack:
            note_off(last, ch, note)
    if loop_marks:
        put_loop_marks(1 << 62)
    if unmapped:
        warn(f"{unmapped} notes have no program in the map and were dropped")

# ── Main conversion ─────────────────────────────────────────────────


def _find_loop(events, end_tick, warn) -> Tuple[int, int]:
    start = end = None
    for tick, _, _, kind, a in events:
        if kind == 'marker':
            t = a[0].lower().replace(' ', '').replace('_', '')
            if t in ('loopstart', 'loop', '[') and start is None:
                start = tick
            elif t in ('loopend', ']'):
                end = tick
        elif kind == 'cc' and a[1] == CC_LOOP_START_RPGM and start is None:
            start = tick
    start = 0 if start is None else start
    end = end_tick if end is None or end <= start else end
    if end <= start:
        end = start + 1
    return start, end


def midi_to_seq(data: bytes, mode: str, vmap: Optional[str] = None, keep_nrpn=False,
                loop=False, table=True, warn=print, resolution: Optional[int] = None):
    division, events = read_midi(data, warn)
    if resolution and resolution != division:
        # Rescale ticks (tempo is per quarter note, so timing stays the same).
        events = [(int(round(e[0] * resolution / division)),) + e[1:] for e in events]
        division = resolution
    # Note-offs first at each tick, otherwise file order.
    events.sort(key=lambda e: (e[0], 0 if e[3] == 'off' else 1, e[1], e[2]))
    end_tick = max((e[0] for e in events), default=0)

    tempo = 500000
    rhythm = (4, 2)
    for tick, _, _, kind, a in events:
        if tick > 0:
            break
        if kind == 'tempo':
            tempo = a[0]
        elif kind == 'timesig':
            rhythm = a
    if not any(k == 'tempo' for _, _, _, k, _ in events):
        warn("no tempo in the MIDI file; using 120 BPM")

    if mode == 'song':
        mapper = SongMapper(warn)
        convert(events, mapper, None, warn)
        mapper.finish()
    elif mode == 'vmap':
        mapper = VmapMapper(vmap, warn)
    elif mode == 'gm':
        mapper = GMMapper(warn)
    else:
        mapper = PlainMapper(warn)

    lp = _find_loop(events, end_tick, warn) if loop else None
    if lp:
        print(f"[loop] ticks {lp[0]}..{lp[1]} repeat forever (libsnd NRPN loop)")
    w = SeqWriter()
    dropped = {}
    convert(events, mapper, w, warn, keep_nrpn, lp, dropped)
    w.end(max(end_tick, w.tick + (1 if lp else 0)))
    for num, n in sorted(dropped.items()):
        if num in (CC_BANK_MSB, CC_BANK_LSB, CC_ALL_NOTES_OFF, CC_ALL_SOUND_OFF) or (
                num == CC_LOOP_START_RPGM and loop):
            continue  # resolved into programs / note-offs / the loop
        if num in (CC_NRPN_MSB, CC_NRPN_LSB):
            if not keep_nrpn:
                warn(f"CC{num} (NRPN): {n} events dropped (libsnd reads NRPNs as VAB "
                     f"attributes; --keep-nrpn to keep)")
            continue
        warn(f"CC{num} not supported by libsnd: {n} events dropped")

    hdr = (b'pQES' + struct.pack('>IH', 1, division) + tempo.to_bytes(3, 'big') +
           bytes([rhythm[0] & 0xFF, rhythm[1] & 0xFF]))
    seq = hdr + bytes(w.out)
    tbl = mapper.table()
    if table and tbl:
        text = ''.join(f"{b} {p} {n}" + (f" keys={k[0]}-{k[1]}" if k else '') + f" {name}\n"
                       for b, p, n, k, name in tbl).encode('ascii')
        seq += TABLE_MAGIC + struct.pack('<I', len(text)) + text
    return seq, tbl, division, len(events)


def main():
    ap = argparse.ArgumentParser(description='Convert a MIDI file to a PlayStation libsnd SEQ')
    ap.add_argument('input', help='Input .mid')
    ap.add_argument('output', nargs='?', help='Output .seq (default: input name with .seq)')
    g = ap.add_mutually_exclusive_group()
    g.add_argument('-m', '--voice-map', metavar='VMAP',
                   help='Voice map from "sf2vab.py --all-banks"')
    g.add_argument('--gm', action='store_true', help='Target "sf2vab.py --gm"')
    g.add_argument('--plain', action='store_true',
                   help='Keep program numbers as they are (like SMF2SEQ)')
    ap.add_argument('--no-table', action='store_true',
                    help="Don't append the instrument table after the end-of-track "
                         "(sf2vab --seq then reads program numbers as GM)")
    ap.add_argument('--keep-nrpn', action='store_true',
                    help='Keep NRPN controllers (for MIDI files authored for libsnd: VAB '
                         'attributes, loops, marks)')
    ap.add_argument('--loop', action='store_true',
                    help='Loop forever between the loopStart/loopEnd markers (or CC111), '
                         'or the whole song')
    ap.add_argument('--resolution', type=int, default=None, metavar='PPQ',
                    help='Rescale to this many ticks per quarter note (default: keep the '
                         "MIDI file's, like SMF2SEQ). libsnd plays SEQs at its tick rate "
                         '(SsSetTickMode, at most 240 Hz) whatever the resolution, so events '
                         'closer together than one libsnd tick play together anyway')
    args = ap.parse_args()

    warnings = []

    def warn(msg):
        if msg not in warnings:
            warnings.append(msg)

    mode = 'vmap' if args.voice_map else 'gm' if args.gm else 'plain' if args.plain else 'song'
    with open(args.input, 'rb') as f:
        data = f.read()
    seq, tbl, division, n_ev = midi_to_seq(data, mode, args.voice_map, args.keep_nrpn,
                                           args.loop, not args.no_table, warn, args.resolution)
    out = args.output or os.path.splitext(args.input)[0] + '.seq'
    with open(out, 'wb') as f:
        f.write(seq)
    for w in warnings:
        print(f"[warn] {w}")
    print(f"[out] {out} ({len(seq)} bytes, resolution {division}, {n_ev} MIDI events)")
    if tbl and mode in ('song', 'gm'):
        print("\nSEQ programs:" + ("" if not args.no_table else " (table not written)"))
        for b, p, n, k, name in tbl:
            what = f"kit {p} keys {k[0]}-{k[1]}" if k else f"bank {b} program {p}"
            print(f"  {n:3d}: {what}")
        if mode == 'song' and not args.no_table:
            print(f"\nBuild the matching VAB with: sf2vab.py <font.sf2> --seq {out}")


if __name__ == '__main__':
    main()
