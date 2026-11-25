import { bindMasonryLayout } from "./ag-controls/masonry-layout.js";

const tables = document.querySelector("[data-metric-masonry]");
if (tables) {
  const masonry = bindMasonryLayout(tables, { minColumnWidth: 352, gap: 12, rowHeight: 1 });
  window.addEventListener("pagehide", (event) => {
    // A page restored from the back-forward cache still owns its layout.
    if (!event.persisted) masonry.dispose();
  });
}
