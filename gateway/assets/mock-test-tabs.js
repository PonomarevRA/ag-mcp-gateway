"use strict";

/** Activate server-rendered panes without moving controls or discarding draft values. */
function enhanceMockTabs(group) {
  const tabList = group.querySelector("[data-mock-tablist]");
  const buttons = Array.from(tabList.querySelectorAll("[data-mock-tab]"));
  const panels = buttons.map(button => document.getElementById(button.dataset.mockTab));
  if (!buttons.length || panels.some(panel => !panel || panel.parentElement !== group)) return;

  tabList.setAttribute("role", "tablist");
  for (const [index, button] of buttons.entries()) {
    const panel = panels[index];
    button.setAttribute("role", "tab");
    button.setAttribute("aria-controls", panel.id);
    panel.setAttribute("role", "tabpanel");
    panel.setAttribute("aria-labelledby", button.id);
    panel.tabIndex = 0;
  }

  /** Update selection and visibility together. Move focus only after keyboard navigation. */
  function select(index, moveFocus = false) {
    for (const [candidate, button] of buttons.entries()) {
      const active = candidate === index;
      button.setAttribute("aria-selected", String(active));
      button.tabIndex = active ? 0 : -1;
      panels[candidate].hidden = !active;
    }
    if (moveFocus) buttons[index].focus();
  }

  tabList.addEventListener("click", event => {
    const index = buttons.indexOf(event.target.closest("button"));
    if (index >= 0) select(index);
  });
  tabList.addEventListener("keydown", event => {
    const index = buttons.indexOf(event.target);
    if (index < 0) return;
    let next;
    switch (event.key) {
      case "ArrowRight": next = (index + 1) % buttons.length; break;
      case "ArrowLeft": next = (index + buttons.length - 1) % buttons.length; break;
      case "Home": next = 0; break;
      case "End": next = buttons.length - 1; break;
      default: return;
    }
    event.preventDefault();
    select(next, true);
  });

  const initial = panels.findIndex(panel => panel.id === group.dataset.selected);
  select(Math.max(0, initial));
  group.dataset.enhanced = "";
  tabList.hidden = false;
}

for (const group of document.querySelectorAll("[data-mock-tabs]")) enhanceMockTabs(group);

/** Keep shortcuts local to the draft and reveal identity before the browser focuses an invalid field. */
function enhanceMockCallForm(form) {
  const run = form.querySelector('button[value="save-run"]');
  const save = form.querySelector('button[value="save"]');
  form.addEventListener("keydown", event => {
    if (event.repeat || event.isComposing || event.altKey || event.shiftKey || !(event.metaKey || event.ctrlKey)) {
      return;
    }
    const key = event.key.toLowerCase();
    const action = key === "enter" ? run : key === "s" ? save : null;
    if (!action) return;
    event.preventDefault();
    form.requestSubmit(action);
  });
  form.addEventListener("invalid", event => {
    const properties = event.target.closest(".mock-properties");
    if (properties) properties.open = true;
  }, true);
}

const mockCallForm = document.getElementById("mock-call-form");
if (mockCallForm) enhanceMockCallForm(mockCallForm);

/** Restore only collapse preferences, scoped to the current tab and gateway profile. */
function savedMockCollections(key) {
  try {
    const stored = JSON.parse(sessionStorage.getItem(key));
    if (Array.isArray(stored)) return new Set(stored.filter(id => typeof id === "string"));
  } catch {
    // Collapse still works when the browser restricts storage.
  }
  return new Set();
}

/** Manage filtering and placement in the catalog while leaving the request editor mounted. */
function enhanceMockCatalog(catalog) {
  const search = catalog.querySelector("[data-mock-search]");
  const status = catalog.querySelector("[data-mock-catalog-status]");
  const sections = Array.from(catalog.querySelectorAll("[data-mock-collection]"));
  const destinations = new Map(sections.map(section => [section.dataset.mockCollection, section]));
  const storageKey = "gateway.mock-collections:" + catalog.dataset.mockProfile;
  const collapsed = savedMockCollections(storageKey);
  const searchCollapsed = new Set();
  let dragged = null;
  let target = null;
  let moving = false;

  /** Keep the active disclosure, result count and list visibility synchronized. */
  function filter() {
    const query = search.value.trim().toLocaleLowerCase();
    for (const section of sections) {
      const toggle = section.querySelector("[data-mock-collapse]");
      const groupName = toggle.getAttribute("aria-label");
      let visible = 0;
      for (const row of section.querySelectorAll("[data-mock-request]")) {
        const requestName = row.querySelector("a").getAttribute("aria-label");
        row.hidden = ![groupName, requestName].some(text => text.toLocaleLowerCase().includes(query));
        if (!row.hidden) visible += 1;
      }
      const key = section.dataset.mockCollection;
      const expanded = query ? visible > 0 && !searchCollapsed.has(key) : !collapsed.has(key);
      toggle.setAttribute("aria-expanded", String(expanded));
      section.querySelector("ul").hidden = !expanded;
      section.querySelector("[data-mock-count]").textContent = String(visible);
    }
  }

  /** Update the user's persistent tree state while keeping search expansion transient. */
  function setCollapsed(section, closed) {
    const key = section.dataset.mockCollection;
    if (closed) collapsed.add(key);
    else collapsed.delete(key);
    try {
      sessionStorage.setItem(storageKey, JSON.stringify([...collapsed]));
    } catch {
      // The in-memory state is sufficient for the current page.
    }
    filter();
  }

  /** Change only the highlight for the active drop target. */
  function highlight(section) {
    if (target) target.classList.remove("mock-drop-target");
    target = section;
    if (target) target.classList.add("mock-drop-target");
  }

  /** Submit a revision-checked move, then update the existing row and its form. */
  async function move(form, section) {
    if (moving) return;
    moving = true;
    status.textContent = "";
    const row = form.closest("[data-mock-request]");
    const data = new URLSearchParams(new FormData(form));
    data.set("group_id", section.dataset.mockCollection);
    try {
      const response = await fetch(form.action, {
        method: "POST", body: data, headers: {Accept: "application/json"}
      });
      if (!response.ok) throw new Error("Move failed");
      const saved = await response.json();
      const list = section.querySelector("ul");
      const action = form.closest("details");
      const restoreFocus = action.contains(document.activeElement);
      if (row.parentElement !== list) {
        const next = Array.from(list.children).find(item => item.dataset.mockRequest > row.dataset.mockRequest);
        list.insertBefore(row, next || null);
      }
      form.elements.namedItem("revision").value = String(saved.placement_revision);
      form.elements.namedItem("group_id").value = saved.group_id || "";
      action.open = false;
      searchCollapsed.delete(section.dataset.mockCollection);
      setCollapsed(section, false);
      if (restoreFocus) {
        if (row.hidden || list.hidden) search.focus();
        else action.querySelector("summary").focus();
      }
      status.textContent = catalog.dataset.moved;
    } catch {
      status.textContent = catalog.dataset.moveError;
    } finally {
      moving = false;
    }
  }

  search.hidden = false;
  search.addEventListener("input", () => {
    searchCollapsed.clear();
    filter();
  });
  for (const section of sections) {
    section.querySelector("[data-mock-unenhanced]").hidden = true;
    section.querySelector("[data-mock-collapse]").hidden = false;
    if (section.querySelector("a[aria-current]")) collapsed.delete(section.dataset.mockCollection);
  }
  for (const handle of catalog.querySelectorAll("[data-mock-drag]")) handle.draggable = true;
  filter();

  catalog.addEventListener("click", event => {
    const toggle = event.target.closest("[data-mock-collapse]");
    if (!toggle) return;
    const section = toggle.closest("[data-mock-collection]");
    const expanded = toggle.getAttribute("aria-expanded") === "true";
    if (search.value.trim()) {
      if (expanded) searchCollapsed.add(section.dataset.mockCollection);
      else searchCollapsed.delete(section.dataset.mockCollection);
      filter();
      return;
    }
    setCollapsed(section, expanded);
  });
  catalog.addEventListener("keydown", event => {
    if (event.key !== "Escape") return;
    const disclosure = event.target.closest("details[open]");
    if (!disclosure) return;
    disclosure.open = false;
    disclosure.querySelector("summary").focus();
    event.preventDefault();
  });
  catalog.addEventListener("submit", event => {
    const form = event.target.closest("[data-mock-move]");
    if (!form) return;
    const section = destinations.get(form.elements.namedItem("group_id").value);
    if (!section) return;
    event.preventDefault();
    move(form, section);
  });
  catalog.addEventListener("dragstart", event => {
    const handle = event.target.closest("[data-mock-drag]");
    if (!handle || moving) return;
    dragged = handle.closest("[data-mock-request]");
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", dragged.dataset.mockRequest);
  });
  catalog.addEventListener("dragover", event => {
    const section = event.target.closest("[data-mock-collection]");
    if (!dragged || !section || section.contains(dragged)) {
      if (target) highlight(null);
      return;
    }
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
    if (target !== section) highlight(section);
  });
  catalog.addEventListener("dragleave", event => {
    if (target && !target.contains(event.relatedTarget)) highlight(null);
  });
  catalog.addEventListener("drop", event => {
    const section = event.target.closest("[data-mock-collection]");
    if (!dragged || !section || section.contains(dragged)) return;
    event.preventDefault();
    const form = dragged.querySelector("[data-mock-move]");
    dragged = null;
    highlight(null);
    move(form, section);
  });
  catalog.addEventListener("dragend", () => {
    dragged = null;
    highlight(null);
  });
}

const mockCatalog = document.querySelector("[data-mock-catalog]");
if (mockCatalog) enhanceMockCatalog(mockCatalog);
