# Examples

Sample HP-GL files for trying out `hpgl-buddy`.

| File | What it is |
|---|---|
| `demo-g.hpgl` | A hand-made spiral drawn as a **single ~15 KB `PD` instruction** — the >1024-byte *oversized-instruction* case. `plot` streams it across `ESC.B`-gated sub-blocks (the instruction is never split as HP-GL; the plotter reassembles the byte stream). |

The generated `demo --scene house` is the same case in generated form; see the
project README and `DESIGN.md` for the chunking/streaming model.

```bash
hpgl-buddy check examples/demo-g.hpgl
hpgl-buddy plot  examples/demo-g.hpgl --port /dev/cu.usbserial-XXXX
```
