export const WORKBENCH_RIGHT_TABS = ["reply", "knowledge", "tools", "tasks"] as const;

export type WorkbenchRightTab = (typeof WORKBENCH_RIGHT_TABS)[number];

/** Keyboard movement for the horizontal workbench tablist. */
export function nextWorkbenchRightTab(
  current: WorkbenchRightTab,
  key: string,
): WorkbenchRightTab | null {
  const index = WORKBENCH_RIGHT_TABS.indexOf(current);
  if (key === "Home") return WORKBENCH_RIGHT_TABS[0];
  if (key === "End") return WORKBENCH_RIGHT_TABS[WORKBENCH_RIGHT_TABS.length - 1];
  if (key === "ArrowRight") {
    return WORKBENCH_RIGHT_TABS[(index + 1) % WORKBENCH_RIGHT_TABS.length];
  }
  if (key === "ArrowLeft") {
    return WORKBENCH_RIGHT_TABS[(index - 1 + WORKBENCH_RIGHT_TABS.length) % WORKBENCH_RIGHT_TABS.length];
  }
  return null;
}
