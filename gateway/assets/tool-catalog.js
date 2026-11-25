import { createFeedRail } from "./ag-rail/feed-rail.js";

// The pinned rail updates its own Russian labels when scrolling or opening a preview.
function useEnglishLabels(element, items) {
  const range = element.querySelector(".ag-vt-feed-rail__range");
  const [up, down] = element.querySelectorAll(".ag-vt-feed-rail__browse");
  const position = element.querySelector(".ag-vt-feed-rail__position");
  const preview = element.querySelector(".ag-vt-rail-card");
  const title = preview.querySelector(".ag-vt-rail-card__title");

  function label(node, text, tooltip = false) {
    if (node.getAttribute("aria-label") !== text) node.setAttribute("aria-label", text);
    if (tooltip && node.title !== text) node.title = text;
  }

  function refresh() {
    label(range, "Tool range");
    label(up, "Scroll markers up · " + (up.lastElementChild.textContent || "0") + " more above", true);
    label(down, "Scroll markers down · " + (down.lastElementChild.textContent || "0") + " more below", true);
    label(preview, "Tool preview");
    if (title.textContent) label(title, "Go to " + title.textContent);
    const current = items[Number(element.dataset.activeId)];
    if (current) label(position, "Current tool " + position.textContent + " · " + current.label, true);
  }

  const observer = new MutationObserver(refresh);
  const options = { attributes: true, attributeFilter: ["aria-label"], subtree: true };
  observer.observe(element, options);
  // The fallback preview can move to the document body on older WebViews.
  observer.observe(preview, options);
  refresh();
  return () => observer.disconnect();
}

// Attach the shared rail to the server-rendered results on this page.
const catalog = document.querySelector("[data-tool-catalog]");
if (catalog) {
  const results = catalog.querySelector("[data-tool-results]");
  const targets = Array.from(results.querySelectorAll("[data-tool-card]"));
  if (targets.length > 1) {
    const items = targets.map((target, index) => ({
      id: String(index),
      label: target.querySelector("strong").textContent,
      target,
      preview: target.querySelector("p").textContent,
      context: target.querySelector("small").textContent,
    }));
    const rail = createFeedRail({
      items,
      boundsElement: results,
      header: document.querySelector(".workspace-bar"),
      label: catalog.dataset.railLabel,
    });
    const stopLabels = document.documentElement.lang === "en" ? useEnglishLabels(rail.element, items) : null;
    catalog.prepend(rail.element);
    catalog.dataset.rail = "";
    // The page can be restored from browser history, where the live rail is still mounted.
    window.addEventListener("pagehide", (event) => {
      if (!event.persisted) {
        stopLabels?.();
        rail.dispose();
      }
    });
  }
}

// Load one tool's terms on demand. A failed request can be retried by reopening.
for (const details of document.querySelectorAll("[data-keyword-tool]")) {
  let loading = false;
  details.addEventListener("toggle", async () => {
    if (!details.open || loading || details.dataset.loaded) return;
    const status = details.querySelector('[role="status"]');
    loading = true;
    status.textContent = details.dataset.loading;
    try {
      const query = new URLSearchParams({ tool: details.dataset.keywordTool });
      const response = await fetch("/api/tools/keywords?" + query, { credentials: "same-origin" });
      if (!response.ok) throw new Error("Keywords unavailable");
      const { keywords, observed_keywords: observed } = await response.json();
      status.textContent = [...new Set([...keywords, ...observed])].join(", ") || details.dataset.empty;
      details.dataset.loaded = "true";
    } catch {
      status.textContent = details.dataset.error;
    } finally {
      loading = false;
    }
  });
}
