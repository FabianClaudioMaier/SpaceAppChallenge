# TODO for the next session

The work is in `~/BranchedSpaceAppsChallenge` on branch `mega_overhaul`.

## Your list

1. **Option to merge overlapping results:** one marker per object instead of Gaia, 2MASS, CatWISE and
   unWISE on top of each other.
   - **How:** cross-match by position after moving each catalogue to the frame date with its proper
     motion. Radius ~2-3″; a SPHEREx pixel is 6.15″. A grid in JS is fast enough.
   - **Star card:** lists every catalogue entry of the merged object (all magnitudes ≈ a small SED).
   - **Feasible:** yes. Watch out for double stars closer than the radius.

2. **Hide all catalogues in one click:** the preset "Nothing" in the menu next to "Layers" does it, but
   it's easy to miss. A visible "all off / back on" switch next to the heading would work.

3. **Legend:** what the symbols and colours on the frame mean. Ideas:
   - one row per switched-on layer (symbol, colour, name)
   - one shape per kind of object (star, diamond, cross, triangle, circle, square, plus, hexagon)
   - marker size = brightness
   - dashed line = a mover's track
   - green box = your marked box
   - the coordinate grid's lines
   - grey = no change in the difference images

   Where: a small, foldable box in a corner of the frame, or in the right panel under Settings. The
   layer list already shows each layer's symbol and colour, so the legend can reuse `symbolSvg`
   (`layers.js`).

4. **Pop-up on first visit** explaining the page:
   1. RA/Dec and Submit
   2. the player, the frame date, Space and the `,` / `.` keys
   3. clicking a star, zooming (click or drag), the box ("Yes" or `B`)
   4. the layer list (eye, solo, chevron, reload) and the star card
   5. "Is something moving?" → Yes/No → Save, and Download GIF
   6. Settings: difference images, coordinate grid, globe, reset

   Shown once (a localStorage flag, e.g. `seenIntro`, which "Reset to defaults" should not clear),
   reopenable via a "?" button. A `<dialog>` like the zoom, a few short steps with Next/Back, and
   "Don't show again".

## Also open

- **CatWISE spike lines:** they're still visible around bright stars. CatWISE leaves most of them
  unflagged (274 of 437 on ε Indi's upper spike), so they need a geometric mask along the spikes of
  bright stars.
- **Export memory:** after bursts of simultaneous exports the server keeps the memory it used
  (about 1 GB after 12 at once with every layer on; it levels off, no leak). To give it back:
  restart each worker every 25 exports (`max_tasks_per_child=25` on the pool in
  `middleware/export.py`). To cap the peak: lower `MAX_PARALLEL_EXPORTS` (10) in `middleware.py`.
- **Fonts:** DM Sans and Space Grotesk load from Google's servers, which sends every visitor's IP
  address to Google (a GDPR issue in Germany). Serve the font files from `frontend/` instead.
- **Earth texture:** `frontend/earth.jpg` (the globe) comes from three.js's example textures, whose
  licence is unclear. Replace it with NASA's Blue Marble (public domain).
- **VizieR's main server:** `vizier.cds.unistra.fr/viz-bin/asu-tsv` answered 404 on 2026-11-16, so
  every first catalogue fetch went through the Harvard mirror. Check whether CDS moved the service
  and update `VIZIER_HOSTS` (`overlays.py`).
- **Merging into `main`:** `mega_overhaul` into `main` (with a PR). Only that brings this work to
  `main`, including the fix for Save always saving Barnard's star.
