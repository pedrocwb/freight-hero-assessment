import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from "react";

export interface Toast {
  id: number;
  tone: "error" | "info";
  message: string;
}

interface ToastApi {
  toasts: Toast[];
  error: (message: string) => void;
  info: (message: string) => void;
  dismiss: (id: number) => void;
}

const ToastContext = createContext<ToastApi | null>(null);

const LIFETIME_MS = { error: 9000, info: 4500 } as const;

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const next = useRef(1);

  const dismiss = useCallback((id: number) => {
    setToasts((list) => list.filter((t) => t.id !== id));
  }, []);

  const push = useCallback(
    (tone: Toast["tone"], message: string) => {
      const id = next.current++;
      // The same error twice in a row (a retried click) shows once.
      setToasts((list) =>
        list.some((t) => t.message === message) ? list : [...list.slice(-3), { id, tone, message }],
      );
      setTimeout(() => dismiss(id), LIFETIME_MS[tone]);
    },
    [dismiss],
  );

  const value = useMemo<ToastApi>(
    () => ({
      toasts,
      dismiss,
      error: (message) => push("error", message),
      info: (message) => push("info", message),
    }),
    [toasts, dismiss, push],
  );
  return <ToastContext.Provider value={value}>{children}</ToastContext.Provider>;
}

export function useToasts(): ToastApi {
  const value = useContext(ToastContext);
  if (!value) throw new Error("useToasts needs a ToastProvider");
  return value;
}
