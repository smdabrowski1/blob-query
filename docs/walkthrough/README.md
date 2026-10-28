# Walkthrough media

Five assets used on the portfolio site's BlobQuery card and walkthrough page.

- `card-base.png` — the project card's screenshot (a choropleth map,
  colouring UK local authorities by average deprivation rank). The "Blob
  Query" title shown on top of it in the portfolio is a CSS text overlay,
  not baked into this image — kept that way so it stays crisp and correctly
  positioned at any viewport width, instead of drifting as `object-cover`'s
  crop ratio changes with screen size. An earlier version did bake the
  title into the pixels; that one's gone — this is the title-free base.
- `story-1-ask-v2.mp4` — asking the landing assistant what datasets are
  available.
- `story-2-join-map-v4.mp4` — the generated join query from natural
  language, dropped onto a choropleth map.
- `story-3-scroll-v2.mp4` — scrolling the full postcodes dataset, close to
  2M rows, streaming as you go.
- `story-4-refresh.mp4` — re-fetching UK district boundaries live, progress
  and log updating in real time.

These are the canonical source files; the portfolio site keeps its own
served copy under `frontend/public/media/blobquery/`.
