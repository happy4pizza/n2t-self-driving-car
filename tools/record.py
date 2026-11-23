"""Record the game running on the real VM Emulator (headless) and turn it into an animated GIF.

usage: python3 record.py OUT.gif FIRST LAST [EVERY] [FPS] [STILL_FRAME STILL.png]

    FIRST, LAST  game frames to record (the AI drives the first game, seed 12345)
    EVERY        keep every EVERY-th game frame (default 3)
    FPS          GIF playback rate (default 30)
    STILL_FRAME  optional: also save that game frame (or the nearest recorded one) as a PNG

Example (what made docs/demo.gif; it takes about 5 minutes):
    python3 tools/record.py docs/demo.gif 240 2400 3 30

Every picture is exactly what the Hack screen showed when Hud.draw returned to Game.run, i.e. when a
frame was completely drawn. The game is not changed or sped up for the recording.

How it works (all on the VM Emulator, headless, so the OS .vm files are copied into build/record/)
  The script command `while currentFunction <> X { vmstep; }` stops at an exact point but is about
  150 times slower than `repeat N { vmstep; }`. So:
    pass 0  runs (with `while`) to the first Hud.draw and reads the stack to find the RAM address of
            Game.run's local variable `frame`.
    pass 1  runs (with `repeat`) and samples that variable every CHUNK steps, which shows in which
            chunk each frame ends.
    pass 2  for each wanted frame, `repeat`s to a few chunks before that frame ends, then uses
            `while` for the last few thousand steps to stop exactly when Hud.draw returns, and dumps
            the 8192 screen words.
  The run is deterministic (no keys are pressed), so the step counts from pass 1 hold in pass 2.
  ffmpeg (must be on PATH) turns the pictures into a 2-colour GIF, scaled 2x without smoothing.
"""
import os, sys, glob, shutil, subprocess
HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import run_tests, screenshot

SCREEN = 16384
CHUNK = 2000            # pass 1 sampling interval, VM steps
BACK = 3                # pass 2 starts its exact `while` search this many chunks before a frame ends
FIRST_SEED = 12345      # Main.jack starts the first game with this seed (Game.run's local 0)
D = os.path.join(SRC, "build", "record")


def vm(tst_text, name):
    path = os.path.join(D, name + ".tst")
    open(path, "w").write(tst_text)
    r = subprocess.run([os.path.join(run_tests.tools_dir(), "VMEmulator.sh"), path],
                       capture_output=True, text=True, timeout=3600)
    if "End of script" not in r.stdout + r.stderr:
        sys.exit("VM Emulator problem: " + (r.stdout + r.stderr).strip())
    vals = []
    for line in open(os.path.join(D, name + ".out")):
        if "RAM" in line or not line.strip(): continue
        vals += [int(v) for v in line.strip().strip("|").split("|")]
    return vals


def outputs(first, count):
    return "".join("output-list " + " ".join("RAM[%d]%%D1.6.1" % (first + i + j) for j in range(20) if i + j < count)
                   + ";\noutput;\n" for i in range(0, count, 20))


def build():
    shutil.rmtree(D, ignore_errors=True); os.makedirs(D)
    for f in glob.glob(os.path.join(SRC, "*.jack")):
        shutil.copy(f, D)
    for f in glob.glob(os.path.join(run_tests.tools_dir(), "OS", "*.vm")):
        shutil.copy(f, D)
    r = subprocess.run([os.path.join(run_tests.tools_dir(), "JackCompiler.sh"), D], capture_output=True, text=True)
    if "rror" in r.stdout + r.stderr:
        sys.exit(r.stdout + r.stderr)


def frame_address():
    """Pass 0. At the entry to Hud.draw(frame, mask, manual), the caller's saved LCL is at ARG + 3 + 1."""
    ram = vm("load,\noutput-file P0.out,\nwhile currentFunction <> Hud.draw {\n  vmstep;\n}\n" + outputs(0, 512), "P0")
    arg = ram[2]
    lcl = ram[arg + 4]
    assert ram[lcl] == FIRST_SEED and ram[lcl + 5] == 0 and ram[arg] == 0, "unexpected stack layout"
    return lcl, lcl + 5


def frame_ends(lcl, addr, last, per_frame=1700000):
    """Pass 1. Returns ends[k] = index n of the first sample taken after frame k ended
    (sample n is taken after (n + 1) * CHUNK steps). Start-up takes about 2 million steps and a
    frame about 1.4 million (the OS runs as VM code here); if the guess is short, it runs again."""
    n = (3000000 + (last + 2) * per_frame) // CHUNK
    tst =("load,\noutput-file P1.out,\noutput-list RAM[%d]%%D1.6.1 RAM[%d]%%D1.6.1;\nrepeat %d {\n%s  output;\n}\n"
           % (lcl, addr, n, "  vmstep;\n" * CHUNK))
    v = vm(tst, "P1")
    seed, frame = v[0::2], v[1::2]
    # Before `let frame = 0` runs, that stack word holds leftovers from earlier calls. The counter is
    # valid on the tail where it only ever counts down by 0 or 1 going backward, so walk back to its start.
    i = len(frame) - 1
    while i > 0 and seed[i - 1] == FIRST_SEED and frame[i - 1] in (frame[i], frame[i] - 1) and frame[i - 1] >= 0:
        i -= 1
    start = i
    assert frame[start] == 0, "could not find the start of the first game"
    ends, k = [], 0
    for i in range(start, len(frame)):
        assert seed[i] == FIRST_SEED and frame[i] in (k, k + 1), "frame counter did not count up by one"
        if frame[i] == k + 1:
            ends.append(i)
            k += 1
            if k > last: break
    if k <= last:
        print("pass 1 reached frame %d of %d, running again with more steps" % (k, last))
        return frame_ends(lcl, addr, last, per_frame * 3 // 2)
    return ends


def capture(first, last, every):
    """Returns a list of (game frame number, 8192 screen words)."""
    build()
    lcl, addr = frame_address()
    ends = frame_ends(lcl, addr, last)
    shots = list(range(first, last + 1, every))
    tst = ["load,\noutput-file Rec.out,\n"]
    pos = 0          # step count we may be at, at most; the true position is at most CHUNK + a few steps less
    for f in shots:
        target = (ends[f] + 1 - BACK) * CHUNK
        assert f == 0 or target - CHUNK > (ends[f - 1] + 1) * CHUNK, "frame too short for BACK"
        if target > pos:
            tst.append("repeat %d {\n  vmstep;\n}\n" % (target - pos))
        tst.append("while currentFunction <> Hud.draw {\n  vmstep;\n}\n"
                   "while currentFunction <> Game.run {\n  vmstep;\n}\n")
        tst.append("output-list RAM[%d]%%D1.6.1;\noutput;\n" % addr)      # frame counter, checked below
        tst.append(outputs(SCREEN, 8192))
        pos = (ends[f] + 1) * CHUNK
    vals = vm("".join(tst), "Rec")
    assert len(vals) == 8193 * len(shots), (len(vals), len(shots))
    out = []
    for n, f in enumerate(shots):
        block = vals[n * 8193:(n + 1) * 8193]
        assert block[0] == f, "picture %d shows frame %d, wanted %d" % (n, block[0], f)
        out.append((f, block[1:]))
    return out


def gray(words):
    """8192 screen words -> 512x256 bytes, 0 = black pixel, 255 = white. Bit 0 is the leftmost pixel."""
    out = bytearray(512 * 256)
    for i, v in enumerate(words):
        v &= 0xFFFF
        if v == 0:
            out[i * 16:i * 16 + 16] = b"\xff" * 16
            continue
        base = i * 16
        for b in range(16):
            out[base + b] = 0 if (v >> b) & 1 else 255
    return bytes(out)


def make_gif(path, shots, fps):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    graph = ("scale=1024:512:flags=neighbor,pad=1032:520:4:4:color=0x606060,"
             "split[a][b];[a]palettegen=max_colors=4:reserve_transparent=0:stats_mode=full[p];"
             "[b][p]paletteuse=dither=none:diff_mode=rectangle")
    p = subprocess.Popen(["ffmpeg", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "gray",
                          "-s", "512x256", "-r", str(fps), "-i", "-", "-filter_complex", graph,
                          "-loop", "0", path], stdin=subprocess.PIPE)
    for _, words in shots:
        p.stdin.write(gray(words))
    p.stdin.close()
    if p.wait():
        sys.exit("ffmpeg failed")


def main():
    a = sys.argv[1:]
    out, first, last = a[0], int(a[1]), int(a[2])
    every = int(a[3]) if len(a) > 3 else 3
    fps = int(a[4]) if len(a) > 4 else 30
    shots = capture(first, last, every)
    make_gif(out, shots, fps)
    print("wrote %s: %d pictures of game frames %d..%d (every %d), %d fps" % (out, len(shots), first, shots[-1][0], every, fps))
    if len(a) > 6:
        still, png = int(a[5]), a[6]
        f, words = min(shots, key=lambda s: abs(s[0] - still))
        screenshot.write_png(png, words)
        print("wrote %s (game frame %d)" % (png, f))


if __name__ == "__main__":
    main()
