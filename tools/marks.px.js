// The animated recording marks of the pixel looks, drawn in pxl (PXL = node <claude-pxl>/bin/pxl.js).
// Every mark is an 11x9 canvas: the 9x9 mark in columns 1..9 and a column either side for glints and
// flames. The letters are roles, not colours: X is the recording colour (it means the same in every
// look), h its light, o its dark; k black, w white, e eye yellow, f fire orange, s foam, p pink, b ice
// blue, c cyan. Regenerate marks.py with: python tools/build_marks.py
pal({ X: "#40c060", h: "#c0ffd0", o: "#104020", k: "#000000", w: "#ffffff", e: "#ffe85a",
      f: "#ff9a3c", s: "#f4efe0", p: "#ffd6f0", b: "#d6e6ff", c: "#2de6ff" })

const p = (x, y, c) => rect(x + 1, y, 1, 1, c)          // one pixel, in the 9x9's own coordinates
const g = (rows) => grid(1, 0, rows)

// a base grid, then what changes in each of its frames
function mark(name, fps, base, ...patches) {
  anim(name, { w: 11, h: 9, fps, frames: patches.length }, g(base), ...patches.map((ps) => frame(...ps)))
}

mark("orb", 6, `
  ..ooooo..
  .oXXXXXo.
  oXhhXXXXo
  oXhhXXXXo
  oXXXXXXXo
  oXXXXXXXo
  oXXXXXXXo
  .oXXXXXo.
  ..ooooo..`,
  [], [p(2, 2, "w")], [p(2, 2, "w"), p(3, 3, "w")], [p(6, 5, "w")])

mark("crown", 5, `
  .........
  h...h...h
  X...X...X
  XX.XXX.XX
  XXXXXXXXX
  XoXXoXXoX
  XXXXXXXXX
  .........
  .........`,
  [p(0, 1, "w"), p(0, 0, "w")], [p(4, 1, "w"), p(4, 0, "w")], [p(8, 1, "w"), p(8, 0, "w")],
  [p(2, 0, "w"), p(6, 0, "w"), p(4, 8, "w")])

mark("moon", 3, `
  ...ooo...
  .ooXXXo..
  .oXXXo...
  oXXXo....
  oXXXo....
  oXXXo....
  .oXXXo...
  .ooXXXo..
  ...ooo...`,
  [p(7, 2, "w"), p(7, 6, "w")],
  [p(7, 1, "w"), p(6, 2, "w"), p(7, 2, "w"), p(8, 2, "w"), p(7, 3, "w"), p(6, 6, "w")],
  [p(7, 2, "w"), p(6, 5, "w"), p(7, 6, "w"), p(8, 6, "w"), p(7, 7, "w")],
  [p(8, 3, "w"), p(7, 7, "w")])

mark("sun", 4, `
  .........
  .........
  ...XXX...
  ..XXXXX..
  ..XhXXX..
  ..XXXXX..
  ...XXX...
  .........
  .........`,
  [p(4, 1, "X"), p(4, 0, "X"), p(4, 7, "X"), p(4, 8, "X"), p(0, 4, "X"), p(1, 4, "X"), p(7, 4, "X"), p(8, 4, "X")],
  [p(4, 1, "X"), p(4, 7, "X"), p(1, 4, "X"), p(7, 4, "X"), p(2, 2, "X"), p(6, 2, "X"), p(2, 6, "X"), p(6, 6, "X")],
  [p(1, 1, "X"), p(7, 1, "X"), p(1, 7, "X"), p(7, 7, "X"), p(0, 0, "X"), p(8, 0, "X"), p(0, 8, "X"), p(8, 8, "X")],
  [p(4, 1, "X"), p(4, 7, "X"), p(1, 4, "X"), p(7, 4, "X"), p(2, 2, "X"), p(6, 2, "X"), p(2, 6, "X"), p(6, 6, "X")])

mark("shield", 5, `
  ooooooooo
  oXXXhXXXo
  oXXXhXXXo
  ohhhhhhho
  oXXXhXXXo
  .oXXhXXo.
  ..oXhXo..
  ...oXo...
  ....o....`,
  [p(1, 1, "w")], [p(2, 2, "w"), p(1, 1, "h")], [p(3, 4, "w"), p(2, 3, "w")], [p(5, 5, "w"), p(4, 6, "h")])

mark("star", 5, `
  ....X....
  ....X....
  ...XhX...
  ..XXhXX..
  XXXhhhXXX
  ..XXhXX..
  ...XhX...
  ....X....
  ....X....`,
  [], [p(4, 0, "_"), p(4, 8, "_"), p(0, 4, "_"), p(8, 4, "_")],
  [p(1, 1, "w"), p(7, 7, "w")],
  [p(4, 0, "_"), p(4, 8, "_"), p(0, 4, "_"), p(8, 4, "_"), p(7, 1, "w"), p(1, 7, "w")])

mark("bolt", 10, `
  .....XXX.
  ....XXX..
  ...XXX...
  ..XXXXXX.
  ...XXXX..
  ....XXX..
  ....XX...
  ...XX....
  ...X.....`,
  [p(0, 6, "c"), p(8, 2, "c")],
  [p(6, 0, "w"), p(5, 1, "w"), p(4, 2, "w"), p(3, 3, "w"), p(5, 4, "w"), p(4, 6, "w"), p(3, 7, "w")],
  [p(1, 2, "c"), p(7, 7, "c"), p(8, 4, "c")],
  [p(0, 3, "c"), p(8, 5, "c"), p(1, 7, "c")])

mark("leaf", 4, `
  ......oo.
  ....ooXXo
  ..ooXXXXo
  .oXXhXXXo
  .oXhXXXo.
  oXhXXXoo.
  ohXXoo...
  oo.o.....
  o........`,
  [], [p(4, 3, "w")], [p(3, 4, "w"), p(2, 5, "h")], [p(7, 6, "X"), p(8, 7, "o")])

mark("dragon", 5, `
  o.o......
  oXo..oo..
  .oXXXXXo.
  .XXXeXXXo
  XXXXXXXXX
  XXXXXXoXX
  .XXXXXXXo
  ..oXhhXo.
  ...ooooo.`,
  [], [p(9, 5, "f")], [p(9, 5, "f"), p(10, 5, "e"), p(9, 4, "f"), p(9, 6, "f")],
  [p(9, 5, "f"), p(10, 4, "e"), p(10, 6, "f"), p(9, 6, "e"), p(4, 3, "o")])

// the balance tips one way, then the other (the tipped frames are drawn whole)
anim("scales", { w: 11, h: 9, fps: 3, frames: 4 },
  frame(g(`
  ....h....
  XXXXXXXXX
  X...X...X
  X...X...X
  X...X...X
  XXX.X.XXX
  .X..X..X.
  ....X....
  ..XXXXX..`)),
  frame(g(`
  ....h....
  XXXXXXXXX
  X...X...X
  X...X.XXX
  X...X..X.
  X...X....
  XXX.X....
  .X..X....
  ..XXXXX..`)),
  frame(g(`
  ....h....
  XXXXXXXXX
  X...X...X
  X...X...X
  X...X...X
  XXX.X.XXX
  .X..X..X.
  ....X....
  ..XXXXX..`)),
  frame(g(`
  ....h....
  XXXXXXXXX
  X...X...X
  XXX.X...X
  .X..X...X
  ....X...X
  ....X.XXX
  ....X..X.
  ..XXXXX..`)))

mark("pen", 5, `
  ......oo.
  .....oXXo
  ....oXXo.
  ...oXXo..
  ..oXXo...
  .oXho....
  ohXo.....
  oXo......
  oo.......`,
  [], [p(3, 8, "o")], [p(3, 8, "o"), p(4, 8, "o"), p(5, 7, "o")],
  [p(3, 8, "o"), p(4, 8, "o"), p(5, 7, "o"), p(6, 7, "o"), p(7, 8, "o")])

mark("gem", 5, `
  ..ooooo..
  .ohXhXho.
  ohXXhXXho
  ooooooooo
  .oXhXhXo.
  ..oXhXo..
  ...oXo...
  ....o....
  .........`,
  [p(0, 0, "w"), p(8, 7, "b"), p(3, 1, "w")],
  [p(1, 7, "w"), p(0, 6, "p"), p(2, 7, "p"), p(1, 6, "p"), p(5, 2, "w")],
  [p(7, 0, "w"), p(8, 1, "b"), p(6, 0, "b"), p(7, 1, "b"), p(4, 4, "w")],
  [p(0, 4, "w"), p(8, 4, "p"), p(1, 2, "w"), p(7, 7, "w")])

anim("flame", { w: 11, h: 9, fps: 8, frames: 4 },
  frame(g(`
  ....o....
  ...oXo...
  ...oXXo..
  ..oXhXXo.
  .oXXhhXXo
  .oXhhhhXo
  .oXhhhhXo
  ..oXhhXo.
  ...ooooo.`)),
  frame(g(`
  .....o...
  ....oXo..
  ...oXXo..
  ..oXhXXo.
  .oXXhhXXo
  .oXhhhhXo
  .oXhhhhXo
  ..oXhhXo.
  ...ooooo.`)),
  frame(g(`
  ....o....
  ...oXo...
  ...oXXo..
  ..oXhXXo.
  .oXXhhXXo
  .oXhhhhXo
  .oXhhhhXo
  ..oXhhXo.
  ...ooooo.`)),
  frame(g(`
  ..o......
  .oXo.....
  .oXXo....
  ..oXhXXo.
  .oXXhhXXo
  .oXhhhhXo
  .oXhhhhXo
  ..oXhhXo.
  ...ooooo.`)))

mark("wave", 6, `
  ...hhhh..
  ..hXXXXh.
  .hXXooXXh
  .XXoo..Xh
  XXXo....X
  XXXXo..h.
  XXXXXXXX.
  XXooXXXXX
  XXXXXXXXX`,
  [], [p(7, 0, "s"), p(5, 3, "h")], [p(7, 0, "s"), p(8, 1, "s"), p(6, 4, "h")],
  [p(8, 0, "s"), p(8, 2, "s"), p(5, 4, "h")])
