import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { useLang } from "../lib/i18n";

/**
 * In-page prompts, replacing `window.prompt` / `window.confirm`.
 *
 * Those three problems mattered here: they block the whole tab, they cannot be
 * styled, and a screen reader is not told the result of the button press. An
 * operator working through a queue of gaps dismissed a browser dialog per
 * step, and a mistyped value was rejected with another dialog.
 *
 * `ask` returns a promise, so a call site still reads top-to-bottom:
 *
 *     const values = await prompt.ask({ title: "Transition", fields: [...] });
 *     if (!values) return;                      // cancelled
 *     onCommand("transition", { target: values.target });
 *
 * The prompt itself is rendered by `prompt.element`, which the page places
 * once. It is inline rather than modal: it appears where the page already is,
 * so the operator keeps the context they were reading.
 */

export interface PromptField {
  name: string;
  label: string;
  placeholder?: string;
  initial?: string;
  /**
   * Render a select instead of a text input.
   *
   * A bare string is both the value and the label. Pass `{ value, label }`
   * when the label is not unique or not what the API wants: selecting a
   * knowledge space by its *name* and then looking the id up in a map meant
   * two spaces with the same name silently published to the wrong one.
   */
  options?: readonly (string | PromptOption)[];
  /** Reject a non-integer value before submitting. */
  integer?: boolean;
  /** Reject an empty value before submitting. */
  required?: boolean;
  /** Range check for `integer` fields. */
  min?: number;
  max?: number;
}

export interface PromptOption {
  value: string;
  label: string;
}

function optionOf(option: string | PromptOption): PromptOption {
  return typeof option === "string" ? { value: option, label: option } : option;
}

export interface PromptSpec {
  title: string;
  /** Empty means a yes/no confirmation with no input. */
  fields?: readonly PromptField[];
  confirmLabel?: string;
  /** Shown above the fields, for a confirmation that needs an explanation. */
  detail?: string;
}

export type PromptValues = Record<string, string>;

export interface PromptHandle {
  ask: (spec: PromptSpec) => Promise<PromptValues | null>;
  /** Yes/no. Resolves `false` when cancelled, so it reads like `window.confirm`. */
  confirm: (title: string, confirmLabel?: string, detail?: string) => Promise<boolean>;
  element: ReactNode;
}

export function usePrompt(): PromptHandle {
  const { t } = useLang();
  const [spec, setSpec] = useState<PromptSpec | null>(null);
  const [values, setValues] = useState<PromptValues>({});
  const [error, setError] = useState<string | null>(null);
  const resolver = useRef<((result: PromptValues | null) => void) | null>(null);
  const dialogRef = useRef<HTMLDivElement | null>(null);

  const settle = useCallback((result: PromptValues | null) => {
    const resolve = resolver.current;
    resolver.current = null;
    setSpec(null);
    setError(null);
    resolve?.(result);
  }, []);

  const ask = useCallback((next: PromptSpec) => {
    const initial: PromptValues = {};
    for (const field of next.fields ?? []) initial[field.name] = field.initial ?? "";
    setValues(initial);
    setError(null);
    setSpec(next);
    return new Promise<PromptValues | null>((resolve) => {
      resolver.current = resolve;
    });
  }, []);

  const confirm = useCallback(
    async (title: string, confirmLabel?: string, detail?: string) => {
      const result = await ask({ title, confirmLabel, detail });
      return result !== null;
    },
    [ask],
  );

  const validate = (fields: readonly PromptField[], values: PromptValues): string | null => {
    for (const field of fields) {
      const raw = (values[field.name] ?? "").trim();
      if (field.required && !raw) {
        return t("prompt.required", { label: field.label });
      }
      if (field.integer && raw !== "") {
        const n = Number(raw);
        if (!Number.isInteger(n)) return t("prompt.integer", { label: field.label });
        if (field.min !== undefined && n < field.min)
          return t("prompt.min", { label: field.label, min: field.min });
        if (field.max !== undefined && n > field.max)
          return t("prompt.max", { label: field.label, max: field.max });
      }
    }
    return null;
  };

  const fields = spec?.fields ?? [];

  // Move focus into the prompt when it opens. Without this the keyboard user
  // is left on the button they pressed, with the dialog somewhere else on
  // the page, and has to tab across everything in between to answer it.
  useEffect(() => {
    if (!spec) return;
    const node = dialogRef.current;
    if (!node) return;
    const first = node.querySelector<HTMLElement>(
      "input, select, textarea, .btn-primary",
    );
    (first ?? node).focus();
  }, [spec]);

  const element = spec ? (
    <div
      className="prompt"
      // role="dialog" and not "group": a screen reader has to announce this
      // as a thing that appeared and expects an answer, not as a passive
      // region of the page it happens to be reading.
      role="dialog"
      aria-modal="false"
      aria-label={spec.title}
      ref={dialogRef}
      onKeyDown={(e) => {
        // Escape is the one dismissal a keyboard user expects to work
        // everywhere. Cancelling is safe: the promise resolves null and the
        // caller treats that exactly like pressing Cancel.
        if (e.key === "Escape") {
          e.stopPropagation();
          settle(null);
        }
      }}
    >
      <div className="prompt-head">
        <strong>{spec.title}</strong>
      </div>
      {spec.detail ? <p className="muted">{spec.detail}</p> : null}
      {fields.length > 0 ? (
        <div className="prompt-fields">
          {fields.map((field) => (
            <label key={field.name} className="prompt-field">
              <span>{field.label}</span>
              {field.options ? (
                <select
                  className="text-input"
                  value={values[field.name] ?? ""}
                  onChange={(e) => setValues((v) => ({ ...v, [field.name]: e.target.value }))}
                >
                  {!field.required ? <option value="">—</option> : null}
                  {field.options.map((option) => {
                    const { value, label } = optionOf(option);
                    return (
                      <option key={value} value={value}>
                        {label}
                      </option>
                    );
                  })}
                </select>
              ) : (
                <input
                  className="text-input"
                  inputMode={field.integer ? "numeric" : undefined}
                  placeholder={field.placeholder}
                  value={values[field.name] ?? ""}
                  onChange={(e) => setValues((v) => ({ ...v, [field.name]: e.target.value }))}
                />
              )}
            </label>
          ))}
        </div>
      ) : null}
      {error ? (
        <p className="prompt-error" role="alert">
          {error}
        </p>
      ) : null}
      <div className="prompt-actions">
        <button
          className="btn btn-primary"
          onClick={() => {
            const problem = validate(fields, values);
            if (problem) {
              setError(problem);
              return;
            }
            settle(values);
          }}
        >
          {spec.confirmLabel ?? t("common.confirm")}
        </button>
        <button className="btn" onClick={() => settle(null)}>
          {t("common.cancel")}
        </button>
      </div>
    </div>
  ) : null;

  return { ask, confirm, element };
}
