import { useEffect, useState } from "react";

/**
 * Light/dark theme (plan: two shell toggles).
 *
 * Pure CSS: the only React state is which icon the toggle shows. The theme
 * itself is a `data-theme` attribute on <html>, and `styles.css` switches
 * every colour through variable overrides — no component branches on theme.
 *
 * Choice persists to localStorage; the default follows the operating
 * system's own preference, so a first-time visitor never gets a flash of
 * the wrong theme.
 */

export type Theme = "light" | "dark";

const STORAGE_KEY = "b2b_theme";

function initialTheme(): Theme {
  const saved = localStorage.getItem(STORAGE_KEY);
  if (saved === "dark" || saved === "light") return saved;
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export function useTheme(): { theme: Theme; toggle: () => void } {
  const [theme, setTheme] = useState<Theme>(initialTheme);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem(STORAGE_KEY, theme);
  }, [theme]);

  return {
    theme,
    toggle: () => setTheme((current) => (current === "dark" ? "light" : "dark")),
  };
}
