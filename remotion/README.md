# Caption overlays (Remotion)

Animated caption templates for the 9:16 Short. `render_short` uses one when the caption style is
`rm:<template>` (`render.captions: rm:punch`); the ASS styles (`word`, `pop`, ...) are untouched and remain the
fallback whenever this renderer is missing or fails.

## Licence of Remotion: read this first

Remotion is not MIT. Its licence (checked 19 Nov 2026) gives a **free licence** to individuals, to
for-profit organisations **with up to 3 employees**, to non-profits, and for evaluation; everyone else
needs a paid **company licence** (at the time of writing "Remotion for Automators": $0.01 per render,
$100 a month minimum). This team is three operators, so it is inside the free licence today. A fourth person makes it a
company licence.

- Licence text: https://github.com/remotion-dev/remotion/blob/main/LICENSE.md
- Licence pages: https://www.remotion.dev/docs/license and https://www.remotion.dev/docs/license/pricing

The fonts are OFL 1.1 or Apache 2.0 and are in `assets/fonts/captions/` with their licence files
(`INSTANCES.txt` there lists the five static instances cut from variable fonts). opentype.js (glyph
outlines) is MIT. Emoji are **Apple Color Emoji only** (owner, 20 Nov): the system font of macOS, which
may not be redistributed and is therefore never bundled. On a render machine without it (Linux, Windows)
emoji are taken out of the caption text; no other emoji set is ever drawn.

## Setup (once per machine)

```
cd remotion
npm ci            # pinned versions, package-lock.json
npm run setup     # downloads the headless browser Remotion renders with, builds the bundle
```

After that nothing is fetched at render time. `clipwatcher.render.remotion.available()` is true, and the
bundle (`remotion/build/`, git-ignored) is rebuilt automatically, once, whenever the sources change.
`npm run studio` opens Remotion Studio on `demo/props.json`; `npm run typecheck` runs tsc.

## How a clip gets its captions

1. Python plans (`render/rm_plan.py`): cleans the words, groups them into pages (phrases), fixes every
   start and end. The plan is the specification the frames are checked against.
2. Python picks colours (`render/palette.py`): the template's fixed scheme, or `auto`.
3. Python writes one props JSON and runs `node scripts/render.mjs`: a transparent PNG per frame of the
   **caption band only** (1080 wide, as tall as the caption box), at the clip's own frame rate. Frames
   with nothing on them are hard links to one blank frame.
4. ffmpeg lays the band over the Short (`remotion.overlay_filter`): blended in 4:4:4 BT.709, the rest
   of the picture untouched, overlay frame k on video frame k.

Config, all optional (`config/global.yaml`):

| key | default | |
|---|---|---|
| `render.captions` | `word` | `rm:<template>` selects a template below |
| `render.rm.colors` | `auto` | `auto` or `fixed` |
| `render.rm.fallback` | `word` | ASS style used when the overlay cannot be made |
| `render.rm.max_fps` | `60` | the overlay runs at the clip's frame rate, capped here |
| `render.rm.concurrency` | all cores | browser tabs rendering in parallel |
| `render.rm.timeout_s` | `300` | |
| `render.rm.templates.<name>` | `{}` | any parameter of `templates/<name>.json`, deep-merged over it |
| `tools.node` | `node` on PATH | or env `CLIPWATCHER_NODE` |

Every template is fully described by its `templates/<name>.json`: font, size, case, paging, timing,
colours, the auto rules and its own `fx` block. Every value can be overridden per template from the
config, e.g. `render.rm.templates.punch.fx.activeScale: 1.2` or `...punch.paging.maxWords: 2`. The
parameter schema (`src/core/schema.ts` plus the `paramsSchema({...})` at the top of each template) rejects
unknown or missing keys before the first frame, and a rejected render falls back to ASS.

## Templates

<!-- TEMPLATES -->
| template | font | words | look |
|---|---|---|---|
| `rm:arcade` | Press Start 2P | up to 3 | 8-bit: pixel caps with a one-pixel outline and a hard stepped extrusion. Letters drop in one by one and bounce, the spoken word hops letter by letter and runs between two colours, shouted words are bigger and blink, and before a pause the letters fall off one by one. |
| `rm:bounce` | Chewy | up to 4 | Follow the bouncing ball: a glossy ball hops in arcs from word to word and lands on each one as it is spoken; the word gives way under it, springs back and takes the highlight; before a pause the ball bounces away and the words drop out. |
| `rm:chips` | Bungee | up to 3 | Every word is a chip: a rounded candy-coloured plate with dark letters. Chips fly in from both sides and settle on a spring, the spoken chip turns over like a card onto its dark face with letters in its own colour, shouted chips are bigger and tilted, and at the end they scatter and drop. |
| `rm:chrome` | Unbounded | up to 2 | Liquid metal: wide caps filled with a chrome reflection whose horizon sloshes, on a dark outline with a hard shadow. Lines are poured in from the left, leaning forward and settling; a slanted glint runs across the spoken word, which takes its own tint; shouted words are gold; before a pause the lines lean back and leave to the right. |
| `rm:comic` | Bangers | up to 3 | Comic-book title: slanted yellow-to-orange caps with a black outline and a red 3D side; words spin in on a spring, the spoken word punches up white-to-cyan, shouted words get a turning starburst, the block zooms away spinning. |
| `rm:creep` | Creepster | up to 2 | Horror: dripping acid-green caps on black with a sickly glow. Letters ooze down into place one by one and never hold quite still; the spoken word turns hotter and its heart beats twice; shouted words are blood red and shudder; before a pause the letters melt through the floor. |
| `rm:focus` | Titan One | up to 3 | A lens moves along the line: the spoken word is large and coloured with a soft glow under it, its neighbours are small, and the whole line re-flows smoothly as the lens moves on; shouted words swell even more; words grow out of the baseline and shrink back into it. |
| `rm:glide` | Archivo Black | up to 4 | A rounded highlight box glides from word to word and stretches as it goes; the word inside it turns dark on the box colour, the rest stay white with an outline. |
| `rm:glitch` | Russo One | up to 3 | RGB split: white caps on a dark outline with a cyan and a pink copy behind them. Words arrive torn into three bands that slide into register while the copies snap together, the spoken word takes the highlight with a split pulse, shouted words tear again and lean, and before a pause the copies fly apart and the word collapses to a line. |
| `rm:hollow` | Alfa Slab One | up to 3 | Outline letters that fill up: slab caps as glass, dark inside a white line. A golden liquid with a head of foam rises in each word as it is spoken and stays, the white line giving way to it; shouted words fill pink and slosh to the brim; lines slide in from opposite sides, then the liquid drains and they slide out. |
| `rm:jelly` | Fredoka | up to 3 | Soft round lowercase that behaves like jelly: every letter springs up with a stretch and a squash, a wave runs through the spoken word letter by letter and leaves the highlight behind it, the line keeps breathing, and before a pause the letters pop away. Plum outline, candy colours. |
| `rm:label` | Bricolage Grotesque | up to 5 | The social label look: every line on its own tight white plate, dark letters, no outline. Plates wipe open from the left; the part under the spoken word opens in the highlight colour and lifts with its word; shouted words sit on their own tilted plate in the emphasis colour. |
| `rm:marker` | Permanent Marker | up to 3 | Hand lettering on a slope: words are written on from left to right, a highlighter is swiped across each word as it is spoken and stays on what has been said, shouted words get a scribbled underline; wiped away from right to left. |
| `rm:neon` | Tilt Neon | up to 3 | A neon sign: lowercase tube letters on a near-black plate with a glowing rim. The tubes switch on with a stutter, the spoken word runs white-hot in a breathing coloured glow, shouted words buzz in their own hue, and before a pause the tubes go dark and the plate closes. |
| `rm:punch` | Poppins | up to 3 | The viral standard, built from the animation library: fat geometric caps with a thick outline; the phrase pops in, the spoken word punches up in the highlight colour, shouted words shake, the phrase shrinks away before a pause. Each of those motions can be swapped by name from the settings. |
| `rm:ransom` | Special Elite | up to 3 | Ransom note: every letter on its own paper scrap, turned and nudged, slapped on one by one; the spoken word lifts, straightens and turns highlighter yellow, shouted words are bigger and red, and the scraps fall off turning. |
| `rm:reel` | Ultra | 1 | Slot machine: one word at a time rolls into a window between two rails and locks with a bounce while the word before rolls out below. Gold gradient letters arrive white-hot and cool down, shouted words turn red, swell and make the rails flash, and before a pause the word rolls out and the rails close. |
| `rm:retro` | Shrikhand | up to 2, as spoken | Seventies sign: fat cream script on a dark base with a deep extrusion in three sunset stripes. Words push out of the wall as they are spoken; the spoken word is raised, warms to gold and its stripes flow; shouted words are bigger with cool stripes; the extrusion collapses and the words sink. |
| `rm:slam` | Anton | 1 | One word at a time, as big as the box allows: every word comes down from far too big, hits, flattens, shakes and throws out a deep extrusion; shouted words are bigger, yellow and shake longer; before a pause the word tips over and drops out. |
| `rm:stamp` | Barlow Condensed | up to 2 | A rubber stamp: heavy condensed italics in a frame on a dark plate, turned a few degrees. It comes down from too big with a hard stop, settles and lands a second, lighter time; the spoken word takes the highlight colour, a shouted word brings the whole stamp down twice; it lifts and slides off to leave. |
| `rm:sticker` | Lilita One | up to 3 | Die-cut stickers: every word in its own candy colour with a dark outline, a fat white border and a soft shadow, stuck on askew one after another with a jelly squash; the spoken one lifts, wobbles and gleams; before a pause they peel off one by one. |
| `rm:surf` | Knewave | up to 3 | The line rides a wave: brush script in an ocean gradient on a navy outline with a white rim, every word bobbing and tilting on a travelling wave over a white water line. Words surf in from the left, the water swells under the spoken word, shouted words splash, and everything is washed away to the right. |
| `rm:sweep` | Bebas Neue | up to 5 | Karaoke: one line of tall condensed capitals, all up from the start; the highlight colour fills each word from left to right exactly while it is spoken and stays, with a bump when a word is full. The line grows out of its baseline, drains and drops to leave. |
| `rm:typer` | JetBrains Mono | up to 4, as spoken | A terminal line: a dark plate with a prompt mark and a block caret. Words are typed letter by letter as they are spoken, the caret runs ahead of every key and swells slowly when idle, the spoken word is bright, shouted words are in inverse video, and the plate switches on and off like a CRT. |
| `rm:vogue` | DM Serif Display + DM Serif Display italic | up to 4 | Editorial: a cream display serif on a soft dark outline. Lines rise out of a mask one after the other, a thin underline draws itself under the spoken word and glides on, shouted words are italic, larger and in the accent colour. |
<!-- /TEMPLATES -->

## Animations

Besides the motion that is built into each template, there is a library of 25 named animations
(`src/core/animations.ts`), written for this project, in five slots: how a unit **enters**, how it
**exits** before a pause, what the **active** (spoken) word does, what an **emph**asised (shouted) word
does on top, and what everything keeps doing when **idle**. Each is a pure function of time with its own
bounded parameters and defaults; none fades anything (things arrive and leave by moving, turning,
squashing and scaling).

`punch` is built entirely from the library, so every slot of it can be set from the config, with any
parameter. Entrances and exits run page by page, word by word or letter by letter (`by`,
`timing.staggerS` apart), each in the units it suits: a slam or a slide is for the whole block, since word
by word the pieces would run over each other, and the parameter check refuses the combination.

```yaml
render:
  captions: rm:punch
  rm:
    templates:
      punch:
        fx:
          enter: {name: drop, by: letter, params: {height: 1.2}}   # another animation replaces the block whole
          active: {params: {scale: 1.2}}               # same animation: only this parameter changes
          idle: {name: float}
        timing: {staggerS: 0.03, exitS: 0.3}           # units that take turns need the time for it
```

<!-- ANIMATIONS -->
| slot | animation | what it does | parameters |
|---|---|---|---|
| enter | `pop` | Springs up from a smaller size with a little overshoot while rising into place. | from, freq, damp, rise |
| enter | `drop` | Falls in from above, lands with a squash and bounces twice. | height, fallS, squash |
| enter | `slide` | Shoots in from the side, leaning into the motion, and straightens as it stops. | distance, slideS, lean, alternate |
| enter | `twirl` | Spins in from a tilt while growing, overshoots the upright and settles. | angle, from, twirlS, overshoot |
| enter | `unfold` | Stands up from flat on the baseline like a pop-up card, rubbery. | freq, damp, widen |
| enter | `slam` | Comes down from far too big, hits its size hard and shudders. | from, slamS, shudder, shudderHz, settleS |
| enter | `swing` | Swings down on a hinge like a sign and rocks until it hangs still. | angle, swingHz, swingS |
| exit | `shrink` | Winds up a touch, then shrinks away to nothing while sinking. | windup, drop |
| exit | `melt` | Sags into the floor: flattens downwards and spreads until it is a line, then gone. | spread, sag |
| exit | `fling` | Leans back and is thrown out sideways, thinning as it goes. | distance, lean, alternate |
| exit | `topple` | Tips over on its base like a felled tree, falls, and is gone only once it is down. | angle, fall, goneFrom |
| exit | `blink` | Closes like an eye: squeezes shut to a horizontal line around its middle, then the line snaps away. | widen, shutAt |
| active | `punch` | Punches up in size and lifts while it is spoken. | scale, lift |
| active | `hop` | Jumps once when its turn comes and lands with a squash. | height, hopS, squash |
| active | `rock` | Rocks from side to side for as long as it is spoken. | angle, hz, scale |
| active | `tall` | Stretches tall and narrow from the baseline while spoken, like taking a breath. | stretch, narrow |
| active | `lean` | Leans forward into italics and edges ahead while spoken. | skew, nudge |
| emph | `shake` | A quick angry shake that dies away. | angle, hz, shakeS |
| emph | `quake` | Rattles in place, up-down and left-right at different speeds, like the ground moving. | amount, hz, quakeS |
| emph | `throb` | Two heartbeats: swells, relaxes, swells again a little less. | swell, beatS |
| emph | `wobble` | Wobbles like jelly: wide then tall, a few times, settling. | amount, hz, wobbleS |
| emph | `kick` | Kicks out bigger with a twist and snaps back. | scale, twist, kickS |
| idle | `float` | Bobs gently up and down, every unit a little out of step. | height, hz, step |
| idle | `sway` | Tilts slowly from side to side like something hanging. | angle, hz, step |
| idle | `breathe` | Grows and shrinks a hair, slowly. | amount, hz, step |
<!-- /ANIMATIONS -->

They are tested the way templates are. `node scripts/anim_check.mjs` samples every animation as a
function (no rendering): its defaults respect its bounds, it is finite, it starts and ends where its slot
says (an entrance ends exactly at rest, an exit starts at rest and is gone at its end whatever the time it
is given, an emphasis is at rest before and after), it has no discontinuity, at 30 fps it neither jumps
nor flickers by the frame checks' own limits, every parameter changes the motion, and inside a slot no two
animations move alike (distance between their motion curves, compared as shapes). Then
`tests/test_remotion_frames.py::test_every_frame_of_the_animation` renders each one through `punch` at 30
and 60 fps and runs every frame check on every frame.

## Colours

**The standard.** WCAG 2.2 contrast ratio `(L1 + 0.05) / (L2 + 0.05)` on relative luminance
([technique G18](https://www.w3.org/WAI/WCAG22/Techniques/general/G18)). Captions are large-scale text,
for which WCAG asks 3:1 (SC 1.4.3, AA) and 4.5:1 ([SC 1.4.6](https://www.w3.org/WAI/WCAG22/Understanding/contrast-enhanced.html), AAA). We hold more than that: a
**floor of 4.5:1 on every frame** and a **target of 7:1 for every colour pair a template ships with**,
never rounded ("4.499:1 would not meet the 4.5:1 threshold",
[Understanding SC 1.4.3](https://www.w3.org/WAI/WCAG22/Understanding/contrast-minimum.html)). APCA Lc is
computed as a second opinion (`palette.apca`, checked against the APCA-W3 reference values); nothing is
gated on it.

**What is measured.** G18 measures a letter against "the background pixels immediately next to the
letter", and a wide border around the letters "acts as a halo and would be considered background"
(Understanding SC 1.4.3). So every template keeps something opaque next to every fill: an outline at
least as wide as the measuring ring, or a plate. The frame checks measure exactly that, in the composited
frame: the darkest pixel of each word's fill against the lightest pixel 2 to 4 px outside it (or the
reverse for dark text). No caption text is ever semi-transparent: entrances and exits move, scale and
wipe, they do not fade, so the ratio holds on the first and the last frame too.

**The palette** (`palette.PALETTE`). Light fills, every one at 7:1 or more on black: white `#FFFFFF`,
cream `#FFF3D6`, yellow `#FFE600`, lime `#8DFF2F`, mint `#3DFFC0`, cyan `#00E5FF`, sky `#6CC8FF`, violet
`#B9A0FF`, pink `#FF70CB`, red `#FF6B5E`, orange `#FF9A1F`, gold `#FFC53D`. Inks: black `#000000`, ink
`#0B0B12`, navy `#0A1238`, plum `#240A33`, forest `#052B1B`, maroon `#3A0710`. There is no published
standard for which caption colours hold attention (vendor claims only, no study isolates colour); this is
the convention of the big clip channels: white base, one saturated highlight, a warm colour for shouts.
"Red" is a light red on purpose: pure `#FF0000` is 5.25:1 on black.

**Auto mode** (`palette.auto_colors`). For each phrase the video under the caption box is sampled over the
time the phrase is on screen (the Short's own picture before captions, 5 samples a second). Then, role by
role, the template's candidate list is searched for the first colour that (1) keeps the floor against
every role it sits next to, (2) is not the hue of a large part of that video (more than 18 % of the
pixels within 38 degrees of OKLCh hue: no yellow on a yellow car), (3) is told apart from the roles it
must differ from (35 degrees or more). Roles a template does not list keep their fixed colour, so the
result is always that template's design in another colourway, never a random scheme. Stability: colours
are constant inside a phrase; the colour of the phrase before wins ties; a role that changed less than 2 s
ago keeps its colour unless it now collides; and when a scheme does change between two phrases that
follow each other, the new phrase eases in from the old colours (OKLab) if every step of that path keeps
the floor, else it simply starts in the new ones.

## Output format and speed

Measured on an 8-core Apple M3, nothing else running, 30 s of captions at 60 fps (1800 frames, about 1650
of them with something on screen), `punch` / `jelly` / `neon`:

| overlay | render | size | error after compositing |
|---|---|---|---|
| **PNG sequence of the caption band** (1080x256, what is used) | **12.8 / 13.5 / 13.6 s** | 44-103 MB | 1-2 levels of 255 |
| PNG sequence of the full 1080x1920 frame | 19.2 / 20.6 / 21.9 s | 122-182 MB | the same |
| ProRes 4444 with alpha of the band (ffmpeg, from the PNGs) | + 4.3-5.1 s | 113-213 MB | 2-3 levels |
| VP9 with alpha (4:2:0) | about 108 s in the first test | 4 MB | chroma subsampled before blending |

PNG is lossless, needs no second encode and lets blank frames be links to one file, so it is both the most
exact and the fastest; the band instead of the full frame saves a third. The error is against an exact
composite done in RGB and converted the same way, in the video's own 4:2:0, over a moving striped
background; it is rounding. Compositing adds about 1.2 s to a 30 s x264 `veryfast` encode. The auto colour
mode costs one more pass over the clip's picture (decode and filters, no encode).

So a 30 s clip costs about 13 s of caption rendering, a 59 s clip about 26 s, on all eight cores. That is too
slow to add to the live lane's minute per clip on a machine that is also recording and encoding. Options:
keep an ASS style for the live lane and use `rm:` styles in the VOD and batch lanes; cap the overlay at
30 fps there (`render.rm.max_fps: 30`, half the time, visibly less smooth on a 60 fps clip); or give the
render its own machine.

## Known limits

- Specks: an outline and its shadow can leave a walled-in gap of video a few pixels across. Letter counters
  and the channels between letters of a word are closed by the renderer; nooks where two outlines almost
  meet (between a `0` and a `%`), and some gaps between a template's own shapes and its text, are not yet.
  The frame checks count such frames (`speck_frames` in the report) without failing them.
- Emoji are the system's Apple Color Emoji: bitmap text, which the browser moves in whole-pixel steps
  vertically, so an emoji in slow motion is less smooth than the lettering around it; they get the
  caption's halo but not its fill colour, and the contrast floor is not measured on them.
- Text is laid out with the browser's kerning; ligatures and contextual forms are off, so script faces
  (Knewave) do not join as they would in a word processor.
- The caption box positions are this project's (the seam at y 960; nothing below y 1536 or within 60 px
  of the sides). The platforms publish safe areas for ads only; YouTube's ad diagram keeps the bottom 672 px
  and the right 192 px clear, more than the project does.

## Frame checks

`python -m clipwatcher.render.framecheck --templates all --fixtures full --work <dir>` renders every
frame of every template at 30 and 60 fps over the fixtures in `render/framecheck.py` and checks each
frame; `tests/test_remotion_frames.py` runs the same on a short fixture set (marker `remotion`, skipped
without the Node setup). The checks are listed at the top of `render/framecheck.py`. In short: only what
the plan puts on a frame is drawn; every word is there, highlighted within one frame of its times; ink
stays inside the caption box (so never in the platform dead zone or within 60 px of the edge) and where
the renderer says it is; lines and neighbouring words never overlap; nothing jumps, flashes or lives for
a single frame; 30 and 60 fps show the same motion; all glyphs come from the bundled fonts; two renders
are byte-identical; ffmpeg's composite matches the exact one within 2 levels on every frame; and every
fill keeps the contrast floor over white, black, flashing, noise, stripes and its own colours.

## Writing a template

A template is two files, nothing to register:

- `src/templates/<name>.tsx`: `export default defineTemplate<P>({ name, schema, build, label })`
- `templates/<name>.json`: every parameter, read by Python (paging, colours) and passed to the renderer

`build(c)` is a **pure function** from one instant to a `Scene`: a list of text `units` and `shapes` in
overlay pixels. It gets the page, each word's state (`phase`, eased highlight `a`, `since`), the fitted
`layout`, the colours of this phrase and its own parameters (`src/core/scene.ts`: `Ctx`). Start from
`punch.tsx` (page pop, spoken word), `glide.tsx` (a shape, a unit drawn twice through a clip window) and
the helpers in `src/core/` (`ease.ts`: springs, easings, `pulse`, `shake`, `drift`, `hash01`; `affine.ts`:
`pose(pivot, {x, y, scale, sx, sy, rot, skewX})` and `chain`; `color.ts`: `mix`; `scene.ts`: `wordUnit`,
`letterUnits`, `rect`, `ellipse`, `path`, `inked`, `dropShadow`, `extrude`).

Rules the frame checks enforce (they are what "no imperfection" means here):

1. **Time, not frames.** Everything is a function of seconds (`c.t`, `w.since`, `c.T`). No frame counters,
   no `Math.random` (use `hash01(seed)`), no state.
2. **Text is opaque.** Never fade text. Enter and leave by moving, scaling (not below about 0.5 while it
   should still be read), turning, squashing, wiping (`clip`). Only decoration (`glow` layers, `alpha` on
   `decor` shapes) may be translucent.
3. **Every fill has its halo.** Right next to every fill there is an opaque outline of at least
   0.07 em (`inked(fill, colors.stroke, halo)`) or a plate. Report in `fg` the colours that carry the
   letters this frame. Colours come from `c.colors` roles only, mixed with `mix()`; never a literal.
4. **Stay in the box.** Ink (outline, shadow, overshoot, tilt included) stays inside `c.region`. Lengths
   in `fx` are in em (`em(c)`), so they shrink with the text; `margin`, `grow` and `tilt` in the JSON
   reserve room for outline, word growth and rotation.
5. **Smooth at 30 fps too.** Nothing oscillates faster than about 6 Hz; a pop takes at least three frames
   at 30 fps (0.1 s); colour changes take at least 0.07 s.
6. **The plan is law.** A word's highlight follows `w.phase`/`w.a`; in `page` mode every word is on
   screen `timing.enterS + i * timing.staggerS` after the page starts, in `build` mode a word appears
   when it is spoken; nothing is drawn before `c.t = 0` or after the exit.
7. One unit id per thing per frame (`key` when a word is drawn more than once).

Check a template while writing it:

```
python -m clipwatcher.render.framecheck --templates <name> --fixtures quick --work <dir> --keep
python -m clipwatcher.render.framecheck --templates <name> --fixtures full  --work <dir> --keep
python -m clipwatcher.render.rm_gallery --templates <name> --videos <a 1080x1920 mp4> --out <dir>
```

and then look at the frames: the checks passing is necessary, not sufficient.
