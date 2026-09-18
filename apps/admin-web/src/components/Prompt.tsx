import { useCallback, useRef, useState } from "react";
import type { ReactNode } from "react";

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
  /** Render a select instead of a text input. */
  options?: readonly string[];
  /** Reject a non-integer value before submitting. */
  integer?: boolean;
  /** Reject an empty value before submitting. */
  required?: boolean;
  /** Range check for `integer` fields. */
  min?: number;
  max?: number;
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

function validate(fields: readonly PromptField[], values: PromptValues): string | null {
  for (const field of fields) {
    const raw = (values[field.name] ?? "").trim();
    if (field.required && !raw) {
      return `${field.label} is required.`;
    }
    if (field.integer && raw !== "") {
      const n = Number(raw);
      if (!Number.isInteger(n)) return `${field.label} must be a whole number.`;
      if (field.min !== undefined && n < field.min) return `${field.label} must be at least ${field.min}.`;
      if (field.max !== undefined && n > field.max) return `${field.label} must be at most ${field.max}.`;
    }
  }
  return null;
}

export function usePrompt(): PromptHandle {
  const [spec, setSpec] = useState<PromptSpec | null>(null);
  const [values, setValues] = useState<PromptValues>({});
  const [error, setError] = useState<string | null>(null);
  const resolver = useRef<((result: PromptValues | null) => void) | null>(null);

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

  const fields = spec?.fields ?? [];

  const element = spec ? (
    <div className="prompt" role="group" aria-label={spec.title}>
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
                  {field.options.map((option) => (
                    <option key={option} value={option}>
                      {option}
                    </option>
                  ))}
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
          {spec.confirmLabel ?? "Confirm"}
        </button>
        <button className="btn" onClick={() => settle(null)}>
          Cancel
        </button>
      </div>
    </div>
  ) : null;

  return { ask, confirm, element };
}
