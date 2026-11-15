# TODO for the next session

The work is in `~/BranchedSpaceAppsChallenge` on branch `post-hackathon`.

## Your list

1. **Reset button:** one click puts everything back to its defaults:
   - the layer list: order, on/off, options
   - the tool
   - contrast, brightness, invert, show diffs, speed, cutout size

   It must not touch the job on screen (`videoUuid`, `videoMetadata`). Today the settings live in
   localStorage under `layers`, `tool`, `contrast`, `brightness`, `showDiff`, etc.

2. **Rendering speed** with all catalogues on (5-6k objects on a 250 px frame is slow).
   - **Today:** SVG with roughly 5 elements and 3 listeners per star (group, title, marker, shape, hit
     circle, label). `placeMarkers` rewrites them on every frame change, and the zoom builds a second copy.
     Any option change rebuilds everything (`layersVersion`).
   - **Idea:** draw the markers on a `<canvas>` (one draw per frame). Find the star under the mouse with a
     grid index. Keep SVG only for labels and fast-mover tracks.
   - **Measure first** (Performance tab): building vs. per-frame placement.

3. **Option to merge overlapping results:** one marker per object instead of Gaia + 2MASS + CatWISE +
   unWISE on top of each other.
   - **How:** cross-match by position after moving each catalogue to the frame date with its proper
     motion. Radius ~2-3″; a SPHEREx pixel is 6.15″. A grid in JS is fast enough.
   - **Star card:** lists every catalogue entry of the merged object (all magnitudes ≈ a small SED).
   - **Feasible:** yes. Watch out for double stars closer than the radius.

4. **"Download GIF" button:** the frames with everything currently shown burned in (the switched-on
   layers' markers, the rulers, the UTC timestamp per frame).
   - **Server-side (recommended):** extend `middleware/export.py`, which already burns in rulers,
     timestamps and the box. The page sends the visible layers' star positions per frame. Same look as
     the saved GIFs.
   - **Client-side:** canvas plus a GIF encoder (e.g. gifenc). Matches the screen exactly, but needs a
     library.

5. **Half-width window (snapped left/right):** the page falls into the one-column layout (< 900 px) and
   the layer list comes first. Today the order is: layers, settings, coordinates, GIF + tools, play
   controls, question, star info.
   - **Cause:** in `style.css` (`@media (max-width: 900px)`) `.layers` has no `order`, so it lands at
     order 0, before everything else.
   - **Better order:** coordinates, GIF + tools, play controls, question, star info, then the layer list
     (folded), then settings.
   - **Also:** give the layer list a fold/unfold header.

6. **Hide all catalogues in one click** (part of 5). The preset "Nothing" in the menu above the list
   does it, but nobody sees it there. A visible "all off / back on" switch next to the "Layers" heading
   would work, or the folded header from 5.

7. **Rename the button:** "+ Add catalogue…" becomes "Select catalogue", without the three dots (in
   `index.html`, `#layer-add-button`).

8. **Legend:** what the symbols and colours on the frame mean. Ideas:
   - one row per switched-on layer (symbol, colour, name)
   - marker size = brightness
   - dashed line = a mover's track
   - green box = your marked box
   - grey = no change in the difference images

   Where: a small, foldable box in a corner of the frame, or in the right panel under Settings. The
   layer list already shows each layer's symbol and colour, so the legend can reuse `symbolSvg`
   (`layers.js`).

9. **Pop-up on first visit** explaining the page:
   1. RA/Dec + Submit
   2. the player and the frame date
   3. the tools (Pointer `V`, Zoom `Z`, Box `B`)
   4. the layer list and the star card
   5. "Is something moving?" → Yes/No → Save
   6. the difference images (Settings)

   Shown once (a localStorage flag, e.g. `seenIntro`), reopenable via a "?" button. A `<dialog>` like
   the zoom, a few short steps with Next/Back, and "Don't show again".

10. **Chevrons instead of "⋯"** on every layer row: "v" while the row is folded, "^" while its
    options are open (a disclosure arrow; clicking the whole row could toggle it too). In `layers.js`
    (`renderLayerList`, the `.layer-more` button) and `style.css` (`.layer-more`). (If you meant up/down
    arrows to reorder the layers instead: that's a separate small feature, also doable.)

## Also open from this session

- **CatWISE spike lines:** they're still visible around bright stars. CatWISE leaves most of them
  unflagged (274 of 437 on ε Indi's upper spike), so they need a geometric mask along the spikes of
  bright stars.
- **Globe:** it's hidden. Add a setting to show it again.
- **Pole:** a coordinate grid overlay (option) would read better there than edge rulers.
- **README:** not updated for the layer list, `/api/catalogs`, `/api/catalog`, `catalogs.py` and
  `layers.js`.
- **Restart:** your own server (5173) needs a restart and a hard refresh to pick up the latest backend
  and middleware changes.
