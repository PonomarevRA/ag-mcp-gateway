"""Local browser controller shared by browser and AppKit shells."""

WORKSPACE_SCRIPT = r"""
(() => {
  const body = document.body;
  const sidebar = document.getElementById("workspace-navigation");
  const toggle = document.getElementById("sidebar-toggle");
  const main = document.getElementById("main-content");
  const probe = document.querySelector(".workspace-brand svg");
  const language = document.querySelector(".language-switch");
  if (!sidebar || !toggle || !main || !language) return;
  const navigationLinks = sidebar.querySelectorAll("nav a");
  let nativeHost = false;
  let compact = false;
  let compactPreference = null;
  let widePreference = null;
  try {
    const value = localStorage.getItem("amg.sidebar");
    if (value === "collapsed" || value === "expanded") widePreference = value === "collapsed";
  } catch {}
  let nativePreference = null;

  function isCollapsed() {
    return (nativeHost ? nativePreference : compact ? compactPreference : widePreference) ?? compact;
  }

  function render() {
    // Media-query rem is resolved from the initial font. Use the rendered root font instead.
    compact = document.documentElement.clientWidth < 31 * parseFloat(getComputedStyle(document.documentElement).fontSize);
    const collapsed = isCollapsed();
    body.dataset.sidebarLayout = compact ? "compact" : "wide";
    body.dataset.sidebarCollapsed = String(collapsed);
    sidebar.hidden = compact && collapsed;
    // The collapsed rail contains glyphs. Expanded navigation wraps long labels.
    sidebar.dataset.agWidthMinEm = collapsed && !compact ? "1.5" : "6.5";
    // A tooltip supplies the label only when wide navigation displays icons alone.
    for (const link of navigationLinks) {
      if (collapsed && !compact) link.title = link.getAttribute("aria-label");
      else link.removeAttribute("title");
    }
    toggle.hidden = nativeHost;
    body.dataset.nativeHost = String(nativeHost);
    toggle.setAttribute("aria-expanded", String(!collapsed));
    const label = collapsed ? toggle.dataset.show : toggle.dataset.hide;
    toggle.setAttribute("aria-label", label);
    toggle.title = label;
  }

  function toggleSidebar(restoreFocus = false) {
    const collapsed = !isCollapsed();
    if (nativeHost) nativePreference = collapsed;
    else if (compact) compactPreference = collapsed;
    else {
      widePreference = collapsed;
      try { localStorage.setItem("amg.sidebar", collapsed ? "collapsed" : "expanded"); } catch {}
    }
    const focusWasInSidebar = sidebar.contains(document.activeElement);
    render();
    if (collapsed && (focusWasInSidebar || restoreFocus)) (nativeHost ? main : toggle).focus();
    return collapsed;
  }

  // A card owns its configuration and feedback. A rejected clipboard request leaves a manual copy path.
  async function copyConnectionSettings(event) {
    const button = event.target.closest("button[data-copy-mcp]");
    if (!button) return;
    const card = button.closest("li");
    const settings = card.querySelector("[data-mcp-config]");
    const status = card.querySelector("[data-copy-status]");
    status.textContent = "";
    try {
      await navigator.clipboard.writeText(settings.value);
      settings.hidden = true;
      status.textContent = button.dataset.copied;
    } catch {
      settings.hidden = false;
      settings.focus();
      settings.select();
      status.textContent = button.dataset.manual;
    }
  }

  // Let the operator decide when to reload so a change in another client cannot erase a draft.
  const revisionNotice = main.querySelector("[data-settings-revision]");
  if (revisionNotice) {
    const { revisionKind, settingsRevision } = revisionNotice.dataset;
    const checkRevision = async () => {
      if (document.visibilityState === "visible") {
        try {
          const response = await fetch("/api/settings/revisions", { cache: "no-store" });
          if (response.ok) {
            const current = await response.json();
            if (typeof current[revisionKind] === "string" && current[revisionKind] !== settingsRevision) {
              revisionNotice.hidden = false;
              return;
            }
          }
        } catch {
          // The next check can recover from a temporary network error.
        }
      }
      window.setTimeout(checkRevision, 4000);
    };
    window.setTimeout(checkRevision, 4000);
  }

  body.addEventListener("click", copyConnectionSettings);
  toggle.addEventListener("click", () => {
    const collapsed = toggleSidebar();
    if (!collapsed && compact) sidebar.querySelector("nav a")?.focus();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    if (language?.open) {
      language.open = false;
      language.querySelector("summary")?.focus();
      return;
    }
    if (!nativeHost && !isCollapsed() && (sidebar.contains(document.activeElement) || toggle === document.activeElement)) {
      event.preventDefault();
      toggleSidebar(true);
    }
  });
  window.addEventListener("resize", render);
  window.addEventListener("pageshow", render);
  if (window.ResizeObserver && probe) new ResizeObserver(render).observe(probe);
  // Supply the current document context and its measured sidebar boundary to AppKit.
  function presentation() {
    return {
      collapsed: isCollapsed(),
      sidebarEdge: compact ? 0 : sidebar.getBoundingClientRect().right,
      title: (document.getElementById("section-title")?.textContent || "").slice(0, 100),
      language: document.documentElement.lang,
    };
  }

  window.gatewaySidebar = {
    presentation,
    chooseLanguage(code) {
      for (const link of language.querySelectorAll("a[hreflang]")) {
        if (link.hreflang === code) {
          link.click();
          return;
        }
      }
    },
    attach(preference) {
      nativeHost = true;
      nativePreference = typeof preference === "boolean" ? preference : null;
      render();
      return presentation();
    },
    toggle() {
      toggleSidebar();
      return presentation();
    },
  };
  render();
})();
"""
