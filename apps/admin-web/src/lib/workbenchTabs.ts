export const WORKBENCH_RIGHT_TABS = ["reply", "knowledge", "tools", "tasks"] as const;

export type WorkbenchRightTab = (typeof WORKBENCH_RIGHT_TABS)[number];

export const WORKBENCH_QUEUE_TABS = ["queue", "mine", "waiting"] as const;

export type WorkbenchQueueTab = (typeof WORKBENCH_QUEUE_TABS)[number];

type KeyboardModifiers = Partial<Pick<KeyboardEvent, "altKey" | "ctrlKey" | "metaKey" | "shiftKey">>;

/** Keyboard movement for the horizontal conversation-queue tablist. */
export function nextWorkbenchQueueTab(
  current: WorkbenchQueueTab,
  key: string,
  modifiers: KeyboardModifiers = {},
): WorkbenchQueueTab | null {
  if (modifiers.altKey || modifiers.ctrlKey || modifiers.metaKey || modifiers.shiftKey) return null;
  const index = WORKBENCH_QUEUE_TABS.indexOf(current);
  if (key === "Home") return WORKBENCH_QUEUE_TABS[0];
  if (key === "End") return WORKBENCH_QUEUE_TABS[WORKBENCH_QUEUE_TABS.length - 1];
  if (key === "ArrowRight") {
    return WORKBENCH_QUEUE_TABS[(index + 1) % WORKBENCH_QUEUE_TABS.length];
  }
  if (key === "ArrowLeft") {
    return WORKBENCH_QUEUE_TABS[
      (index - 1 + WORKBENCH_QUEUE_TABS.length) % WORKBENCH_QUEUE_TABS.length
    ];
  }
  return null;
}

/** Keyboard movement for the horizontal workbench tablist. */
export function nextWorkbenchRightTab(
  current: WorkbenchRightTab,
  key: string,
  modifiers: KeyboardModifiers = {},
): WorkbenchRightTab | null {
  if (modifiers.altKey || modifiers.ctrlKey || modifiers.metaKey || modifiers.shiftKey) return null;
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
